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
COLS = ["company", "domain", "headcount", "stage", "industry_tags", "locations", "board", "board_url", "open_jobs"]


def _network_id(base: str) -> tuple[int | None, dict]:
    html = feeds_mod._get(f"{base}/companies").text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        html = feeds_mod._get(f"{base}/jobs").text
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
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
    hc = _first(o, "head_count", "headcount", "size", "employee_count", "company_size")
    if isinstance(hc, dict):
        hc = _first(hc, "name", "label", "value")
    stage = _first(o, "stage", "funding_stage", "company_stage")
    if isinstance(stage, dict):
        stage = _first(stage, "name", "label", "value")
    tags = _first(o, "industry_tags", "industries", "topics", "tags", default=[])
    if isinstance(tags, list):
        tags = "; ".join(t.get("name", t) if isinstance(t, dict) else str(t) for t in tags)
    locs = _first(o, "locations", "location", default=[])
    if isinstance(locs, list):
        locs = "; ".join(l.get("name", l) if isinstance(l, dict) else str(l) for l in locs)
    jobs = _first(o, "active_jobs_count", "jobs_count", "open_jobs", "job_count", default="")
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
        _diag(board, "initialState keys: " + ", ".join(sorted(st.keys()))[:400] if isinstance(st, dict) else "initialState not a dict")
    else:
        _diag(board, "no __NEXT_DATA__ on /companies or /jobs")
    if net_id:
        try:
            for page in range(max_pages):
                resp = feeds_mod._post(f"https://api.getro.com/api/v2/collections/{net_id}/search/organizations",
                                       {"hitsPerPage": 100, "page": page, "filters": {}, "query": ""})
                if page == 0:
                    _diag(board, f"net_id={net_id} status={getattr(resp, 'status_code', '?')} body[:600]={resp.text[:600]!r}")
                r = resp.json()
                res = r.get("results") or r
                if page == 0:
                    _diag(board, "organizations API keys: " + ", ".join(sorted(res.keys()))[:300]
                          if isinstance(res, dict) else f"organizations API returned {type(res).__name__}")
                batch = (res.get("organizations") or res.get("companies") or res.get("hits") or res.get("items") or []) \
                    if isinstance(res, dict) else (res if isinstance(res, list) else [])
                orgs += batch
                total = int(res.get("count") or res.get("total") or res.get("nbHits") or 0) if isinstance(res, dict) else 0
                if not batch or (total and len(orgs) >= total):
                    break
            note = f"search API, {len(orgs)} companies"
        except Exception as e:
            orgs, note = [], f"search API failed ({type(e).__name__}: {str(e)[:200]})"
            _diag(board, note)
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
                        and any(k in d for k in ("head_count", "headcount", "stage", "industry_tags", "active_jobs_count", "slug")):
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
    rows = sorted(merged.values(), key=lambda r: r["company"].lower())
    run.write_csv(Path(args.out), rows, COLS)
    print(f"  {len(rows)} companies -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
