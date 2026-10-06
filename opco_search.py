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
             timeout: int = 25, retries: int = 3) -> requests.Response:
    headers = {}
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


import json
import logging
import re
from dataclasses import dataclass, asdict, field

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

    def to_dict(self) -> dict:
        return asdict(self)


def _loc(*parts) -> str:
    return ", ".join(p for p in parts if p and str(p).strip())


# ---------------------------------------------------------------- high confidence

def greenhouse(slug: str, **_) -> list[Role]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [
        Role(id=f"gh-{j['id']}", title=j.get("title", ""), url=j.get("absolute_url", ""),
             location=(j.get("location") or {}).get("name", ""),
             posted=(j.get("updated_at") or "")[:10])
        for j in d.get("jobs", [])
    ]


def lever(slug: str, **_) -> list[Role]:
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
    if not isinstance(d, list):
        raise FetchError("unexpected Lever payload")
    out = []
    for j in d:
        cats = j.get("categories") or {}
        out.append(Role(id=f"lv-{j['id']}", title=j.get("text", ""), url=j.get("hostedUrl", ""),
                        location=cats.get("location", ""), department=cats.get("team", "")))
    return out


def ashby(slug: str, **_) -> list[Role]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    return [
        Role(id=f"ab-{j['id']}", title=j.get("title", ""), url=j.get("jobUrl", ""),
             location=j.get("location", ""), department=j.get("department", ""),
             posted=(j.get("publishedAt") or "")[:10])
        for j in d.get("jobs", []) if j.get("isListed", True)
    ]


def workable(slug: str, **_) -> list[Role]:
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}").json()
    return [
        Role(id=f"wk-{j.get('shortcode')}", title=j.get("title", ""),
             url=j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
             location=_loc(j.get("city"), j.get("state")), department=j.get("department", ""),
             posted=(j.get("published_on") or "")[:10])
        for j in d.get("jobs", [])
    ]


def smartrecruiters(slug: str, **_) -> list[Role]:
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                f"?limit=100&offset={offset}").json()
        content = d.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            out.append(Role(id=f"sr-{j['id']}", title=j.get("name", ""),
                            url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
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
        Role(id=f"rt-{j['id']}", title=j.get("title", ""), url=j.get("careers_url", ""),
             location=j.get("location", ""), department=j.get("department", ""),
             posted=(j.get("published_at") or "")[:10])
        for j in d.get("offers", [])
    ]


def breezy(slug: str, **_) -> list[Role]:
    d = get(f"https://{slug}.breezy.hr/json").json()
    if not isinstance(d, list):
        raise FetchError("unexpected Breezy payload")
    return [
        Role(id=f"bz-{j['id']}", title=j.get("name", ""), url=j.get("url", ""),
             location=(j.get("location") or {}).get("name", ""),
             department=j.get("department", ""), posted=(j.get("published_date") or "")[:10])
        for j in d
    ]


def workday(slug: str, host: str = "", site: str = "", **_) -> list[Role]:
    """Workday's public CXS API. `slug` is the tenant.

    Workday caps pages at 20 and only reports `total` reliably on the first
    page, so the first page's total drives pagination.
    """
    if not host or not site:
        raise FetchError("Workday needs host and site (resolve from careers page)")
    endpoint = f"https://{host}/wday/cxs/{slug}/{site}/jobs"
    out, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        d = post_json(endpoint, {"appliedFacets": {}, "limit": 20,
                                 "offset": offset, "searchText": ""}).json()
        if total is None:
            total = int(d.get("total") or 0)
        postings = d.get("jobPostings", [])
        for j in postings:
            path = j.get("externalPath", "")
            ref = (j.get("bulletFields") or [path])[0]
            out.append(Role(id=f"wd-{slug}-{ref}", title=j.get("title", ""),
                            url=f"https://{host}/{site}{path}",
                            location=j.get("locationsText", ""),
                            posted=j.get("postedOn", "")))
        offset += len(postings)
        if not postings or offset >= total:
            break
    return out


# ---------------------------------------------------------------- moderate confidence

def ukg(slug: str, board: str = "", host: str = "recruiting.ultipro.com", **_) -> list[Role]:
    """UKG / UltiPro. `slug` is the tenant code, `board` the job-board GUID."""
    if not board:
        raise FetchError("UKG needs the job-board GUID (resolve from careers page)")
    base = f"https://{host}/{slug}/JobBoard/{board}"
    out, skip = [], 0
    for _ in range(MAX_PAGES):
        body = {"opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": "",
                                      "OrderBy": [{"Value": "postedDateDesc",
                                                   "PropertyName": "PostedDate",
                                                   "Ascending": False}],
                                      "Filters": []},
                "matchCriteria": {"PreferredJobs": [], "Educations": [],
                                  "LicenseAndCertifications": [], "Skills": [],
                                  "hasNoLicenses": False, "SkippedSkills": []}}
        d = post_json(f"{base}/JobBoardView/LoadSearchResults", body).json()
        opps = d.get("opportunities", [])
        for j in opps:
            locs = j.get("Locations") or [{}]
            addr = (locs[0] or {}).get("Address") or {}
            city = addr.get("City") or ""
            state = (addr.get("State") or {}).get("Code", "") if isinstance(addr.get("State"), dict) else ""
            out.append(Role(id=f"uk-{j['Id']}", title=j.get("Title", ""),
                            url=f"{base}/OpportunityDetail?opportunityId={j['Id']}",
                            location=_loc(city, state),
                            posted=(j.get("PostedDate") or "")[:10]))
        skip += len(opps)
        if not opps or skip >= int(d.get("totalCount") or 0):
            break
    return out


def adp(slug: str, **_) -> list[Role]:
    """ADP Workforce Now. `slug` is the client id (cid) from the careers URL."""
    url = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
           f"staffing/v1/job-requisitions?cid={slug}&lang=en_US&iccFlag=yes&eccFlag=yes")
    d = get(url, accept="application/json").json()
    out = []
    for j in d.get("jobRequisitions", []):
        locs = j.get("requisitionLocations") or [{}]
        addr = ((locs[0] or {}).get("address") or {})
        out.append(Role(
            id=f"adp-{j.get('itemID')}", title=j.get("requisitionTitle", ""),
            url=("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                 f"recruitment.html?cid={slug}&jobId={j.get('itemID')}"),
            location=_loc(addr.get("cityName"),
                          (addr.get("countrySubdivisionLevel1") or {}).get("codeValue")),
            posted=(j.get("postDate") or "")[:10]))
    return out


def bamboohr(slug: str, **_) -> list[Role]:
    d = get(f"https://{slug}.bamboohr.com/careers/list", accept="application/json").json()
    return [
        Role(id=f"bh-{j['id']}", title=j.get("jobOpeningName", ""),
             url=f"https://{slug}.bamboohr.com/careers/{j['id']}",
             location=_loc((j.get("location") or {}).get("city"),
                           (j.get("location") or {}).get("state")),
             department=j.get("departmentLabel", ""))
        for j in d.get("result", [])
    ]


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


ADAPTERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "breezy": breezy,
    "workday": workday, "ukg": ukg, "adp": adp, "bamboohr": bamboohr, "jsonld": jsonld,
}

# Recognised but no adapter yet. Named in the coverage report so you know
# exactly which ones would need building, rather than seeing a vague failure.
KNOWN_UNSUPPORTED = {"icims", "paylocity", "jobvite", "jazzhr", "rippling", "paradox",
                     "taleo", "successfactors", "dayforce", "applicantpro", "indeed"}


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
    ("dayforce", re.compile(r"dayforcehcm\.com", re.I)),
    ("applicantpro", re.compile(r"applicantpro\.com", re.I)),
]

# Slugs that are ATS infrastructure words, never a real company board.
_BAD_SLUGS = {"embed", "js", "v1", "jobs", "api", "www", "careers", "job_board",
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
        params = {k: v for k, v in groups.items() if v}
        return Resolution(ats=ats, slug=slug, params=params)
    return None


def _scan_html(html: str, base: str) -> tuple[Resolution | None, list[str]]:
    """Find an ATS reference in a page. Also return follow-up job links."""
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    for el in soup.find_all(True):
        for attr in ("href", "src", "action", "data-src", "data-url"):
            val = el.get(attr)
            if isinstance(val, str) and val:
                candidates.append(urljoin(base, val))
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
    base_host = urlparse(base).netloc
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        text = a.get_text(" ", strip=True)
        if href == base or not _JOB_LINK.search(f"{text} {href}"):
            continue
        # Same-site job pages, or any off-site link that reads like openings.
        if urlparse(href).netloc == base_host or re.search(r"opening|position|view.*job|see.*job", text, re.I):
            follow.append(href)

    return (supported_hit or unsupported_hit), follow[:4]


def from_careers_page(url: str) -> Resolution | None:
    if not url:
        return None
    r = try_get(url, timeout=20)
    if not r:
        return None

    # The careers URL itself may already be the ATS (redirects included).
    for candidate in (r.url, url):
        res = _match_url(candidate)
        if res and res.supported:
            res.source = "careers-page"
            return res

    res, follow = _scan_html(r.text, r.url)
    if res and res.supported:
        res.source = "careers-page"
        return res

    # One hop: "View openings" buttons.
    for link in follow:
        hit = _match_url(link)
        if hit and hit.supported:
            hit.source = "careers-page (link)"
            return hit
        r2 = try_get(link, timeout=20)
        if not r2:
            continue
        hit2 = _match_url(r2.url)
        if hit2 and hit2.supported:
            hit2.source = "careers-page (1 hop)"
            return hit2
        hit3, _ = _scan_html(r2.text, r2.url)
        if hit3 and hit3.supported:
            hit3.source = "careers-page (1 hop)"
            return hit3
        if hit3 and not res:
            res = hit3

    if res:  # only an unsupported ATS was found — worth reporting by name
        res.source = "careers-page"
        res.note = f"{res.ats} detected; no adapter yet"
        return res
    return None


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


def by_probing(name: str, website: str) -> Resolution | None:
    for slug in candidate_slugs(name, website):
        for ats in PROBEABLE:
            try:
                roles = ADAPTERS[ats](slug)
            except Exception:  # noqa: BLE001 — a probe miss is expected
                continue
            if roles:
                return Resolution(ats=ats, slug=slug, source="probe",
                                  note=f"probed slug, {len(roles)} roles live")
    return None


def resolve(company, overrides: dict, cache: dict, *, refresh: bool = False) -> Resolution:
    key = company.name.lower()
    today = date.today().isoformat()

    if key in overrides:
        return overrides[key]

    cached = cache.get(key)
    if cached and not refresh:
        res = Resolution(**cached)
        if res.ats:
            return res
        # Cached failure: don't re-probe every week.
        retry_after = (date.fromisoformat(res.resolved_on or today)
                       + timedelta(days=UNRESOLVED_RETRY_DAYS)).isoformat()
        if today < retry_after:
            return res

    res = from_careers_page(company.careers_url)
    if not (res and res.supported):
        probed = by_probing(company.name, company.website)
        if probed:
            res = probed
    if not (res and res.supported) and company.careers_url:
        try:
            ADAPTERS["jsonld"](url=company.careers_url)
            res = Resolution(ats="jsonld", source="jsonld",
                             params={"url": company.careers_url},
                             note="schema.org markup on careers page")
        except Exception:  # noqa: BLE001
            pass

    if not res:
        res = Resolution(source="none",
                         note="no careers URL" if not company.careers_url
                         else "no ATS found on careers page or by probing")
    res.resolved_on = today
    cache[key] = res.to_dict()
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
    state: str = ""
    segment: str = ""


_NA = {"", "n/a", "na", "none", "-"}


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _NA else v


def load_companies(path: Path) -> list[Company]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    out, seen = [], set()
    for r in rows:
        name = _clean(r.get("Company Name", ""))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(Company(
            name=name,
            website=_clean(r.get("Website URL", "")),
            careers_url=_clean(r.get("Careers Page URL", "")),
            state=_clean(r.get("State", "")),
            segment=_clean(r.get("Industry Segment", "")),
        ))
    log.info("loaded %d companies from %s", len(out), path)
    return out


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
        for name, rule in (cfg.get("company_overrides") or {}).items():
            self.overrides[name.lower()] = {
                "require": rx(rule.get("require") or []),
                "exclude": rx(rule.get("exclude") or []),
            }

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

    def carry_forward(self, company: str, reason: str) -> dict:
        prev = self.previous.get(company)
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
            if result.get("status") == "ok":
                self.previous[company] = {"fetched_on": self.run_date,
                                          "roles": result["roles"]}
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


def write_report(results: dict, diff: dict, run_date: str, first_run: bool, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    open_roles = [r for c in results.values() for r in c.get("filtered", [])]
    companies_with_roles = sorted({r["company"] for r in open_roles})

    md = [f"# OpCo job search — week of {run_date}", ""]
    md.append(f"{len(open_roles)} matching roles open across "
              f"{len(companies_with_roles)} of {len(results)} companies.")
    md += ["", "> Open each link before it goes in the newsletter. Postings move.", ""]

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
           "| Company | ATS | Status | Roles (all / matching) | Note |",
           "|---|---|---|---|---|"]
    order = {"ok": 0, "stale": 1, "unsupported": 2, "failed": 3, "unresolved": 4}
    for name, c in sorted(results.items(), key=lambda kv: (order.get(kv[1]["status"], 9), kv[0])):
        md.append(f"| {name} | {c.get('ats') or '—'} | {c['status']} | "
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


def fetch_company(company, res, role_filter, state) -> dict:
    base = {"ats": res.ats, "slug": res.slug, "source": res.source, "note": res.note}

    if not res.ats:
        return {**base, "status": "unresolved", "reason": res.note, "roles": [], "filtered": []}
    if not res.supported:
        return {**base, "status": "unsupported", "reason": f"{res.ats} has no adapter yet",
                "roles": [], "filtered": []}

    try:
        params = dict(res.params)
        if res.ats == "jsonld":
            params.setdefault("url", company.careers_url)
        roles = [r.to_dict() for r in ADAPTERS[res.ats](res.slug, **params)]
    except Exception as exc:  # noqa: BLE001 — one company must never sink the run
        reason = str(exc) or type(exc).__name__
        log.warning("  %s: fetch failed (%s)", company.name, reason)
        result = {**base, **state.carry_forward(company.name, reason)}
    else:
        if state.is_suspicious_drop(company.name, len(roles)):
            reason = "dropped to 0 roles from 5+ last week; treated as a scrape failure"
            log.warning("  %s: %s", company.name, reason)
            result = {**base, **state.carry_forward(company.name, reason)}
        else:
            result = {**base, "status": "ok", "roles": roles}

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

    try:
        companies = load_companies(find_companies_file(args.companies))
    except (FileNotFoundError, KeyError) as exc:
        log.error("%s", exc)
        return 2

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("config not found: %s", config_path)
        return 2
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

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
        except Exception:  # noqa: BLE001
            log.error("  %s: resolver crashed\n%s", company.name, traceback.format_exc())
            continue

        log.info("[%2d/%d] %-34s %-15s %s", i, len(companies), company.name[:34],
                 res.ats or "-", res.source)
        if args.discover:
            results[company.name] = {"ats": res.ats, "status": "discover",
                                     "note": res.note, "roles": [], "filtered": []}
            continue

        result = fetch_company(company, res, role_filter, state)
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
    path = write_report(results, diff, state.run_date, first_run, Path(args.out))
    state.save()

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
