"""Polite HTTP client shared by every source.

Rules baked in here because every source needs them:
  - a descriptive User-Agent (SEC *requires* one and will block you without it)
  - per-host rate limiting so we never hammer a publisher
  - retries on transient 5xx / connection errors, but never on 403/404
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Optional

import requests

log = logging.getLogger(__name__)

# SEC fair-access policy asks for contact info in the UA string.
# Override with PEOPLE_MOVES_UA if the contact address changes.
DEFAULT_UA = os.environ.get(
    "PEOPLE_MOVES_UA",
    "Syndeo People-Moves Research (hello@syndeollc.com)",
)

# Minimum seconds between requests to the same host.
HOST_DELAY = {
    "www.sec.gov": 0.15,
    "data.sec.gov": 0.15,
    "efts.sec.gov": 0.15,
    "web.archive.org": 1.0,
    "news.google.com": 1.0,
}
DEFAULT_DELAY = 0.75

_last_hit: dict[str, float] = defaultdict(float)


def _throttle(host: str) -> None:
    delay = HOST_DELAY.get(host, DEFAULT_DELAY)
    elapsed = time.time() - _last_hit[host]
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_hit[host] = time.time()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept-Encoding": "gzip, deflate",
        }
    )
    return s


SESSION = make_session()


def get(
    url: str,
    *,
    timeout: int = 25,
    retries: int = 3,
    accept: Optional[str] = None,
) -> Optional[requests.Response]:
    """GET a URL, returning None on permanent failure rather than raising.

    Sources are expected to degrade gracefully: one dead careers page must
    never take down the whole weekly run.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    headers = {"Accept": accept} if accept else {}

    for attempt in range(1, retries + 1):
        _throttle(host)
        try:
            r = SESSION.get(url, timeout=timeout, headers=headers)
        except requests.RequestException as exc:
            log.warning("  %s -> connection error (%s), attempt %d", url, exc, attempt)
            time.sleep(2 * attempt)
            continue

        if r.status_code == 200:
            return r

        # Permanent: don't burn retries on these.
        if r.status_code in (401, 403, 404, 410):
            log.warning("  %s -> HTTP %d (permanent)", url, r.status_code)
            return None

        # 429 / 5xx: back off and try again.
        log.warning("  %s -> HTTP %d, attempt %d", url, r.status_code, attempt)
        time.sleep(3 * attempt)

    return None
