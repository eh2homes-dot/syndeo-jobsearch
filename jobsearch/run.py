"""Weekly job-search run.

    python -m jobsearch.run                 # full run (network)
    python -m jobsearch.run --tier A        # only tier-A companies
    python -m jobsearch.run --only Entrata,Kiavi
    python -m jobsearch.run --detect        # also auto-detect ATS for unmapped companies
    python -m jobsearch.run --fixtures tests/fixtures   # offline test using recorded JSON

Inputs
  leads/master_leads.csv      the master list (from the SYNDEO sheet)
  data/ats_map.json           company -> {ats, slug, ...}; manual/verified entries win
  data/history.json           every job ever seen, with first_seen / last_seen / status
  config.json                 focus keywords, min days for "likely filled", etc.

Outputs (output/YYYY-MM-DD/)
  open_roles.csv              every open role with a direct posting link
  new_this_week.csv           roles first seen this run
  closed_this_week.csv        roles that disappeared since last run  ("recently hired" signal)
  company_status.csv          per-company: ats, open count, new, closed, scrape status
  report.md / report.html     human-readable weekly report
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from .adapters import ADAPTERS
from . import detect as detect_mod
from . import verify as verify_mod

ROOT = Path(__file__).resolve().parent.parent
LEADS = ROOT / "leads" / "master_leads.csv"
ATS_MAP = ROOT / "data" / "ats_map.json"
HISTORY = ROOT / "data" / "history.json"
NEEDS_MAP = ROOT / "data" / "needs_manual_mapping.csv"
CONFIG = ROOT / "config.json"
OUT = ROOT / "output"

UNSUPPORTED = {"icims", "paylocity", "ukg", "adp", "paradox", "jazzhr", "betterteam"}  # detected but no JSON adapter yet


# ----------------------------------------------------------------- helpers
def load_json(p: Path, default):
    if p.exists():
        return json.loads(p.read_text())
    return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True))


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


# ----------------------------------------------------------------- scraping
def scrape_company(lead: dict, mapping: dict, fixtures: Path | None) -> tuple[list[dict], str]:
    """Returns (jobs, status). status in: ok | unmapped | unsupported | failed:<reason>"""
    ats = mapping.get("ats", "")
    if mapping.get("confidence") == "excluded":
        return [], "excluded"
    if not ats:
        return [], "unmapped"
    if ats in UNSUPPORTED:
        return [], f"unsupported:{ats}"
    if fixtures:
        fx = fixtures / f"{slugify(lead['company'])}.json"
        if not fx.exists():
            return [], "failed:no fixture"
        raw = json.loads(fx.read_text())
        jobs = raw if isinstance(raw, list) else raw.get("jobs", [])
        return jobs, "ok"
    try:
        kwargs = {k: v for k, v in mapping.items() if k not in ("ats", "slug")}
        kwargs.setdefault("careers_url", lead["careers_url"])
        jobs = ADAPTERS[ats](mapping["slug"], **kwargs)
        return jobs, "ok"
    except Exception as e:  # noqa
        return [], f"failed:{type(e).__name__}: {str(e)[:140]}"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="")
    ap.add_argument("--only", default="", help="comma-separated company names")
    ap.add_argument("--detect", action="store_true", help="auto-detect ATS for unmapped companies")
    ap.add_argument("--fixtures", default="", help="dir of recorded JSON; no network")
    ap.add_argument("--date", default="", help="override run date (YYYY-MM-DD)")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--no-verify", action="store_true", help="skip link verification")
    ap.add_argument("--detect-minutes", type=float, default=15, help="time budget for ATS detection")
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
        wanted = [x.strip().lower() for x in args.only.split(",") if x.strip()]
        picked, unmatched = [], []
        for w in wanted:
            hits = [l for l in leads if w == l["company"].lower()] or \
                   [l for l in leads if w in l["company"].lower() or w in l["website"].lower()]
            if hits:
                picked += [h for h in hits if h not in picked]
            else:
                unmatched.append(w)
        leads = picked
        if unmatched:
            print(f"  WARNING: not in the master leads list: {', '.join(unmatched)}", flush=True)
        print(f"  on-demand run for: {', '.join(l['company'] for l in leads) or '(nothing matched)'}", flush=True)

    ats_map = load_json(ATS_MAP, {})
    history = load_json(HISTORY, {})
    history = {k: v for k, v in history.items()
               if passes_company_filter(v.get("company", ""), v.get("title", ""), v.get("location", ""), cfg)}
    fixtures = Path(args.fixtures) if args.fixtures else None

    # ---- 1. ATS mapping (manual/verified entries are never overwritten)
    needs_manual = []
    for lead in leads:
        m = ats_map.get(lead["company"])
        if m and (m.get("ats") or m.get("confidence") in ("excluded", "manual")):
            continue
        if args.detect and not fixtures and (time.time() - run_start) > args.detect_minutes * 60:
            needs_manual.append({"company": lead["company"], "careers_url": lead["careers_url"],
                                 "reason": "detection time budget used up this run; will retry next run"})
            continue
        if args.detect and not fixtures:
            t0 = time.time()
            hit = detect_mod.detect(lead)
            print(f"  detect  {lead['company']:32} -> {hit.get('ats') or '-':15} {hit.get('slug',''):22} "
                  f"({hit['confidence']}, {time.time()-t0:.0f}s)", flush=True)
            hit["detected_on"] = today.isoformat()
            ats_map[lead["company"]] = hit
            time.sleep(args.sleep)
            if hit["confidence"] == "none":
                needs_manual.append({"company": lead["company"], "careers_url": lead["careers_url"],
                                     "reason": hit["evidence"]})
        else:
            needs_manual.append({"company": lead["company"], "careers_url": lead["careers_url"],
                                 "reason": "not in ats_map.json (run with --detect or map by hand)"})
    save_json(ATS_MAP, ats_map)
    if not adhoc:
        write_csv(NEEDS_MAP, needs_manual, ["company", "careers_url", "reason"])

    # ---- 2. scrape
    all_jobs, company_rows = [], []
    for lead in leads:
        mapping = ats_map.get(lead["company"], {})
        t0 = time.time()
        jobs, status = scrape_company(lead, mapping, fixtures)
        if mapping.get("ats") and not fixtures:
            print(f"  scrape  {lead['company']:32} {len(jobs):5} jobs  {status[:60]}  ({time.time()-t0:.0f}s)", flush=True)
        _seen = set()
        jobs = [j for j in jobs if not (j["job_key"] in _seen or _seen.add(j["job_key"]))]
        _before = len(jobs)
        jobs = [j for j in jobs if passes_company_filter(lead["company"], j["title"], j.get("location", ""), cfg)]
        if _before != len(jobs) and not fixtures:
            print(f"  filter  {lead['company']:32} kept {len(jobs)} of {_before} (company_filters rule)", flush=True)
        for j in jobs:
            j["company"] = lead["company"]
            j["segment"] = lead["segment"]
            j["state"] = lead["state"]
            j["tier"] = lead["tier"]
            if j.get("_posted_rel") and not j.get("posted_at"):
                j["posted_at"] = rel_to_date(j["_posted_rel"], today)
            j["focus"] = focus_tag(j["title"], cfg)
        all_jobs.extend(jobs)
        company_rows.append({"company": lead["company"], "tier": lead["tier"], "ats": mapping.get("ats", ""),
                             "slug": mapping.get("slug", ""), "status": status, "open": len(jobs),
                             "new": 0, "closed": 0, "careers_url": lead["careers_url"]})
        if not fixtures:
            time.sleep(args.sleep)
    ok_companies = {r["company"] for r in company_rows if r["status"] == "ok"}
    by_company = {r["company"]: r for r in company_rows}

    # ---- 3. diff against history
    seen_now = {j["job_key"] for j in all_jobs}
    new_jobs, closed_jobs = [], []
    for j in all_jobs:
        h = history.get(j["job_key"])
        if h is None:
            history[j["job_key"]] = {**{k: j[k] for k in ("company", "title", "location", "url", "posted_at", "ats", "focus")},
                                     "first_seen": today.isoformat(), "last_seen": today.isoformat(), "status": "open"}
            j["first_seen"] = today.isoformat()
            new_jobs.append(j)
            by_company[j["company"]]["new"] += 1
        else:
            h["last_seen"] = today.isoformat()
            h["status"] = "open"
            h["title"], h["location"], h["url"] = j["title"], j["location"], j["url"]
            j["first_seen"] = h["first_seen"]
    for key, h in history.items():
        # Only close roles for companies we scraped successfully this run.
        if h["status"] == "open" and key not in seen_now and h["company"] in ok_companies:
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

    # ---- 3b. verify every link; reopen "closed" roles whose posting is still live
    scraper_misses = []
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
        n = len(sample) + len(closed_jobs)
        print(f"  verify  checking {n} posting links" + ("" if adhoc else f" ({len(in_scope)} in-scope open, sampled {per_co}/company; all closed)") + "...", flush=True)
        t0 = time.time()
        results = verify_mod.check_many([j["url"] for j in sample] + [c["url"] for c in closed_jobs])
        print(f"  verify  done in {time.time()-t0:.0f}s", flush=True)
        for j in all_jobs:
            j["link_status"], j["link_http"] = results.get(j["url"], ("listed in ATS, not rechecked" if j["role_group"] else "not checked (out of scope)", 0))
        still_closed = []
        for c in closed_jobs:
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
    save_json(HISTORY, history)

    # ---- 4. outputs
    out_dir = OUT / (f"on-demand/{today.isoformat()}_{time.strftime('%H%M')}" if adhoc else today.isoformat())
    job_cols = ["company", "title", "location", "url", "link_status", "posted_at", "first_seen", "days_open", "focus",
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
              ["company", "title", "location", "url", "link_status", "first_seen", "closed_on", "days_open", "role_group", "ats", "job_key"])
    write_csv(out_dir / "scraper_misses.csv", scraper_misses,
              ["company", "title", "url", "link_status", "first_seen", "ats", "job_key"])
    write_csv(out_dir / "company_status.csv", company_rows,
              ["company", "tier", "ats", "slug", "status", "focus_open", "open", "new", "closed", "careers_url"])
    write_report(out_dir, today, all_jobs, new_jobs, closed_jobs, company_rows, cfg)
    if not adhoc:
      save_json(ROOT / "data" / "last_run.json", {"date": today.isoformat(), "companies": len(leads),
                                                 "ok": len(ok_companies), "open_in_scope": len(all_jobs), "open_all": len(all_open),
                                                 "new": len(new_jobs), "closed": len(closed_jobs)})
    if adhoc:
        write_step_summary(all_jobs, company_rows, unmatched, out_dir)
    print(f"[{today}] companies={len(leads)} scraped_ok={len(ok_companies)} open_in_scope={len(all_jobs)} open_all={len(all_open)} "
          f"new={len(new_jobs)} closed={len(closed_jobs)} -> {out_dir}")
    return 0


# ------------------------------------------------------- run-page summary
def write_step_summary(all_jobs, company_rows, unmatched, out_dir):
    """On-demand runs: print results straight onto the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## On-demand job pull", ""]
    for c in company_rows:
        note = {"unmapped": "not mapped to a job board yet", "excluded": "excluded (see ats_map.json)"}.get(
            c["status"], c["status"] if c["status"] != "ok" else f"{c.get('focus_open', c['open'])} Sales/GTM/Engineering/VP+ roles (of {c['open']} open)")
        lines.append(f"- **{c['company']}**: {note}")
    if unmatched:
        lines.append(f"- Not in the master leads list: {', '.join(unmatched)}")
    lines += ["", "| Company | Role | Type | Location | Link check | Link |", "|---|---|---|---|---|---|"]
    for j in all_jobs[:500]:
        lines.append(f"| {j['company']} | {j['title']} | {j.get('role_group','')} | {j['location']} | {j.get('link_status','')} | [open posting]({j['url']}) |")
    if len(all_jobs) > 500:
        lines.append(f"\n_First 500 of {len(all_jobs)} shown. Full list: `{out_dir.relative_to(ROOT)}/open_roles.csv`_")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a") as f:
            f.write(text)
    else:
        print(text)


# ------------------------------------------------------------------ report
def write_report(out_dir: Path, today, all_jobs, new_jobs, closed_jobs, company_rows, cfg):
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

    md += ["## Companies needing attention", "", "| Company | Status | Careers page |", "|---|---|---|"]
    for c in bad:
        md.append(f"| {c['company']} | {c['status']} | {c['careers_url']} |")
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
