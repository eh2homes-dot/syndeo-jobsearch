#!/usr/bin/env python3
"""Find leadership / team page URLs for companies that don't have one yet.

For each company in config/companies.csv with a blank Leadership URL:

  1. fetch the homepage and collect links that look like a team page
     ("leadership", "team", "management", "about", ...) on the same site
  2. add a handful of common paths (/leadership, /about/team, ...)
  3. fetch each candidate and count the people it lists, using the same
     extractor the weekly leadership diff uses
  4. keep the best page if it lists at least --min-people people

Every result goes into out/leadership-discovery.csv for review. With --apply,
pages that clear the bar are also written into config/companies.csv, so the
next weekly run starts watching them (the first run records a baseline and
reports nothing, which is expected).

    python discover_leadership.py                 # report only
    python discover_leadership.py --apply         # report and update the CSV
    python discover_leadership.py --limit 5 -v    # quick test
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup  # noqa: E402

from lib.http import get  # noqa: E402
from lib.people import extract_from_html  # noqa: E402

log = logging.getLogger("discover")

HERE = Path(__file__).resolve().parent
COMPANIES = HERE / "config" / "companies.csv"
REPORT = HERE / "out" / "leadership-discovery.csv"

# Link text or path fragments that suggest a team page, best first.
_HINTS = [
    ("leadership", 50), ("executive", 40), ("management-team", 40),
    ("management", 30), ("our-team", 30), ("team", 25), ("people", 20),
    ("who-we-are", 15), ("about", 10), ("company", 5),
]
_SKIP = re.compile(
    r"(careers?|jobs?|blog|news|press|login|sign|privacy|terms|cookie|support|"
    r"contact|pricing|demo|webinar|events?|resources?|case-stud|partners?/|"
    r"\.pdf$|\.jpg$|\.png$|mailto:|tel:|#)",
    re.I,
)
_COMMON_PATHS = [
    "/leadership", "/leadership-team", "/about/leadership", "/about-us/leadership",
    "/company/leadership", "/our-team", "/team", "/about/team", "/about-us/team",
    "/about", "/about-us",
]
MAX_CANDIDATES = 10


def _site_root(company_row: dict) -> str | None:
    for field in ("Website URL", "Careers Page URL"):
        url = (company_row.get(field) or "").strip()
        if url.startswith("http"):
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}"
    return None


def _same_site(a: str, b: str) -> bool:
    strip = lambda h: h.lower().removeprefix("www.")
    return strip(urlparse(a).netloc) == strip(urlparse(b).netloc)


def _hint_score(url: str, text: str = "") -> int:
    blob = f"{urlparse(url).path} {text}".lower().replace(" ", "-")
    return max((score for word, score in _HINTS if word in blob), default=0)


def _candidates(root: str) -> tuple[list[str], bool]:
    """Ranked candidate URLs, and whether the homepage itself loaded."""
    found: dict[str, int] = {}
    r = get(root, timeout=15, retries=1)
    if r is not None:
        base = r.url or root
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"].strip()).split("#")[0].rstrip("/")
            if not href.startswith("http") or not _same_site(href, root):
                continue
            if _SKIP.search(href):
                continue
            score = _hint_score(href, a.get_text(" ", strip=True))
            if score:
                found[href] = max(found.get(href, 0), score)
    for path in _COMMON_PATHS:
        url = root.rstrip("/") + path
        found.setdefault(url, _hint_score(url) - 1)  # site links beat guesses
    ranked = sorted(found, key=lambda u: -found[u])
    return ranked[:MAX_CANDIDATES], r is not None


def discover(row: dict, min_people: int) -> dict:
    name = row["Company Name"]
    root = _site_root(row)
    result = {
        "company": name, "website": root or "", "status": "", "leadership_url": "",
        "people_found": 0, "sample_people": "", "candidates_checked": 0,
    }
    if not root:
        result["status"] = "no website"
        return result

    best = None  # (people_count, hint_score, url, people)
    checked = 0
    urls, home_ok = _candidates(root)
    for url in urls:
        r = get(url, timeout=15, retries=1)
        checked += 1
        if r is None:
            continue
        final = (r.url or url).rstrip("/")
        if not _same_site(final, root):
            continue
        people = extract_from_html(r.text)
        key = (len(people), _hint_score(final), final, people)
        if best is None or key[:2] > best[:2]:
            best = key
        # A clearly-named leadership page with enough people: stop looking.
        if len(people) >= min_people and _hint_score(final) >= 40:
            break

    result["candidates_checked"] = checked
    if best is None:
        result["status"] = "no team page found" if home_ok else "site unreachable"
    elif best[0] >= min_people:
        result.update(
            status="found", leadership_url=best[2], people_found=best[0],
            sample_people="; ".join(f"{p.name} ({p.title})" for p in best[3][:3]),
        )
    elif best[0] > 0:
        result.update(
            status="too few people (check by hand)", leadership_url=best[2],
            people_found=best[0],
            sample_people="; ".join(f"{p.name} ({p.title})" for p in best[3][:3]),
        )
    else:
        result["status"] = "no people found (page missing or JS-rendered)"
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write found URLs into config/companies.csv")
    p.add_argument("--min-people", type=int, default=3, help="people a page must list to count (default 3)")
    p.add_argument("--limit", type=int, default=0, help="only check the first N companies (testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    with COMPANIES.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)

    todo = [r for r in rows if not (r.get("Leadership URL") or "").strip()]
    if args.limit:
        todo = todo[: args.limit]
    log.info("checking %d companies without a leadership URL", len(todo))

    results = []
    for i, row in enumerate(todo, 1):
        res = discover(row, args.min_people)
        results.append(res)
        log.info("[%d/%d] %-35s %s %s", i, len(todo), row["Company Name"][:35],
                 res["status"], res["leadership_url"])

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else ["company"])
        w.writeheader()
        w.writerows(results)

    found = {r["company"]: r["leadership_url"] for r in results if r["status"] == "found"}
    if args.apply and found:
        for row in rows:
            if row["Company Name"] in found and not (row.get("Leadership URL") or "").strip():
                row["Leadership URL"] = found[row["Company Name"]]
        with COMPANIES.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    log.info("done: %s", summary)
    log.info("review file: %s%s", REPORT, " (companies.csv updated)" if args.apply and found else "")

    import os
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as fh:
            fh.write(f"## Leadership page discovery\n\n{summary}\n\n")
            fh.write("| Company | Status | URL | People | Sample |\n|---|---|---|---|---|\n")
            for r in results:
                fh.write(f"| {r['company']} | {r['status']} | {r['leadership_url']} | "
                         f"{r['people_found']} | {r['sample_people']} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
