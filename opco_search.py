#!/usr/bin/env python3
"""OpCo weekly job search — Column D edition.

Column D of the master sheet's OpCo tab is the only input. If it holds a
job-board link (Workday, UKG, Dayforce, ADP, Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, Recruitee, Breezy, BambooHR), that board is read.
If it holds an ordinary web page, only the jobs listed on that page are read.
Nothing is discovered or guessed, and nothing is cached between runs except
last week's roles, which the new/closed comparison needs.

Anything that can't be read lands in the brief's "Needs your attention"
section with the reason, so Column D can be fixed.

    python opco_search.py                 # weekly run
    python opco_search.py --discover      # check what each Column D is; read no jobs
    python opco_search.py --only "Greystar,Lamar Advertising Company" -v

Files: opco_search.py (this), opco_config.yml (role filter, live-sheet link),
opco.csv (the OpCo tab, unless the live-sheet link is set).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("opco")


# ==========================================================================
# HTTP
# ==========================================================================

"""Polite HTTP client.

Every source in this module talks to a different host (seven ATS providers plus
each company's own careers page), so throttling is per host. That is the
structural fix for the Sept 30 failure: no single provider's rate limit can
govern the whole run, the way Built In's did.
"""


import logging
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (compatible; SyndeoJobSearch/1.0; +mailto:hello@syndeollc.com)"
)
DEFAULT_DELAY = 0.8
_last: dict[str, float] = defaultdict(float)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})


class FetchError(Exception):
    """Raised with a short human-readable reason for the coverage report."""


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    wait = DEFAULT_DELAY - (time.time() - _last[host])
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def _request(method: str, url: str, *, json_body=None, accept: Optional[str] = None,
             timeout: int = 25, retries: int = 3, headers: Optional[dict] = None) -> requests.Response:
    headers = dict(headers or {})
    if accept:
        headers["Accept"] = accept
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    last_reason = "no response"
    for attempt in range(1, retries + 1):
        _throttle(url)
        try:
            r = SESSION.request(method, url, json=json_body, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_reason = f"connection error ({type(exc).__name__})"
            time.sleep(2 * attempt)
            continue

        if r.status_code == 200:
            return r

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 5 * attempt
            last_reason = f"HTTP 429 rate limited (waited {delay}s)"
            time.sleep(min(delay, 60))
            continue

        if r.status_code in (401, 403, 404, 410):
            body = r.text[:300].lower()
            if r.status_code == 403 and ("cloudflare" in body or "just a moment" in body):
                raise FetchError("HTTP 403 bot protection (Cloudflare)")
            raise FetchError(f"HTTP {r.status_code}")

        last_reason = f"HTTP {r.status_code}"
        time.sleep(3 * attempt)

    raise FetchError(last_reason)


def get(url: str, **kw) -> requests.Response:
    return _request("GET", url, **kw)


def post_json(url: str, body: dict, **kw) -> requests.Response:
    return _request("POST", url, json_body=body, accept="application/json", **kw)


def try_get(url: str, **kw) -> Optional[requests.Response]:
    """GET that returns None instead of raising. For probing."""
    try:
        return get(url, **kw)
    except FetchError:
        return None


# ==========================================================================
# JOB-SYSTEM READERS (unchanged from the previous version)
# ==========================================================================

MAX_PAGES = 30  # hard stop so a runaway paginator can't eat the run


@dataclass
class Role:
    id: str
    title: str
    url: str
    location: str = ""
    department: str = ""
    posted: str = ""

    def __post_init__(self):
        # Job systems send nulls, numbers, {"en": "..."} objects and lists where
        # text belongs. Normalise here, once, so nothing downstream can trip on it.
        for f in ("id", "title", "url", "location", "department", "posted"):
            setattr(self, f, _as_text(getattr(self, f)))

    def to_dict(self) -> dict:
        return asdict(self)


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, dict):
        for x in v.values():
            t = _as_text(x)
            if t:
                return t
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(t for t in (_as_text(x) for x in v) if t)
    return str(v)


def _records(seq, what: str = "job list") -> list:
    """The job records in a response, skipping any that aren't records.

    A response whose job list isn't a list at all is a real failure and says
    so, rather than passing as "this company has no jobs".
    """
    if seq is None:
        return []
    if not isinstance(seq, list):
        raise FetchError(f"the job system sent its {what} in an unexpected format")
    return [x for x in seq if isinstance(x, dict)]


def _rid(j: dict, *keys) -> str:
    """A stable id for a record, even when the id field is missing."""
    for k in keys:
        if j.get(k) not in (None, ""):
            return _as_text(j[k])
    return hashlib.sha1(json.dumps(j, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _loc(*parts) -> str:
    return ", ".join(p for p in parts if p and str(p).strip())


class RoleList(list):
    """A list of roles that can also say it was cut short."""
    truncated: str = ""


def greenhouse(slug: str, **_) -> list[Role]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [
        Role(id=f"gh-{_rid(j, 'id', 'absolute_url')}", title=j.get("title", ""), url=j.get("absolute_url", ""),
             location=(j.get("location") or {}).get("name", ""),
             posted=(j.get("updated_at") or "")[:10])
        for j in _records(d.get("jobs"))
    ]


def lever(slug: str, **_) -> list[Role]:
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
    out = []
    for j in _records(d):
        cats = j.get("categories") or {}
        out.append(Role(id=f"lv-{_rid(j, 'id', 'hostedUrl')}", title=j.get("text", ""), url=j.get("hostedUrl", ""),
                        location=cats.get("location", ""), department=cats.get("team", "")))
    return out


def ashby(slug: str, **_) -> list[Role]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    return [
        Role(id=f"ab-{_rid(j, 'id', 'jobUrl')}", title=j.get("title", ""), url=j.get("jobUrl", ""),
             location=j.get("location", ""), department=j.get("department", ""),
             posted=(j.get("publishedAt") or "")[:10])
        for j in _records(d.get("jobs")) if j.get("isListed", True)
    ]


def workable(slug: str, **_) -> list[Role]:
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}").json()
    return [
        Role(id=f"wk-{j.get('shortcode')}", title=j.get("title", ""),
             url=j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
             location=_loc(j.get("city"), j.get("state")), department=j.get("department", ""),
             posted=(j.get("published_on") or "")[:10])
        for j in _records(d.get("jobs"))
    ]


def smartrecruiters(slug: str, **_) -> list[Role]:
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                f"?limit=100&offset={offset}").json()
        content = _records(d.get("content"))
        for j in content:
            loc = j.get("location") or {}
            out.append(Role(id=f"sr-{_rid(j, 'id')}", title=j.get("name", ""),
                            url=f"https://jobs.smartrecruiters.com/{slug}/{_rid(j, 'id')}",
                            location=_loc(loc.get("city"), loc.get("region")),
                            department=(j.get("department") or {}).get("label", ""),
                            posted=(j.get("releasedDate") or "")[:10]))
        offset += len(content)
        if not content or offset >= int(d.get("totalFound") or 0):
            break
    return out


def recruitee(slug: str, **_) -> list[Role]:
    d = get(f"https://{slug}.recruitee.com/api/offers/").json()
    return [
        Role(id=f"rt-{_rid(j, 'id', 'careers_url')}", title=j.get("title", ""), url=j.get("careers_url", ""),
             location=j.get("location", ""), department=j.get("department", ""),
             posted=(j.get("published_at") or "")[:10])
        for j in _records(d.get("offers"))
    ]


def breezy(slug: str, **_) -> list[Role]:
    d = get(f"https://{slug}.breezy.hr/json").json()
    return [
        Role(id=f"bz-{_rid(j, 'id', 'url')}", title=j.get("name", ""), url=j.get("url", ""),
             location=(j.get("location") or {}).get("name", ""),
             department=j.get("department", ""), posted=(j.get("published_date") or "")[:10])
        for j in _records(d)
    ]


WORKDAY_MAX_PAGES = 60  # 1,200 postings per search


def workday(slug: str, host: str = "", site: str = "", search_terms=None, **_) -> list[Role]:
    """Workday's public CXS API. `slug` is the tenant.

    Workday caps pages at 20 and only reports `total` reliably on the first
    page, so the first page's total drives pagination.

    Big operators post thousands of site-level roles, and the corporate ones
    sit far down the list. For those, set `search_terms` in opco_config.yml:
    each term runs as its own Workday keyword search and the results merge.
    """
    if not host or not site:
        raise FetchError("Workday needs host and site (resolve from careers page)")
    endpoint = f"https://{host}/wday/cxs/{slug}/{site}/jobs"
    out, seen, cut = RoleList(), set(), []
    for term in (search_terms or [""]):
        offset, total = 0, None
        for _ in range(WORKDAY_MAX_PAGES):
            d = post_json(endpoint, {"appliedFacets": {}, "limit": 20,
                                     "offset": offset, "searchText": term}).json()
            if total is None:
                try:
                    total = int(d.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
            postings = _records(d.get("jobPostings"))
            for j in postings:
                path = j.get("externalPath", "")
                ref = (j.get("bulletFields") or [path])[0]
                rid = f"wd-{slug}-{ref}"
                if rid in seen:
                    continue
                seen.add(rid)
                out.append(Role(id=rid, title=j.get("title", ""),
                                url=f"https://{host}/{site}{path}",
                                location=j.get("locationsText", ""),
                                posted=j.get("postedOn", "")))
            offset += len(postings)
            # An unknown total (0) means: keep going until a page comes back empty.
            if not postings or (total and offset >= total):
                break
        if total and offset < total:
            cut.append(f"'{term or 'all'}' stopped at {offset} of {total}")
    if cut:
        out.truncated = "; ".join(cut)
    return out


UKG_PAGE = 50


def _ukg_place(loc) -> str:
    """UKG locations nest an Address whose City and State are sometimes plain
    text and sometimes {Name}/{Code} objects, depending on the tenant."""
    if not isinstance(loc, dict):
        return ""
    addr = loc.get("Address") if isinstance(loc.get("Address"), dict) else loc
    city, state = addr.get("City"), addr.get("State")
    city = city.get("Name") if isinstance(city, dict) else city
    state = (state.get("Code") or state.get("Name")) if isinstance(state, dict) else state
    return _loc(_as_text(city), _as_text(state))


def ukg(slug: str, board: str = "", host: str = "recruiting.ultipro.com", **_) -> list[Role]:
    """UKG / UltiPro. `slug` is the tenant code (LAM1000LAC), `board` the GUID.

    The host comes from the link because tenants live on different servers:
    Lamar is on recruiting2.ultipro.com, not recruiting.ultipro.com.

    Ported from the job board's working adapter, which learned two things the
    hard way. First, the three empty filter entries below are required: some
    tenants answer a request without them with a normal-looking empty list,
    which reads as "not hiring" instead of as a malformed request. Second, an
    empty board is never treated as a normal result, since a wrong board ID
    and a company that isn't hiring look identical.
    """
    if not board:
        raise FetchError("UKG needs the job-board GUID (resolve from careers page)")
    base = f"https://{host}/{slug}/JobBoard/{board}"
    out, skip, total = [], 0, None
    for _ in range(MAX_PAGES):
        body = {
            "opportunitySearch": {
                "Top": UKG_PAGE, "Skip": skip, "QueryString": "",
                "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                             "Ascending": False}],
                "Filters": [
                    {"t": "TermsSearchFilterDto", "fieldName": 4, "extra": None, "values": []},
                    {"t": "TermsSearchFilterDto", "fieldName": 5, "extra": None, "values": []},
                    {"t": "TermsSearchFilterDto", "fieldName": 6, "extra": None, "values": []},
                ],
            },
            "matchCriteria": {"PreferredJobs": [], "Educations": [],
                              "LicenseAndCertifications": [], "Skills": [],
                              "hasNoLicenses": False, "SkippedSkills": []},
        }
        d = post_json(f"{base}/JobBoardView/LoadSearchResults", body).json()
        if not isinstance(d, dict) or not isinstance(d.get("opportunities"), list):
            raise FetchError("UKG didn't return a job list (the board may have moved)")
        opps = _records(d["opportunities"])
        if total is None:
            try:
                total = int(d.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            if not opps and total == 0:
                raise FetchError("UKG board reports no openings - check the board link, "
                                 "or the company isn't hiring right now")
        for j in opps:
            locs = j.get("Locations")
            out.append(Role(id=f"uk-{_rid(j, 'Id', 'RequisitionNumber')}",
                            title=j.get("Title", ""),
                            url=f"{base}/OpportunityDetail?opportunityId={_rid(j, 'Id')}",
                            location=_ukg_place(locs[0] if isinstance(locs, list) and locs else None),
                            posted=_as_text(j.get("PostedDate"))[:10]))
        skip += len(opps)
        if len(opps) < UKG_PAGE or (total and skip >= total):
            break
    return out


ADP_PAGE = 20


ADP_LIST = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
            "staffing/v1/job-requisitions")


def _adp_place(locs) -> str:
    if not isinstance(locs, list) or not locs or not isinstance(locs[0], dict):
        return ""
    loc = locs[0]
    addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
    region = addr.get("countrySubdivisionLevel1")
    region = region.get("codeValue") if isinstance(region, dict) else region
    place = _loc(_as_text(addr.get("cityName")), _as_text(region))
    if not place and isinstance(loc.get("nameCode"), dict):
        place = _as_text(loc["nameCode"].get("shortName") or loc["nameCode"].get("longName"))
    return place


def adp(slug: str, cc: str = "", **_) -> list[Role]:
    """ADP Workforce Now career centers. `slug` is the client id (cid), `cc`
    the career-center id (ccId) when the link carries one.

    ADP hands back 20 openings per request and reports the full count in
    meta.totalNumber, so this pages until it has them all. Its page offset
    ($skip) counts from 1, not 0. A company can run several career centers
    under one client id; the ccId picks the right one, which is why it's kept.
    """
    common = f"cid={slug}" + (f"&ccId={cc}" if cc else "") + "&lang=en_US&locale=en_US"
    out, seen, total = RoleList(), set(), None
    for page in range(MAX_PAGES):
        d = get(f"{ADP_LIST}?{common}&$top={ADP_PAGE}&$skip={1 + page * ADP_PAGE}",
                accept="application/json").json()
        if not isinstance(d, dict):
            raise FetchError("ADP didn't return a job list")
        if total is None:
            meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
            try:
                total = int(meta.get("totalNumber") or 0)
            except (TypeError, ValueError):
                total = 0
        reqs = _records(d.get("jobRequisitions"))
        new = 0
        for j in reqs:
            iid = _rid(j, "itemID", "clientRequisitionID")
            if iid in seen:
                continue
            seen.add(iid)
            new += 1
            out.append(Role(
                id=f"adp-{iid}", title=j.get("requisitionTitle", ""),
                url=("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                     f"recruitment.html?cid={slug}" + (f"&ccId={cc}" if cc else "")
                     + f"&jobId={iid}&lang=en_US"),
                location=_adp_place(j.get("requisitionLocations")),
                posted=_as_text(j.get("postDate"))[:10]))
        if not reqs or not new or (total and len(seen) >= total):
            break
    if not out:
        raise FetchError("ADP career center reports no openings - if the company is hiring, "
                         "put a job link that includes its ccId in Column D")
    if total and len(out) < total:
        out.truncated = f"read {len(out)} of the {total} ADP says are open"
    return out


def bamboohr(slug: str, **_) -> list[Role]:
    d = get(f"https://{slug}.bamboohr.com/careers/list", accept="application/json").json()
    return [
        Role(id=f"bh-{_rid(j, 'id')}", title=j.get("jobOpeningName", ""),
             url=f"https://{slug}.bamboohr.com/careers/{_rid(j, 'id')}",
             location=_loc((j.get("location") or {}).get("city"),
                           (j.get("location") or {}).get("state")),
             department=j.get("departmentLabel", ""))
        for j in _records(d.get("result"))
    ]


DAYFORCE = "https://jobs.dayforcehcm.com"


DAYFORCE_PAGE = 25  # Dayforce returns 25 postings per request


def dayforce(slug: str, board: str = "CANDIDATEPORTAL", **_) -> list[Role]:
    """Dayforce's hosted job sites (jobs.dayforcehcm.com/{namespace}/{board}).

    The job list on the page is loaded by JavaScript from a data endpoint, which
    refuses requests without a session token. So: ask for the token first (the
    shared session keeps the cookie that comes with it), then request the list
    25 postings at a time, passing the token as a header. `slug` is the client
    namespace (A&B's is "abhi"), `board` the job-board code.
    """
    try:
        token = get(f"{DAYFORCE}/api/auth/csrf", accept="application/json").json().get("csrfToken")
    except FetchError as exc:
        raise FetchError(f"Dayforce didn't issue a session token ({exc})") from exc
    except (ValueError, AttributeError):
        token = None
    if not token:
        raise FetchError("Dayforce didn't issue a session token, so its job list can't be read")

    endpoint = f"{DAYFORCE}/api/geo/{slug}/jobposting/search"
    out, start, total = [], 0, None
    for _ in range(MAX_PAGES):
        d = post_json(endpoint, {"clientNamespace": slug, "jobBoardCode": board,
                                 "cultureCode": "en-US", "distanceUnit": 0,
                                 "paginationStart": start},
                      headers={"x-csrf-token": token}).json()
        if not isinstance(d, dict):
            raise FetchError("the job system sent its job list in an unexpected format")
        if total is None:
            try:
                total = int(d.get("maxCount") or 0)
            except (TypeError, ValueError):
                total = 0
        postings = _records(d.get("jobPostings"))
        for j in postings:
            pid = _rid(j, "jobPostingId", "jobReqId", "id")
            locs = j.get("postingLocations") or j.get("locations") or []
            first = locs[0] if isinstance(locs, list) and locs and isinstance(locs[0], dict) else {}
            where = (first.get("formattedAddress")
                     or _loc(first.get("cityName") or first.get("city"),
                             first.get("stateCode") or first.get("state")))
            out.append(Role(id=f"df-{slug}-{pid}",
                            title=j.get("jobTitle") or j.get("title"),
                            url=f"{DAYFORCE}/en-US/{slug}/{board}/jobs/{pid}",
                            location=where,
                            posted=_as_text(j.get("postingStartTimestampUTC")
                                            or j.get("postingStartTimestamp")
                                            or j.get("datePosted"))[:10]))
        start += len(postings)
        if not postings or (total and start >= total):
            break
    return out


def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            yield node
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                yield from _walk_jsonld(node[key])


def jsonld(slug: str = "", url: str = "", **_) -> list[Role]:
    """schema.org JobPosting markup on the careers page itself.

    Covers Paradox, many WordPress job plugins, and hand-built careers pages.
    """
    if not url:
        raise FetchError("JSON-LD fallback needs the careers URL")
    soup = BeautifulSoup(get(url).text, "lxml")
    out = []
    for tag in soup.find_all(attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for j in _walk_jsonld(data):
            locs = j.get("jobLocation") or {}
            locs = locs[0] if isinstance(locs, list) and locs else locs
            addr = (locs.get("address") or {}) if isinstance(locs, dict) else {}
            ident = j.get("identifier")
            ident = ident.get("value") if isinstance(ident, dict) else ident
            link = j.get("url") or url
            out.append(Role(id=f"ld-{ident or link}-{j.get('title','')}"[:200],
                            title=j.get("title", ""), url=link,
                            location=_loc(addr.get("addressLocality"), addr.get("addressRegion")),
                            posted=(j.get("datePosted") or "")[:10]))
    if not out:
        raise FetchError("no JobPosting markup on careers page")
    return out


# ==========================================================================
# READING A PAGE'S OWN JOB LISTINGS (unchanged)
# ==========================================================================

# Paths that are unambiguously a single job's page.
_JOB_PATH_STRONG = re.compile(
    r"/(job-listings?|jobs?|job-openings?|positions?|openings?|opportunit(?:y|ies)|"
    r"vacanc(?:y|ies)|postings?)/[^/?#]+/?$", re.I)


# /careers/<slug> is a job page on many sites, but also a section page on many
# others, so it only counts when several appear together, like a list.
_JOB_PATH_WEAK = re.compile(r"/careers?/[^/?#]+/?$", re.I)


_NOT_A_JOB = re.compile(
    r"^(benefits|culture|team|teams|life|life-at-.*|values|faqs?|students?|interns?|"
    r"internships?|early-careers|university|why-.*|about.*|perks|locations?|offices?|"
    r"search|apply|login|sign-?in|privacy.*|terms.*|our-.*|meet-.*|diversity.*|"
    r"inclusion.*|blog.*|news.*|events?|people|benefits-.*|how-we-hire|hiring-process|"
    r"open-positions|openings|all-jobs|jobs|positions)$", re.I)


_GENERIC_LINK_TEXT = re.compile(
    r"^(learn more|read more|more info|view|view (job|details|role|position|posting)|"
    r"apply|apply now|details|see (more|details)|open|[›»→>]+)$", re.I)


# Headings that introduce a list of jobs rather than naming one.
_SECTION_HEADING = re.compile(
    r"^(open (positions|roles|jobs)|job (postings|openings|listings)|current (openings|"
    r"opportunities|positions)|careers?|join (us|our team)|we.?re hiring|"
    r"opportunities|available positions|now hiring)$", re.I)


_LOCATION_HINT = re.compile(
    r"(,\s*[A-Z]{2}\b|\bremote\b|\bhybrid\b|\bon-?site\b|\bUSA\b|\bUnited States\b|"
    r"\b[A-Z]{2}\s+\d{5}\b|\b[A-Z][a-z]+,\s*[A-Z][a-z]+)", re.I)


def _join(base: str, href) -> str:
    """urljoin that returns "" for links Python can't parse (e.g. "http://[::1")."""
    if not isinstance(href, str) or not href.strip():
        return ""
    try:
        return urljoin(base, href.strip())
    except ValueError:
        return ""


def _site(url_or_host: str) -> str:
    """Registrable domain, roughly: jobs.kiterealty.com -> kiterealty.com."""
    try:
        host = urlparse(url_or_host).netloc if "//" in url_or_host else url_or_host
    except ValueError:
        return ""
    host = host.lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _job_href(href: str) -> str:
    """'strong' / 'weak' if the URL looks like one job's page, else ''."""
    try:
        path = urlparse(href).path
    except ValueError:
        return ""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not last or _NOT_A_JOB.match(last):
        return ""
    if _JOB_PATH_STRONG.search(path):
        return "strong"
    if _JOB_PATH_WEAK.search(path):
        return "weak"
    return ""


# Words that name a person's role. Deliberately not domain words like
# "development", "community" or "sales" on their own: "Growth & Development"
# and "Community Impact" are culture-page cards, not jobs.
_ROLE_NOUN = re.compile(
    r"\b(manager|mgr|mngr|dir|director|engineer|engr|eng|developer|analyst|associate|specialist|coordinator|"
    r"technician|tech|techs|agent|representative|rep|assistant|asst|assoc|lead|leader|officer|president|"
    r"vp|svp|evp|avp|head of|chief|c[eftmrio]o|consultant|designer|accountant|"
    r"administrator|admin|supervisor|intern|internship|executive|superintendent|clerk|"
    r"controller|counsel|attorney|paralegal|scientist|architect|planner|recruiter|"
    r"generalist|partner|principal|advisor|adviser|strategist|producer|writer|editor|"
    r"operator|installer|worker|driver|porter|cook|nurse|caregiver|housekeeper|concierge|"
    r"receptionist|teller|underwriter|closer|processor|appraiser|inspector|estimator|"
    r"buyer|marketer|owner|foreman|mechanic|electrician|plumber|painter|janitor|"
    r"custodian|programmer|tester|auditor|bookkeeper|cashier|server|bartender|guard|"
    r"apprentice|trainee|fellow|ambassador|expert|professional|dispatcher|scheduler|"
    r"merchandiser|therapist|aide|attendant|laborer|landscaper|groundskeeper|carpenter|"
    r"welder|lineman|line worker|splicer|team member|crew member|staff accountant|"
    r"trader|banker|broker|realtor|underwriting|actuary|economist|researcher)s?\b", re.I)


def _plausible_jobs(titles: list[str], where: str) -> None:
    """Refuse to publish a page-layout read that doesn't look like job listings.

    Raises FetchError naming what was found, so the brief can show it. Reads
    that come straight from a job system skip this; only pattern-based reads
    (jobs written on a page, jobs found through a sitemap) are checked.
    """
    if not titles:
        return
    with_role = sum(1 for t in titles if _ROLE_NOUN.search(t))
    if with_role * 3 < len(titles):
        examples = ", ".join(f"'{t}'" for t in list(dict.fromkeys(titles))[:3])
        raise FetchError(f"found {len(titles)} listings {where}, but they don't look like "
                         f"job titles (e.g. {examples})")


def _job_card(link, page_url: str) -> tuple[str, str]:
    """Title and location for one job link, read from the card around it.

    Walks up from the link until it finds a heading, but stops before the
    container grows to hold a second job, so titles never bleed across cards.
    """
    link_text = link.get_text(" ", strip=True)
    target = _join(page_url, link["href"]).split("#")[0].rstrip("/")
    node, card = link, None
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        others = {_join(page_url, a["href"]).split("#")[0].rstrip("/")
                  for a in node.find_all("a", href=True)
                  if _job_href(_join(page_url, a["href"]))}
        if len(others - {target}) > 0:
            break
        card = node
        if node.find(["h1", "h2", "h3", "h4", "h5", "h6"]):
            break

    title, heading = "", None
    if card is not None:
        heading = card.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading:
            title = heading.get_text(" ", strip=True)
    if not title and link_text and not _GENERIC_LINK_TEXT.match(link_text):
        title = link_text

    # Flat layouts: heading, details, "Apply" link, next heading... with no box
    # around each job. Use the nearest heading before the link, but only if no
    # other job link sits between them, so one job's title can't be borrowed by
    # the next. Section headings ("Open Positions") never count as a title.
    if not title:
        h = link.find_previous(["h2", "h3", "h4", "h5", "h6"])
        if h is not None and not _SECTION_HEADING.match(h.get_text(" ", strip=True)):
            clean_run = True
            for el in h.find_all_next("a", href=True):
                if el is link:
                    break
                if _job_href(_join(page_url, el["href"])):
                    clean_run = False
                    break
            if clean_run:
                heading, title = h, h.get_text(" ", strip=True)
                card = None
                location = ""
                for el in h.next_elements:
                    if el is link:
                        break
                    if isinstance(el, str):
                        text = el.strip()
                        if text and text != title and len(text) <= 60 and _LOCATION_HINT.search(text):
                            location = text
                            break
                if title and len(title) <= 140:
                    return title, location
    if not title or len(title) > 140 or _GENERIC_LINK_TEXT.match(title):
        return "", ""

    location = ""
    if card is not None:
        for text in card.stripped_strings:
            if text in (title, link_text) or len(text) > 60:
                continue
            if _LOCATION_HINT.search(text):
                location = text
                break
    return title, location


ONPAGE_MAX_PAGES = 15


# A next-page link must look like another page of the same list: ?page=2,
# /page/2/, ?start=20... This is what keeps "Next post" and other "next" links
# elsewhere on a site from being followed.
_PAGE_URL = re.compile(r"([?&](page|pg|p|paged|pagenum|pageno|start|offset|skip)=\d+)|/page/\d+/?($|[?#])", re.I)


_NEXT_TEXT = re.compile(r"^(next|next page|next ›|next »|next >|more|›|»|>|→|>>)$", re.I)


def _next_page(soup, current: str) -> str:
    """The URL of the next page of a paginated job list, or ""."""
    candidates = []
    for tag in soup.find_all(["link", "a"], href=True):
        rel = [x.lower() for x in (tag.get("rel") or [])]
        if "next" in rel:
            candidates.append(tag["href"])
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        label = (a.get("aria-label") or "") + " " + " ".join(a.get("class") or [])
        if _NEXT_TEXT.match(text) or re.search(r"\bnext\b", label, re.I):
            candidates.append(a["href"])
    for href in candidates:
        nxt = _join(current, href)
        if (nxt and _site(nxt) == _site(current) and _PAGE_URL.search(nxt)
                and nxt.split("#")[0] != current.split("#")[0]):
            return nxt.split("#")[0]
    return ""


def onpage(slug: str = "", url: str = "", trusted: bool = False, **_) -> list[Role]:
    """Jobs written directly onto the company's own careers page.

    For companies with no job system at all (MPT, many small operators): each
    opening sits on the careers page with a link to its own page. Only links on
    the company's own site count, and links that could be ordinary section
    pages (/careers/benefits) are excluded, or only accepted as a group.
    """
    if not url:
        raise FetchError("on-page listings need the careers URL")

    found: dict[str, tuple[str, Role]] = {}
    untitled = 0
    page_url, visited, pages_read = url, set(), 0
    while page_url and page_url not in visited and pages_read < ONPAGE_MAX_PAGES:
        visited.add(page_url)
        try:
            r = get(page_url)
        except FetchError:
            if pages_read:
                break          # a later page failing keeps what was already read
            raise
        pages_read += 1
        visited.add(r.url.split("#")[0])
        soup = BeautifulSoup(r.text, "lxml")
        # Find the next page BEFORE stripping menus: pagination links usually
        # sit inside a <nav>, which the next line removes.
        next_url = _next_page(soup, r.url)
        for tag in soup.find_all(["nav", "header", "footer"]):
            tag.decompose()
        before = len(found)
        untitled += _read_listing_page(soup, r.url, found)
        if pages_read > 1 and len(found) == before:
            break              # a "next" page with nothing new: stop
        page_url = next_url

    strong = [role for kind, role in found.values() if kind == "strong"]
    weak = [role for kind, role in found.values() if kind == "weak"]
    roles = strong + (weak if len(weak) >= 2 else [])
    if not roles:
        if untitled:
            raise FetchError(f"found {untitled} job links on the careers page "
                             "but couldn't read their titles")
        raise FetchError("no job listings found on the careers page")
    if not trusted:
        _plausible_jobs([r.title for r in roles], "on the careers page")
    return roles


def _read_listing_page(soup, page_url: str, found: dict) -> int:
    """Add one page's job links to `found`; return how many had no title."""
    untitled = 0
    for a in soup.find_all("a", href=True):
        href = _join(page_url, a["href"]).split("#")[0]
        if not href.startswith("http") or _site(href) != _site(page_url):
            continue
        if href.rstrip("/") == page_url.rstrip("/").split("#")[0]:
            continue
        if _PAGE_URL.search(href) and not _job_href(href.split("?")[0]):
            continue           # a pagination link, not a job
        kind = _job_href(href)
        if not kind or href in found:
            continue
        title, location = _job_card(a, page_url)
        if title:
            found[href] = (kind, Role(id=f"op-{urlparse(href).path.rstrip('/')}",
                                      title=title, url=href, location=location))
        elif kind == "strong":
            untitled += 1
    return untitled


# ==========================================================================
# RECOGNISING JOB-BOARD LINKS (unchanged)
# ==========================================================================

# (ats, compiled pattern). Named groups feed the adapter kwargs.
PATTERNS = [
    ("workday", re.compile(
        r"https?://(?P<host>(?P<slug>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)"
        r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_\-]+)", re.I)),
    ("workday", re.compile(
        r"https?://(?P<host>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
        r"recruiting/(?P<slug>[a-z0-9_-]+)/(?P<site>[A-Za-z0-9_\-]+)", re.I)),
    ("ukg", re.compile(
        r"https?://(?P<host>recruiting\d?\.ultipro\.com)/(?P<slug>[A-Z0-9]+)"
        r"/JobBoard/(?P<board>[0-9a-f-]{36})", re.I)),
    # jobs.dayforcehcm.com/[en-US/]{namespace}/{board}[/jobs/123]
    ("dayforce", re.compile(
        r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<slug>[a-z0-9_-]+)"
        r"/(?P<board>[A-Za-z0-9_-]+)", re.I)),
    # older format: dayforcehcm.com/CandidatePortal/en-US/{namespace}
    ("dayforce", re.compile(
        r"dayforcehcm\.com/CandidatePortal/(?:[a-z]{2}-[A-Z]{2}/)?(?P<slug>[a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?"
        r"(?P<slug>[a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=(?P<slug>[a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[a-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"(?P<slug>[a-z0-9-]+)\.recruitee\.com", re.I)),
    ("breezy", re.compile(r"(?P<slug>[a-z0-9-]+)\.breezy\.hr", re.I)),
    ("bamboohr", re.compile(r"(?P<slug>[a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("adp", re.compile(r"workforcenow\.adp\.com/.*?[?&]cid=(?P<slug>[0-9a-f-]{36})", re.I)),
    # Recognised, unsupported.
    ("icims", re.compile(r"(?P<slug>[a-z0-9-]+)\.icims\.com", re.I)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("jazzhr", re.compile(r"(?P<slug>[a-z0-9-]+)\.applytojob\.com", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("paradox", re.compile(r"paradox\.ai", re.I)),
    ("taleo", re.compile(r"taleo\.net", re.I)),
    ("successfactors", re.compile(r"successfactors\.com|jobs\.sap\.com", re.I)),
    ("applicantpro", re.compile(r"applicantpro\.com", re.I)),
]


# Slugs that are ATS infrastructure words, never a real company board.
_BAD_SLUGS = {"embed", "js", "v1", "jobs", "api", "www", "careers", "job_board", "mydayforce",
              "candidateportal", "en-us",
              "boards", "en-us", "recruiting", "app", "static", "assets"}


# ==========================================================================
# COLUMN D
# ==========================================================================
#
# The whole design in one place. Column D of the OpCo tab is the only input:
#
#   a job-board link      -> read that board with its system's reader
#   any other web page    -> read only the jobs listed on that page itself
#   empty, or not a link  -> "needs a job-board link"
#
# Nothing is discovered. No guessing boards from company names, no scanning
# pages for links, no following links, no sitemaps, no cached detections. If
# Column D is right, the result is right; if it isn't, the brief says so.

READERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "breezy": breezy,
    "workday": workday, "ukg": ukg, "dayforce": dayforce, "adp": adp, "bamboohr": bamboohr,
}

# Recognised in Column D, but no reader yet. Listed by name in the brief so it's
# clear which readers would be worth building, and for how many companies.
EXTRA_UNSUPPORTED = [
    ("paycom", re.compile(r"paycomonline\.(net|com)", re.I)),
    ("paycor", re.compile(r"recruitingbypaycor\.com", re.I)),
    ("isolved", re.compile(r"isolvedhire\.com", re.I)),
    ("hireology", re.compile(r"hireology\.com", re.I)),
    ("apploi", re.compile(r"apploi\.com", re.I)),
]


class NeedsLink(FetchError):
    """Column D can't be read as it stands; the message says why."""


@dataclass
class Board:
    system: str = ""          # a READERS key, "page", an unsupported system name, or ""
    slug: str = ""
    params: dict = field(default_factory=dict)
    problem: str = ""         # why Column D can't be used, when system is ""

    @property
    def readable(self) -> bool:
        return self.system in READERS or self.system == "page"

    @property
    def key(self) -> str:
        """Identity of the board, so a changed Column D starts a fresh baseline."""
        return f"{self.system}:{self.slug}:{json.dumps(self.params, sort_keys=True)}"


def classify(column_d: str) -> Board:
    """Decide what a Column D value is. Pure: no network, no memory."""
    url = (column_d or "").strip()
    if not url:
        return Board(problem="Column D is empty")
    if not re.match(r"https?://", url, re.I):
        return Board(problem=f"Column D isn't a link ({url[:60]!r})")

    for system, pattern in PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        groups = m.groupdict()
        slug = (groups.pop("slug", "") or "").strip("/")
        if slug.lower() in _BAD_SLUGS:
            continue
        if system in READERS and not slug:
            continue
        params = {k: v for k, v in groups.items() if v}
        if system == "adp":
            try:
                cc = parse_qs(urlparse(url).query).get("ccId", [""])[0]
            except ValueError:
                cc = ""
            if cc:
                params["cc"] = cc
        return Board(system=system, slug=slug, params=params)

    for system, pattern in EXTRA_UNSUPPORTED:
        if pattern.search(url):
            return Board(system=system)

    return Board(system="page", params={"url": url})


_CAREERS_HOST = re.compile(r"(careers?|jobs?|join|work|talent|hiring)\.", re.I)
_LOOKS_LIKE_HTML = re.compile(
    r"<\s*(html|head|body|div|a|p|h[1-6]|section|main|ul|span|meta|title|link|"
    r"style|form|table|img|nav|header|footer|article|!doctype)\b", re.I)


def read_page(url: str, trusted: bool = False) -> tuple[list[Role], str]:
    """Jobs listed on the Column D page itself. Returns (roles, how they were read).

    Reads structured job data on the page if there is any, otherwise the job
    listings on the page (following its pagination). Never leaves the page to
    look for a job system: if the jobs aren't on this page, Column D needs the
    job board's link instead, and NeedsLink says so.
    """
    try:
        r = get(url, timeout=25)
    except FetchError as exc:
        raise NeedsLink(f"the Column D page doesn't load ({exc})") from exc

    body = (r.text or "").strip()
    if not body:
        raise NeedsLink("the Column D page is empty")
    if not _LOOKS_LIKE_HTML.search(body[:300000]) and "application/ld+json" not in body[:300000]:
        raise NeedsLink("the Column D link isn't a web page (it returned a file or raw data)")

    asked, landed = urlparse(url), urlparse(r.url)
    if asked.path in ("", "/") and not _CAREERS_HOST.match(asked.netloc):
        raise NeedsLink("Column D is a homepage, not a careers page or job board")
    if asked.path not in ("", "/") and landed.path in ("", "/") and not _CAREERS_HOST.match(landed.netloc):
        raise NeedsLink("the Column D page redirects to the homepage, so it probably doesn't exist")
    moved = (f"Column D now redirects to {landed.netloc} - worth updating; "
             if _site(r.url) != _site(url) else "")

    try:
        return jsonld(url=r.url), moved + "read from structured job data on the page"
    except FetchError:
        pass
    try:
        return onpage(url=r.url, trusted=trusted), moved + "read from the jobs listed on the page"
    except FetchError as exc:
        msg = str(exc)
        if msg.startswith("found "):
            raise NeedsLink(f"{msg}, so nothing was published") from exc
        raise NeedsLink("no jobs are listed on this page itself (it may load them with "
                        "JavaScript) - put the job board's link in Column D") from exc


# ==========================================================================
# BASELINE
# ==========================================================================
#
# The only thing remembered between runs: last week's roles per company, so the
# brief can say what's new and what closed. Stored with the board it came from,
# so changing Column D starts that company over instead of reporting false
# closures.

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "opco"
BASELINE_PATH = STATE / "baseline.json"
LIVE_COPY = STATE / "opco_live.csv"
SUSPICIOUS_DROP = 5

# Files the previous version of this search kept in state/. Nothing reads them
# any more; they're removed so nobody mistakes them for live data. Only these
# exact names: state/ also holds the people-moves workflow's files.
LEGACY_FILES = ("ats_cache.json", "last_good.json", "job_pages.json", "opco_live.csv")


class Baseline:
    def __init__(self, run_date: str | None = None):
        self.run_date = run_date or date.today().isoformat()
        self.data: dict = _read(BASELINE_PATH, {})

    def _prev(self, company: str, board: str) -> dict | None:
        prev = self.data.get(company)
        return prev if prev and prev.get("board") == board else None

    def carry_forward(self, company: str, reason: str, board: str) -> dict:
        prev = self._prev(company, board)
        if not prev:
            return {"status": "failed", "reason": reason, "roles": []}
        return {"status": "stale", "reason": reason, "roles": prev["roles"],
                "stale_since": prev.get("fetched_on")}

    def is_suspicious_drop(self, company: str, count: int, board: str) -> bool:
        prev = self._prev(company, board)
        return bool(prev) and count == 0 and len(prev.get("roles", [])) >= SUSPICIOUS_DROP

    def is_first_read(self, company: str, board: str) -> bool:
        return self._prev(company, board) is None

    def save(self, results: dict) -> None:
        for company, r in results.items():
            if r["status"] == "ok":
                self.data[company] = {"fetched_on": self.run_date, "board": r["board"],
                                      "roles": r["roles"]}
            elif r["status"] in ("needs-link", "unsupported"):
                self.data.pop(company, None)   # no board, so no trustworthy baseline
        _write(BASELINE_PATH, self.data)


def remove_legacy_state() -> None:
    old = ROOT / "state"
    for name in LEGACY_FILES:
        f = old / name
        if f.exists():
            f.unlink()
            log.info("removed old state file %s (no longer used)", f.relative_to(ROOT))
    for f in old.glob("checkpoint-*.json"):
        f.unlink()
        log.info("removed old state file %s (no longer used)", f.relative_to(ROOT))


# ==========================================================================
# ONE COMPANY
# ==========================================================================

def read_company(company, role_filter, baseline) -> dict:
    """Read one company from its Column D. Always returns a result."""
    board = classify(company.careers_url)
    base = {"column_d": company.careers_url, "column_d_from": company.careers_from,
            "system": board.system, "board": board.key, "note": "",
            "roles": [], "filtered": [], "total": 0}

    if not board.system:
        return {**base, "status": "needs-link", "reason": board.problem}
    if not board.readable:
        return {**base, "status": "unsupported", "reason": f"{board.system} isn't supported yet"}

    name = company.name
    try:
        if board.system == "page":
            fetched, how = read_page(board.params["url"],
                                     trusted=name.lower() in role_filter.trusted_pages)
            base["note"] = how
        else:
            params = dict(board.params)
            if board.system == "workday" and role_filter.search_terms.get(name.lower()):
                params["search_terms"] = role_filter.search_terms[name.lower()]
            fetched = READERS[board.system](board.slug, **params)
        roles = [r.to_dict() for r in fetched if r.title]
    except NeedsLink as exc:
        return {**base, "status": "needs-link", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one company never sinks the run
        reason = plain_error(exc)
        log.warning("  %s: read failed (%s)", name, reason)
        result = {**base, **baseline.carry_forward(name, reason, board.key)}
    else:
        truncated = getattr(fetched, "truncated", "")
        if truncated:
            base["note"] = (base["note"] + "; " if base["note"] else "") + f"cut short: {truncated}"
        if baseline.is_suspicious_drop(name, len(roles), board.key):
            result = {**base, **baseline.carry_forward(
                name, "dropped to 0 roles from 5+ last week; treated as a read failure", board.key)}
        else:
            result = {**base, "status": "ok", "roles": roles}
            # First time this Column D is read: list it once so a person can
            # confirm the jobs really belong to this company. A wrong link in
            # Column D is the one way the wrong company's jobs can still appear.
            if baseline.is_first_read(name, board.key) and roles:
                result["spot_check"] = [f"{r['title']} ({r['location']})" if r.get("location")
                                        else r["title"] for r in roles[:3]]

    result["filtered"] = []
    for r in result["roles"]:
        category = role_filter.categorize(r["title"], name)
        if category:
            result["filtered"].append({**r, "company": name, "category": category,
                                       "status": result["status"]})
    result["total"] = len(result["roles"])
    return result


def compute_diff(results: dict, baseline: Baseline, role_filter) -> dict:
    """New and closed matching roles, for companies read cleanly both weeks
    from the same board."""
    new, closed = [], []
    for company, c in results.items():
        if c["status"] != "ok":
            continue
        prev = baseline._prev(company, c["board"])
        if not prev:
            continue
        prev_ids = {r["id"] for r in prev["roles"]}
        now_ids = {r["id"] for r in c["roles"]}
        new.extend(r for r in c["filtered"] if r["id"] not in prev_ids)
        for r in prev["roles"]:
            if r["id"] not in now_ids:
                category = role_filter.categorize(r["title"], company)
                if category:
                    closed.append({**r, "company": company, "category": category})
    return {"new": new, "closed": closed}


# ==========================================================================
# COMPANIES
# ==========================================================================

@dataclass
class Company:
    name: str
    website: str = ""
    careers_url: str = ""
    careers_from: str = ""
    state: str = ""
    segment: str = ""


_NA = {"", "n/a", "na", "none", "-"}


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _NA else v


CAREERS_COLUMN = 3   # Column D on the OpCo tab (0-based index)


def parse_companies(text: str, source: str) -> list[Company]:
    """Read the OpCo tab. The careers URL always comes from Column D.

    Column D is found by its header, "Careers Page URL". If that header is ever
    renamed, the parser falls back to Column D by position, so the careers URL
    the scraper checks is always what sits in that column.
    """
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        raise ValueError(f"{source} is empty")
    header = [h.strip() for h in rows[0]]
    if "Company Name" not in header:
        raise ValueError(f"{source} has no 'Company Name' header; is it the OpCo tab?")

    def col(name, fallback=None):
        return header.index(name) if name in header else fallback

    i_name, i_web = col("Company Name"), col("Website URL")
    i_careers = col("Careers Page URL", CAREERS_COLUMN)
    if "Careers Page URL" not in header:
        found = header[CAREERS_COLUMN] if CAREERS_COLUMN < len(header) else "(missing)"
        log.warning("no 'Careers Page URL' header; using Column D, headed %r", found)
    i_state, i_seg = col("State"), col("Industry Segment")

    def cell(r, i):
        return _clean(r[i]) if i is not None and i < len(r) else ""

    out, seen = [], set()
    for r in rows[1:]:
        name = cell(r, i_name)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        website = cell(r, i_web)
        careers = cell(r, i_careers)
        if not website.startswith("http"):
            website = ""  # e.g. "(TBD need to find...)"
        careers_from = "Column D"
        # Some rows put the careers link in the Website column instead.
        if not careers and re.search(r"/(careers?|jobs|employment)", website, re.I):
            careers, careers_from = website, "Column B (Column D empty)"
        out.append(Company(name=name, website=website, careers_url=careers,
                           careers_from=careers_from if careers else "",
                           state=cell(r, i_state), segment=cell(r, i_seg)))
    log.info("loaded %d companies from %s", len(out), source)
    return out


def load_companies(path: Path) -> list[Company]:
    return parse_companies(path.read_text(encoding="utf-8-sig"), str(path))


def find_companies_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in (ROOT.parent / "opco.csv", ROOT.parent / "data" / "opco.csv",
                      ROOT / "opco.csv", Path("opco.csv")):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "opco.csv not found. Expected it at the repo root or data/opco.csv.")


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("could not read %s; starting fresh", path)
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def load_live_companies(url: str) -> tuple[list[Company], str]:
    """Read the OpCo tab straight from Google Sheets.

    Falls back to the last good live copy, so a Sheets outage or a sharing
    change never stops the run. Returns (companies, description of source).
    """
    try:
        r = get(url, timeout=30)
        text = r.content.decode("utf-8-sig", errors="replace")
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise FetchError("Google returned a sign-in page, not CSV - the tab "
                             "isn't published or shared for viewing")
        companies = parse_companies(text, "the live sheet")
        LIVE_COPY.parent.mkdir(parents=True, exist_ok=True)
        LIVE_COPY.write_text(text, encoding="utf-8")
        return companies, "live Google Sheet (OpCo tab)"
    except (FetchError, ValueError) as exc:
        log.warning("could not read the live sheet: %s", exc)
        if LIVE_COPY.exists():
            log.warning("using the last good copy of the sheet instead")
            return load_companies(LIVE_COPY), f"last saved copy of the sheet - live read failed: {exc}"
        raise


# ==========================================================================
# ROLE FILTER
# ==========================================================================

class RoleFilter:
    """Title-keyword filter, configured in opco_config.yml."""

    def __init__(self, cfg: dict):
        def rx(words):
            return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None

        self.include = {k: rx(v) for k, v in (cfg.get("include") or {}).items()}
        self.exclude = rx(cfg.get("exclude") or [])
        self.overrides = {}
        self.search_terms = {}
        self.trusted_pages = set()   # pages whose listings skip the "looks like jobs" check
        for name, rule in (cfg.get("company_overrides") or {}).items():
            rule = rule or {}
            if rule.get("trust_page"):
                self.trusted_pages.add(name.lower())
            self.overrides[name.lower()] = {
                "require": rx(rule.get("require") or []),
                "exclude": rx(rule.get("exclude") or []),
            }
            if rule.get("search_terms"):
                self.search_terms[name.lower()] = [str(t) for t in rule["search_terms"]]

    @classmethod
    def load(cls, path: Path) -> "RoleFilter":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def categorize(self, title: str, company: str = "") -> str | None:
        """Return the matched category, or None if the role is filtered out."""
        t = title or ""
        if self.exclude and self.exclude.search(t):
            return None

        rule = self.overrides.get(company.lower())
        if rule:
            if rule["exclude"] and rule["exclude"].search(t):
                return None
            if rule["require"] and not rule["require"].search(t):
                return None

        for category, pattern in self.include.items():
            if pattern and pattern.search(t):
                return category
        return None


# ==========================================================================
# ERRORS
# ==========================================================================

def plain_error(exc: Exception) -> str:
    """What went wrong, in words a person can act on."""
    detail = f"{type(exc).__name__}: {exc}"[:160]
    if isinstance(exc, FetchError):
        return str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "the job system sent back a web page instead of job data"
    if isinstance(exc, (KeyError, TypeError, AttributeError, ValueError, IndexError)):
        return f"the job system's data wasn't in the expected format ({detail})"
    return f"unexpected error ({detail})"


CATEGORY_LABEL = {"executive": "Executive", "sales": "Sales", "gtm": "GTM",
                  "engineering": "Engineering", "operators": "Operators"}


def _line(r: dict) -> str:
    bits = [f"[{r['title']}]({r['url']})" if r.get("url") else r["title"]]
    if r.get("location"):
        bits.append(r["location"])
    return " — ".join(bits)


# ==========================================================================
# REPORT
# ==========================================================================

STATUS_ORDER = {"ok": 0, "stale": 1, "failed": 2, "unsupported": 3, "needs-link": 4}


def _attention(results: dict) -> list[str]:
    """Everything that needs a person, grouped by what to do about it."""
    needs = {n: c for n, c in results.items() if c["status"] == "needs-link"}
    unsupported = {n: c for n, c in results.items() if c["status"] == "unsupported"}
    failed = {n: c for n, c in results.items() if c["status"] in ("failed", "stale")}
    spot = {n: c for n, c in results.items() if c.get("spot_check")}
    cut = {n: c for n, c in results.items() if "cut short" in (c.get("note") or "")}
    moved = {n: c for n, c in results.items()
             if c["status"] == "ok" and "now redirects" in (c.get("note") or "")}
    if not any((needs, unsupported, failed, spot, cut, moved)):
        return []

    md = ["## Needs your attention", ""]
    if needs:
        md += [f"### Needs a job-board link in Column D ({len(needs)})", "",
               "*Fix: open the company's careers page, click any job, and paste the address "
               "it lands on into Column D (drop the part that names the specific job). If "
               "the jobs are listed on the company's own page, that page's address works too.*", ""]
        for n, c in sorted(needs.items()):
            md.append(f"- **{n}** — {c['reason']}")
        md.append("")
    if unsupported:
        by_system: dict[str, list[str]] = {}
        for n, c in unsupported.items():
            by_system.setdefault(c["system"], []).append(n)
        md += [f"### Job system not supported yet ({len(unsupported)})", "",
               "*Column D is fine; the scraper has no reader for this system yet. "
               "Ordered by how many companies a reader would unlock.*", ""]
        for system, names in sorted(by_system.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            md.append(f"- **{system}** ({len(names)}) — {', '.join(sorted(names))}")
        md.append("")
    if failed:
        md += [f"### Failed this week ({len(failed)})", "",
               "*Reading went wrong. Last week's roles are carried forward where there were "
               "any, and these are left out of new/closed so a failure never reads as a hire.*", ""]
        for n, c in sorted(failed.items()):
            md.append(f"- **{n}** — {c.get('reason') or 'unknown error'}")
        md.append("")
    if spot:
        md += [f"### Confirm these are the right companies — once ({len(spot)})", "",
               "*First time each of these Column D links was read. Open the link and check the "
               "jobs belong to this company. If one is wrong, fix its Column D; nothing else "
               "is needed. Each is listed only once, and again only if its Column D changes.*", ""]
        for n, c in sorted(spot.items()):
            d = c.get("column_d") or ""
            md.append(f"- **{n}** — [{d.split('//')[-1][:50]}]({d}) · {c.get('total', 0)} jobs, "
                      f"e.g. {'; '.join(c['spot_check'])}")
        md.append("")
    if moved:
        md += [f"### Works, but Column D has moved ({len(moved)})", ""]
        for n, c in sorted(moved.items()):
            md.append(f"- **{n}** — {c['note'].split(';')[0]}")
        md.append("")
    if cut:
        md += [f"### Results cut short ({len(cut)})", ""]
        for n, c in sorted(cut.items()):
            md.append(f"- **{n}** — {c['note']}")
        md.append("")
    return md


def write_report(results: dict, diff: dict, run_date: str, first_run: bool,
                 out_dir: Path, source: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    open_roles = [r for c in results.values() for r in c.get("filtered", [])]
    with_roles = sorted({r["company"] for r in open_roles})
    readable = sum(1 for c in results.values() if c["status"] in ("ok", "stale"))

    md = [f"# OpCo job search — week of {run_date}", "",
          f"{len(open_roles)} matching roles across {len(with_roles)} companies. "
          f"{readable} of {len(results)} companies read from their Column D.", "",
          f"Company list read from: **{source}**.", "",
          "> Open each link before it goes in the newsletter. Postings move.", ""]
    md += _attention(results)

    if first_run:
        md += ["*First run of this version: this is the baseline. New-this-week and "
               "recently-closed start next week.*", ""]
    else:
        md += [f"## New this week ({len(diff['new'])})", ""]
        md += ([f"- **{r['company']}** · {CATEGORY_LABEL.get(r['category'], r['category'])} · {_line(r)}"
                for r in sorted(diff["new"], key=lambda x: (x["company"], x["title"]))]
               or ["No new matching roles."])
        md += ["", f"## Recently closed ({len(diff['closed'])})", "",
               "*Open last week, gone now — the \"recently hired\" signal. Closed usually means "
               "filled, but can mean pulled. Companies that failed this week are left out.*", ""]
        md += ([f"- **{r['company']}** · {r['title']}" + (f" — {r['location']}" if r.get("location") else "")
                for r in sorted(diff["closed"], key=lambda x: (x["company"], x["title"]))]
               or ["No matching roles closed."])
        md.append("")

    md += ["## All open roles", ""]
    for company in with_roles:
        roles = [r for r in open_roles if r["company"] == company]
        c = results[company]
        md.append(f"### {company} ({len(roles)})"
                  + (f" — *carried forward from {c.get('stale_since')}*" if c["status"] == "stale" else ""))
        groups: dict[tuple, list] = {}
        for r in roles:
            groups.setdefault((r["category"], r["title"]), []).append(r)
        for (category, title), group in sorted(groups.items()):
            label = CATEGORY_LABEL.get(category, category)
            if len(group) == 1:
                md.append(f"- {label} · {_line(group[0])}")
            else:
                places = sorted({g["location"] for g in group if g.get("location")})
                where = f" — {', '.join(places[:5])}{'…' if len(places) > 5 else ''}" if places else ""
                md.append(f"- {label} · [{title}]({group[0]['url']}) **({len(group)} openings)**{where}")
        md.append("")

    md += ["## Coverage", "",
           "| Company | Column D | System | Status | Roles (all / matching) | Note |",
           "|---|---|---|---|---|---|"]
    for n, c in sorted(results.items(), key=lambda kv: (STATUS_ORDER.get(kv[1]["status"], 9), kv[0])):
        d = c.get("column_d") or ""
        shown = f"[{d.split('//')[-1][:45]}]({d})" if d.startswith("http") else (d[:45] or "— empty")
        if d and str(c.get("column_d_from", "")).startswith("Column B"):
            shown += " *(from Column B)*"
        note = (c.get("reason") or c.get("note") or "").replace("|", "/")
        md.append(f"| {n} | {shown} | {c.get('system') or '—'} | {c['status']} | "
                  f"{c.get('total', 0)} / {len(c.get('filtered', []))} | {note} |")
    counts: dict[str, int] = {}
    for c in results.values():
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    md += ["", "**Status:** " + ", ".join(f"{k} {v}" for k, v in
                                          sorted(counts.items(), key=lambda kv: STATUS_ORDER.get(kv[0], 9))), ""]

    text = "\n".join(md)
    path = out_dir / f"opco-jobs-{run_date}.md"
    path.write_text(text, encoding="utf-8")
    (out_dir / "opco-jobs-latest.md").write_text(text, encoding="utf-8")
    (out_dir / "opco-jobs-latest.json").write_text(json.dumps({
        "run_date": run_date, "roles": open_roles, "new": diff["new"], "closed": diff["closed"],
        "coverage": {n: {k: v for k, v in c.items() if k not in ("roles", "filtered")}
                     for n, c in results.items()},
    }, indent=2), encoding="utf-8")
    # Every job read, matching or not, with its link: for checking the scraper
    # read the right jobs from the right companies.
    with (out_dir / f"opco-jobs-all-{run_date}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "title", "location", "url", "posted",
                                           "in_scope", "column_d"])
        w.writeheader()
        for n, c in sorted(results.items()):
            in_scope = {r["id"] for r in c.get("filtered", [])}
            for r in c.get("roles", []):
                w.writerow({"company": n, "title": r.get("title", ""), "location": r.get("location", ""),
                            "url": r.get("url", ""), "posted": r.get("posted", ""),
                            "in_scope": "yes" if r.get("id") in in_scope else "no",
                            "column_d": c.get("column_d", "")})

    with (out_dir / f"opco-jobs-{run_date}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "category", "title", "location", "url",
                                           "posted", "status"])
        w.writeheader()
        for r in open_roles:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    return path


def write_column_d_check(companies, out_dir: Path) -> Path:
    """--discover: what each Column D is, without reading any jobs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in companies:
        b = classify(c.careers_url)
        if not b.system:
            verdict = f"needs a link — {b.problem}"
        elif b.system == "page":
            verdict = "a web page — jobs will be read from the page itself"
        elif b.readable:
            verdict = f"{b.system} job board — ready"
        else:
            verdict = f"{b.system} — not supported yet"
        rows.append((c.name, c.careers_url, verdict))
    md = ["# Column D check", "", "| Company | Column D | What it is |", "|---|---|---|"]
    md += [f"| {n} | {d[:70] or '— empty'} | {v} |" for n, d, v in rows]
    path = out_dir / "opco-columnd-check.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return path


# ==========================================================================
# MAIN
# ==========================================================================

CONFIG_PATH = ROOT / "opco_config.yml"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--companies", help="path to opco.csv (default: live sheet or repo copy)")
    p.add_argument("--only", help="comma-separated company names")
    p.add_argument("--discover", action="store_true",
                   help="check what each Column D is, without reading jobs")
    p.add_argument("--refresh-ats", action="store_true",
                   help="no longer does anything: nothing is cached between runs")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--out", default=str(ROOT / "out"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    if args.refresh_ats:
        log.info("--refresh-ats has no effect: this version caches nothing between runs")

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("config not found: %s", config_path)
        return 2
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    sheet_url = str(config.get("sheet_csv_url") or "").strip()
    try:
        if args.companies:
            companies, source = load_companies(Path(args.companies)), f"file {Path(args.companies).name}"
        elif sheet_url:
            try:
                companies, source = load_live_companies(sheet_url)
            except (FetchError, ValueError) as exc:
                path = find_companies_file(None)
                companies, source = load_companies(path), f"repo copy {path.name} (live sheet unavailable: {exc})"
        else:
            path = find_companies_file(None)
            companies, source = load_companies(path), f"repo copy {path.name}"
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    log.info("company list: %s", source)

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        companies = [c for c in companies if c.name.lower() in wanted]

    if args.discover:
        path = write_column_d_check(companies, Path(args.out))
        log.info("Column D check written to %s", path)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(path.read_text(encoding="utf-8"))
        return 0

    remove_legacy_state()
    role_filter = RoleFilter(config)
    baseline = Baseline()
    first_run = not baseline.data

    results: dict = {}
    for i, company in enumerate(companies, 1):
        try:
            result = read_company(company, role_filter, baseline)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            log.error("  %s: crashed\n%s", company.name, traceback.format_exc())
            result = {"column_d": company.careers_url, "column_d_from": company.careers_from,
                      "system": "", "board": "", "note": "", "status": "failed",
                      "reason": plain_error(exc), "roles": [], "filtered": [], "total": 0}
        results[company.name] = result
        log.info("[%2d/%d] %-34s %-11s %-11s %d/%d", i, len(companies), company.name[:34],
                 result.get("system") or "-", result["status"],
                 result["total"], len(result["filtered"]))

    diff = {"new": [], "closed": []} if first_run else compute_diff(results, baseline, role_filter)
    baseline.save(results)
    try:
        path = write_report(results, diff, baseline.run_date, first_run, Path(args.out), source)
    except Exception:  # noqa: BLE001 - always leave something readable
        log.error("report failed, writing a plain fallback\n%s", traceback.format_exc())
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"opco-jobs-{baseline.run_date}.md"
        lines = [f"# OpCo job search - {baseline.run_date} (plain fallback)", ""]
        for n, c in sorted(results.items()):
            lines.append(f"## {n} - {c['status']} - {c.get('reason') or c.get('note') or ''}")
            lines += [f"- {r['title']} - {r.get('location', '')} - {r.get('url', '')}"
                      for r in c.get("filtered", [])]
        path.write_text("\n".join(lines), encoding="utf-8")

    ok = sum(1 for c in results.values() if c["status"] == "ok")
    log.info("")
    log.info("%d/%d companies read cleanly -> %s", ok, len(results), path)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(path.read_text(encoding="utf-8"))
    return 0 if ok or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
