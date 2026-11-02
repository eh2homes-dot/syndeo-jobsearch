"""Copy careers links from the live SYNDEO sheet into the repo's company lists.

    python -m jobsearch.sheet_sync            # update the files in place
    python -m jobsearch.sheet_sync --check    # report what would change, write nothing

The sheet is the source of truth for each company's careers link (column D).
The weekly search, hires, events and people-moves read frozen copies
(leads/master_leads.csv, config/companies.csv), so a link fixed in the sheet
never reached them. This runs before those jobs and copies column D across.

What it does and doesn't do:
  - Updates the careers link of a company already in a repo list, matched by
    name (case and spacing ignored). It never adds or removes companies.
  - Skips blank cells and anything that isn't an http(s) link.
  - Tabs are read in the order listed in opco_config.yml -> sheet_sync_tabs;
    a later tab wins when two tabs disagree, so list the Master File last.
  - A tab that can't be read (not published, Google down) is logged and
    skipped. The run carries on with the repo copy as it is.

Each tab must be published as CSV (File > Share > Publish to web) for the
link to work without a sign-in. Only columns A (name) and D (careers link)
are used.
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from pathlib import Path

import yaml

from .http import FetchError, get

log = logging.getLogger("sheet_sync")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "opco_config.yml"
# (file, name column, careers column)
TARGETS = [
    (ROOT / "leads" / "master_leads.csv", "company", "careers_url"),
    (ROOT / "config" / "companies.csv", "Company Name", "Careers Page URL"),
]
HEADER_NAMES = {"company name", "company"}


def norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def tab_links(text: str) -> dict[str, str]:
    """{normalized name: careers link} from one tab's CSV (columns A and D)."""
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise ValueError("got a web page, not CSV - the tab isn't published")
    out: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4:
            continue
        name, link = norm(row[0]), row[3].strip()
        if not name or name in HEADER_NAMES:
            continue
        if link.lower().startswith(("http://", "https://")):
            out[name] = link
    return out


def read_tabs(tabs: list[dict]) -> dict[str, str]:
    links: dict[str, str] = {}
    for tab in tabs:
        label, url = tab.get("name", "tab"), str(tab.get("url") or "").strip()
        if not url:
            continue
        try:
            r = get(url, timeout=30)
            found = tab_links(r.content.decode("utf-8-sig", errors="replace"))
        except (FetchError, ValueError) as exc:
            log.warning("skipping %s: %s", label, exc)
            continue
        log.info("%s: %d careers links", label, len(found))
        links.update(found)
    return links


def apply(path: Path, name_col: str, link_col: str, links: dict[str, str],
          write: bool) -> list[str]:
    """Rewrite only the lines whose link changed, so the diff stays small."""
    if not path.exists():
        return []
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    header = next(csv.reader([lines[0].lstrip("﻿")]))
    ni, ci = header.index(name_col), header.index(link_col)
    changes, out = [], [lines[0]]
    for line in lines[1:]:
        row = next(csv.reader([line])) if line.strip() else []
        new = links.get(norm(row[ni])) if len(row) > max(ni, ci) else None
        if new and row[ci].strip() != new:
            changes.append(f"{path.relative_to(ROOT)}: {row[ni]}: {row[ci] or '(blank)'} -> {new}")
            ending = line[len(line.rstrip("\r\n")):]
            row[ci] = new
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(row)
            line = buf.getvalue() + ending
        out.append(line)
    if write and changes:
        path.write_bytes("".join(out).encode("utf-8"))
    return changes


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="report changes, write nothing")
    p.add_argument("--config", default=str(CONFIG))
    args = p.parse_args(argv)

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    tabs = config.get("sheet_sync_tabs") or []
    if not tabs:
        log.info("no sheet_sync_tabs in %s - nothing to sync", Path(args.config).name)
        return 0
    links = read_tabs(tabs)
    if not links:
        log.warning("no tab could be read - repo copies left as they are")
        return 0

    changes = []
    for path, name_col, link_col in TARGETS:
        changes += apply(path, name_col, link_col, links, write=not args.check)
    verb = "would change" if args.check else "changed"
    log.info("%d careers links %s", len(changes), verb)
    for c in changes:
        log.info("  %s", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
