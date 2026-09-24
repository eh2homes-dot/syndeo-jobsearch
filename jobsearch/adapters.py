"""ATS adapters.

Each adapter takes a slug (plus optional extra config) and returns a list of
normalized job dicts:

    {
      "job_key":   stable unique id  (ats:slug:id)
      "title":     str
      "location":  str
      "url":       direct link to the posting
      "posted_at": ISO date string or ""
      "ats":       adapter name
    }

Every adapter uses a public, unauthenticated JSON endpoint, so there is no
HTML parsing to break. If an adapter raises, run.py records the company as
"scrape_failed" and does NOT mark its previously-seen roles as closed.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable

import requests

MAX_TRIES = 4  # set to 1 during detection probes so wrong guesses fail fast
UA = {"User-Agent": "Mozilla/5.0 (compatible; SyndeoJobSearch/0.1; +https://propertyandtechnologyjobs.com)"}
TIMEOUT = 25


def _retry(fn, url, tries=None):
    import time
    tries = tries or MAX_TRIES
    for i in range(tries):
        r = fn()
        if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
            wait = int(r.headers.get("Retry-After", "0") or 0) or (5 * (i + 1))
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return r


def _get(url: str, **kw) -> requests.Response:
    return _retry(lambda: requests.get(url, headers=UA, timeout=TIMEOUT, **kw), url)


def _post(url: str, payload: dict, **kw) -> requests.Response:
    return _retry(lambda: requests.post(url, headers={**UA, "Content-Type": "application/json"},
                                        json=payload, timeout=TIMEOUT, **kw), url)


def _ms_to_date(ms) -> str:
    try:
        return dt.datetime.utcfromtimestamp(int(ms) / 1000).date().isoformat()
    except Exception:
        return ""


def _date10(s) -> str:
    return (s or "")[:10]


# ---------------------------------------------------------------- Greenhouse
def greenhouse(slug: str, **_) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false").json()
    out = []
    for j in data.get("jobs", []):
        out.append({
            "job_key": f"greenhouse:{slug}:{j['id']}",
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": _date10(j.get("first_published") or j.get("updated_at")),
            "ats": "greenhouse",
        })
    return out


# --------------------------------------------------------------------- Lever
def lever(slug: str, **_) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "job_key": f"lever:{slug}:{j['id']}",
            "title": j.get("text", "").strip(),
            "location": cats.get("location", "") or ", ".join(j.get("allLocations", []) or []),
            "url": j.get("hostedUrl", ""),
            "posted_at": _ms_to_date(j.get("createdAt")),
            "ats": "lever",
        })
    return out


# --------------------------------------------------------------------- Ashby
def ashby(slug: str, **_) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false").json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({
            "job_key": f"ashby:{slug}:{j['id']}",
            "title": j.get("title", "").strip(),
            "location": j.get("location", "") or ", ".join(
                [s.get("location", "") for s in j.get("secondaryLocations", []) if s.get("location")]),
            "url": j.get("jobUrl", ""),
            "posted_at": _date10(j.get("publishedAt")),
            "ats": "ashby",
        })
    return out


# ------------------------------------------------------------------ Workable
def workable(slug: str, **_) -> list[dict]:
    out, token = [], None
    for _ in range(20):  # pagination guard
        payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            payload["token"] = token
        data = _post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", payload).json()
        for j in data.get("results", []):
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            if j.get("remote"):
                loc_s = ("Remote · " + loc_s) if loc_s else "Remote"
            out.append({
                "job_key": f"workable:{slug}:{j['shortcode']}",
                "title": j.get("title", "").strip(),
                "location": loc_s,
                "url": f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                "posted_at": _date10(j.get("published")),
                "ats": "workable",
            })
        token = data.get("nextPage")
        if not token:
            break
    return out


# ------------------------------------------------------------- SmartRecruiters
def smartrecruiters(slug: str, **_) -> list[dict]:
    out, offset = [], 0
    while True:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}").json()
        for j in data.get("content", []):
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            out.append({
                "job_key": f"smartrecruiters:{slug}:{j['id']}",
                "title": j.get("name", "").strip(),
                "location": loc_s,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "posted_at": _date10(j.get("releasedDate")),
                "ats": "smartrecruiters",
            })
        offset += 100
        if offset >= int(data.get("totalFound", 0)):
            break
    return out


# ------------------------------------------------------------------ BambooHR
def bamboohr(slug: str, **_) -> list[dict]:
    data = _get(f"https://{slug}.bamboohr.com/careers/list").json()
    out = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("state")] if x)
        if j.get("isRemote"):
            loc_s = ("Remote · " + loc_s) if loc_s else "Remote"
        out.append({
            "job_key": f"bamboohr:{slug}:{j['id']}",
            "title": j.get("jobOpeningName", "").strip(),
            "location": loc_s,
            "url": f"https://{slug}.bamboohr.com/careers/{j['id']}",
            "posted_at": _date10(j.get("datePosted")),
            "ats": "bamboohr",
        })
    return out


# -------------------------------------------------------------------- Breezy
def breezy(slug: str, **_) -> list[dict]:
    data = _get(f"https://{slug}.breezy.hr/json").json()
    out = []
    for j in data:
        loc = j.get("location") or {}
        out.append({
            "job_key": f"breezy:{slug}:{j['id']}",
            "title": j.get("name", "").strip(),
            "location": loc.get("name", ""),
            "url": j.get("url", f"https://{slug}.breezy.hr/p/{j['id']}"),
            "posted_at": _date10(j.get("published_date")),
            "ats": "breezy",
        })
    return out


# ----------------------------------------------------------------- Recruitee
def recruitee(slug: str, **_) -> list[dict]:
    data = _get(f"https://{slug}.recruitee.com/api/offers/").json()
    out = []
    for j in data.get("offers", []):
        out.append({
            "job_key": f"recruitee:{slug}:{j['id']}",
            "title": j.get("title", "").strip(),
            "location": j.get("location", ""),
            "url": j.get("careers_url", ""),
            "posted_at": _date10(j.get("published_at")),
            "ats": "recruitee",
        })
    return out


# ------------------------------------------------------------------ Rippling
def rippling(slug: str, **_) -> list[dict]:
    data = _get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs").json()
    if isinstance(data, dict):  # some boards wrap the list
        data = data.get("items") or data.get("jobs") or data.get("results") or []
    out = []
    for j in data:
        jid = j.get("uuid") or j.get("id") or j.get("jobId") or j.get("url", "")
        if not jid:
            continue
        loc = j.get("workLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        out.append({
            "job_key": f"rippling:{slug}:{jid}",
            "title": (j.get("name") or j.get("title") or "").strip(),
            "location": loc.get("label", "") if isinstance(loc, dict) else str(loc),
            "url": j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{jid}",
            "posted_at": "",
            "ats": "rippling",
        })
    return out


# ------------------------------------------------------------------- Workday
def workday(slug: str, wd: str = "wd1", site: str = "", **_) -> list[dict]:
    """slug = tenant (e.g. 'costar'), wd = 'wd1'..'wd5', site = external site name (e.g. 'External')."""
    if not site:
        raise ValueError("workday adapter needs `site` in ats_map (e.g. 'External')")
    base = f"https://{slug}.{wd}.myworkdayjobs.com"
    out, offset, total = [], 0, None
    while True:
        data = _post(f"{base}/wday/cxs/{slug}/{site}/jobs",
                     {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}).json()
        for j in data.get("jobPostings", []):
            path = j.get("externalPath", "")
            if not path or not (j.get("title") or "").strip():
                continue  # Workday occasionally returns placeholder rows with no job behind them
            out.append({
                "job_key": f"workday:{slug}:{path}",
                "title": j.get("title", "").strip(),
                "location": j.get("locationsText", ""),
                "url": f"{base}/{site}{path}",
                "posted_at": "",  # Workday gives relative "Posted 3 Days Ago"; resolved in run.py
                "ats": "workday",
                "_posted_rel": j.get("postedOn", ""),
            })
        if total is None:  # Workday only reports total on the first page (later pages say 0)
            total = int(data.get("total", 0) or 0)
        offset += 20
        if not data.get("jobPostings") or offset >= total or offset > 2000:
            break
    return out


# ------------------------------------------------------------------- Jobvite
def jobvite(slug: str, **_) -> list[dict]:
    """Jobvite has no public JSON list endpoint, but its board is server-rendered.
    Verified 2026-09-23 against jobs.jobvite.com/appfolio-internal/jobs."""
    import re
    html = _get(f"https://jobs.jobvite.com/{slug}/jobs").text
    out, seen = [], set()
    # rows look like: <a href="/appfolio-internal/job/oltHAfwP">Title</a> ... <td>Location</td>
    for m in re.finditer(r'href="(?:https://jobs\.jobvite\.com)?/' + re.escape(slug) +
                         r'/job/([A-Za-z0-9]+)"[^>]*>(.*?)</a>(?:.*?<td[^>]*>(.*?)</td>)?', html, re.S):
        jid, title, loc = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2)), re.sub(r"<[^>]+>", " ", m.group(3) or "")
        if jid in seen:
            continue
        seen.add(jid)
        out.append({"job_key": f"jobvite:{slug}:{jid}", "title": " ".join(title.split()),
                    "location": " ".join(loc.split()), "url": f"https://jobs.jobvite.com/{slug}/job/{jid}",
                    "posted_at": "", "ats": "jobvite"})
    return out


# ----------------------------------------------------------- Generic fallback
def generic(slug: str, careers_url: str = "", **_) -> list[dict]:
    """Last-resort HTML link scrape. Only catches server-rendered boards.
    Keep companies here flagged for manual ATS mapping."""
    import re
    from urllib.parse import urljoin
    html = _get(careers_url).text
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        text = " ".join(text.split())
        if not text or len(text) < 6 or len(text) > 90:
            continue
        if not re.search(r"/(job|jobs|career|careers|position|opening|posting)s?/", href, re.I):
            continue
        url = urljoin(careers_url, href)
        if url in seen:
            continue
        seen.add(url)
        out.append({"job_key": f"generic:{slug}:{url}", "title": text, "location": "",
                    "url": url, "posted_at": "", "ats": "generic"})
    return out


ADAPTERS: dict[str, Callable[..., list[dict]]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "bamboohr": bamboohr,
    "breezy": breezy,
    "recruitee": recruitee,
    "rippling": rippling,
    "workday": workday,
    "jobvite": jobvite,
    "generic": generic,
}
