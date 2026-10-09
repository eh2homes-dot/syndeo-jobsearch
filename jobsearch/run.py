"""Weekly job-search run.

    python -m jobsearch.run                 # full run (network)
    python -m jobsearch.run --tier A        # only tier-A companies
    python -m jobsearch.run --only Entrata,Kiavi
    python -m jobsearch.run --detect        # also read unmapped companies from their careers page
    python -m jobsearch.run --no-browser    # never open the headless browser
    python -m jobsearch.run --fixtures tests/fixtures   # offline test using recorded JSON

Inputs
  leads/master_leads.csv      the master list (from the SYNDEO sheet)
  data/ats_map.json           company -> {ats, slug, ...}: which job board each company is read from.
                              A company with no entry is read from its careers page (what that page
                              loads decides the board, and the result is saved here). {"parent": "X"}
                              means "hires through X's board, which is on the list itself".
  data/history.json           every job ever seen, with first_seen / last_seen / status
  config.json                 focus keywords, min days for "likely filled", etc.

Outputs (output/YYYY-MM-DD/)
  open_roles.csv              every open role with a direct posting link
  new_this_week.csv           roles first seen this run
  closed_this_week.csv        roles that disappeared since last run  ("recently hired" signal)
  company_status.csv          per-company: ats, open count, new, closed, scrape status, note
  report.md / report.html     human-readable weekly report
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from . import boards as boards_mod
from . import browser as browser_mod
from . import company as company_mod
from . import verify as verify_mod
from . import feeds as feeds_mod
from .company import slugify

ROOT = Path(__file__).resolve().parent.parent
# JOBSEARCH_DATA / JOBSEARCH_OUT point a test run at scratch folders, so tests never touch real history or reports.
DATA = Path(os.environ.get("JOBSEARCH_DATA") or ROOT / "data")
OUT = Path(os.environ.get("JOBSEARCH_OUT") or ROOT / "output")
LEADS = ROOT / "leads" / "master_leads.csv"
ATS_MAP = DATA / "ats_map.json"
HISTORY = DATA / "history.json"
NEEDS_MAP = DATA / "needs_manual_mapping.csv"
CONFIG = ROOT / "config.json"


# ----------------------------------------------------------------- helpers
def _rel(p: Path) -> str:
    """Path as shown in logs: relative to the repo when it is inside it."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def load_json(p: Path, default):
    if p.exists():
        return json.loads(p.read_text())
    return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True))


MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def record_migrations(done: list, applied_path: Path) -> None:
    """Remember applied migrations. Called only once ats_map.json itself has been saved."""
    if done:
        save_json(applied_path, load_json(applied_path, []) + done)


def apply_migrations(ats_map: dict, applied_path: Path) -> list:
    """One-time changes to data/ats_map.json that ship with the code (jobsearch/migrations/*.json).

    Kept out of ats_map.json itself because the weekly run rewrites that file: a change made there by
    hand in a branch would collide with the run's own commit. Each migration is applied once and its
    id recorded next to the data, so later hand edits to ats_map.json are never overwritten.
    """
    applied = load_json(applied_path, [])
    done = []
    for path in sorted(MIGRATIONS.glob("*.json")) if MIGRATIONS.is_dir() else []:
        m = json.loads(path.read_text())
        if m["id"] in applied:
            continue
        for company, entry in m.get("set", {}).items():
            ats_map[company] = entry
        for company in m.get("remove", []):
            cur = ats_map.get(company) or {}
            if cur.get("ats") == "builtin" or (not cur.get("ats") and not cur.get("parent")
                                               and cur.get("confidence") not in ("manual", "excluded")):
                ats_map.pop(company, None)
        done.append(m["id"])
    return done


def load_leads() -> list[dict]:
    with LEADS.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: list[dict], cols: list[str]):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def rel_to_date(rel: str, today: dt.date) -> str:
    """'Posted 3 Days Ago' -> ISO date. Workday only."""
    m = re.search(r"(\d+)\+?\s*(day|week|month)", rel or "", re.I)
    if not m:
        return today.isoformat() if "today" in (rel or "").lower() else ""
    n, unit = int(m.group(1)), m.group(2).lower()
    days = n * {"day": 1, "week": 7, "month": 30}[unit]
    return (today - dt.timedelta(days=days)).isoformat()


def _norm(s: str) -> str:
    """'Super (Voice Ai)' -> 'super', 'Hired Helpr' -> 'hiredhelpr', 'Lineage.finance' -> 'lineagefinance'."""
    s = re.sub(r"\(.*?\)", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _domain_keys(website: str) -> set:
    host = re.sub(r"^https?://", "", (website or "").lower()).split("/")[0].replace("www.", "")
    return {k for k in (_norm(host), _norm(host.split(".")[0])) if k}


def match_companies(typed: str, leads: list[dict]) -> list[dict]:
    """Match a typed name to leads, most exact first: name, then web address, then partial name."""
    w = _norm(typed)
    if not w:
        return []
    exact = [l for l in leads if _norm(l["company"]) == w]
    if exact:
        return exact
    dom = [l for l in leads if w in _domain_keys(l.get("website", ""))]
    if dom:
        return dom
    return [l for l in leads if len(w) >= 4 and w in _norm(l["company"])]


def parse_company_list(text: str, leads: list[dict]) -> tuple[list[dict], list[str]]:
    """Accepts 'a, b, c', one-per-line, semicolons, or a pasted list whose separators got lost
    ('Nutiliti Second Nature Hired Helpr ...'). Returns (matched leads, names that matched nothing)."""
    picked, unmatched = [], []
    chunks = [c.strip() for c in re.split(r"[,;\n\r]+", text or "") if c.strip()]
    for chunk in chunks:
        hits = match_companies(chunk, leads)
        if hits:
            picked += [h for h in hits if h not in picked]
            continue
        # Separators lost: read left to right, taking the longest run of words (up to 4) that is a company
        words = re.sub(r"\(.*?\)", " ", chunk).split()
        i, leftover = 0, []
        while i < len(words):
            for n in range(min(4, len(words) - i), 0, -1):
                span = " ".join(words[i:i + n])
                w = _norm(span)
                hit = [l for l in leads if _norm(l["company"]) == w or w in _domain_keys(l.get("website", ""))]
                if hit:
                    picked += [h for h in hit if h not in picked]
                    i += n
                    break
            else:
                leftover.append(words[i]); i += 1
        if leftover:
            unmatched.append(" ".join(leftover))
    return picked, unmatched


def passes_company_filter(company: str, title: str, location: str, cfg: dict) -> bool:
    """Per-company include rules from config.json -> company_filters. Companies without a rule keep everything.
    A role is kept if its title OR location looks corporate/leadership, unless the title is an on-site role."""
    f = cfg.get("company_filters", {}).get(company)
    if not f:
        return True
    t, loc = (title or "").lower(), (location or "").lower()
    if any(w in t for w in f.get("always_keep_if_title_has", [])):  # seniority wins over on-site words
        return True
    if any(w in t for w in f.get("drop_if_title_has", [])):
        return False
    return any(w in t for w in f.get("keep_if_title_has", [])) or any(w in loc for w in f.get("keep_if_location_has", []))


def classify_role(title: str, cfg: dict) -> str:
    """Role group from config.json -> role_filter: executive | sales | gtm | engineering | "" (out of scope).
    Seniority wins first (a 'VP of Maintenance' is still an executive); then the exclude list;
    then sales, gtm, engineering in that order."""
    rf = cfg.get("role_filter") or {}
    if not rf.get("enabled"):
        return "all"
    t = (title or "").lower()
    groups = rf.get("groups", {})
    if any(re.search(p, t) for p in groups.get("executive", [])):
        return "executive"
    if any(re.search(p, t) for p in rf.get("exclude", [])):
        return ""
    for g in ("sales", "gtm", "engineering"):
        if any(re.search(p, t) for p in groups.get(g, [])):
            return g
    return ""


def focus_tag(title: str, cfg: dict) -> str:
    t = title.lower()
    for tag, words in cfg.get("focus_keywords", {}).items():
        if any(w in t for w in words):
            return tag
    return ""


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="")
    ap.add_argument("--only", default="", help="comma-separated company names")
    ap.add_argument("--detect", action="store_true",
                    help="read companies with no job board on file from their careers page")
    ap.add_argument("--no-browser", action="store_true", help="never open the headless browser")
    ap.add_argument("--fixtures", default="", help="dir of recorded JSON; no network")
    ap.add_argument("--date", default="", help="override run date (YYYY-MM-DD)")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--no-verify", action="store_true", help="skip link verification")
    ap.add_argument("--no-vc", action="store_true", help="skip VC portfolio job boards")
    ap.add_argument("--detect-minutes", type=float, default=30,
                    help="stop reading careers pages once the run is this many minutes old")
    ap.add_argument("--budget-minutes", type=float, default=40, help="skip link verification if run is past this")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    run_start = time.time()
    cfg = load_json(CONFIG, {})
    leads = load_leads()
    if args.tier:
        leads = [l for l in leads if l["tier"] == args.tier]
    adhoc = bool(args.only.strip())
    if adhoc:
        picked, unmatched = parse_company_list(args.only, leads)
        leads = picked
        if unmatched:
            print(f"  WARNING: not in the master leads list: {', '.join(unmatched)}", flush=True)
        print(f"  on-demand run for: {', '.join(l['company'] for l in leads) or '(nothing matched)'}", flush=True)

    ats_map = load_json(ATS_MAP, {})
    migrated = [] if args.fixtures else apply_migrations(ats_map, DATA / "migrations_applied.json")
    for mid in migrated:
        print(f"  migrate applied one-time change to ats_map.json: {mid}", flush=True)
    history = load_json(HISTORY, {})
    history = {k: v for k, v in history.items()
               if passes_company_filter(v.get("company", ""), v.get("title", ""), v.get("location", ""), cfg)}
    fixtures = Path(args.fixtures) if args.fixtures else None

    # ---- 1. read every company from its own job board
    #      Board on file in ats_map.json -> read it. No board on file (or the one on file has gone
    #      quiet or missing) -> work it out from the company's careers page and remember the answer.
    browser = None if (fixtures or args.no_browser) else browser_mod.shared()
    open_before = collections.Counter(h.get("company", "") for h in history.values() if h.get("status") == "open")
    all_jobs, company_rows, needs_manual = [], [], []
    for lead in leads:
        co = lead["company"]
        mapping = ats_map.get(co, {})
        t0 = time.time()
        in_time = (time.time() - run_start) <= args.detect_minutes * 60
        r = company_mod.read_company(lead, mapping, fixtures=fixtures, browser=browser, today=today.isoformat(),
                                     allow_page=bool(args.detect or adhoc) and in_time, open_before=open_before[co])
        if r.mapping is not None:
            ats_map[co] = mapping = r.mapping
        jobs, status = list(r.jobs), r.status
        if not fixtures:
            print(f"  read    {co:32} {mapping.get('ats') or '-':15} {len(jobs):5} jobs  {status[:70]}"
                  f"  ({time.time()-t0:.0f}s)" + (f"  [{r.note[:110]}]" if r.note else ""), flush=True)
        _seen = set()
        jobs = [j for j in jobs if not (j["job_key"] in _seen or _seen.add(j["job_key"]))]
        _before = len(jobs)
        jobs = [j for j in jobs if passes_company_filter(co, j["title"], j.get("location", ""), cfg)]
        if _before != len(jobs) and not fixtures:
            print(f"  filter  {co:32} kept {len(jobs)} of {_before} (company_filters rule)", flush=True)
        for j in jobs:
            j["company"] = co
            j["segment"] = lead["segment"]
            j["state"] = lead["state"]
            j["tier"] = lead["tier"]
            if j.get("_posted_rel") and not j.get("posted_at"):
                j["posted_at"] = rel_to_date(j["_posted_rel"], today)
            j["focus"] = focus_tag(j["title"], cfg)
        all_jobs.extend(jobs)
        company_rows.append({"company": co, "tier": lead["tier"], "ats": mapping.get("ats", ""),
                             "slug": mapping.get("slug", ""), "status": status, "open": len(jobs),
                             "new": 0, "closed": 0, "careers_url": lead["careers_url"], "note": r.note})
        if status == "unmapped" or status.startswith("needs-link"):
            needs_manual.append({"company": co, "careers_url": lead["careers_url"],
                                 "reason": status.split(": ", 1)[-1] if ": " in status else (r.note or status)})
        if not fixtures:
            time.sleep(args.sleep)
    status_of = {r["company"]: r["status"] for r in company_rows}
    for row in company_rows:                 # a company covered by its parent is only covered if the parent was read
        if row["status"].startswith("covered by parent: "):
            parent = row["status"].split(": ", 1)[1]
            if status_of.get(parent, "ok") != "ok":
                row["note"] = f"{parent} itself was not read this run ({status_of[parent][:60]})"
    if browser is not None:
        print(f"  browser {browser.pages_rendered} pages opened in the headless browser, {browser.seconds:.0f}s", flush=True)
    browser_mod.close_shared()
    save_json(ATS_MAP, ats_map)
    record_migrations(migrated, DATA / "migrations_applied.json")
    if not adhoc:
        write_csv(NEEDS_MAP, needs_manual, ["company", "careers_url", "reason"])
    ok_companies = {r["company"] for r in company_rows if r["status"] == "ok"}
    by_company = {r["company"]: r for r in company_rows}

    # ---- 2b. VC portfolio job boards (Getro). Fill gaps for master-list companies with no working board;
    #          everything else in scope goes to worth_adding.csv for review (never into the main report).
    discovery, vc_ok_boards = [], set()
    if cfg.get("vc_boards") and not fixtures and not args.no_vc:
        all_leads = load_leads()
        key_to_lead = {}
        for l in all_leads:
            for k in _domain_keys(l.get("website", "")) | {_norm(l["company"])}:
                key_to_lead.setdefault(k, l)
        in_run = {l["company"] for l in leads}
        for b in cfg["vc_boards"]:
            t0 = time.time()
            try:
                vjobs, note = feeds_mod.getro(b["url"], b["name"])
                vc_ok_boards.add(b["name"])
            except Exception as e:
                print(f"  vc      {b['name']:32} FAILED: {type(e).__name__}: {str(e)[:120]}", flush=True)
                continue
            filled = collections.Counter()
            for vj in vjobs:
                keys = _domain_keys(vj["org_domain"]) | {_norm(vj["org_name"])}
                lead = next((key_to_lead[k] for k in keys if k in key_to_lead), None)
                if lead is None:
                    if not adhoc:
                        grp = classify_role(vj["title"], cfg)
                        if grp:
                            discovery.append({**vj, "role_group": grp})
                    continue
                co = lead["company"]
                if co not in in_run or co in ok_companies:
                    continue  # not requested this run, or its own job board is the (better) source
                if by_company[co]["status"].startswith(("covered by parent", "excluded")):
                    continue  # deliberately not read on its own
                title_key = _norm(vj["title"]) + "|" + _norm(vj["location"])
                if any(_norm(j["title"]) + "|" + _norm(j.get("location", "")) == title_key
                       for j in all_jobs if j["company"] == co):
                    continue  # already found via LinkedIn / another source
                all_jobs.append({"job_key": f"getro:{b['name']}:{vj['id']}", "title": vj["title"],
                                 "location": vj["location"], "url": vj["url"], "posted_at": vj["posted_at"],
                                 "ats": f"vc:{b['name']}", "company": co, "segment": lead["segment"],
                                 "state": lead["state"], "tier": lead["tier"], "focus": focus_tag(vj["title"], cfg)})
                by_company[co]["open"] += 1
                if not filled[co]:
                    st = by_company[co]["status"]
                    by_company[co]["status"] = f"via VC board ({b['name']})" + ("" if st == "unmapped" else f"; own board {st}")
                filled[co] += 1
            print(f"  vc      {b['name']:32} {len(vjobs):5} jobs  {note}  ({time.time()-t0:.0f}s)"
                  + (f"  filled: {dict(filled)}" if filled else ""), flush=True)

    # ---- 3. diff against history
    seen_now = {j["job_key"] for j in all_jobs}
    new_jobs, closed_jobs = [], []
    # Which job system each company was read from this run, and which it has history from.
    # A company read from a system for the FIRST time (newly covered, or its board changed) is a
    # baseline, not news: its roles were already open, we just couldn't see them. Only roles the
    # board itself dates within the last week count as new. And its roles under the OLD system are
    # retired quietly rather than reported as closed.
    # A source is the job system AND the board on it ("greenhouse:acme"): a company that renames its
    # board is a change of source just as much as one that changes job systems.
    src = lambda key: ":".join(key.split(":", 2)[:2])
    read_from = collections.defaultdict(set)
    for j in all_jobs:
        read_from[j["company"]].add(src(j["job_key"]))
    for row in company_rows:   # read cleanly with nothing open: the source is still known from the board on file
        if row["status"] != "ok" or read_from[row["company"]] or not row["ats"]:
            continue
        if row["ats"] == "page":           # jobs read off a page are keyed by the company, either way they were read
            read_from[row["company"]] |= {f"page:{slugify(row['company'])}", f"jsonld:{slugify(row['company'])}"}
        else:
            ident = row["slug"] or ats_map.get(row["company"], {}).get("key", "")
            if ident:
                read_from[row["company"]].add(f"{row['ats']}:{ident}")
    known_from = collections.defaultdict(set)
    for key, h in history.items():
        known_from[h.get("company", "")].add(src(key))
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    for j in all_jobs:
        h = history.get(j["job_key"])
        if h is None:
            baseline = src(j["job_key"]) not in known_from[j["company"]]
            history[j["job_key"]] = {**{k: j[k] for k in ("company", "title", "location", "url", "posted_at", "ats", "focus")},
                                     "first_seen": today.isoformat(), "last_seen": today.isoformat(), "status": "open"}
            j["first_seen"] = today.isoformat()
            if baseline and not (j.get("posted_at") or "") >= week_ago:
                history[j["job_key"]]["baseline"] = True
                j["baseline"] = True
                continue
            new_jobs.append(j)
            by_company[j["company"]]["new"] += 1
        else:
            h["last_seen"] = today.isoformat()
            h["status"] = "open"
            h["title"], h["location"], h["url"] = j["title"], j["location"], j["url"]
            j["first_seen"] = h["first_seen"]
            if h.get("baseline"):
                j["baseline"] = True
    for key, h in history.items():
        # Only close roles for companies we scraped successfully this run.
        vc_src = key.split(":")[1] if key.startswith("getro:") else None
        if vc_src and h["status"] == "open" and key not in seen_now and h["company"] in ok_companies:
            h["status"] = "retired"  # company now has its own working board; VC copy no longer tracked
            continue
        can_close = (h["company"] in ok_companies) if not vc_src else \
                    (vc_src in vc_ok_boards and h["company"] in {l["company"] for l in leads})
        if (not vc_src and h["status"] == "open" and key not in seen_now and can_close
                and read_from[h["company"]] and src(key) not in read_from[h["company"]]):
            h["status"] = "retired"      # tracked under the company's previous job system; not a closure
            h["retired_on"] = today.isoformat()
            continue
        if h["status"] == "open" and key not in seen_now and can_close:
            h["status"] = "closed"
            h["closed_on"] = today.isoformat()
            first = dt.date.fromisoformat(h["first_seen"])
            h["days_open"] = (today - first).days
            closed_jobs.append({**h, "job_key": key})
            by_company.get(h["company"], {}).setdefault("closed", 0)
            if h["company"] in by_company:
                by_company[h["company"]]["closed"] += 1
    for j in all_jobs:
        j["days_open"] = (today - dt.date.fromisoformat(j["first_seen"])).days
        j["role_group"] = classify_role(j["title"], cfg)
    for c in closed_jobs:
        c["role_group"] = classify_role(c.get("title", ""), cfg)
    for r in company_rows:
        r["focus_open"] = sum(1 for j in all_jobs if j["company"] == r["company"] and j["role_group"])

    # ---- 3b. check links.
    #  A role that a job system's own feed no longer lists has closed: the feed is the authority.
    #  Its posting page proves nothing either way, because most job systems answer a closed
    #  posting's address with a normal-looking page. (Until Oct 2026 such roles were re-opened
    #  whenever that page loaded, so on Workday, Ashby, Rippling and others nothing ever closed.)
    #  Only where the list itself may be incomplete - a careers page, LinkedIn, a VC board, or a
    #  read that was cut short - does a posting that still loads mean "we missed it, keep it open".
    scraper_misses = []
    cut_short = {r["company"] for r in company_rows if "cut short" in (r.get("note") or "")}
    def feed_decides(c) -> bool:
        system = src(c["job_key"]).split(":")[0]
        # Workday is the exception: its long lists are read 20 at a time and can skip a posting,
        # and it has a reliable per-posting check (see verify.py), so each of its closures is checked.
        # iCIMS and Jobvite are read off web pages, several pages long, with no total to check
        # against, so a short read can't be told from a complete one: their closures are checked too.
        return (system in boards_mod.READERS and system not in ("workday", "icims", "jobvite")
                and c["company"] not in cut_short)
    # Roles the old link check had been holding open after they came down. They close now, quietly:
    # they did not close this week, so they are not this week's news.
    held_open = set()
    last = load_json(DATA / "last_run.json", {})
    prev_run = last.get("date", "")
    prev_misses = OUT / prev_run / "scraper_misses.csv"
    # Only for the first run after the change: from then on the misses file holds real misses.
    if prev_run and prev_misses.exists() and not last.get("feed_decides"):
        with prev_misses.open(newline="") as f:
            held_open = {row.get("job_key", "") for row in csv.DictReader(f)
                         if src(row.get("job_key", "")).split(":")[0] in boards_mod.READERS}
    over_budget = (time.time() - run_start) > args.budget_minutes * 60
    if over_budget and not fixtures:
        print("  verify  SKIPPED - run is over its time budget; closed roles kept unverified", flush=True)
    if not fixtures and not args.no_verify and not over_budget:
        # Every closed role is checked. Open roles: a rotating sample of up to N per company per run,
        # so big boards (Greystar) don't get the runner blocked; all roles get covered over a few weeks.
        per_co = 10**6 if adhoc else int(cfg.get("verify_per_company", 40))
        import random
        rng = random.Random(today.isoformat())
        sample = []
        in_scope = [j for j in all_jobs if j["role_group"]]
        for co in {j["company"] for j in in_scope}:
            rows = [j for j in in_scope if j["company"] == co]
            sample += rows if len(rows) <= per_co else rng.sample(rows, per_co)
        to_check = [c for c in closed_jobs if not feed_decides(c)]
        n = len(sample) + len(to_check)
        print(f"  verify  checking {n} posting links" + ("" if adhoc else f" ({len(in_scope)} in-scope open, sampled {per_co}/company; all closed)") + "...", flush=True)
        t0 = time.time()
        results = verify_mod.check_many([j["url"] for j in sample] + [c["url"] for c in to_check])
        print(f"  verify  done in {time.time()-t0:.0f}s", flush=True)
        for j in all_jobs:
            st, code = results.get(j["url"], ("listed in ATS, not rechecked" if j["role_group"] else "not checked (out of scope)", 0))
            if st in ("gone", "error"):
                # The job board itself lists this role as open this run, so it IS open.
                # The link check failing just means the page didn't load cleanly for our checker.
                st = f"open per job board; link check failed (HTTP {code or 'n/a'})"
            j["link_status"], j["link_http"] = st, code
        still_closed = []
        for c in closed_jobs:
            if feed_decides(c):
                c["link_status"], c["link_http"] = "no longer listed by the job board", 0
                still_closed.append(c)
                continue
            st, code = results.get(c["url"], ("error", 0))
            c["link_status"], c["link_http"] = st, code
            if st == "live":
                h = history[c["job_key"]]
                h["status"] = "open"; h.pop("closed_on", None); h.pop("days_open", None)
                h["last_seen"] = today.isoformat()
                scraper_misses.append(c)
                by_company[c["company"]]["closed"] -= 1
            else:
                still_closed.append(c)
        closed_jobs = still_closed
        vc = {}
        for j in all_jobs:
            vc[j["link_status"]] = vc.get(j["link_status"], 0) + 1
        print("link check (open roles):", vc, "| closed reopened as scraper misses:", len(scraper_misses))
    else:
        for j in all_jobs + closed_jobs:
            j.setdefault("link_status", "unchecked")
    late = [c for c in closed_jobs if c["job_key"] in held_open]
    for c in late:
        history[c["job_key"]]["late"] = True          # came down earlier than its closed_on date
        if c["company"] in by_company:
            by_company[c["company"]]["closed"] -= 1
    closed_jobs = [c for c in closed_jobs if c not in late]
    if late and not fixtures:
        print(f"  closed  {len(late)} roles that had already come down before this run were closed "
              "without being reported as this week's closures", flush=True)
    save_json(HISTORY, history)

    # ---- 4. outputs
    out_dir = OUT / (f"on-demand/{today.isoformat()}_{time.strftime('%H%M')}" if adhoc else today.isoformat())
    job_cols = ["company", "title", "location", "url", "link_status", "link_http", "posted_at", "first_seen", "days_open", "focus",
                "ats", "segment", "state", "tier", "job_key"]
    all_jobs.sort(key=lambda j: (j["tier"], j["company"], j["title"]))
    new_jobs.sort(key=lambda j: (j["tier"], j["company"], j["title"]))
    closed_jobs.sort(key=lambda j: (j["company"], j["title"]))
    job_cols.insert(job_cols.index("focus"), "role_group")
    all_open = all_jobs
    all_jobs = [j for j in all_open if j.get("role_group")]
    new_jobs = [j for j in new_jobs if j.get("role_group")]
    closed_jobs = [j for j in closed_jobs if j.get("role_group")]
    write_csv(out_dir / "all_open_roles.csv", all_open, job_cols)   # unfiltered, for data/analysis
    write_csv(out_dir / "open_roles.csv", all_jobs, job_cols)       # Sales / GTM / Engineering / VP+
    write_csv(out_dir / "new_this_week.csv", new_jobs, job_cols)
    write_csv(out_dir / "closed_this_week.csv", closed_jobs,
              ["company", "title", "location", "url", "link_status", "link_http", "first_seen", "closed_on", "days_open", "role_group", "ats", "job_key"])
    if discovery:
        discovery.sort(key=lambda d: (d["org_name"].lower(), d["title"]))
        write_csv(out_dir / "worth_adding.csv", discovery,
                  ["org_name", "org_domain", "title", "role_group", "location", "url", "apply_url", "posted_at", "board"])
    write_csv(out_dir / "scraper_misses.csv", scraper_misses,
              ["company", "title", "url", "link_status", "first_seen", "ats", "job_key"])
    write_csv(out_dir / "company_status.csv", company_rows,
              ["company", "tier", "ats", "slug", "status", "focus_open", "open", "new", "closed", "careers_url", "note"])
    write_report(out_dir, today, all_jobs, new_jobs, closed_jobs, company_rows, cfg, discovery)
    if not adhoc:
      save_json(DATA / "last_run.json", {"date": today.isoformat(), "companies": len(leads), "feed_decides": True,
                                                 "ok": len(ok_companies), "open_in_scope": len(all_jobs), "open_all": len(all_open),
                                                 "new": len(new_jobs), "closed": len(closed_jobs)})
    if adhoc:
        write_step_summary(all_jobs, company_rows, unmatched, out_dir)
    else:
        careers = {l["company"]: l.get("careers_url", "") for l in leads}
        draft = write_jobs_newsletter(all_jobs, closed_jobs, careers, today, cfg)
        print(f"  draft   newsletter section -> {_rel(draft)}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(f"# Now hiring - newsletter draft\n\nPaste-ready version: open `{_rel(draft.with_suffix('.html'))}` "
                        f"in a browser, select all, copy, paste into beehiiv.\n\n---\n\n"
                        + draft.read_text() + "\n---\n")
    print(f"[{today}] companies={len(leads)} scraped_ok={len(ok_companies)} open_in_scope={len(all_jobs)} open_all={len(all_open)} "
          f"new={len(new_jobs)} closed={len(closed_jobs)} -> {out_dir}")
    return 0


# ------------------------------------------------------- paste-ready HTML version of a draft
def draft_html(md_text: str, title: str) -> str:
    """Convert our draft Markdown (##, **bold**, _italic_, [links](url), '- ' bullets, ---) to plain,
    email-friendly HTML. Open it in a browser, select all, copy, paste into the newsletter editor."""
    import html as H

    def inline(t: str) -> str:
        t = H.escape(t, quote=False)
        t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![\w/])_(.+?)_(?![\w/])", r"<em>\1</em>", t)
        return t

    out, in_list = [], False
    for line in md_text.splitlines():
        l = line.rstrip()
        if l.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(l[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if not l:
            continue
        if l.startswith("## "):
            out.append(f"<h2>{inline(l[3:])}</h2>")
        elif l.startswith("# "):
            out.append(f"<h1>{inline(l[2:])}</h1>")
        elif l == "---":
            out.append("<hr>")
        elif re.fullmatch(r"\*\*[^*]+\*\*", l):
            out.append(f"<h3>{inline(l[2:-2])}</h3>")
        else:
            out.append(f"<p>{inline(l)}</p>")
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{H.escape(title)}</title>"
            "<style>body{font-family:Georgia,serif;max-width:680px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1a1a1a}"
            "h1{font-size:1.5rem}h2{font-size:1.35rem;margin-top:2rem}h3{font-size:1.05rem;margin:1.4rem 0 .4rem}"
            "a{color:#1a4d8f}li{margin:.35rem 0}hr{border:0;border-top:1px solid #ddd;margin:2rem 0}em{color:#555}</style>"
            f"</head><body>\n{body}\n</body></html>")


# ------------------------------------------------------- newsletter draft: Now hiring
SENIOR = [r"\bchief\b|\bc[a-z]o\b|\bpresident\b", r"\bsvp\b|\bevp\b", r"\bvp\b|vice president", r"\bhead of\b|general manager",
          r"\bdirector\b", r"\bprincipal\b|\bstaff\b", r"\bsenior\b|\bsr\.?\b|\blead\b|\bmanager\b", r"."]


def _seniority(title: str) -> int:
    t = (title or "").lower()
    return next(i for i, p in enumerate(SENIOR) if re.search(p, t))


SPLIT = r",|\s[-–—]\s|-\s|\("
SENIORITY_ONLY = {"vp", "svp", "evp", "director", "principal", "head", "manager", "lead", "senior", "sr", "staff"}


def _base_title(title: str) -> str:
    """'Regional Vice President of Sales, Central Region (IC-Enterprise)' -> 'regional vice president of sales'."""
    return " ".join(re.split(SPLIT, title or "")[0].lower().split())


def _exact_title(title: str) -> str:
    return " ".join(re.sub(r"\(.*?\)", "", title or "").lower().split())


def _qualifier(title: str) -> str:
    """The part after the base title, minus parentheticals: ', Central Region (IC-Enterprise)' -> 'Central Region'."""
    rest = (title or "")[len(re.split(SPLIT, title or "")[0]):]
    rest = re.sub(r"\(.*?\)|\(.*$", "", rest)
    rest = re.sub(r"(?i)\bregion\b", "", rest)
    return " ".join(rest.strip(" ,-–—").split())


def _collapse(roles: list, by_base: bool) -> list:
    """One line per role even when it's posted several times. by_base=True also merges variants of the same
    role ('Regional VP of Sales, East' + ', West'); by_base=False only merges identical titles.
    Returns list of (job_to_show, n_postings, places)."""
    groups = collections.OrderedDict()
    for j in roles:
        base = _base_title(j["title"])
        key = base if (by_base and len(base.split()) >= 2 and base not in SENIORITY_ONLY) else _exact_title(j["title"])
        groups.setdefault((j["company"], key), []).append(j)
    out = []
    for (_, key), js in groups.items():
        if len(js) == 1:
            out.append((js[0], 1, [_loc(js[0]["location"])] if _loc(js[0]["location"]) else []))
            continue
        quals = [q for q in dict.fromkeys(_qualifier(j["title"]) for j in js) if q]
        if len(quals) > 1:   # variants of one role: show the base title + the variants
            shown = {**js[0], "title": re.split(SPLIT, js[0]["title"])[0].strip()}
            places = quals
        else:                # same role in several places: show the title + cities
            shown = min(js, key=lambda j: len(j["title"]))
            places = list(dict.fromkeys(c for c in (_city(j["location"]) for j in js) if c))
        out.append((shown, len(js), places))
    return out


def _city(loc: str) -> str:
    l = _loc(loc)
    return l if l in ("Remote", "Multiple locations") else l.split(",")[0].strip()


def _loc(loc: str) -> str:
    loc = re.sub(r",?\s*(United States|USA|US)$", "", (loc or "").strip())
    if re.search(r"(?i)remote", loc):
        return "Remote"
    if re.search(r"(?i)^\d+ locations$", loc) or ";" in loc:
        return "Multiple locations"
    return loc[:40]


def write_jobs_newsletter(jobs, closed, careers: dict, today: dt.date, cfg: dict, max_companies: int = 8) -> Path:
    """Paste-ready 'Now hiring' section: roles first seen in the last 7 days, most senior first."""
    week_ago = today - dt.timedelta(days=7)
    fresh = [j for j in jobs if j.get("role_group") and j.get("first_seen") and not j.get("baseline") and
             dt.date.fromisoformat(j["first_seen"]) >= week_ago]
    cos = {j["company"] for j in fresh}
    md = ["## Now hiring", "",
          f"_{len(fresh)} new Sales, GTM, Engineering and leadership {'role' if len(fresh) == 1 else 'roles'} this week at "
          f"{len(cos)} {'company' if len(cos) == 1 else 'companies'} "
          f"across proptech and scattered site rental operations._", ""]

    lead = sorted([j for j in fresh if j["role_group"] == "executive"], key=lambda j: (_seniority(j["title"]), j["company"]))
    if lead:
        md += ["**Leadership roles**", ""]
        for j, n, places in _collapse(lead, by_base=True):
            where = (f" ({', '.join(places[:4])}{', +' + str(len(places) - 4) + ' more' if len(places) > 4 else ''})"
                     if places else "")
            md.append(f"- **{j['title']}**, {j['company']}{where}" + (f", {n} openings" if n > 1 else "")
                      + f". [Apply]({j['url']})")
        md.append("")

    for label, groups in (("Sales & GTM", ("sales", "gtm")), ("Engineering", ("engineering",))):
        pool = [j for j in fresh if j["role_group"] in groups]
        if not pool:
            continue
        by_co = collections.defaultdict(list)
        for j in pool:
            by_co[j["company"]].append(j)
        # companies with the most senior openings first, then the most openings
        order = sorted(by_co, key=lambda c: (min(_seniority(j["title"]) for j in by_co[c]), -len(by_co[c]), c))
        md += [f"**{label}**", ""]
        for c in order[:max_companies]:
            roles = [j for j, _, _ in _collapse(sorted(by_co[c], key=lambda j: _seniority(j["title"])), by_base=False)]
            shown = ", ".join(f"[{j['title']}]({j['url']})" for j in roles[:3])
            md.append(f"- **{c}**: {shown}")
        md.append("")

    filled = sorted([c for c in closed if _seniority(c.get("title", "")) <= 4 and c.get("role_group")],
                    key=lambda c: (_seniority(c["title"]), c["company"]))
    if filled:
        md += ["**Seats filled**", "",
               "_Senior roles that came down this week - a likely sign of a hire._", ""]
        md += [f"- **{c['title']}**, {c['company']}" for c in filled[:10]]
        md.append("")
    if not fresh and not filled:
        md.append("_No new roles in scope this week._")

    nl = OUT / "newsletter"
    nl.mkdir(parents=True, exist_ok=True)
    path = nl / f"now-hiring-{today.isoformat()}.md"
    path.write_text("\n".join(md))
    path.with_suffix(".html").write_text(draft_html("\n".join(md), f"Now hiring - {today.isoformat()}"))
    return path


# ------------------------------------------------------- run-page summary
def write_step_summary(all_jobs, company_rows, unmatched, out_dir):
    """On-demand runs: print results straight onto the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## On-demand job pull", ""]
    for c in company_rows:
        counts = f"{c.get('focus_open', c['open'])} Sales/GTM/Engineering/VP+ roles (of {c['open']} open)"
        note = {"unmapped": "no job board, LinkedIn page, or VC board listing found", "excluded": "excluded (see ats_map.json)"}.get(
            c["status"], counts if c["status"] == "ok" else (f"{counts} - {c['status']}" if c["status"].startswith("via VC") else c["status"]))
        lines.append(f"- **{c['company']}**: {note}")
    if unmatched:
        lines.append(f"- Not in the master leads list: {', '.join(unmatched)}")
    lines += ["", "| Company | Role | Type | Location | Link check | Link |", "|---|---|---|---|---|---|"]
    for j in all_jobs[:500]:
        lines.append(f"| {j['company']} | {j['title']} | {j.get('role_group','')} | {j['location']} | {j.get('link_status','')} | [open posting]({j['url']}) |")
    if len(all_jobs) > 500:
        lines.append(f"\n_First 500 of {len(all_jobs)} shown. Full list: `{_rel(out_dir)}/open_roles.csv`_")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as f:
            f.write(text)
    else:
        print(text)


# ------------------------------------------------------------------ report
def write_report(out_dir: Path, today, all_jobs, new_jobs, closed_jobs, company_rows, cfg, discovery=None):
    ok = [c for c in company_rows if c["status"] == "ok"]
    bad = [c for c in company_rows if c["status"] != "ok"]
    top = sorted(ok, key=lambda c: -c["open"])[:15]
    focus_new = [j for j in new_jobs if j.get("focus")]

    md = [f"# Syndeo weekly hiring signal — week of {today.isoformat()}", ""]
    md += ["_Showing Sales, GTM, Engineering, and VP-level+ roles only (rules in config.json → role_filter). "
           "Every role is still tracked; the full list is in all_open_roles.csv._", ""]
    md += [f"**{len(all_jobs)} open roles in scope** across **{len(ok)} companies** scraped successfully "
           f"({len(bad)} companies need attention). **{len(new_jobs)} new this week**, "
           f"**{len(closed_jobs)} closed / likely filled**.", ""]

    md += ["## Closed since last run — likely filled (\"recently hired\" signal)", ""]
    if closed_jobs:
        md += ["Each posting below was re-checked this run and is confirmed gone (or blocked from checking).", "",
               "| Company | Role | Location | Days open | Link check | Was at |", "|---|---|---|---|---|---|"]
        for j in closed_jobs:
            md.append(f"| {j['company']} | {j['title']} | {j['location']} | {j.get('days_open','')} | {j.get('link_status','')} | [posting]({j['url']}) |")
    else:
        md.append("_No history yet — closures appear from the second run onward._")
    md.append("")

    md += ["## New roles this week", ""]
    if new_jobs:
        md += ["| Company | Role | Location | Focus | Link |", "|---|---|---|---|---|"]
        for j in new_jobs:
            md.append(f"| {j['company']} | {j['title']} | {j['location']} | {j.get('focus','')} | [apply]({j['url']}) |")
    else:
        md.append("_None_")
    md.append("")

    md += ["## Most active companies (open roles)", "", "| Company | Tier | ATS | Open | New | Closed |", "|---|---|---|---|---|---|"]
    for c in top:
        md.append(f"| {c['company']} | {c['tier']} | {c['ats']} | {c['open']} | {c['new']} | {c['closed']} |")
    md.append("")

    md += ["## All open roles", "", "| Company | Role | Location | Posted | Link check | Link |", "|---|---|---|---|---|---|"]
    for j in all_jobs:
        md.append(f"| {j['company']} | {j['title']} | {j['location']} | {j.get('posted_at','')} | {j.get('link_status','')} | [apply]({j['url']}) |")
    md.append("")

    if discovery:
        cos = collections.Counter(d["org_name"] for d in discovery)
        md += ["## Worth adding? Companies NOT on your list hiring Sales / GTM / Engineering / VP+ (from VC portfolio boards)", "",
               f"_{len(discovery)} in-scope roles at {len(cos)} companies. Full list with links: worth_adding.csv. "
               "Add a company to leads/master_leads.csv to start tracking it properly._", "",
               "| Company | In-scope roles | Board |", "|---|---|---|"]
        for name, n in cos.most_common(25):
            md.append(f"| {name} | {n} | {next(d['board'] for d in discovery if d['org_name']==name)} |")
        md.append("")
    moved = [c for c in ok if "job board changed" in (c.get("note") or "")]
    if moved:
        md += ["## Job board changed — worth a glance", "",
               "_The board on file had gone quiet or missing, and the company's careers page now loads "
               "its jobs from a different one. The new board was read and saved in data/ats_map.json._", ""]
        md += [f"- **{c['company']}** — {c['note']}" for c in moved]
        md.append("")
    covered = [c for c in bad if c["status"].startswith(("covered by parent", "excluded"))]
    bad = [c for c in bad if c not in covered]
    md += ["## Companies needing attention", "",
           "_`needs-link` means the careers link on file couldn't be read: put the company's job-board "
           "link in data/ats_map.json or fix its careers link. `failed` means something went wrong this "
           "run; last run's roles are kept and nothing is reported closed._", "",
           "| Company | Status | Careers page |", "|---|---|---|"]
    for c in bad:
        md.append(f"| {c['company']} | {c['status']} | {c['careers_url']} |")
    md.append("")
    if covered:
        md += ["## Not read on their own", "", "| Company | Why | Note |", "|---|---|---|"]
        md += [f"| {c['company']} | {c['status']} | {(c.get('note') or '').replace('|', '/')} |" for c in covered]
        md.append("")
    (out_dir / "report.md").write_text("\n".join(md))

    # minimal HTML (same content, sortable-ish tables)
    import html as H
    def table(cols, rows):
        h = "<table><thead><tr>" + "".join(f"<th>{H.escape(c)}</th>" for c in cols) + "</tr></thead><tbody>"
        for r in rows:
            h += "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
        return h + "</tbody></table>"
    link = lambda u, t="apply": f'<a href="{H.escape(u)}" target="_blank">{t}</a>'
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Syndeo hiring signal {today}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin-bottom:2rem}}th,td{{border-bottom:1px solid #e5e5e5;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f6f6;position:sticky;top:0}}h2{{margin-top:2.5rem}}.kpi{{display:flex;gap:2rem;margin:1rem 0}}.kpi div{{background:#f6f6f6;padding:1rem;border-radius:8px}}.kpi b{{font-size:1.6rem;display:block}}</style></head><body>
<h1>Syndeo weekly hiring signal — {today}</h1>
<p><i>Sales, GTM, Engineering and VP-level+ roles only. Full list: all_open_roles.csv.</i></p>
<div class="kpi"><div><b>{len(all_jobs)}</b>open roles in scope</div><div><b>{len(new_jobs)}</b>new this week</div><div><b>{len(closed_jobs)}</b>closed / likely filled</div><div><b>{len(ok)}/{len(company_rows)}</b>companies scraped</div></div>
<h2>Closed since last run — likely filled</h2>{table(["Company","Role","Location","Days open","Link check","Was at"], [[H.escape(j['company']),H.escape(j['title']),H.escape(j['location']),j.get('days_open',''),j.get('link_status',''),link(j['url'],'posting')] for j in closed_jobs]) if closed_jobs else '<p><i>No history yet — closures appear from the second run onward.</i></p>'}
<h2>New roles this week</h2>{table(["Company","Role","Location","Focus","Link"], [[H.escape(j['company']),H.escape(j['title']),H.escape(j['location']),H.escape(j.get('focus','')),link(j['url'])] for j in new_jobs])}
<h2>Most active companies</h2>{table(["Company","Tier","ATS","Open","New","Closed"], [[H.escape(c['company']),c['tier'],c['ats'],c['open'],c['new'],c['closed']] for c in top])}
<h2>All open roles in scope</h2>{table(["Company","Role","Type","Location","Posted","Link check","Link"], [[H.escape(j['company']),H.escape(j['title']),j.get('role_group',''),H.escape(j['location']),j.get('posted_at',''),j.get('link_status',''),link(j['url'])] for j in all_jobs])}
<h2>Companies needing attention</h2>{table(["Company","Status","Careers page"], [[H.escape(c['company']),H.escape(c['status']),link(c['careers_url'],'careers')] for c in bad])}
</body></html>"""
    (out_dir / "report.html").write_text(page)


if __name__ == "__main__":
    sys.exit(main())
