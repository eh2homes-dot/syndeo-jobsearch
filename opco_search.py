#!/usr/bin/env python3
"""OpCo weekly job search.

Scrapes the companies on the master sheet's OpCo tab straight from their ATS —
no Built In, no aggregator, no single host whose rate limit can sink the run.

    python opco_search.py                  # weekly run
    python opco_search.py --discover       # resolve ATS only, no job fetch
    python opco_search.py --refresh-ats    # ignore the ATS cache, re-detect
    python opco_search.py --only "Greystar,Belong" -v
    python opco_search.py --companies path/to/opco.csv
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opcojobs.adapters import ADAPTERS  # noqa: E402
from opcojobs.core import (ROOT, RoleFilter, RunState, find_companies_file,  # noqa: E402
                           load_companies)
from opcojobs.resolve import load_overrides, resolve  # noqa: E402
from opcojobs import report  # noqa: E402

log = logging.getLogger("opco")


def fetch_company(company, res, role_filter, state) -> dict:
    base = {"ats": res.ats, "slug": res.slug, "source": res.source, "note": res.note}

    if not res.ats:
        return {**base, "status": "unresolved", "reason": res.note, "roles": [], "filtered": []}
    if not res.supported:
        return {**base, "status": "unsupported", "reason": f"{res.ats} has no adapter yet",
                "roles": [], "filtered": []}

    try:
        params = dict(res.params)
        if res.ats == "jsonld":
            params.setdefault("url", company.careers_url)
        roles = [r.to_dict() for r in ADAPTERS[res.ats](res.slug, **params)]
    except Exception as exc:  # noqa: BLE001 — one company must never sink the run
        reason = str(exc) or type(exc).__name__
        log.warning("  %s: fetch failed (%s)", company.name, reason)
        result = {**base, **state.carry_forward(company.name, reason)}
    else:
        if state.is_suspicious_drop(company.name, len(roles)):
            reason = "dropped to 0 roles from 5+ last week; treated as a scrape failure"
            log.warning("  %s: %s", company.name, reason)
            result = {**base, **state.carry_forward(company.name, reason)}
        else:
            result = {**base, "status": "ok", "roles": roles}

    filtered = []
    for r in result["roles"]:
        category = role_filter.categorize(r["title"], company.name)
        if category:
            filtered.append({**r, "company": company.name, "category": category,
                             "status": result["status"]})
    result["filtered"] = filtered
    result["total"] = len(result["roles"])
    return result


def compute_diff(results: dict, state: RunState, role_filter) -> dict:
    """New and closed matching roles, for companies scraped cleanly both weeks."""
    new, closed = [], []
    for company, c in results.items():
        if c["status"] != "ok":
            continue  # stale/failed companies are excluded from the diff
        prev = state.previous.get(company)
        if not prev:
            continue
        prev_ids = {r["id"] for r in prev["roles"]}
        now_ids = {r["id"] for r in c["roles"]}
        new.extend(r for r in c["filtered"] if r["id"] not in prev_ids)
        for r in prev["roles"]:
            if r["id"] in now_ids:
                continue
            category = role_filter.categorize(r["title"], company)
            if category:
                closed.append({**r, "company": company, "category": category})
    return {"new": new, "closed": closed}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--companies", help="path to opco.csv (default: auto-find)")
    p.add_argument("--only", help="comma-separated company names")
    p.add_argument("--discover", action="store_true", help="resolve ATS only")
    p.add_argument("--refresh-ats", action="store_true", help="ignore the ATS cache")
    p.add_argument("--out", default=str(ROOT / "out"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    try:
        companies = load_companies(find_companies_file(args.companies))
    except (FileNotFoundError, KeyError) as exc:
        log.error("%s", exc)
        return 2

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        companies = [c for c in companies if c.name.lower() in wanted]

    role_filter = RoleFilter.load(ROOT / "opco_config" / "roles.yml")
    overrides = load_overrides(ROOT / "opco_config" / "ats_overrides.csv")
    state = RunState()
    first_run = not state.previous

    results: dict = {}
    for i, company in enumerate(companies, 1):
        if state.done(company.name) and not args.discover:
            results[company.name] = state.checkpoint[company.name]
            log.info("[%2d/%d] %s — already done today, skipping", i, len(companies), company.name)
            continue

        try:
            res = resolve(company, overrides, state.cache, refresh=args.refresh_ats)
        except Exception:  # noqa: BLE001
            log.error("  %s: resolver crashed\n%s", company.name, traceback.format_exc())
            continue

        log.info("[%2d/%d] %-34s %-15s %s", i, len(companies), company.name[:34],
                 res.ats or "—", res.source)
        if args.discover:
            results[company.name] = {"ats": res.ats, "status": "discover",
                                     "note": res.note, "roles": [], "filtered": []}
            continue

        result = fetch_company(company, res, role_filter, state)
        results[company.name] = result
        state.record(company.name, result)

    if args.discover:
        state.save()
        resolved = sum(1 for r in results.values() if r["ats"])
        log.info("\nResolved %d of %d. Cached for next run.", resolved, len(results))
        for name, r in results.items():
            if not r["ats"] or r["ats"] not in ADAPTERS:
                log.info("  needs attention: %-34s %s", name, r.get("note") or r.get("ats"))
        return 0

    diff = compute_diff(results, state, role_filter) if not first_run else {"new": [], "closed": []}
    path = report.write(results, diff, state.run_date, first_run, Path(args.out))
    state.save()

    ok = sum(1 for r in results.values() if r["status"] == "ok")
    log.info("")
    log.info("%d/%d companies scraped cleanly -> %s", ok, len(results), path)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(path.read_text(encoding="utf-8"))

    # Fail the job only if nothing at all worked.
    return 0 if ok or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
