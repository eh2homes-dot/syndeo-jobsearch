"""Link verification.

Every run, each posting URL (open + newly closed) is fetched and classified:
  live      200 and no "gone" markers
  gone      404/410, a "no longer available"-style page, or a redirect away from the job
  blocked   403/429 — site refuses bots; not evidence either way
  error     network/timeout

Two jobs:
  1. open roles  -> link_status column, so every link in the report has been checked this run
  2. closed roles -> if the posting is still LIVE, the role wasn't filled; the scraper missed it.
     It's reopened in history and reported as a scraper miss, not a hire.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from .adapters import UA

GONE_MARKERS = re.compile(
    r"no longer (exists|available|accepting|open|active)|job (is )?(not found|closed|expired)|"
    r"position (has been )?(filled|closed)|posting (has )?(closed|expired)|page not found|"
    r"this job (has|is) (been )?(closed|filled|expired)|couldn.t find (that|this) job|"
    r"job you are looking for|no open positions", re.I)


# Unambiguous "this posting is gone" notices that job sites show at the top of an otherwise full page.
STRONG_GONE = re.compile(r"sorry, this job was removed|this job (post(ing)?|listing) (has been|was) removed|"
                         r"no longer accepting applications", re.I)


def _job_token(url: str) -> str:
    """The piece of the URL that identifies the specific job (id / gh_jid / last path segment)."""
    m = re.search(r"gh_jid=(\d+)", url)
    if m:
        return m.group(1)
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def check(url: str) -> tuple[str, int]:
    if not url:
        return "error", 0
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
    except Exception:
        return "error", 0
    code = r.status_code
    if code in (403, 429, 999):
        return "blocked", code
    if code in (404, 410) or code >= 500:
        return ("gone" if code in (404, 410) else "error"), code
    tok = _job_token(url)
    # Greenhouse/Lever/etc. redirect a closed job to the board root; the job id disappears from the URL
    if tok and len(tok) > 3 and tok not in r.url and "error=true" in r.url.lower():
        return "gone", code
    if "error=true" in r.url.lower():
        return "gone", code
    visible = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", r.text[:600_000])
    visible = " ".join(re.sub(r"<[^>]+>", " ", visible).split())
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text[:50_000])
    if title and GONE_MARKERS.search(title.group(1)):
        return "gone", code
    # A "this job is closed" page is short. A live posting is a long description that can innocently
    # contain phrases like "no longer accepting..." in the company blurb - don't treat those as closed.
    if STRONG_GONE.search(visible[:3000]):
        return "gone", code
    if len(visible) < 4000 and GONE_MARKERS.search(visible):
        return "gone", code
    return "live", code


_HOST_LOCKS: dict = {}


def _check_throttled(url: str):
    import threading
    host = urlparse(url).netloc
    sem = _HOST_LOCKS.setdefault(host, threading.Semaphore(4))  # max 4 in flight per site
    with sem:
        return check(url)


def check_many(urls: list[str], workers: int = 24) -> dict[str, tuple[str, int]]:
    urls = list(dict.fromkeys(u for u in urls if u))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(urls, ex.map(_check_throttled, urls)))
