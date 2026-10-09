"""Job-system readers shared by the weekly search and the OpCo search.

One reader per job system. Each takes the board's identifier (`slug`) plus any
extra parameters the system needs, and returns a JobList of plain dicts:

    {"system": "greenhouse", "slug": "kasa", "id": "4012345", "title": "...",
     "url": "direct link to the posting", "location": "...", "department": "...",
     "posted": "YYYY-MM-DD or ''", "posted_rel": "Posted 3 Days Ago" (Workday only),
     "ref": requisition reference (Workday only)}

`id` is the job system's own id for the posting. Both searches build their
history keys from it (see weekly_key / opco_id), in exactly the format each used
before this module existed, so a role that was already being tracked is still
the same role after the rebuild.

`classify(url)` recognises a job-board link and says which reader handles it.

A reader that cannot tell "no openings" from "wrong board" raises instead of
returning an empty list: an empty list is reported as "not hiring", and a
wrong link must never read as that.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .http import FetchError, NotFound, get, post_json

MAX_PAGES = 30  # hard stop so a runaway paginator can't eat the run


class JobList(list):
    """A list of jobs that can also say it was cut short."""
    truncated: str = ""


# ------------------------------------------------------------------ helpers
def as_text(v) -> str:
    """Job systems send nulls, numbers, {"en": "..."} objects and lists where
    text belongs. Normalise once so nothing downstream can trip on it."""
    if v is None:
        return ""
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, dict):
        for x in v.values():
            t = as_text(x)
            if t:
                return t
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(t for t in (as_text(x) for x in v) if t)
    return str(v)


def records(seq, what: str = "job list") -> list:
    """The job records in a response, skipping any that aren't records. A job
    list that isn't a list at all is a real failure and says so."""
    if seq is None:
        return []
    if not isinstance(seq, list):
        raise FetchError(f"the job system sent its {what} in an unexpected format")
    return [x for x in seq if isinstance(x, dict)]


def rid(j: dict, *keys) -> str:
    """A stable id for a record, even when the id field is missing."""
    for k in keys:
        if j.get(k) not in (None, ""):
            return as_text(j[k])
    return hashlib.sha1(json.dumps(j, sort_keys=True, default=str).encode()).hexdigest()[:12]


def place(*parts) -> str:
    return ", ".join(as_text(p) for p in parts if p and as_text(p))


def _ms_to_date(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _job(system: str, slug: str, id_, title, url, location="", department="", posted="", **extra) -> dict:
    return {"system": system, "slug": slug, "id": as_text(id_), "title": as_text(title),
            "url": as_text(url), "location": as_text(location), "department": as_text(department),
            "posted": as_text(posted)[:10], "posted_rel": "", "ref": "", **extra}


def _json(resp, what: str):
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(f"{what} sent back a web page instead of job data") from exc


# ---------------------------------------------------------------- Greenhouse
def greenhouse(slug: str, **_) -> JobList:
    try:
        d = _json(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"), "Greenhouse")
    except NotFound as exc:
        raise NotFound(f"Greenhouse has no board named '{slug}' (it may have moved)") from exc
    out = JobList()
    for j in records(d.get("jobs")):
        out.append(_job("greenhouse", slug, rid(j, "id", "absolute_url"), j.get("title"),
                        j.get("absolute_url"), (j.get("location") or {}).get("name", ""),
                        posted=j.get("first_published") or j.get("updated_at")))
    return out


# --------------------------------------------------------------------- Lever
def lever(slug: str, host: str = "", **_) -> JobList:
    """Lever answers a board that doesn't exist with an empty list, exactly as it
    answers a company with no openings. So an empty answer is checked against
    the public board page: if that page is a 404, the board is gone."""
    eu = "eu." in (host or "")
    api = "https://api.eu.lever.co" if eu else "https://api.lever.co"
    page = "https://jobs.eu.lever.co" if eu else "https://jobs.lever.co"
    try:
        d = _json(get(f"{api}/v0/postings/{slug}?mode=json"), "Lever")
    except NotFound as exc:
        raise NotFound(f"Lever has no board named '{slug}' (it may have moved)") from exc
    out = JobList()
    for j in records(d):
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(j.get("allLocations", []) or [])
        out.append(_job("lever", slug, rid(j, "id", "hostedUrl"), j.get("text"), j.get("hostedUrl"),
                        loc, cats.get("team", ""), _ms_to_date(j.get("createdAt"))))
    if not out:
        try:
            get(f"{page}/{slug}", retries=1)
        except NotFound as exc:
            raise NotFound(f"Lever has no board named '{slug}' (it may have moved)") from exc
        except FetchError:
            pass  # couldn't check; an empty list is still the best answer we have
    return out


# --------------------------------------------------------------------- Ashby
def _ashby_graphql(slug: str) -> list[dict]:
    """The feed jobs.ashbyhq.com/<slug> itself uses. Used when the posting API 404s
    while the board is live (seen for Footprint on 2026-09-24)."""
    q = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: "
         "jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) "
         "{ jobPostings { id title locationName } } }")
    data = _json(post_json("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
                           {"operationName": "ApiJobBoardWithTeams",
                            "variables": {"organizationHostedJobsPageName": slug}, "query": q}), "Ashby")
    if (data or {}).get("errors"):
        raise FetchError(f"Ashby GraphQL error: {str(data['errors'])[:160]}")
    board = ((data or {}).get("data") or {}).get("jobBoard")
    if not board:
        raise NotFound(f"Ashby has no job board named '{slug}'")
    return [{"id": j["id"], "title": j.get("title", ""), "location": j.get("locationName", ""),
             "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{j['id']}", "publishedAt": ""}
            for j in board.get("jobPostings", []) or []]


def _ashby_page(slug: str) -> list[dict]:
    """The postings embedded in jobs.ashbyhq.com/<slug> (window.__appData)."""
    html = get(f"https://jobs.ashbyhq.com/{slug}").text
    m = re.search(r"window\.__appData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        raise FetchError(f"Ashby page for '{slug}' has no embedded job data")
    posts = ((json.loads(m.group(1)).get("jobBoard") or {}).get("jobPostings")) or []
    return [{"id": j["id"], "title": j.get("title", ""), "location": j.get("locationName", ""),
             "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
             "publishedAt": j.get("publishedDate", "") or ""} for j in posts]


def ashby(slug: str, **_) -> JobList:
    try:
        jobs = records(_json(get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                                 "?includeCompensation=false"), "Ashby").get("jobs"))
    except NotFound:
        errs = []
        for fn in (_ashby_graphql, _ashby_page):
            try:
                jobs = fn(slug)
                break
            except Exception as ex:  # noqa: BLE001 - try the next way in; report every reason
                errs.append(f"{fn.__name__}: {ex}")
        else:
            raise NotFound(f"Ashby has no board named '{slug}' (it may have moved); " + " | ".join(errs))
    out = JobList()
    for j in jobs:
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") or ", ".join(
            s.get("location", "") for s in (j.get("secondaryLocations") or []) if s.get("location"))
        out.append(_job("ashby", slug, rid(j, "id", "jobUrl"), j.get("title"), j.get("jobUrl"),
                        loc, j.get("department", ""), j.get("publishedAt")))
    return out


# ------------------------------------------------------------------ Workable
def _workable_v3(slug: str) -> JobList:
    out, token = JobList(), None
    for _ in range(MAX_PAGES):
        payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            payload["token"] = token
        data = _json(post_json(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", payload), "Workable")
        for j in records(data.get("results")):
            loc = j.get("location") or {}
            loc_s = place(loc.get("city"), loc.get("region"), loc.get("country"))
            if j.get("remote"):
                loc_s = ("Remote · " + loc_s) if loc_s else "Remote"
            out.append(_job("workable", slug, j.get("shortcode"), j.get("title"),
                            f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/", loc_s,
                            as_text(j.get("department")), j.get("published")))
        token = data.get("nextPage")
        if not token:
            break
    return out


def workable(slug: str, **_) -> JobList:
    """Workable's widget feed returns every posting in one answer. Its paged
    search API is the fallback: it carries the same postings, ten at a time."""
    try:
        d = _json(get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"), "Workable")
    except NotFound as exc:
        raise NotFound(f"Workable has no account named '{slug}' (it may have moved)") from exc
    except FetchError:
        return _workable_v3(slug)
    out = JobList()
    for j in records(d.get("jobs")):
        loc_s = place(j.get("city"), j.get("state"), j.get("country"))
        if j.get("telecommuting"):
            loc_s = ("Remote · " + loc_s) if loc_s else "Remote"
        out.append(_job("workable", slug, j.get("shortcode"), j.get("title"),
                        f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/", loc_s,
                        j.get("department", ""), j.get("published_on")))
    return out


# ------------------------------------------------------------- SmartRecruiters
def smartrecruiters(slug: str, **_) -> JobList:
    out, offset = JobList(), 0
    for _ in range(MAX_PAGES):
        d = _json(get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                      f"?limit=100&offset={offset}"), "SmartRecruiters")
        content = records(d.get("content"))
        for j in content:
            loc = j.get("location") or {}
            jid = rid(j, "id")
            out.append(_job("smartrecruiters", slug, jid, j.get("name"),
                            f"https://jobs.smartrecruiters.com/{slug}/{jid}",
                            place(loc.get("city"), loc.get("region"), loc.get("country")),
                            (j.get("department") or {}).get("label", ""), j.get("releasedDate")))
        offset += len(content)
        if not content or offset >= int(d.get("totalFound") or 0):
            break
    return out


# ----------------------------------------------------------------- Recruitee
def recruitee(slug: str, **_) -> JobList:
    d = _json(get(f"https://{slug}.recruitee.com/api/offers/"), "Recruitee")
    return JobList(_job("recruitee", slug, rid(j, "id", "careers_url"), j.get("title"), j.get("careers_url"),
                        j.get("location", ""), j.get("department", ""), j.get("published_at"))
                   for j in records(d.get("offers")))


# -------------------------------------------------------------------- Breezy
def breezy(slug: str, origin: str = "", **_) -> JobList:
    """`origin` is set when a company serves its Breezy board from its own
    address (jobs.doorstead.com): the same feed answers there."""
    base = (origin or f"https://{slug}.breezy.hr").rstrip("/")
    d = _json(get(f"{base}/json"), "Breezy")
    return JobList(_job("breezy", slug, rid(j, "id", "url"), j.get("name"),
                        j.get("url") or f"{base}/p/{rid(j, 'friendly_id', 'id')}",
                        (j.get("location") or {}).get("name", ""), j.get("department", ""),
                        j.get("published_date"))
                   for j in records(d))


# ------------------------------------------------------------------ BambooHR
def bamboohr(slug: str, **_) -> JobList:
    d = _json(get(f"https://{slug}.bamboohr.com/careers/list", accept="application/json"), "BambooHR")
    out = JobList()
    for j in records(d.get("result")):
        loc = j.get("location") or {}
        loc_s = place(loc.get("city"), loc.get("state"))
        if j.get("isRemote"):
            loc_s = ("Remote · " + loc_s) if loc_s else "Remote"
        jid = rid(j, "id")
        out.append(_job("bamboohr", slug, jid, j.get("jobOpeningName"),
                        f"https://{slug}.bamboohr.com/careers/{jid}", loc_s,
                        j.get("departmentLabel", ""), j.get("datePosted")))
    return out


# ------------------------------------------------------------------ Rippling
def rippling(slug: str, **_) -> JobList:
    try:
        data = _json(get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"), "Rippling")
    except NotFound as exc:
        raise NotFound(f"Rippling has no board named '{slug}' (it may have moved)") from exc
    if isinstance(data, dict):  # some boards wrap the list
        data = data.get("items") or data.get("jobs") or data.get("results") or []
    out = JobList()
    for j in records(data):
        jid = j.get("uuid") or j.get("id") or j.get("jobId") or j.get("url", "")
        if not jid:
            continue
        loc = j.get("workLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        dept = j.get("department")
        out.append(_job("rippling", slug, jid, j.get("name") or j.get("title"),
                        j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{jid}",
                        loc.get("label", "") if isinstance(loc, dict) else str(loc),
                        dept.get("label", "") if isinstance(dept, dict) else as_text(dept)))
    return out


# ------------------------------------------------------------------- Workday
WORKDAY_MAX_PAGES = 60  # 1,200 postings per search


def workday(slug: str, host: str = "", site: str = "", wd: str = "", search_terms=None,
            max_pages: int = WORKDAY_MAX_PAGES, **_) -> JobList:
    """Workday's public CXS API. `slug` is the tenant.

    Workday caps pages at 20 and only reports `total` reliably on the first
    page, so the first page's total drives pagination.

    Big operators post thousands of site-level roles, and the corporate ones
    sit far down the list. For those, pass `search_terms`: each term runs as its
    own Workday keyword search and the results merge.
    """
    if not host and wd:
        host = f"{slug}.{wd}.myworkdayjobs.com"
    if not host or not site:
        raise FetchError("Workday needs its host and site name (paste the Workday job-board link)")
    endpoint = f"https://{host}/wday/cxs/{slug}/{site}/jobs"
    public = f"https://{host}/recruiting/{slug}/{site}" if "myworkdaysite" in host else f"https://{host}/{site}"
    out, seen, cut = JobList(), set(), []
    for term in (search_terms or [""]):
        offset, total = 0, None
        for _ in range(max_pages):
            d = _json(post_json(endpoint, {"appliedFacets": {}, "limit": 20,
                                           "offset": offset, "searchText": term}), "Workday")
            if total is None:
                try:
                    total = int(d.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
            postings = records(d.get("jobPostings"))
            for j in postings:
                path = as_text(j.get("externalPath"))
                if not path or not as_text(j.get("title")):
                    continue  # Workday occasionally returns placeholder rows with no job behind them
                if path in seen:
                    continue
                seen.add(path)
                bullets = j.get("bulletFields")
                ref = as_text(bullets[0]) if isinstance(bullets, list) and bullets else path
                out.append(_job("workday", slug, path, j.get("title"), f"{public}{path}",
                                j.get("locationsText", ""), posted="",
                                posted_rel=as_text(j.get("postedOn")), ref=ref))
            offset += len(postings)
            # An unknown total (0) means: keep going until a page comes back empty.
            if not postings or (total and offset >= total):
                break
        if total and offset < total:
            cut.append(f"'{term or 'all'}' stopped at {offset} of {total}")
    if cut:
        out.truncated = "; ".join(cut)
    return out


# ----------------------------------------------------------------------- UKG
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
    return place(as_text(city), as_text(state))


def ukg(slug: str, board: str = "", host: str = "recruiting.ultipro.com", **_) -> JobList:
    """UKG / UltiPro. `slug` is the tenant code (LAM1000LAC), `board` the GUID.

    The host comes from the link because tenants live on different servers.
    The three empty filter entries are required: some tenants answer a request
    without them with a normal-looking empty list. And an empty board is never
    treated as a normal result, since a wrong board ID and a company that isn't
    hiring look identical.
    """
    if not board:
        raise FetchError("UKG needs the job-board ID (paste the link that contains /JobBoard/...)")
    base = f"https://{host}/{slug}/JobBoard/{board}"
    out, skip, total = JobList(), 0, None
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
        d = _json(post_json(f"{base}/JobBoardView/LoadSearchResults", body), "UKG")
        if not isinstance(d, dict) or not isinstance(d.get("opportunities"), list):
            raise FetchError("UKG didn't return a job list (the board may have moved)")
        opps = records(d["opportunities"])
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
            out.append(_job("ukg", slug, rid(j, "Id", "RequisitionNumber"), j.get("Title"),
                            f"{base}/OpportunityDetail?opportunityId={rid(j, 'Id')}",
                            _ukg_place(locs[0] if isinstance(locs, list) and locs else None),
                            posted=as_text(j.get("PostedDate"))))
        skip += len(opps)
        if len(opps) < UKG_PAGE or (total and skip >= total):
            break
    return out


# ----------------------------------------------------------------------- ADP
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
    where = place(as_text(addr.get("cityName")), as_text(region))
    if not where and isinstance(loc.get("nameCode"), dict):
        where = as_text(loc["nameCode"].get("shortName") or loc["nameCode"].get("longName"))
    return where


def adp(slug: str, cc: str = "", **_) -> JobList:
    """ADP Workforce Now career centers. `slug` is the client id (cid), `cc`
    the career-center id (ccId) when the link carries one.

    ADP hands back 20 openings per request and reports the full count in
    meta.totalNumber. Its page offset ($skip) counts from 1, not 0. A company
    can run several career centers under one client id; the ccId picks one.
    """
    common = f"cid={slug}" + (f"&ccId={cc}" if cc else "") + "&lang=en_US&locale=en_US"
    out, seen, total = JobList(), set(), None
    for page in range(MAX_PAGES):
        d = _json(get(f"{ADP_LIST}?{common}&$top={ADP_PAGE}&$skip={1 + page * ADP_PAGE}",
                      accept="application/json"), "ADP")
        if not isinstance(d, dict):
            raise FetchError("ADP didn't return a job list")
        if total is None:
            meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
            try:
                total = int(meta.get("totalNumber") or 0)
            except (TypeError, ValueError):
                total = 0
        reqs = records(d.get("jobRequisitions"))
        new = 0
        for j in reqs:
            iid = rid(j, "itemID", "clientRequisitionID")
            if iid in seen:
                continue
            seen.add(iid)
            new += 1
            out.append(_job("adp", slug, iid, j.get("requisitionTitle"),
                            ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                             f"recruitment.html?cid={slug}" + (f"&ccId={cc}" if cc else "")
                             + f"&jobId={iid}&lang=en_US"),
                            _adp_place(j.get("requisitionLocations")),
                            posted=as_text(j.get("postDate"))))
        if not reqs or not new or (total and len(seen) >= total):
            break
    if not out:
        raise FetchError("ADP career center reports no openings - if the company is hiring, "
                         "use a job link that includes its ccId")
    if total and len(out) < total:
        out.truncated = f"read {len(out)} of the {total} ADP says are open"
    return out


# ------------------------------------------------------------------ Dayforce
DAYFORCE = "https://jobs.dayforcehcm.com"


def dayforce(slug: str, board: str = "CANDIDATEPORTAL", **_) -> JobList:
    """Dayforce's hosted job sites (jobs.dayforcehcm.com/{namespace}/{board}).

    The list is loaded from a data endpoint that refuses requests without a
    session token. So: ask for the token first (the shared session keeps the
    cookie that comes with it), then request the list 25 postings at a time.
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
    out, start, total = JobList(), 0, None
    for _ in range(MAX_PAGES):
        d = _json(post_json(endpoint, {"clientNamespace": slug, "jobBoardCode": board,
                                       "cultureCode": "en-US", "distanceUnit": 0,
                                       "paginationStart": start},
                            headers={"x-csrf-token": token}), "Dayforce")
        if not isinstance(d, dict):
            raise FetchError("the job system sent its job list in an unexpected format")
        if total is None:
            try:
                total = int(d.get("maxCount") or 0)
            except (TypeError, ValueError):
                total = 0
        postings = records(d.get("jobPostings"))
        for j in postings:
            pid = rid(j, "jobPostingId", "jobReqId", "id")
            locs = j.get("postingLocations") or j.get("locations") or []
            first = locs[0] if isinstance(locs, list) and locs and isinstance(locs[0], dict) else {}
            where = (first.get("formattedAddress")
                     or place(first.get("cityName") or first.get("city"),
                              first.get("stateCode") or first.get("state")))
            out.append(_job("dayforce", slug, pid, j.get("jobTitle") or j.get("title"),
                            f"{DAYFORCE}/en-US/{slug}/{board}/jobs/{pid}", where,
                            posted=as_text(j.get("postingStartTimestampUTC")
                                           or j.get("postingStartTimestamp") or j.get("datePosted"))))
        start += len(postings)
        if not postings or (total and start >= total):
            break
    return out


# ------------------------------------------------------------------- Jobvite
def jobvite(slug: str, **_) -> JobList:
    """Jobvite has no public JSON list, but its board is server-rendered.
    Follows the board's own "next" link when the list runs to several pages."""
    out, seen = JobList(), set()
    url, visited = f"https://jobs.jobvite.com/{slug}/jobs", set()
    row = re.compile(r'href="(?:https://jobs\.jobvite\.com)?/' + re.escape(slug) +
                     r'/job/([A-Za-z0-9]+)"[^>]*>(.*?)</a>(?:.*?<td[^>]*>(.*?)</td>)?', re.S)
    for _ in range(MAX_PAGES):
        if url in visited:
            break
        visited.add(url)
        try:
            html = get(url).text
        except NotFound as exc:
            raise NotFound(f"Jobvite has no board named '{slug}' (it may have moved)") from exc
        before = len(out)
        for m in row.finditer(html):
            jid = m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            title = re.sub(r"<[^>]+>", " ", m.group(2))
            loc = re.sub(r"<[^>]+>", " ", m.group(3) or "")
            out.append(_job("jobvite", slug, jid, title, f"https://jobs.jobvite.com/{slug}/job/{jid}", loc))
        nxt = re.search(r'<a[^>]+class="[^"]*jv-pagination-next[^"]*"[^>]+href="([^"]+)"', html) or \
            re.search(r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*jv-pagination-next[^"]*"', html)
        if not nxt or len(out) == before:
            break
        url = urljoin(url, nxt.group(1).replace("&amp;", "&"))
    return out


# ----------------------------------------------------------------- Paylocity
def paylocity(slug: str, **_) -> JobList:
    """Paylocity's board page carries its whole job list as data inside the page
    (window.pageData), so no browser is needed. `slug` is the board's GUID."""
    try:
        html = get(f"https://recruiting.paylocity.com/recruiting/jobs/All/{slug}").text
    except NotFound as exc:
        raise NotFound("Paylocity has no board with that ID (it may have moved)") from exc
    m = re.search(r"window\.pageData\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if not m:
        raise FetchError("Paylocity's page didn't include its job list (the page layout may have changed)")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise FetchError("Paylocity's job list couldn't be read (unexpected format)") from exc
    out = JobList()
    for j in records(data.get("Jobs")):
        if j.get("IsInternal"):
            continue
        loc = j.get("JobLocation") if isinstance(j.get("JobLocation"), dict) else {}
        where = as_text(j.get("LocationName")) or place(loc.get("City"), loc.get("State"))
        if j.get("IsRemote") and "remote" not in where.lower():
            where = ("Remote · " + where) if where else "Remote"
        jid = rid(j, "JobId")
        out.append(_job("paylocity", slug, jid, j.get("JobTitle"),
                        f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}", where,
                        as_text(j.get("HiringDepartment")), j.get("PublishedDate")))
    return out


# --------------------------------------------------------------------- iCIMS
ICIMS_MAX_PAGES = 40


def icims(slug: str, host: str = "", **_) -> JobList:
    """iCIMS career sites. The public page wraps the real list in a frame; asking
    for the frame's own address (in_iframe=1) returns the list as plain HTML,
    one page at a time (pr=0, 1, 2...)."""
    host = host or f"{slug}.icims.com"
    out, seen = JobList(), set()
    for page in range(ICIMS_MAX_PAGES):
        try:
            html = get(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}").text
        except NotFound as exc:
            if page:
                break
            raise NotFound(f"iCIMS has no career site at {host} (it may have moved)") from exc
        soup = BeautifulSoup(html, "lxml")
        new = 0
        for a in soup.find_all("a", href=True):
            m = re.search(r"/jobs/(\d+)/[^/?#]+/job", a["href"])
            if not m or m.group(1) in seen:
                continue
            heading = a.find(["h1", "h2", "h3", "h4"])
            title = heading.get_text(" ", strip=True) if heading else re.sub(r"^\d+\s*-\s*", "", a.get("title") or "")
            if not title:
                continue
            seen.add(m.group(1))
            new += 1
            where = ""
            row = a.find_parent(class_="row")
            if row is not None:
                for label in row.find_all(class_="field-label"):
                    if label.get_text(strip=True).lower().startswith("location"):
                        sib = label.find_next_sibling()
                        where = sib.get_text(" ", strip=True) if sib else ""
                        break
            link = urljoin(f"https://{host}/", a["href"]).split("?")[0]
            out.append(_job("icims", slug, m.group(1), title, link, where))
        if not new:
            break
    else:
        out.truncated = f"stopped after {ICIMS_MAX_PAGES} pages"
    return out


READERS: dict[str, Callable[..., JobList]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
    "smartrecruiters": smartrecruiters, "recruitee": recruitee, "breezy": breezy,
    "bamboohr": bamboohr, "rippling": rippling, "workday": workday, "ukg": ukg, "adp": adp,
    "dayforce": dayforce, "jobvite": jobvite, "paylocity": paylocity, "icims": icims,
}


# ==========================================================================
# HISTORY KEYS
# ==========================================================================
# Both searches remember roles between runs, each by its own key format. These
# two functions are the only place those formats are written down.

def weekly_key(job: dict) -> str:
    """Weekly search: 'greenhouse:kasa:4012345'."""
    return f"{job['system']}:{job['slug']}:{job['id']}"


_OPCO_PREFIX = {"greenhouse": "gh", "lever": "lv", "ashby": "ab", "workable": "wk",
                "smartrecruiters": "sr", "recruitee": "rt", "breezy": "bz", "ukg": "uk",
                "adp": "adp", "bamboohr": "bh"}


def opco_id(job: dict) -> str:
    """OpCo search: 'gh-4012345', 'wd-<tenant>-<requisition>', 'df-<namespace>-<id>'."""
    system = job["system"]
    if system in _OPCO_PREFIX:
        return f"{_OPCO_PREFIX[system]}-{job['id']}"
    if system == "workday":
        return f"wd-{job['slug']}-{job.get('ref') or job['id']}"
    if system == "dayforce":
        return f"df-{job['slug']}-{job['id']}"
    if system == "page":
        return f"op-{job['id']}"          # a job link on a page: its path
    if system == "jsonld":
        return f"ld-{job['id']}"          # structured job data on a page
    return f"{system}-{job['slug']}-{job['id']}"


# ==========================================================================
# RECOGNISING JOB-BOARD LINKS
# ==========================================================================

@dataclass
class Board:
    system: str = ""          # a READERS key, a RENDERED key, "page", an unsupported system name, or ""
    slug: str = ""
    params: dict = field(default_factory=dict)
    problem: str = ""         # why the link can't be used, when system is ""

    @property
    def readable(self) -> bool:
        return self.system in READERS and bool(self.slug)

    @property
    def rendered(self) -> bool:
        return self.system in RENDERED and bool(self.slug or self.params.get("key"))

    @property
    def key(self) -> str:
        """Identity of the board, so a changed link starts a fresh baseline."""
        return f"{self.system}:{self.slug}:{json.dumps(self.params, sort_keys=True)}"

    @property
    def listing_url(self) -> str:
        """The public page that lists this board's jobs."""
        s, p = self.slug, self.params
        if self.system in RENDERED:
            return RENDERED[self.system]["url"].format(slug=s, **{k: p.get(k, "") for k in ("host", "key")})
        public = {
            "greenhouse": f"https://job-boards.greenhouse.io/{s}",
            "lever": f"https://jobs.lever.co/{s}",
            "ashby": f"https://jobs.ashbyhq.com/{s}",
            "workable": f"https://apply.workable.com/{s}/",
            "smartrecruiters": f"https://jobs.smartrecruiters.com/{s}",
            "recruitee": f"https://{s}.recruitee.com/",
            "breezy": p.get("origin") or f"https://{s}.breezy.hr/",
            "bamboohr": f"https://{s}.bamboohr.com/careers",
            "rippling": f"https://ats.rippling.com/{s}/jobs",
            "workday": f"https://{p.get('host', '')}/{p.get('site', '')}",
            "ukg": f"https://{p.get('host', 'recruiting.ultipro.com')}/{s}/JobBoard/{p.get('board', '')}/",
            "adp": ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                    f"?cid={s}" + (f"&ccId={p['cc']}" if p.get("cc") else "")),
            "dayforce": f"https://jobs.dayforcehcm.com/{s}/{p.get('board', 'CANDIDATEPORTAL')}",
            "jobvite": f"https://jobs.jobvite.com/{s}/jobs",
            "paylocity": f"https://recruiting.paylocity.com/recruiting/jobs/All/{s}",
            "icims": f"https://{p.get('host') or s + '.icims.com'}/jobs/search?ss=1",
        }
        return public.get(self.system, p.get("url", ""))

    def label(self) -> str:
        return f"{self.system}: {self.slug}" if self.slug else self.system

    def identity(self) -> tuple:
        """The board with spelling differences removed, for asking "is this the
        same board?": letter case, and Workday's two ways of naming its host
        ({"wd": "wd5"} in older ats_map entries, {"host": ...} from a link)."""
        p = {k: str(v).lower() for k, v in self.params.items() if k in ("host", "site", "board", "cc", "key", "origin")}
        if self.system == "workday" and "host" not in p and self.params.get("wd"):
            p["host"] = f"{self.slug}.{self.params['wd']}.myworkdayjobs.com".lower()
        if self.system == "page":
            p["url"] = str(self.params.get("url", "")).rstrip("/").lower()
        return (self.system, self.slug.lower(), tuple(sorted(p.items())))


# Job systems with no data feed we can call directly. Their board page is
# opened in the headless browser and the job links on it are read. `links` says
# what a posting's address looks like on that system, so nothing else on the
# page can be mistaken for a job.
RENDERED: dict[str, dict] = {
    "paycom": {"url": "https://{host}/v4/ats/web.php/portal/{key}/career-page",
               "links": r"/portal/[A-F0-9]{32}/jobs/\d+"},
    "paycom-classic": {"url": "https://{host}/v4/ats/web.php/jobs?clientkey={key}",
                       "links": r"/jobs/ViewJobDetails\?|/jobs/\d+"},
    "isolved": {"url": "https://{slug}.isolvedhire.com/jobs/", "links": r"/jobs/\d+"},
    "applicantpro": {"url": "https://{slug}.applicantpro.com/jobs/", "links": r"/jobs/\d+"},
    "gem": {"url": "https://jobs.gem.com/{slug}", "links": r"^/[^/]+/[A-Za-z0-9_=-]{8,}/?$"},
    "jazzhr": {"url": "https://{slug}.applytojob.com/apply/", "links": r"/apply/[A-Za-z0-9]{6,}"},
    "teamtailor": {"url": "https://{slug}.teamtailor.com/jobs", "links": r"/jobs/\d+-"},
    "trinethire": {"url": "https://app.trinethire.com/companies/{slug}/jobs", "links": r"/jobs/\d+"},
    "pinpoint": {"url": "https://{slug}.pinpointhq.com/", "links": r"/postings/[0-9a-f-]{8,}"},
}

_G = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# (system, compiled pattern). Named groups feed the reader's parameters.
# Order matters: the data-feed addresses a careers page calls come first, then
# public board links, then systems that are recognised but not readable.
PATTERNS = [
    ("workday", re.compile(
        r"https?://(?P<host>(?P<slug>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)"
        r"/wday/cxs/[a-z0-9_-]+/(?P<site>[A-Za-z0-9_\-]+)", re.I)),
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
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?/?\?(?:[^\s\"'<>]*&)?for=(?P<slug>[a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"(?P<host>jobs(?:\.eu)?\.lever\.co)/(?P<slug>[a-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"(?P<host>api(?:\.eu)?\.lever\.co)/v0/postings/(?P<slug>[a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/(?P<slug>[a-z0-9_.%-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<slug>[a-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/api/v\d/(?:widget/)?accounts/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/(?P<slug>[A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"(?P<slug>[a-z0-9-]+)\.recruitee\.com", re.I)),
    ("breezy", re.compile(r"(?P<slug>[a-z0-9-]+)\.breezy\.hr", re.I)),
    ("bamboohr", re.compile(r"(?P<slug>[a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("adp", re.compile(r"workforcenow\.adp\.com/.*?[?&]cid=(?P<slug>[0-9a-f-]{36})", re.I)),
    ("rippling", re.compile(r"api\.rippling\.com/platform/api/ats/v1/board/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<slug>[a-z0-9_-]+)", re.I)),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/(?:careers/)?(?P<slug>[a-z0-9_-]+)", re.I)),
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/recruiting/jobs/(?:All|List)/(?P<slug>" + _G + ")", re.I)),
    ("icims", re.compile(r"(?P<host>(?P<slug>[a-z0-9-]+)\.icims\.com)", re.I)),
    # Read by opening the board in the headless browser.
    ("paycom", re.compile(r"(?P<host>[a-z0-9.-]*paycomonline\.(?:net|com))/v4/ats/web\.php/portal/(?P<key>[A-F0-9]{32})", re.I)),
    ("paycom-classic", re.compile(r"(?P<host>[a-z0-9.-]*paycomonline\.(?:net|com))/v4/ats/web\.php/jobs.*?[?&]clientkey=(?P<key>[A-F0-9]{32})", re.I)),
    ("isolved", re.compile(r"(?P<slug>[a-z0-9-]+)\.isolvedhire\.com", re.I)),
    ("applicantpro", re.compile(r"(?P<slug>[a-z0-9-]+)\.applicantpro\.com", re.I)),
    ("gem", re.compile(r"jobs\.gem\.com/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("jazzhr", re.compile(r"(?P<slug>[a-z0-9-]+)\.applytojob\.com", re.I)),
    ("teamtailor", re.compile(r"(?P<slug>[a-z0-9-]+)\.teamtailor\.com", re.I)),
    ("trinethire", re.compile(r"app\.trinethire\.com/companies/(?P<slug>[a-z0-9_-]+)", re.I)),
    ("pinpoint", re.compile(r"(?P<slug>[a-z0-9-]+)\.pinpointhq\.com", re.I)),
    # Recognised, no reader: named in the report so it's clear what would be worth building.
    ("paylocity", re.compile(r"recruiting\.paylocity\.com", re.I)),
    ("paycom", re.compile(r"paycomonline\.(net|com)", re.I)),
    ("paradox", re.compile(r"paradox\.ai", re.I)),
    ("taleo", re.compile(r"taleo\.net", re.I)),
    ("successfactors", re.compile(r"successfactors\.com|jobs\.sap\.com", re.I)),
    ("paycor", re.compile(r"recruitingbypaycor\.com", re.I)),
    ("hireology", re.compile(r"hireology\.com", re.I)),
    ("apploi", re.compile(r"apploi\.com", re.I)),
]

# Slugs that are job-system infrastructure words, never a real company board.
_BAD_SLUGS = {"embed", "js", "v1", "jobs", "api", "www", "careers", "job_board", "mydayforce",
              "candidateportal", "en-us", "boards", "recruiting", "app", "static", "assets",
              "cdn", "widget", "widgets", "accounts", "postings", "sso", "auth", "login", "support",
              "help", "info", "go", "get", "try", "resources", "hire", "status", "developers",
              "docs", "apply", "c", "j", "o", "share", "oneclick-ui", "wday", "public", "dist",
              "build", "images", "img", "fonts", "css", "scripts", "favicon.ico", "robots.txt"}

# The vendors' own marketing, help and asset hosts (assets-cdn.breezy.hr,
# images4.bamboohr.com, community.icims.com): they sit where a company's name
# would, on systems that put the company in the host name.
_VENDOR_HOST = re.compile(
    r"^(www\d*|api|app|cdn\d*|assets?(-cdn)?|static\d*|images?\d*|img\d*|media|files|attachments?|"
    r"marketing|blog|partners?|community|help|support|docs?|developers?|status|learn|academy|"
    r"info|resources|email|mail|secure|login|sso|auth|accounts?|go|get|try|hire|track|click|"
    r"links?|share|news|press|events?|webinars?|university|training|demo|trial|pages|lp)$", re.I)
_HOST_NAMED = {"recruitee", "breezy", "bamboohr", "icims", "isolved", "applicantpro", "jazzhr",
               "teamtailor", "pinpoint"}
_NOT_A_SITE = {"static", "assets", "wday", "dist", "build", "public", "api", "js", "css", "images"}
_GOOD_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.%-]*$")

# Fast first check: does this address mention any job system at all? classify()
# runs on every address a careers page contains, and almost none are boards.
JOB_SYSTEM_HOSTS = re.compile(
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|recruitee\.com|"
    r"breezy\.hr|bamboohr\.com|rippling\.com|myworkdayjobs\.com|myworkdaysite\.com|ultipro\.com|"
    r"adp\.com|dayforcehcm\.com|jobvite\.com|paylocity\.com|icims\.com|paycomonline\.|"
    r"isolvedhire\.com|applicantpro\.com|gem\.com|applytojob\.com|teamtailor\.com|trinethire\.com|"
    r"pinpointhq\.com|paradox\.ai|taleo\.net|successfactors\.com|jobs\.sap\.com|"
    r"recruitingbypaycor\.com|hireology\.com|apploi\.com", re.I)


def classify(link: str) -> Board:
    """Decide what a link is. Pure: no network, no memory.

    A link to a job board gives that board. Any other web address gives
    Board("page"), meaning "read the jobs listed on that page itself".
    """
    url = (link or "").strip()
    if not url:
        return Board(problem="the link is empty")
    if not re.match(r"https?://", url, re.I):
        return Board(problem=f"it isn't a link ({url[:60]!r})")
    full, url = url, url[:600]            # a board is named at the start of its address
    if not JOB_SYSTEM_HOSTS.search(url):
        return Board(system="page", params={"url": full})

    for system, pattern in PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        groups = m.groupdict()
        slug = (groups.pop("slug", "") or "").strip("/")
        if slug.lower() in _BAD_SLUGS or (slug and not _GOOD_SLUG.match(slug)):
            continue
        if system in _HOST_NAMED and _VENDOR_HOST.match(slug):
            continue
        if any((groups.get(k) or "").lower() in _NOT_A_SITE or (groups.get(k) or "").startswith("_")
               for k in ("site", "board")):
            continue
        needs_slug = system in READERS or (system in RENDERED and "{slug}" in RENDERED[system]["url"])
        if needs_slug and not slug:
            if system == "paylocity":
                return Board(system=system)   # Paylocity, but this link doesn't identify the board
            continue
        params = {k: v for k, v in groups.items() if v}
        if system == "lever":
            # only the EU host is a parameter; the default host stays out of the board's identity
            params = {"host": params["host"]} if ".eu." in params.get("host", "") else {}
        if system == "icims" and params.get("host", "").lower() == f"{slug}.icims.com".lower():
            params = {"host": params["host"].lower()}
        if system == "adp":
            try:
                cc = parse_qs(urlparse(url).query).get("ccId", [""])[0]
            except ValueError:
                cc = ""
            if cc:
                params["cc"] = cc
        if system in ("paycom", "paycom-classic") and "key" in params:
            params["key"] = params["key"].upper()
        if system == "paycom" and not params.get("key"):
            return Board(system=system)   # Paycom, but this link doesn't identify the board
        return Board(system=system, slug=slug, params=params)

    return Board(system="page", params={"url": full})


def read_board(board: Board, **extra) -> JobList:
    """Read a board that has a direct reader."""
    return READERS[board.system](board.slug, **{**board.params, **extra})
