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
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).date().isoformat()
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
def _ashby_graphql(slug: str) -> list[dict]:
    """Same feed jobs.ashbyhq.com/<slug> uses to render its page. Used when the posting API 404s
    (seen for Footprint / onefootprint on 2026-09-24 while its job pages were live)."""
    q = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: "
         "jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) "
         "{ jobPostings { id title locationName } } }")
    data = _post("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
                 {"operationName": "ApiJobBoardWithTeams",
                  "variables": {"organizationHostedJobsPageName": slug}, "query": q}).json()
    if (data or {}).get("errors"):
        raise RuntimeError(f"Ashby GraphQL error: {str(data['errors'])[:160]}")
    board = ((data or {}).get("data") or {}).get("jobBoard")
    if not board:
        raise RuntimeError(f"Ashby has no job board named '{slug}' (GraphQL returned none)")
    return [{"id": j["id"], "title": j.get("title", ""), "location": j.get("locationName", ""),
             "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{j['id']}", "publishedAt": ""}
            for j in board.get("jobPostings", []) or []]


def _ashby_page(slug: str) -> list[dict]:
    """Read the postings embedded in jobs.ashbyhq.com/<slug> (window.__appData)."""
    import json as _json, re as _re
    html = _get(f"https://jobs.ashbyhq.com/{slug}").text
    m = _re.search(r"window\.__appData\s*=\s*(\{.*?\});\s*</script>", html, _re.S)
    if not m:
        raise RuntimeError(f"Ashby page for '{slug}' has no embedded job data")
    app = _json.loads(m.group(1))
    posts = ((app.get("jobBoard") or {}).get("jobPostings")) or []
    return [{"id": j["id"], "title": j.get("title", ""), "location": j.get("locationName", ""),
             "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{j['id']}", "publishedAt": j.get("publishedDate", "") or ""}
            for j in posts]


def ashby(slug: str, **_) -> list[dict]:
    try:
        data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false").json()
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        errs = []
        for fn in (_ashby_graphql, _ashby_page):
            try:
                data = {"jobs": fn(slug)}
                break
            except Exception as ex:  # try the next way in; report all reasons if every one fails
                errs.append(f"{fn.__name__}: {ex}")
        else:
            raise RuntimeError("Ashby posting API 404; " + " | ".join(errs))
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
def generic(slug: str, careers_url: str = "", link_regex: str = "", title_from_slug: bool = False, **_) -> list[dict]:
    r"""HTML link scrape for companies with no ATS (custom careers pages, YC job pages).
    link_regex (from ats_map.json) pins exactly which links are job postings, e.g.
      Reffie: r"https://careers\.reffie\.me/[a-z0-9-]+-[a-z0-9-]+$"
      Haven (YC): r"/companies/haven-2/jobs/[A-Za-z0-9]+-[a-z0-9-]+"
    Without link_regex it falls back to common /job(s)/ /career(s)/ URL shapes."""
    import re
    from urllib.parse import urljoin, urlparse
    html = _get(careers_url).text
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        href, inner = m.group(1), m.group(2)
        url = urljoin(careers_url, href).split("#")[0]
        if link_regex:
            if not re.search(link_regex, url):
                continue
        elif not re.search(r"/(job|jobs|career|careers|position|opening|posting)s?/", href, re.I):
            continue
        if url in seen or url.rstrip("/") == careers_url.rstrip("/"):
            continue
        if title_from_slug:
            last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
            text = " ".join(w.capitalize() for w in last.split("-"))
        else:
            text = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        if not text or len(text) < 4 or len(text) > 120:
            continue
        seen.add(url)
        out.append({"job_key": f"generic:{slug}:{url}", "title": text, "location": "",
                    "url": url, "posted_at": "", "ats": "generic"})
    return out


# ------------------------------------------------------------------ Built In
def builtin(slug: str, website: str = "", **_) -> list[dict]:
    """builtin.com/company/<slug>/jobs - server-rendered list of the company's recent postings.
    Stops at 'Jobs at similar companies' so other companies' jobs never leak in."""
    import re
    html = _get(f"https://builtin.com/company/{slug}/jobs").text
    cut = re.search(r"(?i)jobs at similar companies", html)
    own = html[:cut.start()] if cut else html
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(?:https://builtin\.com)?(/job/[^"/]+/(\d+))"[^>]*>(.*?)</a>', own, re.S):
        path, jid, inner = m.groups()
        title = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        if not title or jid in seen:
            continue
        seen.add(jid)
        # location sits in the card right after the title; take the first short text chunk mentioning a place
        tail = re.sub(r"<[^>]+>", "|", own[m.end():m.end() + 1500])
        bits = [b.strip() for b in tail.split("|") if b.strip()]
        loc = ", ".join(b for b in bits[:8] if re.search(r"(?i)remote|hybrid|in-office|united states|usa|locations|, [A-Z]{2}\b", b))[:120]
        out.append({"job_key": f"builtin:{slug}:{jid}", "title": title, "location": loc,
                    "url": f"https://builtin.com{path}", "posted_at": "", "ats": "builtin"})
    return out


def builtin_page_matches(slug: str, website: str) -> bool:
    """True if builtin.com/company/<slug> exists AND links to this company's own website domain."""
    from urllib.parse import urlparse
    try:
        r = requests.get(f"https://builtin.com/company/{slug}/jobs", headers=UA, timeout=TIMEOUT)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    host = urlparse(website if "://" in website else "https://" + website).netloc.lower().replace("www.", "")
    return bool(host) and host in r.text.lower()


# ------------------------------------------------------------------ LinkedIn
def linkedin(slug: str, company_name: str = "", linkedin_pages=None, **_) -> list[dict]:
    """For companies with no job board. Uses LinkedIn's public (no-login) job search, searching by
    company name, and keeps ONLY jobs posted by the exact LinkedIn company page(s) listed in ats_map.json.
    Name collisions ('Boom', 'Mason') can't leak in: another company's page never matches."""
    import re
    from urllib.parse import quote
    pages = {p.strip("/").lower() for p in (linkedin_pages or [slug]) if p}
    out, seen = [], set()
    for start in (0, 25):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote(company_name or slug)}&location=United%20States&start={start}")
        html = _get(url).text
        cards = re.split(r"<li[\s>]", html)[1:]
        for c in cards:
            co = re.search(r'linkedin\.com/company/([^/?"]+)', c)
            if not co or co.group(1).lower() not in pages:
                continue
            jid = re.search(r"jobPosting:(\d+)", c) or re.search(r"/jobs/view/[^\"]*?-(\d+)\?", c)
            if not jid or jid.group(1) in seen:
                continue
            seen.add(jid.group(1))
            t = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', c, re.S)
            loc = re.search(r'job-search-card__location[^>]*>(.*?)</span>', c, re.S)
            dt_ = re.search(r'<time[^>]+datetime="([\d-]+)"', c)
            clean = lambda x: " ".join(re.sub(r"<[^>]+>", " ", x or "").split())
            out.append({"job_key": f"linkedin:{slug}:{jid.group(1)}",
                        "title": clean(t.group(1) if t else ""), "location": clean(loc.group(1) if loc else ""),
                        "url": f"https://www.linkedin.com/jobs/view/{jid.group(1)}/",
                        "posted_at": dt_.group(1) if dt_ else "", "ats": "linkedin"})
        if len(cards) < 25:
            break
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
    "linkedin": linkedin,
    "builtin": builtin,
    "generic": generic,
}
