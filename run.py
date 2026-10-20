#!/usr/bin/env python3
"""People Moves — weekly scan for hires, departures and promotions.

Runs alongside the job search as a separate workflow. Sources are independent:
any one of them can fail without taking down the run.

    python run.py                          # weekly run, all sources
    python run.py --backfill               # one-off Wayback baseline (slow)
    python run.py --sources edgar,news     # just those
    python run.py --check-feeds            # validate trade press feed URLs
    python run.py --lookback-days 14 -v    # wider window, verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import companies as companies_lib  # noqa: E402
from lib.state import SeenStore  # noqa: E402
import report  # noqa: E402

ALL_SOURCES = ["edgar", "leadership", "news", "trade", "reqs"]
ROOT = Path(__file__).resolve().parent
# Who is watched: the same lists the two job searches read.
MASTER_LEADS = ROOT / "leads" / "master_leads.csv"
OPCO_CSV = ROOT / "opco.csv"
OPCO_CONFIG = ROOT / "opco_config.yml"
# What we know about them beyond the lists: ticker / SEC CIK, leadership page, aliases.
ENRICHMENT = ROOT / "config" / "companies.csv"


def _opco_sheet_url() -> str:
    """The live OpCo tab, as configured for the OpCo job search (blank if none)."""
    try:
        import yaml
        cfg = yaml.safe_load(OPCO_CONFIG.read_text(encoding="utf-8")) or {}
        url = str(cfg.get("sheet_csv_url") or "").strip()
        return url if url.startswith("http") else ""
    except Exception:  # noqa: BLE001
        return ""


def default_company_sources() -> list:
    return [str(MASTER_LEADS), (_opco_sheet_url(), str(OPCO_CSV))]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--companies", default="",
                   help="comma-separated company lists (default: leads/master_leads.csv "
                        "plus the OpCo tab, live sheet first and opco.csv as fallback)")
    p.add_argument("--enrich", default=str(ENRICHMENT),
                   help="tickers, SEC CIKs, leadership pages and aliases for listed companies")
    p.add_argument("--sources", default=",".join(ALL_SOURCES),
                   help=f"comma-separated subset of: {', '.join(ALL_SOURCES)}")
    p.add_argument("--lookback-days", type=int, default=8,
                   help="how far back to look for news and filings (default 8)")
    p.add_argument("--out", default="out", help="output directory")
    p.add_argument("--top", type=int, default=0,
                   help="cap the report at N items (0 = no cap)")
    p.add_argument("--jobs-file", default=None,
                   help="job-search outputs for inference signals, comma-separated (CSV or JSON); default: $JOBS_RESULTS_PATH")
    p.add_argument("--backfill", action="store_true",
                   help="one-off Wayback baseline instead of a weekly run")
    p.add_argument("--backfill-days", type=int, default=120,
                   help="how far back to pull archive snapshots (default 120)")
    p.add_argument("--check-feeds", action="store_true",
                   help="validate the trade press feed URLs and exit")
    p.add_argument("--limit", type=int, default=0,
                   help="only process the first N companies (for testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    log = logging.getLogger("run")

    if args.check_feeds:
        from sources import trade_press
        print("\nChecking trade press feeds:\n")
        trade_press.check_feeds()
        print()
        return 0

    try:
        sources = ([s.strip() for s in args.companies.split(",") if s.strip()]
                   if args.companies else default_company_sources())
        all_companies = companies_lib.load_lists(sources, enrich=args.enrich)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2

    if args.limit:
        all_companies = all_companies[: args.limit]
        log.info("limited to first %d companies", len(all_companies))

    seen = SeenStore()
    moves = []
    failures = []

    if args.backfill:
        from sources import wayback
        log.info("running Wayback backfill — this takes a while")
        try:
            moves += wayback.collect(all_companies, days_back=args.backfill_days)
        except Exception:
            failures.append("wayback")
            log.error("wayback backfill failed:\n%s", traceback.format_exc())
    else:
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]

        for name in wanted:
            try:
                if name == "edgar":
                    from sources import edgar
                    moves += edgar.collect(all_companies, args.lookback_days, seen)
                elif name == "leadership":
                    from sources import leadership
                    moves += leadership.collect(all_companies)
                elif name == "news":
                    from sources import news_rss
                    moves += news_rss.collect(all_companies, args.lookback_days, seen)
                elif name == "trade":
                    from sources import trade_press
                    moves += trade_press.collect(all_companies, args.lookback_days, seen)
                elif name == "reqs":
                    from sources import req_signals
                    moves += req_signals.collect(all_companies, jobs_file=args.jobs_file)
                else:
                    log.warning("unknown source %r, skipping", name)
            except Exception:
                failures.append(name)
                log.error("source %r failed:\n%s", name, traceback.format_exc())

    seen.save()
    md_path, json_path = report.write(moves, out_dir=args.out, top_n=args.top)

    log.info("")
    log.info("%d moves -> %s", len(moves), md_path)
    log.info("                %s", json_path)
    if failures:
        log.warning("sources that failed: %s", ", ".join(failures))

    # Surface the brief in the GitHub Actions run summary.
    import os
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md_path.read_text(encoding="utf-8"))

    # Fail the run only if every source broke.
    if failures and not moves:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
