"""Detect which ATS a careers page uses, and the slug.

Strategy (cheap → expensive):
  1. Regex the careers page HTML (and its final redirect URL) for ATS hostnames.
  2. If nothing found, probe the well-known ATS APIs with slug guesses derived
     from the company domain (e.g. 'appfolio', 'appfolioinc', 'appfolio-com').

Returns {"ats": name, "slug": slug, "confidence": "html"|"probe"|"none", "evidence": str}
Anything with confidence "none" is written to data/needs_manual_mapping.csv.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from . import adapters as _ad
from .adapters import ADAPTERS, UA, TIMEOUT

_RATE_LIMITED: set[str] = set()  # ATS APIs that returned 429 this run -> skip in later probes

# Order matters: more specific first.
PATTERNS = [
    ("greenhouse",     r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("greenhouse",     r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse",     r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever",          r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("lever",          r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)"),
    ("ashby",          r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
    ("ashby",          r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)"),
    ("workable",       r"apply\.workable\.com/(?:api/v\d/accounts/)?([A-Za-z0-9_-]+)"),
    ("smartrecruiters",r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters",r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)"),
    ("bamboohr",       r"https?://([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)"),
    ("breezy",         r"https?://([A-Za-z0-9-]+)\.breezy\.hr"),
    ("recruitee",      r"https?://([A-Za-z0-9-]+)\.recruitee\.com"),
    ("rippling",       r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("workday",        r"https?://([A-Za-z0-9-]+)\.(wd\d)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    ("jobvite",        r"jobs\.jobvite\.com/(?:careers/)?([A-Za-z0-9_-]+)"),
    ("jazzhr",         r"https?://([A-Za-z0-9-]+)\.applytojob\.com"),
    ("betterteam",     r"https?://([A-Za-z0-9-]+)\.betterteam\.com"),
    ("icims",          r"https?://([A-Za-z0-9-]+)\.icims\.com"),
    ("paylocity",      r"recruiting\.paylocity\.com/recruiting/jobs/All/([A-Za-z0-9-]+)"),
    ("ukg",            r"recruiting\.ultipro\.com/([A-Za-z0-9_-]+)"),
    ("adp",            r"workforcenow\.adp\.com"),
    ("paradox",        r"paradox\.ai"),
]

IGNORE_SLUGS = {"embed", "api", "v1", "v0", "widget", "job_board", "js", "accounts"}


def _domain_slugs(website: str) -> list[str]:
    host = urlparse(website).netloc.lower().replace("www.", "")
    base = host.split(".")[0]
    return list(dict.fromkeys([base, base.replace("-", ""), f"{base}inc", f"{base}hq", f"{base}-com", f"{base}careers"]))


def detect_from_html(html: str, final_url: str = "") -> dict | None:
    blob = (final_url or "") + "\n" + html
    if "gh_jid=" in blob and not re.search(r"greenhouse\.io/[A-Za-z]", blob):
        return {"ats": "greenhouse", "slug": "", "confidence": "html-needs-slug",
                "evidence": "gh_jid= links on own domain (Greenhouse embed); slug via probe"}
    for ats, pat in PATTERNS:
        m = re.search(pat, blob)
        if not m:
            continue
        if ats == "workday":
            return {"ats": ats, "slug": m.group(1), "wd": m.group(2), "site": m.group(3),
                    "confidence": "html", "evidence": m.group(0)}
        slug = m.group(1) if m.groups() else ""
        if slug in IGNORE_SLUGS:
            continue
        return {"ats": ats, "slug": slug, "confidence": "html", "evidence": m.group(0)}
    return None


def fetch_page(url: str) -> tuple[str, str]:
    r = requests.get(url, headers=UA, timeout=15, allow_redirects=True)
    return r.text, r.url


ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
             "bamboohr.com", "breezy.hr", "recruitee.com", "rippling.com", "myworkdayjobs.com", "jobvite.com")


def _root(host: str) -> str:
    parts = host.lower().replace("www.", "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def probe_is_same_company(jobs: list[dict], website: str) -> tuple[bool, str]:
    """A slug guess like 'evernest' or 'proof' can belong to a different company with the same name.
    Reject if any posting links to a non-ATS domain that isn't the lead's own domain."""
    own = _root(urlparse(website).netloc)
    foreign = set()
    for j in jobs:
        host = urlparse(j.get("url", "")).netloc
        if not host or any(h in host for h in ATS_HOSTS):
            continue
        if _root(host) != own:
            foreign.add(_root(host))
    if foreign:
        return False, f"postings link to {', '.join(sorted(foreign))}, not {own}"
    return True, ""


def probe(website: str) -> dict | None:
    """Try slug guesses against APIs that fail fast (Greenhouse, Lever, Ashby, Workable)."""
    rejected = []
    saved, _ad.MAX_TRIES = _ad.MAX_TRIES, 1
    try:
        return _probe(website, rejected)
    finally:
        _ad.MAX_TRIES = saved


def _probe(website: str, rejected: list) -> dict | None:
    for slug in _domain_slugs(website):
        for ats in ("greenhouse", "lever", "ashby"):  # Workable excluded: it rate-limits the runner IP
            if ats in _RATE_LIMITED:
                continue
            try:
                jobs = ADAPTERS[ats](slug)
            except Exception as e:
                if "429" in str(e):
                    _RATE_LIMITED.add(ats)
                continue
            if not jobs:
                continue
            ok, why = probe_is_same_company(jobs, website)
            if not ok:
                rejected.append(f"{ats}:{slug} rejected ({why})")
                continue
            return {"ats": ats, "slug": slug, "confidence": "probe",
                    "evidence": f"{ats}:{slug} returned {len(jobs)} jobs" + (f"; {'; '.join(rejected)}" if rejected else "")}
    if rejected:
        return {"ats": "", "slug": "", "confidence": "none", "evidence": "; ".join(rejected)}
    return None


def detect(company: dict) -> dict:
    """company = row from master_leads.csv"""
    try:
        html, final = fetch_page(company["careers_url"])
        hit = detect_from_html(html, final)
        if hit and hit.get("slug"):
            return hit
        if hit:  # greenhouse embed without slug -> probe for it
            p = probe(company["website"])
            if p and p["ats"] == "greenhouse":
                p["evidence"] = hit["evidence"] + " -> " + p["evidence"]
                return p
    except Exception as e:  # noqa
        html_err = str(e)[:120]
    else:
        html_err = "no ATS pattern in HTML"
    hit = probe(company["website"])
    if hit:
        return hit
    return {"ats": "", "slug": "", "confidence": "none", "evidence": html_err}
