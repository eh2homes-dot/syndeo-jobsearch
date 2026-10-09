"""Polite HTTP client shared by every job search in this repo.

Each source is a different host (a dozen job systems plus every company's own
careers page), so throttling is per host: no single provider's rate limit can
govern a whole run.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger("jobsearch.http")

UA = "Mozilla/5.0 (compatible; SyndeoJobSearch/1.0; +mailto:hello@syndeollc.com)"
DEFAULT_DELAY = 0.8
RETRIES = 3            # set to 1 for a cheap probe
_last: dict[str, float] = defaultdict(float)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})


class FetchError(Exception):
    """Raised with a short, human-readable reason for the coverage report."""


class NotFound(FetchError):
    """The address doesn't exist (HTTP 404/410): a wrong or retired board, not a blip."""


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    wait = DEFAULT_DELAY - (time.time() - _last[host])
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def _request(method: str, url: str, *, json_body=None, accept: Optional[str] = None,
             timeout: int = 25, retries: Optional[int] = None,
             headers: Optional[dict] = None) -> requests.Response:
    headers = dict(headers or {})
    if accept:
        headers["Accept"] = accept
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    retries = retries or RETRIES

    last_reason = "no response"
    for attempt in range(1, retries + 1):
        _throttle(url)
        try:
            r = SESSION.request(method, url, json=json_body, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_reason = f"connection error ({type(exc).__name__})"
            if attempt < retries:
                time.sleep(2 * attempt)
            continue

        if r.status_code == 200:
            return r

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 5 * attempt
            last_reason = f"HTTP 429 rate limited (waited {delay}s)"
            if attempt < retries:
                time.sleep(min(delay, 60))
            continue

        if r.status_code in (404, 410):
            raise NotFound(f"HTTP {r.status_code}")
        if r.status_code in (401, 403):
            body = r.text[:300].lower()
            if r.status_code == 403 and ("cloudflare" in body or "just a moment" in body):
                raise FetchError("HTTP 403 bot protection (Cloudflare)")
            raise FetchError(f"HTTP {r.status_code}")

        last_reason = f"HTTP {r.status_code}"
        if attempt < retries:
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
