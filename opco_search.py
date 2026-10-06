#!/usr/bin/env python3
"""OpCo weekly job search — single-file edition.

Scrapes the companies on the master sheet's OpCo tab straight from each
company's own applicant tracking system. No Built In, no aggregator, no single
host whose rate limit can sink the run.

Three files make up the whole thing:
    opco_search.py                         this file
    opco_config.yml                        which roles count, pinned ATS
    .github/workflows/opco-job-search.yml  the Sunday schedule

    python opco_search.py                  # weekly run
    python opco_search.py --discover       # resolve ATS only, no job fetch
    python opco_search.py --refresh-ats    # ignore the ATS cache, re-detect
    python opco_search.py --only "Greystar,Belong" -v
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback


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
# ADAPTERS
# ==========================================================================

"""One fetcher per ATS. Each returns a list of Role dicts.

Every adapter hits a public, keyless endpoint the ATS serves to its own careers
widgets. Response shapes are verified against fixtures, not live calls — the
first CI run is the real test. A failing adapter raises, gets caught per
company, and shows up in the coverage report; it never sinks the run.

CONFIDENCE
  High:     Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee,
            Breezy, Workday (CXS)
  Moderate: UKG/UltiPro, ADP Workforce Now, BambooHR
  Fallback: schema.org JobPosting JSON-LD scraped from the careers page
"""


import hashlib
import json
import logging
import re
from dataclasses import dataclass, asdict, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------- high confidence

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


class RoleList(list):
    """A list of roles that can also say it was cut short."""
    truncated: str = ""


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


# ---------------------------------------------------------------- moderate confidence

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


# ---------------------------------------------------------------- fallback

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


# ---------------------------------------------------------------- on-page listings

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


# ---------------------------------------------------------------- sitemap

SITEMAP_MAX_NEW_PAGES = 250   # job pages read per company per run; the rest next run
SITEMAP_MAX_FILES = 12


def _sitemap_job_urls(page_url: str) -> list[str]:
    """Every job page a site lists in its sitemap, from robots.txt onward.

    JavaScript-built careers pages ship with an empty job list, but sites built
    for search engines list every job page in a sitemap so Google can find
    them. That file is plain XML and needs no browser.
    """
    root = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    maps = []
    robots = try_get(f"{root}/robots.txt", timeout=15)
    if robots:
        maps = [line.split(":", 1)[1].strip() for line in robots.text.splitlines()
                if line.lower().startswith("sitemap:")]
    if not maps:
        maps = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]

    found, read, queue = [], set(), list(maps)
    while queue and len(read) < SITEMAP_MAX_FILES:
        sm = queue.pop(0)
        if sm in read or sm.endswith(".gz"):
            continue
        read.add(sm)
        r = try_get(sm, timeout=20)
        if not r:
            continue
        locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", r.text)
        if "<sitemapindex" in r.text:
            # Read job-looking sitemaps first: sitemap-jobs.xml, jobs-sitemap.xml...
            queue.extend(sorted(locs, key=lambda u: 0 if re.search(r"job|career|position", u, re.I) else 1))
        else:
            found.extend(locs)

    site = _site(page_url)
    strong, weak = [], []
    for u in dict.fromkeys(found):
        if _site(u) != site:
            continue
        kind = _job_href(u)
        (strong if kind == "strong" else weak if kind == "weak" else []).append(u)
    # /careers/<x> pages only count in bulk, as with on-page listings.
    return strong + (weak if len(weak) >= 3 else [])


def _read_job_page(url: str) -> tuple[str, str, str]:
    """Title, location and posted date from one job's own page."""
    soup = BeautifulSoup(get(url, timeout=20).text, "lxml")

    for tag in soup.find_all(attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for j in _walk_jsonld(data):
            if j.get("title"):
                locs = j.get("jobLocation") or {}
                locs = locs[0] if isinstance(locs, list) and locs else locs
                addr = (locs.get("address") or {}) if isinstance(locs, dict) else {}
                return (str(j["title"]).strip(),
                        _loc(addr.get("addressLocality"), addr.get("addressRegion")),
                        (j.get("datePosted") or "")[:10])

    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif og and og.get("content"):
        title = og["content"]
    elif soup.title and soup.title.string:
        title = soup.title.string
    title = re.split(r"\s+[|\u2013\u2014]\s+|\s+-\s+(?=[A-Z][\w&. ]+(Careers|Jobs)\b)", title.strip())[0].strip()
    if not title or len(title) > 140:
        return "", "", ""

    location = ""
    strings = list(soup.stripped_strings)
    if title in strings:
        for text in strings[strings.index(title) + 1: strings.index(title) + 12]:
            if len(text) <= 60 and _LOCATION_HINT.search(text):
                location = text
                break
    return title, location, ""


def sitemap(slug: str = "", url: str = "", trusted: bool = False, **_) -> list[Role]:
    """Jobs found through the site's sitemap, each read from its own page.

    Pages already read on earlier runs are remembered, so after the first run
    only new postings are fetched.
    """
    if not url:
        raise FetchError("sitemap reading needs the careers URL")
    job_urls = _sitemap_job_urls(url)
    if not job_urls:
        raise FetchError("no sitemap listing job pages")

    cache_path = STATE / "job_pages.json"
    cache = _read(cache_path, {})
    today = date.today().isoformat()
    out, fetched, skipped = RoleList(), 0, 0
    for u in job_urls:
        entry = cache.get(u)
        if entry is None:
            if fetched >= SITEMAP_MAX_NEW_PAGES:
                skipped += 1
                continue
            fetched += 1
            try:
                title, location, posted = _read_job_page(u)
            except FetchError:
                continue
            entry = {"title": title, "location": location, "posted": posted}
        entry["seen"] = today
        cache[u] = entry
        if entry["title"]:
            out.append(Role(id=f"sm-{urlparse(u).path.rstrip('/')}", title=entry["title"],
                            url=u, location=entry.get("location", ""),
                            posted=entry.get("posted", "")))
    # Forget pages that dropped out of every sitemap more than 60 days ago.
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    _write(cache_path, {k: v for k, v in cache.items() if v.get("seen", today) >= cutoff})

    if skipped:
        out.truncated = f"read {fetched} new job pages, {skipped} more next run"
    if not out and job_urls:
        raise FetchError(f"sitemap lists {len(job_urls)} job pages but no titles could be read")
    if not trusted:
        _plausible_jobs([r.title for r in out], "through the sitemap")
    return out


ADAPTERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "breezy": breezy,
    "workday": workday, "ukg": ukg, "adp": adp, "bamboohr": bamboohr, "jsonld": jsonld,
    "onpage": onpage, "sitemap": sitemap, "dayforce": dayforce,
}

# Recognised but no adapter yet. Named in the coverage report so you know
# exactly which ones would need building, rather than seeing a vague failure.
KNOWN_UNSUPPORTED = {"icims", "paylocity", "jobvite", "jazzhr", "rippling", "paradox",
                     "taleo", "successfactors", "applicantpro", "indeed"}


# ==========================================================================
# RESOLVE
# ==========================================================================

"""Work out which ATS a company uses, and its identifiers.

Order of preference, cheapest and most certain first:

  1. Pinned override   opco_config/ats_overrides.csv — a human said so
  2. Cache             state/ats_cache.json — resolved on a previous run
  3. Careers page      read the page, find a link or embed into a known ATS
                       (one hop: if the page only has a "View openings" button,
                       follow it once and look again)
  4. Slug probing      try candidate slugs against keyless JSON APIs
  5. JSON-LD           schema.org JobPosting markup on the careers page

Step 3 is what makes this list work. REITs and property managers mostly run
Workday, UKG or ADP, whose URLs embed identifiers no slug guess can produce
(acme.wd5.myworkdayjobs.com/External_Careers). The careers page links to them.
"""


import csv
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup


log = logging.getLogger(__name__)

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

PROBEABLE = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters",
             "recruitee", "breezy", "bamboohr"]

UNRESOLVED_RETRY_DAYS = 28

_JOB_LINK = re.compile(r"(job|career|opening|position|opportunit|apply|join|hiring|work with us)", re.I)


@dataclass
class Resolution:
    ats: str = ""
    slug: str = ""
    params: dict = field(default_factory=dict)
    source: str = ""          # override / cache / careers-page / probe / jsonld
    note: str = ""
    resolved_on: str = ""
    verified: bool = True     # False = guessed; never published until pinned
    careers_check: str = ""   # what happened when the careers URL was fetched
    careers_url: str = ""     # the Column D value this result was based on
    version: int = 0
    fresh: bool = False       # detected this run (never True in the cache)

    @property
    def supported(self) -> bool:
        return self.ats in ADAPTERS

    def to_dict(self) -> dict:
        return asdict(self)


def load_overrides(raw: dict | None) -> dict[str, Resolution]:
    """`ats_overrides` block of opco_config.yml -> {company name: Resolution}."""
    out = {}
    for name, spec in (raw or {}).items():
        spec = spec or {}
        ats = str(spec.get("ats") or "").strip().lower()
        if not ats:
            continue
        params = {k: str(spec[k]) for k in ("host", "site", "board") if spec.get(k)}
        out[name.lower()] = Resolution(ats=ats, slug=str(spec.get("slug") or ""),
                                       params=params, source="override",
                                       note=str(spec.get("note") or ""))
    return out


def _match_url(url: str) -> Resolution | None:
    for ats, pattern in PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        groups = m.groupdict()
        slug = (groups.pop("slug", "") or "").strip("/")
        if slug.lower() in _BAD_SLUGS:
            continue
        if ats in ADAPTERS and ats not in ("jsonld", "onpage", "sitemap") and not slug:
            continue
        params = {k: v for k, v in groups.items() if v}
        if ats == "adp":
            try:
                cc = parse_qs(urlparse(url).query).get("ccId", [""])[0]
            except ValueError:
                cc = ""
            if cc:
                params["cc"] = cc
        return Resolution(ats=ats, slug=slug, params=params)
    return None


CACHE_VERSION = 2   # bump to force every company to be re-detected

# Hosts that belong to job systems, not to any one company.
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
             "smartrecruiters.com", "recruitee.com", "breezy.hr", "bamboohr.com",
             "myworkdayjobs.com", "myworkdaysite.com", "ultipro.com", "adp.com")

BOARD_URL = {
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
    "breezy": "https://{slug}.breezy.hr",
    "bamboohr": "https://{slug}.bamboohr.com/careers",
}


def _site(url_or_host: str) -> str:
    """Registrable domain, roughly: jobs.kiterealty.com -> kiterealty.com."""
    try:
        host = urlparse(url_or_host).netloc if "//" in url_or_host else url_or_host
    except ValueError:
        return ""
    host = host.lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_ats_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in ATS_HOSTS)


def _scan_html(html: str, base: str) -> tuple[Resolution | None, list[str]]:
    """Find an ATS reference in a page. Also return follow-up job links."""
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    for el in soup.find_all(True):
        for attr in ("href", "src", "action", "data-src", "data-url"):
            val = el.get(attr)
            if isinstance(val, str) and val:
                candidates.append(_join(base, val))
    # Embedded config blobs sometimes carry the board URL in inline script.
    candidates.extend(re.findall(r"https?://[^\s\"'<>]+", html))

    supported_hit, unsupported_hit = None, None
    for url in candidates:
        res = _match_url(url)
        if not res:
            continue
        if res.supported:
            supported_hit = supported_hit or res
        else:
            unsupported_hit = unsupported_hit or res
        if supported_hit:
            break

    follow = []
    for a in soup.find_all("a", href=True):
        href = _join(base, a["href"])
        text = a.get_text(" ", strip=True)
        if href == base or not _JOB_LINK.search(f"{text} {href}"):
            continue
        # Same site only (subdomains count: jobs.company.com). Following
        # off-site pages is how a company ended up with someone else's board.
        # Off-site links straight into a job system are already caught above.
        if _site(href) == _site(base):
            follow.append(href)

    return (supported_hit or unsupported_hit), follow[:4]


SITE_CODE_FILES = 6            # the company's own .js files read per careers page
SITE_CODE_MAX_CHARS = 2_000_000


def _scan_site_code(html: str, page_url: str) -> Resolution | None:
    """Look for a job-system address inside the company's own code files.

    When a careers page fills its job table with a script, the address of the
    job feed usually sits in one of the site's own .js files rather than in the
    page. Only files on the company's own site are read; analytics and other
    third-party code is skipped. Returns a supported match if one is found,
    else an unsupported one (worth naming), else None.
    """
    soup = BeautifulSoup(html, "lxml")
    files = []
    for el in soup.find_all(src=True):
        src = _join(page_url, el.get("src"))
        try:
            is_code = urlparse(src).path.lower().endswith(".js")
        except ValueError:
            continue
        if src and is_code and _site(src) == _site(page_url):
            files.append(src)
    unsupported = None
    for f in list(dict.fromkeys(files))[:SITE_CODE_FILES]:
        r = try_get(f, timeout=20)
        if not r or len(r.text or "") > SITE_CODE_MAX_CHARS:
            continue
        for u in re.findall(r"https?://[^\s\"'<>`)\\]+", r.text):
            hit = _match_url(u)
            if hit and hit.supported:
                return hit
            if hit and not unsupported:
                unsupported = hit
    return unsupported


def check_careers_url(url: str):
    """Fetch the careers URL and say plainly what happened.

    Returns (response or None, diagnosis). The diagnosis is empty when the
    careers page loaded as itself; otherwise it names the problem so the sheet
    can be fixed, rather than the run quietly reading some other page.
    """
    if not url:
        return None, "no careers URL in the sheet"
    asked = urlparse(url)
    careers_host = re.match(r"(careers?|jobs?|join|work|talent|hiring)\.", asked.netloc, re.I)
    if asked.path in ("", "/") and not careers_host:
        diag = "careers URL in the sheet is the homepage"
    else:
        diag = ""
    try:
        r = get(url, timeout=20)
    except FetchError as exc:
        return None, f"careers URL failed ({exc})"

    body = (r.text or "").strip()
    if not body:
        return r, "careers URL returned an empty page"
    if not re.search(r"<\s*(html|head|body|div|a|p|h[1-6]|section|main|ul|span|meta|title|link|"
                     r"style|form|table|img|nav|header|footer|article|!doctype)\b", body[:300000], re.I) \
            and "application/ld+json" not in body[:300000]:
        return r, "careers URL didn't return a web page (it returned a file or raw data)"

    landed = urlparse(r.url)
    if not diag and asked.path not in ("", "/") and landed.path in ("", "/") \
            and not re.match(r"(careers?|jobs?|join|work|talent|hiring)\.", landed.netloc, re.I):
        diag = "careers URL redirects to the homepage, so the page probably doesn't exist"
    elif not diag and _site(r.url) != _site(url) and not _is_ats_host(r.url):
        diag = f"careers URL now redirects to {landed.netloc} - update Column D to the new address"
    return r, diag


def from_careers_page(url: str) -> Resolution | None:
    """Find a job system that the company's own careers page points to.

    Anything found this way is trusted: the company itself is linking to it.
    """
    # If Column D already *is* a job-system link (a Workday, Greenhouse, UKG...
    # board), use it as-is. This is the most reliable value Column D can hold,
    # so it shouldn't depend on loading the page first.
    direct = _match_url(url) if url else None
    if direct:
        direct.source = "Column D (job system link)"
        if not direct.supported:
            direct.note = f"{direct.ats} detected; no adapter yet"
        return direct

    r, diag = check_careers_url(url)
    if not r:
        return Resolution(source="none", careers_check=diag, note=diag)

    def done(res, source):
        res.source, res.careers_check = source, diag
        if diag:
            res.note = diag
        return res

    # The careers URL itself may already be the ATS (redirects included).
    for candidate in (r.url, url):
        res = _match_url(candidate)
        if res and res.supported:
            return done(res, "careers-page")

    res, follow = _scan_html(r.text, r.url)
    if res and res.supported:
        return done(res, "careers-page")

    # The page itself names no job system: check the site's own code files,
    # where scripts that fill job tables keep the feed's address.
    in_code = _scan_site_code(r.text, r.url)
    if in_code and in_code.supported:
        return done(in_code, "careers-page (site code)")
    if in_code and not res:
        res = in_code

    # One hop, same site only: "View openings" buttons and /jobs subpages.
    for link in follow:
        hit = _match_url(link)
        if hit and hit.supported:
            return done(hit, "careers-page (link)")
        r2 = try_get(link, timeout=20)
        if not r2:
            continue
        hit2 = _match_url(r2.url)
        if hit2 and hit2.supported:
            return done(hit2, "careers-page (1 hop)")
        hit3, _ = _scan_html(r2.text, r2.url)
        if hit3 and hit3.supported:
            return done(hit3, "careers-page (1 hop)")
        if hit3 and not res:
            res = hit3

    if res:  # only an unsupported ATS was found, which is worth naming
        res.source, res.careers_check = "careers-page", diag
        res.note = f"{res.ats} detected; no adapter yet"
        return res

    return Resolution(source="none", careers_check=diag or "no-link", note=diag or
                      "careers page loaded but links to no job system "
                      "(it probably builds its job list with JavaScript)")


def candidate_slugs(name: str, website: str) -> list[str]:
    base = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|company|co|group|holdings?|trust|reit|the|"
        r"properties|property|management|services|communities|real estate|living)\b",
        " ", name.lower())
    words = re.sub(r"[^a-z0-9\s]", " ", base).split()
    out: list[str] = []

    def add(s):
        s = s.strip("-")
        if len(s) >= 3 and s not in out:
            out.append(s)

    if words:
        add("".join(words))
        add("-".join(words))
    if website:
        host = urlparse(website if "//" in website else f"//{website}").netloc.replace("www.", "")
        if host:
            add(host.split(".")[0])
    add(re.sub(r"[^a-z0-9]", "", name.lower()))
    return out[:4]


def by_probing(company) -> Resolution | None:
    """Guess a board from the company's name. Last resort, and never trusted.

    Common names collide: a guess of "atlas" finds an AI startup, "uniti" a
    London software company. So a guessed board is never published. It is
    reported with a link, and only goes live once someone pins it in
    opco_config.yml.

    One mismatch is caught automatically: if the guessed board's postings live
    on a company website, and it isn't this company's website, it's someone
    else (that's how "evernest" found Evernest GmbH in Hamburg).
    """
    own_sites = {_site(u) for u in (company.website, company.careers_url) if u}
    for slug in candidate_slugs(company.name, company.website or company.careers_url):
        for ats in PROBEABLE:
            try:
                roles = ADAPTERS[ats](slug)
            except Exception:  # noqa: BLE001 - a probe miss is expected
                continue
            if not roles:
                continue
            foreign = {_site(r.url) for r in roles
                       if r.url and not _is_ats_host(r.url)} - own_sites
            if own_sites and foreign:
                log.info("  %s: guessed %s/%s rejected - postings live on %s",
                         company.name, ats, slug, ", ".join(sorted(foreign)))
                continue
            return Resolution(ats=ats, slug=slug, source="guess", verified=False,
                              note="guessed from the company name - confirm before publishing")
    return None


def resolve(company, overrides: dict, cache: dict, *, refresh: bool = False) -> Resolution:
    """Most trustworthy source first: pinned, the company's own careers page,
    JSON-LD on that page, and only then a guess (which is never published)."""
    key = company.name.lower()
    today = date.today().isoformat()

    if key in overrides:
        return overrides[key]

    cached = cache.get(key)
    # A changed Column D always means a fresh look, never a remembered answer.
    if cached and cached.get("careers_url", "") != (company.careers_url or ""):
        if cached.get("careers_url"):
            log.info("  %s: careers URL changed in the sheet, re-checking", company.name)
        cached = None
    if cached and not refresh and cached.get("version", 0) == CACHE_VERSION:
        res = Resolution(**cached)
        if res.ats:
            return res
        retry_after = (date.fromisoformat(res.resolved_on or today)
                       + timedelta(days=UNRESOLVED_RETRY_DAYS)).isoformat()
        if today < retry_after:
            return res

    res = from_careers_page(company.careers_url)

    # The company's own site, read three ways. These also run when the page
    # named a job system we can't read yet (Dayforce, iCIMS...): if the jobs
    # are readable on the company's own pages, that beats "unsupported".
    refused = ""
    blocked_by = res.ats if res.ats and not res.supported else ""
    via = f" (applications go through {blocked_by})" if blocked_by else ""
    for ats, label, describe in (
        ("jsonld", "jsonld", lambda n: "schema.org markup on careers page"),
        ("onpage", "careers-page (job list)", lambda n: f"jobs listed directly on the careers page ({n} found)"),
        ("sitemap", "careers-site sitemap", lambda n: f"job pages found through the site's sitemap ({n} read)"),
    ):
        if res.supported or not company.careers_url:
            break
        try:
            listed = ADAPTERS[ats](url=company.careers_url)
        except FetchError as exc:
            msg = str(exc)
            if msg.startswith(("found ", "sitemap lists")):
                refused = msg  # the page was read, but what it held wasn't trustworthy
            continue
        except Exception:  # noqa: BLE001
            continue
        res = Resolution(ats=ats, source=label, params={"url": company.careers_url},
                         careers_check=res.careers_check,
                         note=(res.careers_check or describe(len(listed))) + via)

    if refused and not res.ats:
        res.careers_check, res.note = "unreadable", refused

    # Guess only when the careers page found nothing at all. If it found an
    # unsupported system (Dayforce, iCIMS...), that's the real answer.
    if not res.ats:
        guess = by_probing(company)
        if guess:
            guess.careers_check = res.careers_check
            guess.note = (res.note + " | " if res.note else "") + guess.note
            res = guess

    res.resolved_on = today
    res.version = CACHE_VERSION
    res.careers_url = company.careers_url or ""
    cache[key] = res.to_dict()   # stored with fresh=False
    res.fresh = True
    return res


# ==========================================================================
# CORE
# ==========================================================================

"""Companies, the role filter, and run-to-run state.

STATE AND THE SEPT 30 LESSON
----------------------------
A company that fails to scrape must never look like a company that closed all
its roles. That error corrupts the job board and produces false "recently
hired" signals. So:

  - every company's result is checkpointed as soon as it lands, and a re-run
    on the same day skips companies already done
  - a failed company carries forward last good roles, marked stale, and is
    excluded from the closed-role diff
  - a company that drops from 5+ roles to zero in one week is treated as a
    probable scrape failure, not a hiring freeze, and carried forward too
"""


import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
SUSPICIOUS_DROP = 5


# ----------------------------------------------------------------- companies

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


LIVE_COPY = STATE / "opco_live.csv"


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


def find_companies_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in (ROOT.parent / "opco.csv", ROOT.parent / "data" / "opco.csv",
                      ROOT / "opco.csv", Path("opco.csv")):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "opco.csv not found. Expected it at the repo root or data/opco.csv.")


# ----------------------------------------------------------------- role filter

class RoleFilter:
    """Title-keyword filter, configured in opco_config/roles.yml."""

    def __init__(self, cfg: dict):
        def rx(words):
            return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None

        self.include = {k: rx(v) for k, v in (cfg.get("include") or {}).items()}
        self.exclude = rx(cfg.get("exclude") or [])
        self.overrides = {}
        self.search_terms = {}
        for name, rule in (cfg.get("company_overrides") or {}).items():
            rule = rule or {}
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


# ----------------------------------------------------------------- state

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


class RunState:
    def __init__(self, run_date: str | None = None):
        self.run_date = run_date or date.today().isoformat()
        self.cache = _read(STATE / "ats_cache.json", {})
        self.previous = _read(STATE / "last_good.json", {})   # company -> snapshot
        self.checkpoint_path = STATE / f"checkpoint-{self.run_date}.json"
        self.checkpoint = _read(self.checkpoint_path, {})

    def done(self, company: str) -> bool:
        return company in self.checkpoint

    def record(self, company: str, result: dict) -> None:
        """Checkpoint one company immediately, so a crash loses nothing."""
        self.checkpoint[company] = result
        _write(self.checkpoint_path, self.checkpoint)

    def carry_forward(self, company: str, reason: str, board: str = "") -> dict:
        prev = self.previous.get(company)
        # Never carry forward roles that came from a different board.
        if prev and board and prev.get("board") and prev["board"] != board:
            prev = None
        if not prev:
            return {"status": "failed", "reason": reason, "roles": [], "stale_since": None}
        return {"status": "stale", "reason": reason, "roles": prev["roles"],
                "stale_since": prev.get("fetched_on")}

    def is_suspicious_drop(self, company: str, new_count: int) -> bool:
        prev = self.previous.get(company)
        return bool(prev) and new_count == 0 and len(prev.get("roles", [])) >= SUSPICIOUS_DROP

    def save(self) -> None:
        _write(STATE / "ats_cache.json", self.cache)
        # Only fresh results become next week's baseline. Stale ones keep the
        # older snapshot they were carried from.
        for company, result in self.checkpoint.items():
            status = result.get("status")
            if status == "ok":
                self.previous[company] = {"fetched_on": self.run_date,
                                          "roles": result["roles"],
                                          "board": f"{result.get('ats')}:{result.get('slug')}"}
            elif status in ("unconfirmed", "unresolved", "unsupported"):
                # No trustworthy board this week, so whatever was saved before
                # can't serve as a baseline (it may be another company's jobs).
                self.previous.pop(company, None)
        _write(STATE / "last_good.json", self.previous)
        # Prune checkpoints older than this run.
        for old in STATE.glob("checkpoint-*.json"):
            if old.name != self.checkpoint_path.name:
                old.unlink(missing_ok=True)


# ==========================================================================
# REPORT
# ==========================================================================

"""Weekly brief: new roles, closed roles, all open roles, and coverage.

Three outputs per run:
  out/opco-jobs-<date>.md     the brief, with a direct link on every role
  out/opco-jobs-latest.json   machine-readable; this is the file the
                              people-moves `reqs` source can read
  out/opco-jobs-<date>.csv    flat table, for pasting into a sheet or Airtable
"""


import csv
import json
from pathlib import Path

CATEGORY_LABEL = {"executive": "Executive", "sales": "Sales", "gtm": "GTM",
                  "engineering": "Engineering", "operators": "Operators"}


def _line(r: dict) -> str:
    bits = [f"[{r['title']}]({r['url']})" if r.get("url") else r["title"]]
    if r.get("location"):
        bits.append(r["location"])
    return " — ".join(bits)


def _attention_section(results: dict) -> list[str]:
    """Everything a human needs to act on, grouped by what to do about it."""
    guessed = {n: c for n, c in results.items() if c.get("status") == "unconfirmed"}
    bad_url = {n: c for n, c in results.items()
               if c.get("careers_check") and c["careers_check"] not in ("no-link", "unreadable")}
    no_link = {n: c for n, c in results.items()
               if c.get("status") == "unresolved" and c.get("careers_check") == "no-link"}
    no_adapter = {n: c for n, c in results.items() if c.get("status") == "unsupported"}
    cut = {n: c for n, c in results.items() if str(c.get("note", "")).startswith("cut short")}
    refused = {n: c for n, c in results.items()
               if c.get("status") == "unresolved" and c.get("careers_check") == "unreadable"}
    spot = {n: c for n, c in results.items() if c.get("spot_check")}
    failed = {n: c for n, c in results.items() if c.get("status") == "failed"}

    if not any((guessed, bad_url, no_link, no_adapter, cut, refused, spot, failed)):
        return []

    md = ["## Needs your attention", ""]
    if guessed:
        md += [f"### Confirm these job boards ({len(guessed)})", "",
               "*Guessed from the company name, so their roles are held back from this "
               "brief. Open each link and check it's the right company. Wrong ones need "
               "nothing; they stay held back.*", ""]
        for name, c in sorted(guessed.items()):
            md.append(f"- **{name}** — [{c['ats']} board: {c['slug']}]({c['board_url']}) "
                      f"· {c.get('total', 0)} roles, e.g. {'; '.join(c.get('samples', [])[:3])}")
        # No `ats_overrides:` header (pasting a second one would wipe the
        # existing pins) and every entry commented out, so pasting the whole
        # block changes nothing until someone deliberately confirms an entry.
        md += ["", "*To confirm one: paste its lines under the existing `ats_overrides:` line "
               "in opco_config.yml and delete the `#` at the start of each.*", "", "```yaml"]
        for name, c in sorted(guessed.items()):
            md += [f"  # {name}:", f"  #   ats: {c['ats']}", f"  #   slug: {c['slug']}"]
        md += ["```", ""]
    if bad_url:
        md += [f"### Fix the careers URL in the sheet ({len(bad_url)})", ""]
        for name, c in sorted(bad_url.items()):
            url = c.get("careers_url") or ""
            md.append(f"- **{name}** — {c['careers_check']}" + (f" · `{url}`" if url else ""))
        md.append("")
    if no_link:
        md += [f"### Careers page found, but no job system on it ({len(no_link)})", "",
               "*The page loaded but contains no link to a job system — usually because it "
               "builds the job list with JavaScript. Check the page: if jobs show, note "
               "which system the Apply button goes to and pin it in opco_config.yml.*", ""]
        for name, c in sorted(no_link.items()):
            md.append(f"- **{name}** · {c.get('careers_url', '')}")
        md.append("")
    if failed:
        md += [f"### Failed this week ({len(failed)})", "",
               "*Something went wrong reading these. Last week's roles are carried forward "
               "where there were any, and they're left out of new/closed so a failure never "
               "reads as a hire.*", ""]
        for name, c in sorted(failed.items()):
            md.append(f"- **{name}** — {c.get('reason') or 'unknown error'}")
        md.append("")
    if refused:
        md += [f"### Read the page, but didn't trust what it found ({len(refused)})", "",
               "*The scraper found something on these pages but it didn't look like job "
               "listings, so nothing was published. If the page really does list jobs, pin it "
               "in opco_config.yml (`ats: onpage` or `ats: sitemap`) to override.*", ""]
        for name, c in sorted(refused.items()):
            md.append(f"- **{name}** — {c.get('note', '')}")
        md.append("")
    if spot:
        md += [f"### Read for the first time from page layout — spot-check once ({len(spot)})", "",
               "*These have no job system, so their jobs were read from how the page is laid "
               "out. They're published, but glance at the titles once to confirm they're real "
               "jobs. They won't be listed here again unless the careers URL changes.*", ""]
        for name, c in sorted(spot.items()):
            md.append(f"- **{name}** — {'; '.join(c['spot_check'][:4])}")
        md.append("")
    if no_adapter:
        md += [f"### Job system found, but not supported yet ({len(no_adapter)})", ""]
        for name, c in sorted(no_adapter.items()):
            md.append(f"- **{name}** — {c.get('ats')}")
        md.append("")
    if cut:
        md += [f"### Results cut short ({len(cut)})", ""]
        for name, c in sorted(cut.items()):
            md.append(f"- **{name}** — {c['note']}")
        md.append("")
    return md


def write_report(results: dict, diff: dict, run_date: str, first_run: bool, out_dir: Path,
                 source: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    open_roles = [r for c in results.values() for r in c.get("filtered", [])]
    companies_with_roles = sorted({r["company"] for r in open_roles})

    md = [f"# OpCo job search — week of {run_date}", ""]
    md.append(f"{len(open_roles)} matching roles open across "
              f"{len(companies_with_roles)} of {len(results)} companies.")
    if source:
        md += ["", f"Company list and careers URLs read from: **{source}**."]
    md += ["", "> Open each link before it goes in the newsletter. Postings move.", ""]
    md += _attention_section(results)

    if first_run:
        md += ["*First run: this is the baseline. New-this-week and recently-closed "
               "sections start next week.*", ""]
    else:
        new = diff["new"]
        md += [f"## New this week ({len(new)})", ""]
        if new:
            for r in sorted(new, key=lambda x: (x["company"], x["title"])):
                md.append(f"- **{r['company']}** · {CATEGORY_LABEL.get(r['category'], r['category'])} · {_line(r)}")
        else:
            md.append("No new matching roles.")
        md.append("")

        closed = diff["closed"]
        md += [f"## Recently closed ({len(closed)})", "",
               "*Roles that were open last week and are gone now — the \"recently "
               "hired\" signal. Closed usually means filled, but can mean pulled. "
               "Companies that failed to scrape this week are excluded so a broken "
               "fetch never reads as a hire.*", ""]
        if closed:
            for r in sorted(closed, key=lambda x: (x["company"], x["title"])):
                md.append(f"- **{r['company']}** · {r['title']}"
                          + (f" — {r['location']}" if r.get("location") else ""))
        else:
            md.append("No matching roles closed.")
        md.append("")

    md += ["## All open roles", ""]
    for company in companies_with_roles:
        roles = [r for r in open_roles if r["company"] == company]
        stale = results[company].get("status") == "stale"
        md.append(f"### {company} ({len(roles)})"
                  + (f" — *carried forward from {results[company].get('stale_since')}*" if stale else ""))

        # Large operators post one title per region (Greystar: "Regional Vice
        # President" x4). Collapse those to one line so the brief stays
        # readable; the JSON and CSV keep every posting.
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
                md.append(f"- {label} · [{title}]({group[0]['url']}) "
                          f"**({len(group)} openings)**{where}")
        md.append("")

    # Coverage: the part that tells you what to fix.
    md += ["## Coverage", "",
           "*Every company, the exact careers URL checked (from Column D unless noted), "
           "and what happened.*", "",
           "| Company | Careers URL checked | ATS | Status | Roles (all / matching) | Note |",
           "|---|---|---|---|---|---|"]
    order = {"ok": 0, "stale": 1, "unconfirmed": 2, "unsupported": 3, "failed": 4, "unresolved": 5}
    for name, c in sorted(results.items(), key=lambda kv: (order.get(kv[1]["status"], 9), kv[0])):
        url = c.get("careers_url") or ""
        checked = f"[{url.split('//')[-1][:45]}]({url})" if url else "— (Column D empty)"
        if url and c.get("careers_from", "").startswith("Column B"):
            checked += " *(from Column B)*"
        md.append(f"| {name} | {checked} | {c.get('ats') or '—'} | {c['status']} | "
                  f"{c.get('total', 0)} / {len(c.get('filtered', []))} | "
                  f"{(c.get('reason') or c.get('note') or '').replace('|', '/')} |")
    md.append("")

    counts = {}
    for c in results.values():
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    md.append("**Status:** " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    md.append("")

    text = "\n".join(md)
    dated = out_dir / f"opco-jobs-{run_date}.md"
    dated.write_text(text, encoding="utf-8")
    (out_dir / "opco-jobs-latest.md").write_text(text, encoding="utf-8")

    (out_dir / "opco-jobs-latest.json").write_text(json.dumps({
        "run_date": run_date,
        "roles": open_roles,
        "new": diff["new"],
        "closed": diff["closed"],
        "coverage": {k: {kk: v for kk, v in c.items() if kk not in ("roles", "filtered")}
                     for k, c in results.items()},
    }, indent=2), encoding="utf-8")

    with (out_dir / f"opco-jobs-{run_date}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "category", "title", "location",
                                           "url", "posted", "status"])
        w.writeheader()
        for r in open_roles:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    return dated


# ==========================================================================
# MAIN
# ==========================================================================

log = logging.getLogger("opco")
CONFIG_PATH = ROOT / "opco_config.yml"


LAYOUT_SOURCES = ("careers-page (job list)", "careers-site sitemap", "jsonld")


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


def fetch_company(company, res, role_filter, state) -> dict:
    base = {"ats": res.ats, "slug": res.slug, "source": res.source, "note": res.note,
            "careers_url": company.careers_url, "careers_from": company.careers_from,
            "careers_check": res.careers_check,
            "verified": res.verified}

    if not res.ats:
        return {**base, "status": "unresolved", "reason": res.note, "roles": [], "filtered": []}
    if not res.supported:
        return {**base, "status": "unsupported", "reason": f"{res.ats} has no adapter yet",
                "roles": [], "filtered": []}

    try:
        params = dict(res.params)
        if res.ats in ("jsonld", "onpage", "sitemap"):
            params.setdefault("url", company.careers_url)
        if res.source == "override" and res.ats in ("onpage", "sitemap"):
            params["trusted"] = True  # a person confirmed this page reads correctly
        if res.ats == "workday" and role_filter.search_terms.get(company.name.lower()):
            params["search_terms"] = role_filter.search_terms[company.name.lower()]
        fetched = ADAPTERS[res.ats](res.slug, **params)
        truncated = getattr(fetched, "truncated", "")
        if truncated:
            log.warning("  %s: results cut short (%s) - add search_terms in opco_config.yml",
                        company.name, truncated)
            base["note"] = f"cut short: {truncated}; add search_terms in opco_config.yml"
        roles = [r.to_dict() for r in fetched if r.title]  # untitled records aren't jobs
    except Exception as exc:  # noqa: BLE001 — one company must never sink the run
        reason = plain_error(exc)
        log.warning("  %s: fetch failed (%s)", company.name, reason)
        # Before giving up, try reading the company's own careers pages. Only
        # when Column D is the company's site (not the job board itself), and
        # never for a guess.
        fallback = None
        if (res.verified and res.ats not in ("onpage", "sitemap", "jsonld")
                and company.careers_url and not _is_ats_host(company.careers_url)):
            for alt in ("onpage", "sitemap"):
                try:
                    alt_roles = [r.to_dict() for r in ADAPTERS[alt](url=company.careers_url) if r.title]
                except Exception:  # noqa: BLE001
                    continue
                if alt_roles:
                    fallback = (alt, alt_roles)
                    break
        if fallback:
            log.info("  %s: read %d roles from its own pages instead", company.name, len(fallback[1]))
            base["ats"] = fallback[0]
            base["note"] = (f"{res.ats} couldn't be read this week ({reason}); "
                            f"jobs read from the company's own pages instead")
            result = {**base, "status": "ok", "roles": fallback[1]}
        else:
            result = {**base, **state.carry_forward(company.name, reason, f"{res.ats}:{res.slug}")}
    else:
        if state.is_suspicious_drop(company.name, len(roles)):
            reason = "dropped to 0 roles from 5+ last week; treated as a scrape failure"
            log.warning("  %s: %s", company.name, reason)
            result = {**base, **state.carry_forward(company.name, reason, f"{res.ats}:{res.slug}")}
        else:
            result = {**base, "status": "ok", "roles": roles}

    # A guessed board is never published. Keep a few titles as evidence so the
    # brief can show what was found, and let a human pin it if it's right.
    if not res.verified and result["status"] == "ok":
        board = BOARD_URL.get(res.ats, "").format(slug=res.slug)
        return {**result, "status": "unconfirmed", "board_url": board,
                "samples": [f"{r['title']} ({r['location']})" if r.get("location") else r["title"]
                            for r in result["roles"][:4]],
                "filtered": [], "total": len(result["roles"]), "roles": []}

    # The first time a company is read from page layout (not a job system),
    # list it once for a human spot-check. Published normally either way.
    if result["status"] == "ok" and res.source in LAYOUT_SOURCES and res.fresh:
        result["spot_check"] = [r["title"] for r in result["roles"][:5]]

    filtered = []
    for r in result["roles"]:
        category = role_filter.categorize(r["title"], company.name)
        if category:
            filtered.append({**r, "company": company.name, "category": category,
                             "status": result["status"]})
    result["filtered"] = filtered
    result["total"] = len(result["roles"])
    return result


def compute_diff(results: dict, state: RunState, role_filter) -> dict:
    """New and closed matching roles, for companies scraped cleanly both weeks."""
    new, closed = [], []
    for company, c in results.items():
        if c["status"] != "ok":
            continue  # stale/failed companies are excluded from the diff
        prev = state.previous.get(company)
        if not prev:
            continue
        # Board changed since last week (e.g. a guess was replaced by a pinned
        # board): this week is a fresh baseline, not a wave of closures.
        if prev.get("board") and prev["board"] != f"{c.get('ats')}:{c.get('slug')}":
            continue
        prev_ids = {r["id"] for r in prev["roles"]}
        now_ids = {r["id"] for r in c["roles"]}
        new.extend(r for r in c["filtered"] if r["id"] not in prev_ids)
        for r in prev["roles"]:
            if r["id"] in now_ids:
                continue
            category = role_filter.categorize(r["title"], company)
            if category:
                closed.append({**r, "company": company, "category": category})
    return {"new": new, "closed": closed}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--companies", help="path to opco.csv (default: auto-find)")
    p.add_argument("--only", help="comma-separated company names")
    p.add_argument("--discover", action="store_true", help="resolve ATS only")
    p.add_argument("--refresh-ats", action="store_true", help="ignore the ATS cache")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--out", default=str(ROOT / "out"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("config not found: %s", config_path)
        return 2
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # Where the company list (and Column D) comes from, most current first.
    sheet_url = str(config.get("sheet_csv_url") or "").strip()
    try:
        if args.companies:
            companies, source = load_companies(Path(args.companies)), f"file {args.companies}"
        elif sheet_url:
            try:
                companies, source = load_live_companies(sheet_url)
            except (FetchError, ValueError) as exc:
                path = find_companies_file(None)
                companies = load_companies(path)
                source = f"repo copy {path.name} - live sheet unavailable: {exc}"
        else:
            path = find_companies_file(None)
            companies, source = load_companies(path), (
                f"repo copy {path.name} (set sheet_csv_url in opco_config.yml to read "
                "the live sheet instead)")
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    log.info("company list: %s", source)

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        companies = [c for c in companies if c.name.lower() in wanted]

    role_filter = RoleFilter(config)
    overrides = load_overrides(config.get("ats_overrides"))
    state = RunState()
    first_run = not state.previous

    results: dict = {}
    for i, company in enumerate(companies, 1):
        if state.done(company.name) and not args.discover:
            results[company.name] = state.checkpoint[company.name]
            log.info("[%2d/%d] %s - already done today, skipping", i, len(companies), company.name)
            continue

        try:
            res = resolve(company, overrides, state.cache, refresh=args.refresh_ats)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            log.error("  %s: detection crashed\n%s", company.name, traceback.format_exc())
            result = {"ats": "", "slug": "", "source": "", "status": "failed",
                      "reason": f"couldn't inspect this careers page - {plain_error(exc)}",
                      "careers_url": company.careers_url, "careers_from": company.careers_from,
                      "careers_check": "", "verified": True, "roles": [], "filtered": [], "total": 0}
            results[company.name] = result
            if not args.discover:
                state.record(company.name, result)
            continue

        log.info("[%2d/%d] %-34s %-15s %s", i, len(companies), company.name[:34],
                 res.ats or "-", res.source)
        if args.discover:
            results[company.name] = {"ats": res.ats, "status": "discover",
                                     "note": res.note, "roles": [], "filtered": []}
            continue

        try:
            result = fetch_company(company, res, role_filter, state)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            log.error("  %s: processing crashed\n%s", company.name, traceback.format_exc())
            result = {"ats": res.ats, "slug": res.slug, "source": res.source,
                      **state.carry_forward(company.name, plain_error(exc), f"{res.ats}:{res.slug}"),
                      "careers_url": company.careers_url, "careers_check": res.careers_check,
                      "verified": res.verified, "filtered": [], "total": 0}
        results[company.name] = result
        state.record(company.name, result)

    if args.discover:
        state.save()
        resolved = sum(1 for r in results.values() if r["ats"])
        log.info("")
        log.info("Resolved %d of %d. Cached for next run.", resolved, len(results))
        for name, r in results.items():
            if not r["ats"] or r["ats"] not in ADAPTERS:
                log.info("  needs attention: %-34s %s", name, r.get("note") or r.get("ats"))
        return 0

    diff = compute_diff(results, state, role_filter) if not first_run else {"new": [], "closed": []}
    state.save()
    try:
        path = write_report(results, diff, state.run_date, first_run, Path(args.out), source)
    except Exception:  # noqa: BLE001 - always leave something readable
        log.error("report failed, writing a plain fallback\n%s", traceback.format_exc())
        out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"opco-jobs-{state.run_date}.md"
        lines = [f"# OpCo job search - {state.run_date} (plain fallback: the full report failed)", ""]
        for name, c in sorted(results.items()):
            lines.append(f"## {name} - {c.get('status')} - {c.get('reason') or c.get('note') or ''}")
            for r in c.get("filtered", []):
                lines.append(f"- {r.get('title')} - {r.get('location')} - {r.get('url')}")
        path.write_text("\n".join(lines), encoding="utf-8")

    ok = sum(1 for r in results.values() if r["status"] == "ok")
    log.info("")
    log.info("%d/%d companies scraped cleanly -> %s", ok, len(results), path)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(path.read_text(encoding="utf-8"))

    return 0 if ok or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
