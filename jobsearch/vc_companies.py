"""Dump every portfolio company listed on the VC job boards in config.json -> vc_boards.

    python -m jobsearch.vc_companies            # writes leads/vc_portfolio_companies.csv
    python -m jobsearch.vc_companies --board "Fifth Wall"

The boards (MetaProp, Fifth Wall, RET Ventures, Camber Creek) are Getro sites. Each lists its
portfolio companies with a domain, a headcount bucket and, often, a stage. That is exactly the
small-company list the founder-led hiring draft needs and the master leads list lacks.

Output columns: company, domain, headcount, stage, industry_tags, locations, board, board_url,
open_jobs. A company on several boards gets one row with the boards joined by " | ".

Nothing is enriched here; this is the seed list. Pass it through Clay (or fill by hand) and copy
the keepers into leads/founders.csv and leads/master_leads.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import adapters as _ad
from . import feeds as feeds_mod
from . import run

DEBUG_PATH = run.ROOT / "out" / "vc_companies_debug.txt"


def _diag(board: str, msg: str) -> None:
    print(f"  vc  {board:14} {msg}", flush=True)
    DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEBUG_PATH, "a") as f:
        f.write(f"[{board}] {msg}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"- {board}: {msg}\n")


OUT_CSV = run.ROOT / "leads" / "vc_portfolio_companies.csv"
HEADCOUNT_BUCKETS = {"1": "1-10", "2": "11-50", "3": "51-200", "4": "201-500", "5": "501-1,000",
                     "6": "1,001-5,000", "7": "5,001-10,000", "8": "10,001+"}
COLS = ["company", "domain", "headcount", "stage", "industry_tags", "locations", "board", "board_url", "open_jobs"]


def _network_id(base: str) -> tuple[int | None, dict]:
    m = None
    for path in ("/companies", "/jobs", "/"):
        try:
            html = feeds_mod._get(f"{base}{path}").text
        except Exception:
            continue
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            break
    if not m:
        return None, {}
    data = json.loads(m.group(1))
    props = (data.get("props") or {}).get("pageProps") or {}
    net = props.get("network") or {}
    if isinstance(net, dict) and net.get("id"):
        return int(net["id"]), data
    for d in feeds_mod._walk(data):
        n = d.get("network")
        if isinstance(n, dict) and n.get("id"):
            return int(n["id"]), data
        if d.get("collectionId"):
            return int(d["collectionId"]), data
    return None, data


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _norm_org(o: dict, board: str, board_url: str) -> dict:
    domain = _first(o, "domain", "website", "url")
    if isinstance(domain, str) and domain.startswith("http"):
        domain = urlparse(domain).netloc
    domain = (domain or "").lower().replace("www.", "")
    hc = _first(o, "headCount", "head_count", "headcount", "size", "employee_count", "company_size")
    if isinstance(hc, dict):
        hc = _first(hc, "name", "label", "value")
    hc = HEADCOUNT_BUCKETS.get(str(hc), hc)
    stage = _first(o, "stage", "funding_stage", "company_stage")
    if isinstance(stage, dict):
        stage = _first(stage, "name", "label", "value")
    tags = _first(o, "industryTags", "industry_tags", "visibleIndustryTags", "industries", "topics", "tags", default=[])
    if isinstance(tags, list):
        tags = "; ".join(t.get("name", t) if isinstance(t, dict) else str(t) for t in tags)
    locs = _first(o, "locations", "location", default=[])
    if isinstance(locs, list):
        locs = "; ".join(l.get("name", l) if isinstance(l, dict) else str(l) for l in locs)
    jobs = _first(o, "activeJobsCount", "active_jobs_count", "jobs_count", "open_jobs", "job_count", default="")
    return {"company": _first(o, "name", "title"), "domain": domain, "headcount": str(hc or ""),
            "stage": str(stage or ""), "industry_tags": str(tags or ""), "locations": str(locs or ""),
            "board": board, "board_url": board_url, "open_jobs": str(jobs)}


def read_board(board_url: str, board: str, max_pages: int = 60) -> tuple[list[dict], str]:
    p = urlparse(board_url if board_url.startswith("http") else "https://" + board_url)
    base = f"{p.scheme}://{p.netloc}"
    net_id, data = _network_id(base)
    orgs: list[dict] = []
    note = ""
    if data:
        props = (data.get("props") or {}).get("pageProps") or {}
        net = props.get("network") or {}
        _diag(board, "network: " + json.dumps({k: net.get(k) for k in ("id", "name", "slug", "collection_id", "collectionId") if k in net}))
        st = props.get("initialState") or {}
        for key in ("allCompanies", "companies"):
            v = st.get(key) if isinstance(st, dict) else None
            if isinstance(v, dict):
                _diag(board, f"initialState.{key}: dict keys " + ", ".join(sorted(v.keys()))[:300])
                for k2, v2 in v.items():
                    if isinstance(v2, list) and v2 and isinstance(v2[0], dict):
                        _diag(board, f"initialState.{key}.{k2}: list[{len(v2)}], item keys " + ", ".join(sorted(v2[0].keys()))[:400])
            elif isinstance(v, list):
                _diag(board, f"initialState.{key}: list[{len(v)}]" + (", item keys " + ", ".join(sorted(v[0].keys()))[:400] if v and isinstance(v[0], dict) else ""))
            else:
                _diag(board, f"initialState.{key}: {type(v).__name__}")
    else:
        _diag(board, "no __NEXT_DATA__ on /companies or /jobs")
    if net_id:
        host = p.netloc
        endpoints = [
            ("POST", f"https://api.getro.com/api/v2/collections/{net_id}/search/companies"),
            ("POST", f"https://api.getro.com/api/v2/collections/{net_id}/search/organizations"),
            ("POST", f"https://api.getro.com/api/v2/networks/{net_id}/search/companies"),
            ("GET", f"https://api.getro.com/api/v2/collections/{net_id}/companies?per_page=100&page=0"),
            ("GET", f"https://api.getro.com/api/v2/networks/{net_id}/organizations?per_page=100&page=0"),
            ("GET", f"https://{host}/api/v2/collections/{net_id}/companies?per_page=100&page=0"),
        ]
        for method, url in endpoints:
            try:
                for page in range(max_pages):
                    u = url.replace("page=0", f"page={page}") if method == "GET" else url
                    hdrs = {**_ad.UA, "Accept": "application/json", "Content-Type": "application/json",
                            "Origin": base, "Referer": f"{base}/companies"}
                    if method == "POST":
                        resp = _ad._retry(lambda: requests.post(u, headers=hdrs, timeout=_ad.TIMEOUT,
                                                                json={"hitsPerPage": 100, "page": page, "filters": {}, "query": ""}), u)
                    else:
                        resp = _ad._retry(lambda: requests.get(u, headers=hdrs, timeout=_ad.TIMEOUT), u)
                    if page == 0:
                        _diag(board, f"{method} {u} -> {resp.status_code} body[:300]={resp.text[:300]!r}")
                    r = resp.json()
                    res = r.get("results") or r
                    batch = []
                    if isinstance(res, dict):
                        for k in ("companies", "organizations", "hits", "items", "found", "data"):
                            if isinstance(res.get(k), list):
                                batch = res[k]
                                break
                    elif isinstance(res, list):
                        batch = res
                    orgs += batch
                    total = int(res.get("count") or res.get("total") or res.get("nbHits") or 0) if isinstance(res, dict) else 0
                    if not batch or (total and len(orgs) >= total):
                        break
                if orgs:
                    note = f"{method} {url.split('/api/')[1].split('?')[0]}, {len(orgs)} companies"
                    break
            except Exception as e:
                _diag(board, f"{method} {u} failed ({type(e).__name__}: {str(e)[:120]})")
                orgs = []
    if not orgs and data:
        st = ((data.get("props") or {}).get("pageProps") or {}).get("initialState") or {}
        for key in ("allCompanies", "companies"):
            v = st.get(key) if isinstance(st, dict) else None
            cands = []
            if isinstance(v, list):
                cands = v
            elif isinstance(v, dict):
                for v2 in v.values():
                    if isinstance(v2, list) and v2 and isinstance(v2[0], dict) and v2[0].get("name"):
                        cands = v2 if len(v2) > len(cands) else cands
            cands = [c for c in cands if isinstance(c, dict) and c.get("name")]
            if len(cands) > len(orgs):
                orgs = cands
                note = f"initialState.{key}, {len(orgs)} companies"
    if not orgs:
        # Fall back to the server-rendered directory: /companies?page=N, each page a dozen or so cards.
        seen, pages_read = set(), 0
        for page in range(1, max_pages + 1):
            try:
                html = feeds_mod._get(f"{base}/companies?page={page}").text
            except Exception as e:
                _diag(board, f"/companies?page={page} failed: {type(e).__name__}")
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            if not m:
                break
            found = 0
            for d in feeds_mod._walk(json.loads(m.group(1))):
                if isinstance(d, dict) and d.get("name") and (d.get("domain") or d.get("website")) \
                        and any(k in d for k in ("headCount", "head_count", "headcount", "stage", "industryTags", "activeJobsCount", "slug")):
                    key = str(d.get("id") or d.get("slug") or d.get("name"))
                    if key not in seen:
                        seen.add(key)
                        orgs.append(d)
                        found += 1
            pages_read += 1
            if found == 0:
                break
        note = (note + "; " if note else "") + f"directory pages ({pages_read} pages, {len(orgs)} companies)"
    seen_ids, out = set(), []
    for o in orgs:
        key = str(o.get("id") or o.get("slug") or o.get("name"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        out.append(_norm_org(o, board, base))
    return out, note


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="", help="only this board (name from config.json)")
    ap.add_argument("--out", default=str(OUT_CSV))
    ap.add_argument("--count-jobs", action="store_true", help="also read each board's jobs with feeds.getro and log the count")
    args = ap.parse_args(argv)
    cfg = run.load_json(run.CONFIG, {})
    boards = [b for b in cfg.get("vc_boards", []) if not args.board or b["name"].lower() == args.board.lower()]
    merged: dict[str, dict] = {}
    for b in boards:
        try:
            rows, note = read_board(b["url"], b["name"])
        except Exception as e:
            _diag(b["name"], f"FAILED: {type(e).__name__}: {str(e)[:200]}")
            continue
        print(f"  vc  {b['name']:14} {note}", flush=True)
        for r in rows:
            k = r["domain"] or run._norm_co(r["company"])
            if k in merged:
                m = merged[k]
                m["board"] = " | ".join(sorted(set(m["board"].split(" | ")) | {r["board"]}))
                for c in ("headcount", "stage", "industry_tags", "locations", "open_jobs"):
                    m[c] = m[c] or r[c]
            else:
                merged[k] = r
    if args.count_jobs:
        for b in boards:
            try:
                vjobs, jnote = feeds_mod.getro(b["url"], b["name"])
                _diag(b["name"], f"jobs reader: {len(vjobs)} jobs ({jnote})")
            except Exception as e:
                _diag(b["name"], f"jobs reader FAILED: {type(e).__name__}: {str(e)[:160]}")
    rows = sorted(merged.values(), key=lambda r: r["company"].lower())
    run.write_csv(Path(args.out), rows, COLS)
    print(f"  {len(rows)} companies -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
