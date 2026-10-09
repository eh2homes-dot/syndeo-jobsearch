#!/usr/bin/env python3
"""OpCo weekly job search — Column D edition.

Column D of the master sheet's OpCo tab is the only input:

  a job-board link     -> that board is read with its job system's reader
                          (Workday, UKG, Dayforce, ADP, Greenhouse, Lever, Ashby,
                          Workable, SmartRecruiters, Recruitee, Breezy, BambooHR,
                          Rippling, Jobvite, Paylocity, iCIMS; Paycom, isolved and
                          Gem by opening the board in a headless browser)
  any other web page   -> the jobs on that page are read. If the page loads its
                          jobs from a job board (most careers pages do), that
                          board is read, and the brief says which one so the
                          link can be pasted into Column D. Pages that only show
                          their jobs after scripts run are opened in a headless
                          browser, which also clicks "Load more" / "Next".
  empty, or not a link -> "needs a job-board link"

Nothing is guessed from a company's name and no third-party listing site is
read. Nothing is cached between runs except last week's roles, which the
new/closed comparison needs.

Anything that can't be read lands in the brief's "Needs your attention"
section with the reason, so Column D can be fixed.

    python opco_search.py                 # weekly run
    python opco_search.py --discover      # check what each Column D is; read no jobs
    python opco_search.py --only "Greystar,Lamar Advertising Company" -v
    python opco_search.py --no-browser    # never open the headless browser

Files: opco_search.py (this), opco_config.yml (role filter, live-sheet link),
opco.csv (the OpCo tab, unless the live-sheet link is set). The job-system
readers and the page reader are shared with the weekly search: jobsearch/boards.py,
jobsearch/pages.py, jobsearch/browser.py.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so `jobsearch` imports from any working directory

from jobsearch import boards as B                                      # noqa: E402
from jobsearch.boards import Board, classify as classify_link          # noqa: E402
from jobsearch.browser import close_shared, shared as shared_browser   # noqa: E402
from jobsearch.http import FetchError, NotFound, get                   # noqa: E402
from jobsearch.pages import NeedsLink, PageDown, read_page, read_rendered_board  # noqa: E402

log = logging.getLogger("opco")

READERS = B.READERS


# ==========================================================================
# ROLES
# ==========================================================================

@dataclass
class Role:
    id: str
    title: str
    url: str
    location: str = ""
    department: str = ""
    posted: str = ""

    def __post_init__(self):
        for f in ("id", "title", "url", "location", "department", "posted"):
            setattr(self, f, B.as_text(getattr(self, f)))

    def to_dict(self) -> dict:
        return asdict(self)


def to_roles(jobs) -> list[Role]:
    """Shared-reader jobs as this search's roles. The id is built in the format
    this search has always stored (see jobsearch.boards.opco_id), so last week's
    baseline still lines up."""
    out, seen = [], set()
    for j in jobs:
        rid = B.opco_id(j)
        if rid in seen or not j.get("title"):
            continue
        seen.add(rid)
        out.append(Role(id=rid, title=j["title"], url=j.get("url", ""), location=j.get("location", ""),
                        department=j.get("department", ""), posted=j.get("posted") or j.get("posted_rel", "")))
    return out


# ==========================================================================
# COLUMN D
# ==========================================================================

def classify(column_d: str) -> Board:
    """Decide what a Column D value is. Pure: no network, no memory."""
    url = (column_d or "").strip()
    if not url:
        return Board(problem="Column D is empty")
    if not re.match(r"https?://", url, re.I):
        return Board(problem=f"Column D isn't a link ({url[:60]!r})")
    return classify_link(url)


def how_read(board: Board) -> str:
    """What Column D is, for the --discover check."""
    if not board.system:
        return f"needs a link — {board.problem}"
    if board.system == "page":
        return "a web page — jobs are read from the page, or from the job board it loads"
    if board.readable:
        return f"{board.system} job board — ready"
    if board.rendered:
        return f"{board.system} job board — read by opening it in a headless browser"
    return f"{board.system} — no reader yet; the page itself is tried"


# ==========================================================================
# BASELINE
# ==========================================================================
#
# The only thing remembered between runs: last week's roles per company, so the
# brief can say what's new and what closed. Stored with the board it came from,
# so changing Column D starts that company over instead of reporting false
# closures.

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "opco"
BASELINE_PATH = STATE / "baseline.json"
LIVE_COPY = STATE / "opco_live.csv"
SUSPICIOUS_DROP = 5

# Files the previous version of this search kept in state/. Nothing reads them
# any more; they're removed so nobody mistakes them for live data. Only these
# exact names: state/ also holds the people-moves workflow's files.
LEGACY_FILES = ("ats_cache.json", "last_good.json", "job_pages.json", "opco_live.csv")


class Baseline:
    def __init__(self, run_date: str | None = None):
        self.run_date = run_date or date.today().isoformat()
        self.data: dict = _read(BASELINE_PATH, {})

    def _prev(self, company: str, board: str, via: str | None = None) -> dict | None:
        """Last week's read of this company, if it came from the same place.

        `via` is the job board a Column D *page* was read through. When given,
        it must match too: if a careers page switches job systems, every role
        gets a new id, and comparing across that would report the whole list
        as closed and reopened.
        """
        prev = self.data.get(company)
        if not prev or prev.get("board") != board:
            return None
        if via is not None and prev.get("via", "") != via:
            return None
        return prev

    def carry_forward(self, company: str, reason: str, board: str) -> dict:
        prev = self._prev(company, board)
        if not prev:
            return {"status": "failed", "reason": reason, "roles": []}
        return {"status": "stale", "reason": reason, "roles": prev["roles"],
                "stale_since": prev.get("fetched_on")}

    def is_suspicious_drop(self, company: str, count: int, board: str) -> bool:
        """A company with 5+ roles last week that now shows none is treated as a
        bad read - once. The zero is remembered, and if the next run is zero
        too it is believed, so a company that really did stop hiring isn't
        carried forward forever."""
        prev = self._prev(company, board)
        if not prev or count != 0 or len(prev.get("roles", [])) < SUSPICIOUS_DROP:
            return False
        if prev.get("zero_seen") and prev["zero_seen"] < self.run_date:
            return False
        prev.setdefault("zero_seen", self.run_date)
        return True

    def is_first_read(self, company: str, board: str, via: str | None = None) -> bool:
        return self._prev(company, board, via) is None

    def save(self, results: dict) -> None:
        for company, r in results.items():
            if r["status"] == "ok":
                self.data[company] = {"fetched_on": self.run_date, "board": r["board"],
                                      "via": r.get("via", ""), "roles": r["roles"]}
            elif r["status"] in ("needs-link", "unsupported"):
                self.data.pop(company, None)   # no board, so no trustworthy baseline
        _write(BASELINE_PATH, self.data)


def remove_legacy_state() -> None:
    old = ROOT / "state"
    for name in LEGACY_FILES:
        f = old / name
        if f.exists():
            f.unlink()
            log.info("removed old state file %s (no longer used)", f.relative_to(ROOT))
    for f in old.glob("checkpoint-*.json"):
        f.unlink()
        log.info("removed old state file %s (no longer used)", f.relative_to(ROOT))


# ==========================================================================
# ONE COMPANY
# ==========================================================================

def read_company(company, role_filter, baseline, browser=None, out_of_time: bool = False) -> dict:
    """Read one company from its Column D. Always returns a result."""
    board = classify(company.careers_url)
    base = {"column_d": company.careers_url, "column_d_from": company.careers_from,
            "system": board.system, "board": board.key, "via": "", "note": "",
            "roles": [], "filtered": [], "total": 0}

    if not board.system:
        return {**base, "status": "needs-link", "reason": board.problem}
    if out_of_time and not board.readable:
        # Pages are the slow part (a browser, several boards to try). Past the
        # run's time budget they are skipped and last week's roles kept, so the
        # run always finishes and saves what it has.
        result = {**base, **baseline.carry_forward(
            company.name, "not read: the run's time budget was used up before this company", board.key)}
        result["total"] = len(result["roles"])
        result["filtered"] = [{**r, "company": company.name, "category": c, "status": result["status"]}
                              for r in result["roles"]
                              if (c := role_filter.categorize(r["title"], company.name))]
        return result

    name = company.name
    terms = role_filter.search_terms.get(name.lower())
    truncated = ""
    try:
        if board.readable:
            jobs = B.read_board(board, **({"search_terms": terms} if terms and board.system == "workday" else {}))
            truncated = getattr(jobs, "truncated", "")
        elif board.rendered:
            page = read_rendered_board(board, browser)
            jobs, truncated = page.jobs, page.truncated
            base["note"] = f"read by opening the {board.system} job board in a browser"
        else:
            # An ordinary web page - or a job system with no reader, where the
            # page itself is still worth trying before giving up on it.
            page = read_page(company.careers_url, trusted=name.lower() in role_filter.trusted_pages,
                             browser=browser, search_terms=terms, label="Column D page")
            jobs, truncated = page.jobs, page.truncated
            base["note"] = page.how
            if page.board is not None:
                # The page loads its jobs from a job board. Those are the jobs
                # reported, and the brief names the board so its link can go in
                # Column D, which makes the read direct from then on.
                base["via"] = page.board.key
                base["via_label"] = page.board.label()
                base["via_url"] = page.board.listing_url
        roles = [r.to_dict() for r in to_roles(jobs)]
    except NeedsLink as exc:
        if board.system != "page" and not (board.readable or board.rendered):
            return {**base, "status": "unsupported",
                    "reason": f"{board.system} isn't supported yet ({exc})"}
        return {**base, "status": "needs-link", "reason": str(exc)}
    except PageDown as exc:
        # The page didn't load this run. If it was read fine last week, that's a
        # bad week: keep last week's roles. If it has never been read, it's a
        # link to look at.
        if baseline._prev(name, board.key) is None:
            return {**base, "status": "needs-link", "reason": str(exc)}
        log.warning("  %s: read failed (%s)", name, exc)
        result = {**base, **baseline.carry_forward(name, str(exc), board.key)}
        result["filtered"] = [{**r, "company": name, "category": c, "status": result["status"]}
                              for r in result["roles"] if (c := role_filter.categorize(r["title"], name))]
        result["total"] = len(result["roles"])
        return result
    except NotFound as exc:
        # The board Column D names doesn't exist (any more). That's a link to
        # fix, not a bad week, so it goes with the links rather than the failures.
        return {**base, "status": "needs-link",
                "reason": f"Column D points at a job board that isn't there: {exc}"}
    except Exception as exc:  # noqa: BLE001 - one company never sinks the run
        reason = plain_error(exc)
        log.warning("  %s: read failed (%s)", name, reason)
        result = {**base, **baseline.carry_forward(name, reason, board.key)}
    else:
        if truncated:
            base["note"] = (base["note"] + "; " if base["note"] else "") + f"cut short: {truncated}"
        via = base["via"]
        if baseline.is_suspicious_drop(name, len(roles), board.key):
            result = {**base, **baseline.carry_forward(
                name, "dropped to 0 roles from 5+ last week; treated as a read failure", board.key)}
        else:
            result = {**base, "status": "ok", "roles": roles}
            # First time this Column D is read: list it once so a person can
            # confirm the jobs really belong to this company. A wrong link in
            # Column D is the one way the wrong company's jobs can still appear.
            if baseline.is_first_read(name, board.key, via) and roles:
                result["spot_check"] = [f"{r['title']} ({r['location']})" if r.get("location")
                                        else r["title"] for r in roles[:3]]

    result["filtered"] = []
    for r in result["roles"]:
        category = role_filter.categorize(r["title"], name)
        if category:
            result["filtered"].append({**r, "company": name, "category": category,
                                       "status": result["status"]})
    result["total"] = len(result["roles"])
    return result


def compute_diff(results: dict, baseline: Baseline, role_filter) -> dict:
    """New and closed matching roles, for companies read cleanly both weeks
    from the same board."""
    new, closed = [], []
    for company, c in results.items():
        if c["status"] != "ok":
            continue
        prev = baseline._prev(company, c["board"], c.get("via", ""))
        if not prev and not c["roles"]:
            # Nothing open this week, so there are no new ids to mix up with last
            # week's: whatever board last week's roles came through, they closed.
            prev = baseline._prev(company, c["board"])
        if not prev:
            continue
        prev_ids = {r["id"] for r in prev["roles"]}
        now_ids = {r["id"] for r in c["roles"]}
        new.extend(r for r in c["filtered"] if r["id"] not in prev_ids)
        for r in prev["roles"]:
            if r["id"] not in now_ids:
                category = role_filter.categorize(r["title"], company)
                if category:
                    closed.append({**r, "company": company, "category": category})
    return {"new": new, "closed": closed}


# ==========================================================================
# COMPANIES
# ==========================================================================

@dataclass
class Company:
    name: str
    website: str = ""
    careers_url: str = ""
    careers_from: str = ""
    state: str = ""
    segment: str = ""


_NA = {"", "n/a", "na", "none", "-"}


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _NA else v


CAREERS_COLUMN = 3   # Column D on the OpCo tab (0-based index)


def parse_companies(text: str, source: str) -> list[Company]:
    """Read the OpCo tab. The careers URL always comes from Column D.

    Column D is found by its header, "Careers Page URL". If that header is ever
    renamed, the parser falls back to Column D by position, so the careers URL
    the scraper checks is always what sits in that column.
    """
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        raise ValueError(f"{source} is empty")
    header = [h.strip() for h in rows[0]]
    if "Company Name" not in header:
        raise ValueError(f"{source} has no 'Company Name' header; is it the OpCo tab?")

    def col(name, fallback=None):
        return header.index(name) if name in header else fallback

    i_name, i_web = col("Company Name"), col("Website URL")
    i_careers = col("Careers Page URL", CAREERS_COLUMN)
    if "Careers Page URL" not in header:
        found = header[CAREERS_COLUMN] if CAREERS_COLUMN < len(header) else "(missing)"
        log.warning("no 'Careers Page URL' header; using Column D, headed %r", found)
    i_state, i_seg = col("State"), col("Industry Segment")

    def cell(r, i):
        return _clean(r[i]) if i is not None and i < len(r) else ""

    out, seen = [], set()
    for r in rows[1:]:
        name = cell(r, i_name)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        website = cell(r, i_web)
        careers = cell(r, i_careers)
        if not website.startswith("http"):
            website = ""  # e.g. "(TBD need to find...)"
        careers_from = "Column D"
        # Some rows put the careers link in the Website column instead.
        if not careers and re.search(r"/(careers?|jobs|employment)", website, re.I):
            careers, careers_from = website, "Column B (Column D empty)"
        out.append(Company(name=name, website=website, careers_url=careers,
                           careers_from=careers_from if careers else "",
                           state=cell(r, i_state), segment=cell(r, i_seg)))
    log.info("loaded %d companies from %s", len(out), source)
    return out


def load_companies(path: Path) -> list[Company]:
    return parse_companies(path.read_text(encoding="utf-8-sig"), str(path))


def find_companies_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in (ROOT.parent / "opco.csv", ROOT.parent / "data" / "opco.csv",
                      ROOT / "opco.csv", Path("opco.csv")):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "opco.csv not found. Expected it at the repo root or data/opco.csv.")


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("could not read %s; starting fresh", path)
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def load_live_companies(url: str) -> tuple[list[Company], str]:
    """Read the OpCo tab straight from Google Sheets.

    Falls back to the last good live copy, so a Sheets outage or a sharing
    change never stops the run. Returns (companies, description of source).
    """
    try:
        r = get(url, timeout=30)
        text = r.content.decode("utf-8-sig", errors="replace")
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise FetchError("Google returned a sign-in page, not CSV - the tab "
                             "isn't published or shared for viewing")
        companies = parse_companies(text, "the live sheet")
        LIVE_COPY.parent.mkdir(parents=True, exist_ok=True)
        LIVE_COPY.write_text(text, encoding="utf-8")
        return companies, "live Google Sheet (OpCo tab)"
    except (FetchError, ValueError) as exc:
        log.warning("could not read the live sheet: %s", exc)
        if LIVE_COPY.exists():
            log.warning("using the last good copy of the sheet instead")
            return load_companies(LIVE_COPY), f"last saved copy of the sheet - live read failed: {exc}"
        raise


# ==========================================================================
# ROLE FILTER
# ==========================================================================

class RoleFilter:
    """Title-keyword filter, configured in opco_config.yml."""

    def __init__(self, cfg: dict):
        def rx(words):
            return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None

        self.include = {k: rx(v) for k, v in (cfg.get("include") or {}).items()}
        self.exclude = rx(cfg.get("exclude") or [])
        self.overrides = {}
        self.search_terms = {}
        self.trusted_pages = set()   # pages whose listings skip the "looks like jobs" check
        for name, rule in (cfg.get("company_overrides") or {}).items():
            rule = rule or {}
            if rule.get("trust_page"):
                self.trusted_pages.add(name.lower())
            self.overrides[name.lower()] = {
                "require": rx(rule.get("require") or []),
                "exclude": rx(rule.get("exclude") or []),
            }
            if rule.get("search_terms"):
                self.search_terms[name.lower()] = [str(t) for t in rule["search_terms"]]

    @classmethod
    def load(cls, path: Path) -> "RoleFilter":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def categorize(self, title: str, company: str = "") -> str | None:
        """Return the matched category, or None if the role is filtered out."""
        t = title or ""
        if self.exclude and self.exclude.search(t):
            return None

        rule = self.overrides.get(company.lower())
        if rule:
            if rule["exclude"] and rule["exclude"].search(t):
                return None
            if rule["require"] and not rule["require"].search(t):
                return None

        for category, pattern in self.include.items():
            if pattern and pattern.search(t):
                return category
        return None


# ==========================================================================
# ERRORS
# ==========================================================================

def plain_error(exc: Exception) -> str:
    """What went wrong, in words a person can act on."""
    detail = f"{type(exc).__name__}: {exc}"[:160]
    if isinstance(exc, FetchError):
        return str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "the job system sent back a web page instead of job data"
    if isinstance(exc, (KeyError, TypeError, AttributeError, ValueError, IndexError)):
        return f"the job system's data wasn't in the expected format ({detail})"
    return f"unexpected error ({detail})"


CATEGORY_LABEL = {"executive": "Executive", "sales": "Sales", "gtm": "GTM",
                  "engineering": "Engineering", "operators": "Operators"}


def _line(r: dict) -> str:
    bits = [f"[{r['title']}]({r['url']})" if r.get("url") else r["title"]]
    if r.get("location"):
        bits.append(r["location"])
    return " — ".join(bits)


# ==========================================================================
# REPORT
# ==========================================================================

STATUS_ORDER = {"ok": 0, "stale": 1, "failed": 2, "unsupported": 3, "needs-link": 4}


def _attention(results: dict) -> list[str]:
    """Everything that needs a person, grouped by what to do about it."""
    needs = {n: c for n, c in results.items() if c["status"] == "needs-link"}
    unsupported = {n: c for n, c in results.items() if c["status"] == "unsupported"}
    failed = {n: c for n, c in results.items() if c["status"] in ("failed", "stale")}
    spot = {n: c for n, c in results.items() if c.get("spot_check")}
    cut = {n: c for n, c in results.items() if "cut short" in (c.get("note") or "")}
    moved = {n: c for n, c in results.items()
             if c["status"] == "ok" and "now redirects" in (c.get("note") or "")}
    via = {n: c for n, c in results.items() if c["status"] == "ok" and c.get("via_url")}
    if not any((needs, unsupported, failed, spot, cut, moved, via)):
        return []

    md = ["## Needs your attention", ""]
    if needs:
        md += [f"### Needs a job-board link in Column D ({len(needs)})", "",
               "*Fix: open the company's careers page, click any job, and paste the address "
               "it lands on into Column D (drop the part that names the specific job). If "
               "the jobs are listed on the company's own page, that page's address works too.*", ""]
        for n, c in sorted(needs.items()):
            md.append(f"- **{n}** — {c['reason']}")
        md.append("")
    if unsupported:
        by_system: dict[str, list[str]] = {}
        for n, c in unsupported.items():
            by_system.setdefault(c["system"], []).append(n)
        md += [f"### Job system not supported yet ({len(unsupported)})", "",
               "*Column D is fine; the scraper has no reader for this system yet. "
               "Ordered by how many companies a reader would unlock.*", ""]
        for system, names in sorted(by_system.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            md.append(f"- **{system}** ({len(names)}) — {', '.join(sorted(names))}")
        md.append("")
    if failed:
        md += [f"### Failed this week ({len(failed)})", "",
               "*Reading went wrong. Last week's roles are carried forward where there were "
               "any, and these are left out of new/closed so a failure never reads as a hire.*", ""]
        for n, c in sorted(failed.items()):
            md.append(f"- **{n}** — {c.get('reason') or 'unknown error'}")
        md.append("")
    if spot:
        md += [f"### Confirm these are the right companies — once ({len(spot)})", "",
               "*First time each of these Column D links was read. Open the link and check the "
               "jobs belong to this company. If one is wrong, fix its Column D; nothing else "
               "is needed. Each is listed only once, and again only if its Column D changes.*", ""]
        for n, c in sorted(spot.items()):
            d = c.get("column_d") or ""
            md.append(f"- **{n}** — [{d.split('//')[-1][:50]}]({d}) · {c.get('total', 0)} jobs, "
                      f"e.g. {'; '.join(c['spot_check'])}")
        md.append("")
    if via:
        md += [f"### Read through the page's job board — paste these into Column D ({len(via)})", "",
               "*Column D is a careers page, and the jobs were read from the job board that page "
               "loads. That works, but the board's own link is the sturdier thing to keep in "
               "Column D: it keeps working if the careers page is redesigned.*", ""]
        for n, c in sorted(via.items()):
            md.append(f"- **{n}** — {c.get('via_label', '')}: {c['via_url']}")
        md.append("")
    if moved:
        md += [f"### Works, but Column D has moved ({len(moved)})", ""]
        for n, c in sorted(moved.items()):
            md.append(f"- **{n}** — {c['note'].split(';')[0]}")
        md.append("")
    if cut:
        md += [f"### Results cut short ({len(cut)})", ""]
        for n, c in sorted(cut.items()):
            md.append(f"- **{n}** — {c['note']}")
        md.append("")
    return md


def write_report(results: dict, diff: dict, run_date: str, first_run: bool,
                 out_dir: Path, source: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    open_roles = [r for c in results.values() for r in c.get("filtered", [])]
    with_roles = sorted({r["company"] for r in open_roles})
    readable = sum(1 for c in results.values() if c["status"] in ("ok", "stale"))

    md = [f"# OpCo job search — week of {run_date}", "",
          f"{len(open_roles)} matching roles across {len(with_roles)} companies. "
          f"{readable} of {len(results)} companies read from their Column D.", "",
          f"Company list read from: **{source}**.", "",
          "> Open each link before it goes in the newsletter. Postings move.", ""]
    md += _attention(results)

    if first_run:
        md += ["*First run of this version: this is the baseline. New-this-week and "
               "recently-closed start next week.*", ""]
    else:
        md += [f"## New this week ({len(diff['new'])})", ""]
        md += ([f"- **{r['company']}** · {CATEGORY_LABEL.get(r['category'], r['category'])} · {_line(r)}"
                for r in sorted(diff["new"], key=lambda x: (x["company"], x["title"]))]
               or ["No new matching roles."])
        md += ["", f"## Recently closed ({len(diff['closed'])})", "",
               "*Open last week, gone now — the \"recently hired\" signal. Closed usually means "
               "filled, but can mean pulled. Companies that failed this week are left out.*", ""]
        md += ([f"- **{r['company']}** · {r['title']}" + (f" — {r['location']}" if r.get("location") else "")
                for r in sorted(diff["closed"], key=lambda x: (x["company"], x["title"]))]
               or ["No matching roles closed."])
        md.append("")

    md += ["## All open roles", ""]
    for company in with_roles:
        roles = [r for r in open_roles if r["company"] == company]
        c = results[company]
        md.append(f"### {company} ({len(roles)})"
                  + (f" — *carried forward from {c.get('stale_since')}*" if c["status"] == "stale" else ""))
        groups: dict[tuple, list] = {}
        for r in roles:
            groups.setdefault((r["category"], r["title"]), []).append(r)
        for (category, title), group in sorted(groups.items()):
            label = CATEGORY_LABEL.get(category, category)
            if len(group) == 1:
                md.append(f"- {label} · {_line(group[0])}")
            else:
                places = sorted({g["location"] for g in group if g.get("location")})
                where = f" — {', '.join(places[:5])}{'…' if len(places) > 5 else ''}" if places else ""
                md.append(f"- {label} · [{title}]({group[0]['url']}) **({len(group)} openings)**{where}")
        md.append("")

    md += ["## Coverage", "",
           "| Company | Column D | System | Status | Roles (all / matching) | Note |",
           "|---|---|---|---|---|---|"]
    for n, c in sorted(results.items(), key=lambda kv: (STATUS_ORDER.get(kv[1]["status"], 9), kv[0])):
        d = c.get("column_d") or ""
        shown = f"[{d.split('//')[-1][:45]}]({d})" if d.startswith("http") else (d[:45] or "— empty")
        if d and str(c.get("column_d_from", "")).startswith("Column B"):
            shown += " *(from Column B)*"
        note = (c.get("reason") or c.get("note") or "").replace("|", "/")
        md.append(f"| {n} | {shown} | {c.get('system') or '—'} | {c['status']} | "
                  f"{c.get('total', 0)} / {len(c.get('filtered', []))} | {note} |")
    counts: dict[str, int] = {}
    for c in results.values():
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    md += ["", "**Status:** " + ", ".join(f"{k} {v}" for k, v in
                                          sorted(counts.items(), key=lambda kv: STATUS_ORDER.get(kv[0], 9))), ""]

    text = "\n".join(md)
    path = out_dir / f"opco-jobs-{run_date}.md"
    path.write_text(text, encoding="utf-8")
    (out_dir / "opco-jobs-latest.md").write_text(text, encoding="utf-8")
    (out_dir / "opco-jobs-latest.json").write_text(json.dumps({
        "run_date": run_date, "roles": open_roles, "new": diff["new"], "closed": diff["closed"],
        "coverage": {n: {k: v for k, v in c.items() if k not in ("roles", "filtered")}
                     for n, c in results.items()},
    }, indent=2), encoding="utf-8")
    # Every job read, matching or not, with its link: for checking the scraper
    # read the right jobs from the right companies.
    with (out_dir / f"opco-jobs-all-{run_date}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "title", "location", "url", "posted",
                                           "in_scope", "column_d"])
        w.writeheader()
        for n, c in sorted(results.items()):
            in_scope = {r["id"] for r in c.get("filtered", [])}
            for r in c.get("roles", []):
                w.writerow({"company": n, "title": r.get("title", ""), "location": r.get("location", ""),
                            "url": r.get("url", ""), "posted": r.get("posted", ""),
                            "in_scope": "yes" if r.get("id") in in_scope else "no",
                            "column_d": c.get("column_d", "")})

    with (out_dir / f"opco-jobs-{run_date}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "category", "title", "location", "url",
                                           "posted", "status"])
        w.writeheader()
        for r in open_roles:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    return path


def write_column_d_check(companies, out_dir: Path) -> Path:
    """--discover: what each Column D is, without reading any jobs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in companies:
        b = classify(c.careers_url)
        rows.append((c.name, c.careers_url, how_read(b)))
    md = ["# Column D check", "", "| Company | Column D | What it is |", "|---|---|---|"]
    md += [f"| {n} | {d[:70] or '— empty'} | {v} |" for n, d, v in rows]
    path = out_dir / "opco-columnd-check.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return path


# ==========================================================================
# MAIN
# ==========================================================================

CONFIG_PATH = ROOT / "opco_config.yml"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--companies", help="path to opco.csv (default: live sheet or repo copy)")
    p.add_argument("--only", help="comma-separated company names")
    p.add_argument("--discover", action="store_true",
                   help="check what each Column D is, without reading jobs")
    p.add_argument("--refresh-ats", action="store_true",
                   help="no longer does anything: nothing is cached between runs")
    p.add_argument("--budget-minutes", type=float, default=35,
                   help="after this long, careers pages still unread keep last week's roles")
    p.add_argument("--no-browser", action="store_true",
                   help="never open the headless browser (pages that need scripts are reported instead)")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--out", default=str(ROOT / "out"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    if args.refresh_ats:
        log.info("--refresh-ats has no effect: this version caches nothing between runs")

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("config not found: %s", config_path)
        return 2
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    sheet_url = str(config.get("sheet_csv_url") or "").strip()
    try:
        if args.companies:
            companies, source = load_companies(Path(args.companies)), f"file {Path(args.companies).name}"
        elif sheet_url:
            try:
                companies, source = load_live_companies(sheet_url)
            except (FetchError, ValueError) as exc:
                path = find_companies_file(None)
                companies, source = load_companies(path), f"repo copy {path.name} (live sheet unavailable: {exc})"
        else:
            path = find_companies_file(None)
            companies, source = load_companies(path), f"repo copy {path.name}"
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    log.info("company list: %s", source)

    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        companies = [c for c in companies if c.name.lower() in wanted]

    if args.discover:
        path = write_column_d_check(companies, Path(args.out))
        log.info("Column D check written to %s", path)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(path.read_text(encoding="utf-8"))
        return 0

    remove_legacy_state()
    role_filter = RoleFilter(config)
    baseline = Baseline()
    first_run = not baseline.data

    # Job boards first (quick, and never skipped), then careers pages. The pages
    # start from a different company each week, so if the time budget ever runs
    # out it isn't the same companies at the end of the sheet that miss out.
    quick = [c for c in companies if classify(c.careers_url).readable or not classify(c.careers_url).system]
    slow = [c for c in companies if c not in quick]
    if slow:
        turn = (date.today().isocalendar()[1] * max(1, len(slow) // 4)) % len(slow)
        slow = slow[turn:] + slow[:turn]
    companies = quick + slow

    browser = None if args.no_browser else shared_browser()
    started = time.time()
    results: dict = {}
    for i, company in enumerate(companies, 1):
        try:
            result = read_company(company, role_filter, baseline, browser,
                                  out_of_time=(time.time() - started) > args.budget_minutes * 60)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            log.error("  %s: crashed\n%s", company.name, traceback.format_exc())
            result = {"column_d": company.careers_url, "column_d_from": company.careers_from,
                      "system": "", "board": "", "note": "", "status": "failed",
                      "reason": plain_error(exc), "roles": [], "filtered": [], "total": 0}
        results[company.name] = result
        log.info("[%2d/%d] %-34s %-11s %-11s %d/%d", i, len(companies), company.name[:34],
                 result.get("system") or "-", result["status"],
                 result["total"], len(result["filtered"]))

    results = dict(sorted(results.items()))          # stable order in the files, whatever order was read
    if browser is not None:
        log.info("headless browser: %d pages opened, %.0fs", browser.pages_rendered, browser.seconds)
    close_shared()
    diff = {"new": [], "closed": []} if first_run else compute_diff(results, baseline, role_filter)
    baseline.save(results)
    try:
        path = write_report(results, diff, baseline.run_date, first_run, Path(args.out), source)
    except Exception:  # noqa: BLE001 - always leave something readable
        log.error("report failed, writing a plain fallback\n%s", traceback.format_exc())
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"opco-jobs-{baseline.run_date}.md"
        lines = [f"# OpCo job search - {baseline.run_date} (plain fallback)", ""]
        for n, c in sorted(results.items()):
            lines.append(f"## {n} - {c['status']} - {c.get('reason') or c.get('note') or ''}")
            lines += [f"- {r['title']} - {r.get('location', '')} - {r.get('url', '')}"
                      for r in c.get("filtered", [])]
        path.write_text("\n".join(lines), encoding="utf-8")

    ok = sum(1 for c in results.values() if c["status"] == "ok")
    log.info("")
    log.info("%d/%d companies read cleanly -> %s", ok, len(results), path)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(path.read_text(encoding="utf-8"))
    return 0 if ok or not results else 1


if __name__ == "__main__":
    raise SystemExit(main())
