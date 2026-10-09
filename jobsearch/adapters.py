"""Adapters for the weekly search.

The job-system readers themselves live in jobsearch/boards.py and are shared
with the OpCo search. This module is the weekly search's view of them: each
adapter takes a slug (plus extra config from data/ats_map.json) and returns
job dicts in the shape run.py has always used:

    {
      "job_key":   stable unique id  (ats:slug:id)
      "title":     str
      "location":  str
      "url":       direct link to the posting
      "posted_at": ISO date string or ""
      "ats":       adapter name
    }

job_key is built from the job system's own id, in the same format as before
the readers were shared, so data/history.json carries straight over.

If an adapter raises, run.py records the company as failed and does NOT mark
its previously-seen roles as closed.

Two adapters stay here because only the weekly search uses them: `generic`
(a careers page with a hand-written link pattern in ats_map.json) and
`linkedin` (the public job search, for companies with no job board at all).
Built In is no longer a source: every company is read from its own job board.
"""
from __future__ import annotations

from typing import Callable

import requests

from . import boards as _boards
from .boards import JobList

MAX_TRIES = 4  # set to 1 for a cheap probe
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


def to_weekly(job: dict, slug: str = "") -> dict:
    """A shared-reader job in the weekly search's shape."""
    if slug and not job.get("slug"):
        job = {**job, "slug": slug}
    out = {"job_key": _boards.weekly_key(job), "title": job["title"].strip(),
           "location": job.get("location", ""), "url": job.get("url", ""),
           "posted_at": job.get("posted", ""), "ats": job["system"]}
    if job.get("posted_rel"):
        out["_posted_rel"] = job["posted_rel"]   # Workday's "Posted 3 Days Ago"; resolved in run.py
    return out


_READER_PARAMS = ("host", "site", "wd", "board", "cc", "origin", "key", "search_terms", "max_pages")


def _shared(system: str) -> Callable[..., list]:
    def adapter(slug: str, **kw) -> list:
        params = {k: kw[k] for k in _READER_PARAMS if kw.get(k)}
        if system == "workday":
            params.setdefault("max_pages", 100)   # 2,000 postings, as before
        jobs = _boards.READERS[system](slug, **params)
        out = JobList(to_weekly(j) for j in jobs if j["title"].strip())
        out.truncated = getattr(jobs, "truncated", "")
        return out
    adapter.__name__ = system
    return adapter


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



ADAPTERS: dict[str, Callable[..., list]] = {name: _shared(name) for name in _boards.READERS}
ADAPTERS.update({"linkedin": linkedin, "generic": generic})
