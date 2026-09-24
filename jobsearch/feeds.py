"""Multi-company job feeds: VC portfolio job boards (Getro).

Getro powers the portfolio job boards of MetaProp (jobs.metaprop.com) and Fifth Wall (jobs.fifthwall.com),
both verified 2026-09-24. One reader covers every Getro board.

getro(board_url) -> list of {id, title, location, url, posted_at, org_name, org_domain, board}
"""
from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urlparse

from .adapters import _get, _post


def _walk(obj):
    """Yield every dict inside a nested JSON structure."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


def _is_job(d: dict) -> bool:
    return isinstance(d.get("title"), str) and isinstance(d.get("organization"), dict) and "id" in d


def _norm_job(j: dict, base: str, board: str) -> dict:
    org = j.get("organization") or {}
    locs = j.get("locations") or j.get("searchable_locations") or []
    if isinstance(locs, list):
        locs = "; ".join(l if isinstance(l, str) else (l.get("name") or "") for l in locs)
    created = j.get("created_at") or j.get("createdAt") or ""
    if isinstance(created, (int, float)):
        created = dt.datetime.fromtimestamp(created if created < 1e11 else created / 1000, dt.timezone.utc).date().isoformat()
    slug = j.get("slug") or ""
    org_slug = org.get("slug") or ""
    url = f"{base}/companies/{org_slug}/jobs/{j['id']}-{slug}" if org_slug and slug else (j.get("url") or "")
    return {"id": str(j["id"]), "title": (j.get("title") or "").strip(), "location": str(locs or ""),
            "url": url, "apply_url": j.get("url") or "", "posted_at": str(created)[:10],
            "org_name": org.get("name") or "", "org_domain": (org.get("domain") or org.get("website") or ""),
            "board": board}


def getro(board_url: str, board: str, max_pages: int = 40) -> tuple[list[dict], str]:
    """Returns (jobs, note). note says how the jobs were read, so partial reads are visible in the log."""
    p = urlparse(board_url if board_url.startswith("http") else "https://" + board_url)
    base = f"{p.scheme}://{p.netloc}"
    html = _get(f"{base}/jobs").text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(f"{board}: no __NEXT_DATA__ on {base}/jobs (board layout changed?)")
    data = json.loads(m.group(1))
    first_page = [d for d in _walk(data) if _is_job(d)]
    # The board's network/collection id is what the search API is keyed on
    net_id = None
    for d in _walk(data):
        n = d.get("network")
        if isinstance(n, dict) and n.get("id"):
            net_id = n["id"]
            break
        if d.get("collectionId"):
            net_id = d["collectionId"]
            break
    jobs, note = [], ""
    if net_id:
        try:
            for page in range(max_pages):
                r = _post(f"https://api.getro.com/api/v2/collections/{net_id}/search/jobs",
                          {"hitsPerPage": 100, "page": page, "filters": {}, "query": ""}).json()
                res = r.get("results") or r
                batch = res.get("jobs") or []
                jobs += batch
                total = int(res.get("count") or res.get("total") or 0)
                if not batch or len(jobs) >= total:
                    break
            note = f"search API, {len(jobs)} jobs"
        except Exception as e:
            jobs, note = [], f"search API failed ({type(e).__name__}: {str(e)[:80]})"
    if not jobs:
        jobs = first_page
        note = (note + "; " if note else "") + f"FIRST PAGE ONLY ({len(jobs)} jobs) - board shows more"
    seen, out = set(), []
    for j in jobs:
        if str(j.get("id")) in seen:
            continue
        seen.add(str(j.get("id")))
        out.append(_norm_job(j, base, board))
    return out, note
