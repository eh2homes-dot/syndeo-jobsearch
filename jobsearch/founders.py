"""Founder-led hiring: the small-company slice of the weekly job search.

    python -m jobsearch.founders                 # this week's founder-led roles (needs founder_hiring.enabled in config.json)
    python -m jobsearch.founders --force         # run even if the toggle is off
    python -m jobsearch.founders --days 14 --max-headcount 75
    python -m jobsearch.founders --record        # also log the picked companies for the cooldown

The premise: a role posted at a company of ~50 people or fewer is founder-led hiring by
definition. So this reads what the weekly search and the OpCo search already found,
keeps only roles first seen in the last N days at companies small enough (or marked
founder-led) in leads/founders.csv, ranks them, and writes a draft in the founder-item
format plus a review file of companies that had new roles but no headcount on file.

Nothing here is looked up or guessed. Headcount, stage and founder details come only from
leads/founders.csv. A blank there shows up as [VERIFY] in the draft, never as a value.

Reads
    data/history.json              weekly search: every role, with first_seen / status
    out/opco-jobs-latest.json      OpCo search: this run's roles, with posted dates
    leads/founders.csv             company -> headcount, stage, founder name/links, founder_led override
    data/founder_featured.json     which companies ran in which founder issue (cooldown)
    config.json -> founder_hiring  the toggle and the thresholds

Writes (output/founders/)
    founder-roles-YYYY-MM-DD.md    the draft, one item per company, grouped by stage
    founder-roles-YYYY-MM-DD.csv   every eligible role with its score
    founder-roles-YYYY-MM-DD.json  the same, for the newsletter skill
    needs-review-YYYY-MM-DD.csv    companies with fresh roles but no headcount / founder on file
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from . import run

FOUNDERS = run.ROOT / "leads" / "founders.csv"
OPCO_LATEST = run.ROOT / "out" / "opco-jobs-latest.json"

FOUNDER_COLS = ["company", "headcount", "stage", "founder_name", "founder_linkedin", "founder_x", "founder_led", "notes"]

DEFAULTS = {
    "enabled": False,
    "window_days": 7,
    "max_headcount": 50,
    "max_items": 12,
    "min_items": 6,
    "roles_per_company": 1,
    "cooldown_issues": 3,
    "cooldown_exempt": ["Second Nature"],
    "us_only": True,
}

# Stage labels in leads/founders.csv -> newsletter bucket. Matching is loose and lowercased.
BUCKETS = [
    ("Build -1 → 0", "Get on the ground floor with the founding team of a pre-seed or seed-stage company.",
     r"pre-?seed|seed|angel|bootstrap|stealth"),
    ("Build 0 → 1", "Accelerate a Series A–C company with early momentum.",
     r"series ?[abc]\b|early|growth-?stage a|\ba\b|\bb\b|\bc\b"),
    ("Build 1 → beyond", "Join an established company scaling to its next horizon.",
     r"series ?[d-z]|growth|late|established|public|scaled|operator"),
]
UNKNOWN_BUCKET = ("Stage unknown [VERIFY]", "Fill in `stage` in leads/founders.csv to place these.")

FOUNDING = r"\b(founding|first|head of|chief of staff|general manager|gm)\b"


# ------------------------------------------------------------------ config
def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("founder_hiring") or {})
    return s


def founder_path() -> Path:
    return FOUNDERS


def featured_path() -> Path:
    return run.DATA / "founder_featured.json"


# ------------------------------------------------------------------ registry
def _int(s: str) -> int | None:
    s = re.sub(r"[^0-9]", "", s or "")
    return int(s) if s else None


def _bool(s: str) -> bool | None:
    s = (s or "").strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def load_founders(path: Path | None = None) -> dict:
    """norm company -> row from leads/founders.csv (headcount as int, founder_led as bool/None)."""
    p = path or founder_path()
    if not p.exists():
        return {}
    out = {}
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("company") or "").strip()
            if not name:
                continue
            r = {k: (row.get(k) or "").strip() for k in FOUNDER_COLS}
            r["company"] = name
            r["headcount_n"] = _int(r["headcount"])
            r["founder_led_b"] = _bool(r["founder_led"])
            out[run._norm_co(name)] = r
    return out


def eligible(reg_row: dict | None, max_headcount: int) -> tuple[bool, str]:
    """Is this company founder-led by the rules? Returns (yes/no, reason)."""
    if reg_row is None:
        return False, "not in leads/founders.csv"
    if reg_row["founder_led_b"] is False:
        return False, "founder_led = FALSE"
    if reg_row["founder_led_b"] is True:
        return True, "founder_led = TRUE"
    hc = reg_row["headcount_n"]
    if hc is None:
        return False, "no headcount on file"
    if hc <= max_headcount:
        return True, f"headcount {hc} ≤ {max_headcount}"
    return False, f"headcount {hc} > {max_headcount}"


def bucket_for(stage: str) -> tuple[str, str]:
    s = (stage or "").strip().lower()
    if not s:
        return UNKNOWN_BUCKET
    for label, blurb, pat in BUCKETS:
        if re.search(pat, s):
            return label, blurb
    return UNKNOWN_BUCKET


# ------------------------------------------------------------------ roles
def _parse_date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def fresh_roles(history: dict, opco: dict, today: dt.date, window_days: int) -> list[dict]:
    """Open roles first seen (or posted) within the window, from both searches, deduplicated by URL."""
    cutoff = today - dt.timedelta(days=window_days)
    seen_urls, out = set(), []

    def add(role: dict, source: str):
        url = role.get("url") or ""
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        out.append({"source": source, **role})

    for key, j in history.items():
        if j.get("status") != "open":
            continue
        posted = _parse_date(j.get("posted_at", ""))
        first = _parse_date(j.get("first_seen", ""))
        # Baseline roles were already open when the company was first read; only trust a posted date for them.
        when = posted or (None if j.get("baseline") else first)
        if when is None or when < cutoff:
            continue
        add({"company": j.get("company", ""), "title": j.get("title", ""), "url": j.get("url", ""),
             "location": j.get("location", ""), "first_seen": when.isoformat(), "ats": j.get("ats", ""),
             "key": key}, "weekly")

    new_ids = set(opco.get("new") or [])
    for r in opco.get("roles") or []:
        if r.get("status") not in (None, "", "ok", "open"):
            continue
        posted = _parse_date(r.get("posted", ""))
        is_new = r.get("id") in new_ids
        when = posted if posted else (_parse_date(opco.get("run_date", "")) if is_new else None)
        if when is None or when < cutoff:
            continue
        add({"company": r.get("company", ""), "title": r.get("title", ""), "url": r.get("url", ""),
             "location": r.get("location", ""), "first_seen": when.isoformat(), "ats": r.get("ats", ""),
             "key": r.get("id", "")}, "opco")
    return out


def score(role: dict, reg: dict, today: dt.date) -> dict:
    """0–13. Freshness, how founder-shaped the role is, how small the company is."""
    days = (today - dt.date.fromisoformat(role["first_seen"])).days
    fresh = 5 if days <= 7 else 3 if days <= 14 else 1
    t = (role.get("title") or "").lower()
    sen = run._seniority(role.get("title", ""))
    if re.search(FOUNDING, t) or sen <= run.SENIOR.index(r"\bhead of\b|general manager"):
        interest = 5
    elif sen <= run.DIRECTOR_LEVEL:
        interest = 3
    else:
        interest = 2
    hc = reg.get("headcount_n")
    size = 3 if (hc is not None and hc <= 15) else 2 if (hc is not None and hc <= 50) else 1
    return {"score": fresh + interest + size, "s_fresh": fresh, "s_role": interest, "s_size": size, "days_open": days}


# ------------------------------------------------------------------ cooldown
def _load_ledger() -> dict:
    led = run.load_json(featured_path(), {})
    return led if isinstance(led.get("issues"), dict) else {"issues": {}}


def in_cooldown(led: dict, company: str, today: dt.date, n_issues: int, exempt: list) -> bool:
    if run._norm_co(company) in {run._norm_co(e) for e in exempt}:
        return False
    recent = sorted((d for d in led["issues"] if d < today.isoformat()), reverse=True)[:n_issues]
    n = run._norm_co(company)
    return any(n in {run._norm_co(c) for c in led["issues"][d].get("companies", [])} for d in recent)


def record(led: dict, today: dt.date, companies: list) -> None:
    day = today.isoformat()
    if led["issues"].get(day, {}).get("source") == "published":
        return
    led["issues"][day] = {"source": "draft", "companies": sorted(set(companies))}
    led["issues"] = dict(sorted(led["issues"].items()))
    run.save_json(featured_path(), led)


# ------------------------------------------------------------------ select
def select(roles: list[dict], registry: dict, cfg_s: dict, today: dt.date, role_cfg: dict,
           led: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (picked items, all eligible scored roles, needs-review companies)."""
    led = led or {"issues": {}}
    eligible_roles, review, dropped = [], {}, []
    for r in roles:
        n = run._norm_co(r["company"])
        reg = registry.get(n)
        ok, why = eligible(reg, cfg_s["max_headcount"])
        if not ok:
            if reg is None or reg.get("headcount_n") is None and reg.get("founder_led_b") is None:
                rv = review.setdefault(n, {"company": r["company"], "new_roles": 0, "reason": why,
                                          "sample_titles": [], "source": r["source"]})
                rv["new_roles"] += 1
                if len(rv["sample_titles"]) < 3:
                    rv["sample_titles"].append(r["title"])
            continue
        if cfg_s.get("us_only") and not run._is_us(r.get("location", "")):
            continue
        grp = run.classify_role(r.get("title", ""), role_cfg)
        if grp == "":
            continue  # the shared exclude list (recruiting, legal, maintenance, ...)
        if in_cooldown(led, r["company"], today, int(cfg_s["cooldown_issues"]), cfg_s.get("cooldown_exempt", [])):
            dropped.append({**r, "dropped": "cooldown"})
            continue
        sc = score(r, reg, today)
        eligible_roles.append({**r, **sc, "role_group": grp, "eligible_because": why,
                               "headcount": reg.get("headcount", ""), "stage": reg.get("stage", ""),
                               "founder_name": reg.get("founder_name", ""),
                               "founder_linkedin": reg.get("founder_linkedin", ""),
                               "founder_x": reg.get("founder_x", "")})

    eligible_roles.sort(key=lambda r: (-r["score"], r["days_open"], r["company"], r["title"]))

    # One item per company: its best role, plus how many more it has this week.
    per_co: dict[str, dict] = {}
    for r in eligible_roles:
        n = run._norm_co(r["company"])
        if n not in per_co:
            per_co[n] = {**r, "more_roles": [], "more_count": 0}
        else:
            per_co[n]["more_count"] += 1
            if len(per_co[n]["more_roles"]) < int(cfg_s["roles_per_company"]) + 2:
                per_co[n]["more_roles"].append({"title": r["title"], "url": r["url"]})
    items = sorted(per_co.values(), key=lambda r: (-r["score"], r["days_open"], r["company"]))[: int(cfg_s["max_items"])]
    review_rows = sorted(review.values(), key=lambda r: (-r["new_roles"], r["company"]))
    return items, eligible_roles, review_rows


# ------------------------------------------------------------------ draft
def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _v(value: str, what: str) -> str:
    return value if value else f"[VERIFY {what}]"


def item_md(it: dict) -> str:
    founder = _link(_v(it["founder_name"], "founder name"), it["founder_linkedin"])
    if not it["founder_linkedin"]:
        founder += " [VERIFY LinkedIn]"
    company = it["company"]
    title = _link(it["title"], it["url"])
    loc = run._loc(it.get("location", "")) if hasattr(run, "_loc") else (it.get("location") or "")
    where = f" in {loc}" if loc else ""
    more = ""
    if it["more_count"]:
        more = f" (and {it['more_count']} more open)"
    lines = [
        f"- {founder} is hiring at **{company}**, which [VERIFY what the company does and for whom]. "
        f"{'They are' if not it['founder_name'] else it['founder_name'].split()[0] + ' is'} hiring a {title}{where}{more}. "
        f"[VERIFY what the role owns — from the posting] {_link('Apply here!', it['url'])}",
    ]
    if it["founder_x"]:
        lines.append(f"  [EMBED: {it['founder_x']} — only if the founder posted about this role or a raise/launch in the last 14 days]")
    meta = f"  _{it['eligible_because']}; {it['role_group']}; first seen {it['first_seen']}; score {it['score']}_"
    if it["more_roles"]:
        meta += "; more: " + ", ".join(_link(m["title"], m["url"]) for m in it["more_roles"])
    lines.append(meta)
    return "\n".join(lines)


def draft_md(items: list[dict], review: list[dict], today: dt.date, cfg_s: dict, roles_seen: int) -> str:
    md = [f"# Founder-led hiring draft — {today.isoformat()}", "",
          f"_{len(items)} items from {roles_seen} roles first seen in the last {cfg_s['window_days']} days at companies "
          f"with ≤{cfg_s['max_headcount']} people (or marked founder_led) in leads/founders.csv. "
          "Every [VERIFY] is a fact that is not on file; fill it from the posting or the founder's own post, never from memory._", ""]
    if len(items) < int(cfg_s["min_items"]):
        md += [f"> **Thin week:** {len(items)} items, below the minimum of {cfg_s['min_items']}. "
               "Decide: run a sourcing pass, ship short, or skip. The needs-review list below is the fastest way to add items.", ""]
    grouped: dict[str, list] = {}
    blurbs = {}
    for it in items:
        label, blurb = bucket_for(it["stage"])
        grouped.setdefault(label, []).append(it)
        blurbs[label] = blurb
    order = [b[0] for b in BUCKETS] + [UNKNOWN_BUCKET[0]]
    for label in order:
        if label not in grouped:
            continue
        md += ["---", f"#### {label}", f"*{blurbs[label]}*", ""]
        md += [item_md(it) for it in grouped[label]]
        md += ["", "*Building something new and want to be featured next week? [Submit here](SUBMIT_URL).*", ""]
    md += ["---", "#### Needs review (fresh roles, no headcount on file)", ""]
    if review:
        md += ["Add a `headcount` (or `founder_led = TRUE/FALSE`) for these in `leads/founders.csv` and they are in or out next run:", ""]
        for rv in review[:40]:
            md.append(f"- **{rv['company']}** — {rv['new_roles']} new role(s): " + "; ".join(rv["sample_titles"]))
        if len(review) > 40:
            md.append(f"- …and {len(review) - 40} more in needs-review-{today.isoformat()}.csv")
    else:
        md.append("_None — every company with fresh roles has a headcount on file._")
    return "\n".join(md).strip() + "\n"


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", default="", help="run date (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=None, help="window: roles first seen in the last N days")
    ap.add_argument("--max-headcount", type=int, default=None)
    ap.add_argument("--max-items", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="run even when founder_hiring.enabled is false in config.json")
    ap.add_argument("--record", action="store_true", help="log the picked companies to data/founder_featured.json for the cooldown")
    args = ap.parse_args(argv)

    cfg = run.load_json(run.CONFIG, {})
    s = settings(cfg)
    if not s.get("enabled") and not args.force:
        print("founder_hiring is off (config.json -> founder_hiring.enabled). Nothing written. Use --force to run anyway.")
        return 0
    if args.days is not None:
        s["window_days"] = args.days
    if args.max_headcount is not None:
        s["max_headcount"] = args.max_headcount
    if args.max_items is not None:
        s["max_items"] = args.max_items
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    history = run.load_json(run.HISTORY, {})
    opco = run.load_json(OPCO_LATEST, {}) if OPCO_LATEST.exists() else {}
    registry = load_founders()
    led = _load_ledger()

    roles = fresh_roles(history, opco, today, int(s["window_days"]))
    items, scored, review = select(roles, registry, s, today, cfg, led)

    out_dir = run.OUT / "founders"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"founder-roles-{today.isoformat()}"
    md = draft_md(items, review, today, s, len(roles))
    (out_dir / f"{stem}.md").write_text(md)
    (out_dir / f"{stem}.json").write_text(json.dumps({"run_date": today.isoformat(), "settings": s, "items": items,
                                                      "eligible_roles": scored, "needs_review": review}, indent=2))
    cols = ["company", "title", "url", "location", "first_seen", "source", "role_group", "score", "s_fresh", "s_role",
            "s_size", "headcount", "stage", "founder_name", "founder_linkedin", "founder_x", "eligible_because"]
    run.write_csv(out_dir / f"{stem}.csv", scored, cols)
    run.write_csv(out_dir / f"needs-review-{today.isoformat()}.csv",
                  [{**rv, "sample_titles": "; ".join(rv["sample_titles"])} for rv in review],
                  ["company", "new_roles", "source", "reason", "sample_titles"])
    if args.record and items:
        record(led, today, [it["company"] for it in items])

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(md)
    print(f"  founders  {len(items)} items, {len(scored)} eligible roles, {len(review)} companies need a headcount "
          f"-> {run._rel(out_dir / (stem + '.md'))}", flush=True)
    if not registry:
        print("  note: leads/founders.csv is missing or empty, so nothing can be eligible yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
