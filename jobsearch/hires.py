"""People moves - the "who got hired" side of the recently-hired signal.

    python -m jobsearch.hires                  # after jobsearch.run, same day
    python -m jobsearch.hires --date 2026-09-28

Three free sources, all scoped to leads/master_leads.csv:
  1. News       Google News RSS search per company for hire/appointment headlines
  2. SEC        8-K Item 5.02 filings (officer/director appointments and departures) for public companies
  3. Leadership company leadership/team pages, snapshotted weekly; new and removed names are reported

Each move is matched against roles that closed recently at the same company (from data/history.json),
so the report can say "VP Sales posting closed Sept 30 -> press release Oct 2 names Jane Doe VP Sales".

Outputs: output/<date>/people_moves.csv, plus a "People moves" section appended to report.md / report.html.
State:   data/people_history.json (moves already reported), data/leadership_snapshots.json (page baselines).
First run of the leadership source is a baseline only - changes appear from the second run.
Only public, professional information is stored, always with its source link.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html as H
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

from .adapters import UA
from .run import (ROOT, OUT, HISTORY, CONFIG, load_json, save_json, load_leads, write_csv,
                  classify_role, _norm)

PEOPLE_HISTORY = ROOT / "data" / "people_history.json"
SNAPSHOTS = ROOT / "data" / "leadership_snapshots.json"

HIRE_VERBS = re.compile(
    r"\b(appoint(s|ed|ment)?|name[sd]|hire[sd]?|welcome[sd]?|tap(s|ped)|promote[sd]?|elevate[sd]?|"
    r"join(s|ed)?|adds?|added|announces? .{0,40}\b(new|as)\b|succeed(s|ed)?|to lead|steps? down|depart(s|ure)|"
    r"resign(s|ed|ation)?|retire(s|ment)?)\b", re.I)
LEAVE_VERBS = re.compile(r"\b(steps? down|depart(s|ure)|resign(s|ed|ation)?|retire(s|ment)?|exits?)\b", re.I)
NAME = r"([A-Z][a-zA-Z'’\-]+(?: [A-Z]\.)?(?: [A-Z][a-zA-Z'’\-]+){1,2})"
TITLE_WORDS = re.compile(r"\b(chief|officer|president|vice|vp|svp|evp|head|director|founder|ceo|cfo|cto|coo|cro|cmo|"
                         r"cpo|manager|lead|partner|chair|board)\b", re.I)


def _get(url, **kw):
    kw.setdefault("timeout", 20)
    return requests.get(url, headers=kw.pop("headers", UA), **kw)


def _clean(s: str) -> str:
    return " ".join(H.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


SUFFIX = r"(?:\s+(?:Group|Inc\.?|Labs|Companies|Company|Technologies|Homes|Software|Systems|Rewards|Hospitality(?:\s+Group)?|Holdings|Corp\.?|LLC))?"
VERB = r"(?:names?|named|appoints?|appointed|hires?|hired|promotes?|promoted|taps?|tapped|welcomes?|adds?|elevates?|announces?|expands|bolsters|strengthens)"
EXEC = r"(?:ceo|cfo|coo|cto|cro|cmo|cpo|president|chief|head|vp|svp|evp|general\s+manager|chair)"


def company_is_hirer(name: str, headline: str) -> bool:
    """True only if the company is the one hiring / being joined - not a word that happens to appear.
    'Sound Point Capital appoints...', 'Indian Super League appoints...', 'ENGWE names Robin van Persie'
    all fail; 'Greystar appoints...', 'joins Zillow as', 'CEO of Redfin', 'named Redfin CEO' pass."""
    n = re.escape(name)
    h = re.sub(r"\s+", " ", headline.strip())
    pats = [
        rf"^(?:[^:|]{{0,60}}[:|]\s*)?{n}{SUFFIX}(?:'s|’s)?\s+(?:board\s+)?{VERB}\b",   # Greystar appoints / Acme: Zillow names
        rf"^(?:[\w.-]+\s+){{0,4}}(?:giant|leader|firm|operator|developer|platform|startup|company|owner|manager|landlord|lender|brokerage|reit|proptech)\s+{n}{SUFFIX}\s+{VERB}\b",  # US Multifamily Giant Greystar Names
        rf"\b(?:joins?|joined|rejoins|returns to|to lead|leaves|exits|departs)\s+{n}\b",     # X joins Entrata as ...
        rf"\b{EXEC}[\w\s&,-]{{0,40}}?\s+(?:of|at|for)\s+{n}\b",                              # CEO of Redfin
        rf"\b(?:named|names|appointed|appoints|as|new|taps)\s+{n}{SUFFIX}\s+{EXEC}\b",       # named Redfin CEO
        rf"^{n}{SUFFIX}\s+{EXEC}\b[^|]*?\b(?:steps down|to step down|resigns|retires|departs|exits|leaves)\b",  # Entrata CTO steps down
    ]
    return any(re.search(p, h, re.I) for p in pats)


TRAIL = {"chief", "head", "senior", "president", "vice", "marketing", "sales", "product", "technology", "revenue",
         "operating", "financial", "executive", "global", "new", "former", "ceo", "cfo", "coo", "cto", "cro", "cmo",
         "as", "to", "its", "the", "a", "an", "and"}


def _person_from_headline(t: str) -> str:
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        t = t.title()  # ALL-CAPS HEADLINE -> Title Case so names can be recognised
    m = re.search(rf"(?i:{VERB})\s+{NAME}", t)
    if m:
        words = m.group(1).split()
        while words and words[-1].lower().strip(",") in TRAIL:
            words.pop()
        if len(words) >= 2 and not TITLE_WORDS.search(" ".join(words)) and words[0].lower() not in TRAIL:
            return " ".join(words)
    return _person_from_headline_old(t)


def _person_from_headline_old(t: str) -> str:
    """'Acme Appoints Jane Doe as Chief Revenue Officer' -> 'Jane Doe'. Best effort; blank if unsure."""
    for pat in (rf"(?i:appoints?|names?|hires?|welcomes?|taps?|promotes?|elevates?|adds?)\s+{NAME}\s+(?i:as|to)\b",
                rf"^{NAME}\s+(?i:joins|named|appointed|promoted|tapped|steps down|to lead|retires|resigns)",
                rf"(?i:ceo|cfo|cto|coo|cro|cmo|president|chief [a-z]+ officer)\s+{NAME}\s+(?i:steps down|to retire|retires|resigns|departs)",
                rf"{NAME},?\s+(?i:former|ex-)"):
        m = re.search(pat, t)
        if m and not TITLE_WORDS.search(m.group(1)):
            return m.group(1)
    return ""


# ------------------------------------------------------------------ 1. News
def news_moves(lead: dict, cfg: dict, today: dt.date) -> list[dict]:
    pm = cfg.get("people_moves", {})
    days = int(pm.get("news_days", 30))
    name = lead["company"].split("(")[0].strip()
    ctx = pm.get("news_context", "(real estate OR proptech OR property OR rental OR housing OR mortgage OR leasing OR homes)")
    q = f'"{name}" (appoints OR names OR hires OR joins OR promotes OR "steps down") {ctx}'
    url = f"https://news.google.com/rss/search?q={quote(q)}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
    r = _get(url)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    names = [name] + list(pm.get("news_aliases", {}).get(lead["company"], []))
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        src = _clean(it.findtext("source") or "")
        # Google appends " - Source" to titles; drop it before matching
        headline = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
        if not any(company_is_hirer(n, headline) for n in names) or not HIRE_VERBS.search(headline):
            continue
        grp = classify_role(headline, cfg)
        if not grp:
            continue
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").date()
        except Exception:
            pub = today
        if (today - pub).days > days:
            continue
        out.append({"company": lead["company"], "source": "news", "date": pub.isoformat(),
                    "move": "departure" if LEAVE_VERBS.search(headline) else "hire/appointment",
                    "person": _person_from_headline(headline), "title_or_headline": headline,
                    "role_group": grp, "url": link, "detail": src,
                    "key": "news:" + (it.findtext("guid") or link)})
    return out


# ------------------------------------------------------------------ 2. SEC
_TICKERS: dict | None = None


def sec_contact(cfg) -> str:
    """SEC requires a contact name + email in every request. It comes from the SEC_USER_AGENT
    GitHub secret (set in the workflow), so no email address is stored in the repo."""
    import os
    return (os.environ.get("SEC_USER_AGENT") or cfg.get("people_moves", {}).get("sec_user_agent") or "").strip()


def _sec_headers(cfg):
    return {"User-Agent": sec_contact(cfg), "Accept-Encoding": "gzip, deflate"}


def _cik_for(ticker: str, cfg) -> str | None:
    global _TICKERS
    if _TICKERS is None:
        r = _get("https://www.sec.gov/files/company_tickers.json", headers=_sec_headers(cfg))
        r.raise_for_status()
        _TICKERS = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}
    return _TICKERS.get(ticker.upper())


def sec_moves(company: str, ticker: str, cfg: dict, today: dt.date) -> tuple[list[dict], str]:
    days = int(cfg.get("people_moves", {}).get("sec_days", 30))
    cik = _cik_for(ticker, cfg)
    if not cik:
        return [], f"{ticker} not in SEC ticker list (delisted or acquired?)"
    r = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_sec_headers(cfg))
    r.raise_for_status()
    rec = r.json().get("filings", {}).get("recent", {})
    out = []
    for form, fdate, items, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", []),
                                            rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
        if form not in ("8-K", "8-K/A") or "5.02" not in (items or ""):
            continue
        d = dt.date.fromisoformat(fdate)
        if (today - d).days > days:
            break  # filings are newest-first
        link = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        out.append({"company": company, "source": "sec", "date": fdate, "move": "officer/director change",
                    "person": "", "title_or_headline": f"8-K Item 5.02 - departure/appointment of officers or directors ({ticker})",
                    "role_group": "executive", "url": link, "detail": f"items {items}", "key": f"sec:{acc}"})
    return out, f"{len(out)} filings"


# ------------------------------------------------------------------ 3. Leadership pages
PAGE_PATHS = ["/about/leadership", "/leadership", "/about/team", "/team", "/our-team", "/about-us", "/about",
              "/company", "/company/about", "/who-we-are"]
PERSON_TITLE = re.compile(r"(?i)\b(chief|ceo|cfo|cto|coo|cro|cmo|cpo|president|vice president|\bvp\b|svp|evp|"
                          r"head of|general manager|founder|co-founder|director)\b")
NAME_RX = re.compile(r"^[A-Z][a-zA-Z'’\-]+(?: [A-Z]\.)?(?: [A-Z][a-zA-Z'’\-]+){1,2}$")


def extract_people(html: str) -> list[tuple[str, str]]:
    """Pull (name, title) pairs from a leadership/team page: a short title-looking text node with a
    person-name-looking text node right before (or after) it."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    nodes = [_clean(n) for n in re.split(r"<[^>]+>", body)]
    nodes = [n for n in nodes if n and len(n) <= 90]
    pairs, seen = [], set()
    for i, n in enumerate(nodes):
        if not PERSON_TITLE.search(n) or len(n) > 70:
            continue
        for j in (i - 1, i - 2, i + 1):
            if 0 <= j < len(nodes) and NAME_RX.match(nodes[j]) and not PERSON_TITLE.search(nodes[j]) \
                    and not TITLE_WORDS.search(nodes[j]):
                key = (nodes[j], n)
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
                break
    return pairs


def find_leadership_page(lead: dict, override: str = "") -> tuple[str, list]:
    site = lead.get("website", "")
    if not site and not override:
        return "", []
    base = f"{urlparse(site).scheme or 'https'}://{urlparse(site).netloc}" if site else ""
    for url in ([override] if override else [base + p for p in PAGE_PATHS]):
        try:
            r = _get(url, allow_redirects=True, timeout=12)
        except Exception:
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            continue
        people = extract_people(r.text)
        if len(people) >= 3 or (override and people):
            return r.url, people
    return "", []


def leadership_moves(lead, snap: dict, cfg, today) -> tuple[list[dict], dict, str]:
    """Returns (moves, new_snapshot_entry, note)."""
    override = cfg.get("people_moves", {}).get("leadership_pages", {}).get(lead["company"], "")
    prev = snap.get(lead["company"], {})
    # companies with no findable page are retried every 4 weeks, not every run
    if prev.get("url") == "" and prev.get("checked") and not override:
        if (today - dt.date.fromisoformat(prev["checked"])).days < 28:
            return [], prev, "no page (retry later)"
    url, people = find_leadership_page(lead, override or prev.get("url", ""))
    if not url and prev.get("url") and not override:
        url, people = find_leadership_page(lead)  # page moved - rediscover
    entry = {"url": url, "people": [list(p) for p in people], "checked": today.isoformat()}
    if not url:
        return [], entry, "no leadership page found"
    if not prev.get("people") or prev.get("url") != url:
        return [], entry, f"baseline: {len(people)} people"
    before = {tuple(p) for p in prev["people"]}
    after = set(people)
    before_names = {n for n, _ in before}
    after_names = {n for n, _ in after}
    if after and len(after & before) == 0 and len(before) >= 3:
        return [], entry, "page changed completely (redesign?) - re-baselined"
    moves = []
    for name, title in sorted(after - before):
        grp = classify_role(title, cfg)
        if not grp:
            continue
        moves.append({"company": lead["company"], "source": "leadership page", "date": today.isoformat(),
                      "move": "new on page" if name not in before_names else "title changed",
                      "person": name, "title_or_headline": title, "role_group": grp, "url": url, "detail": "",
                      "key": f"page:{lead['company']}:{name}:{title}"})
    for name, title in sorted(before - after):
        if name in after_names:
            continue  # title change already reported above
        grp = classify_role(title, cfg)
        if not grp:
            continue
        moves.append({"company": lead["company"], "source": "leadership page", "date": today.isoformat(),
                      "move": "removed from page", "person": name, "title_or_headline": title, "role_group": grp,
                      "url": url, "detail": "", "key": f"page-gone:{lead['company']}:{name}:{title}:{today}"})
    return moves, entry, f"{len(people)} people, {len(moves)} changes"


def _exec_key(text: str) -> str:
    t = text.lower().replace("&", "and")
    m = re.search(r"chief ([a-z ]+?) officer", t)
    if m:
        short = {"executive": "ceo", "financial": "cfo", "operating": "coo", "technology": "cto",
                 "revenue": "cro", "marketing": "cmo"}
        return short.get(m.group(1).strip(), "chief " + m.group(1).strip())
    for k in ("ceo", "cfo", "coo", "cto", "cro", "cmo", "cpo", "president", "head of [a-z ]+", "general manager"):
        m = re.search(rf"\b{k}\b", t)
        if m:
            return m.group(0)
    return ""


def merge_duplicate_stories(moves: list[dict]) -> list[dict]:
    """News outlets repeat the same appointment. Same company + same person (or same exec title)
    within 10 days = one story; keep the earliest, note how many other articles covered it."""
    out = []
    for m in sorted(moves, key=lambda x: (x["company"], x["date"])):
        if m["source"] != "news":
            out.append(m)
            continue
        p, k = m["person"].lower(), _exec_key(m["title_or_headline"])
        for o in out:
            if o["source"] != "news" or o["company"] != m["company"]:
                continue
            if abs((dt.date.fromisoformat(o["date"]) - dt.date.fromisoformat(m["date"])).days) > 10:
                continue
            op, ok = o["person"].lower(), _exec_key(o["title_or_headline"])
            generic = {"names", "appoints", "named", "appointed", "hires", "new", "head", "chief", "officer", "president",
                       "director", "executive", "vp", "vice", "senior", "lead", "growth", "role", "as", "of", "to", "the",
                       "and", "for", "joins", "promotes", "global"} | set(re.findall(r"[a-z]+", m["company"].lower()))
            shared = (set(re.findall(r"[a-z]{3,}", m["title_or_headline"].lower())) - generic) & \
                     (set(re.findall(r"[a-z]{3,}", o["title_or_headline"].lower())) - generic)
            if (p and op and p == op) or (k and ok and k == ok) or (p and p in o["title_or_headline"].lower()) \
                    or (not (p and op) and len(shared) >= 2):
                o["_dupes"] = o.get("_dupes", 0) + 1
                o["person"] = o["person"] or m["person"]
                o.setdefault("_dupe_keys", []).append(m["key"])
                break
        else:
            out.append(m)
    for o in out:
        if o.get("_dupes"):
            o["detail"] = (o["detail"] + "; " if o["detail"] else "") + f"+{o['_dupes']} more articles"
    return out


# ------------------------------------------------------------------ matching
def _tokens(s: str) -> set:
    stop = {"of", "the", "and", "a", "to", "as", "at", "for", "senior", "sr", "new", "names", "appoints"}
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if w not in stop and len(w) > 1}


def match_closed_roles(move: dict, history: dict, today: dt.date, window: int = 90) -> str:
    """Closed postings at the same company in the last `window` days whose title overlaps this move."""
    mt = _tokens(move["title_or_headline"])
    best = []
    for k, h in history.items():
        if h.get("company") != move["company"] or h.get("status") != "closed" or not h.get("closed_on"):
            continue
        if (today - dt.date.fromisoformat(h["closed_on"])).days > window:
            continue
        ht = _tokens(h.get("title", ""))
        overlap = len(mt & ht) / max(1, len(ht))
        if overlap >= 0.5:
            best.append((overlap, f"{h['title']} (closed {h['closed_on']})"))
    return "; ".join(t for _, t in sorted(best, reverse=True)[:2])


# ------------------------------------------------------------------ report
def append_report(out_dir: Path, rows: list[dict], notes: dict):
    head = "## People moves - who got hired (news, SEC filings, leadership pages)"
    md = ["", head, "",
          "_Named hires, appointments and departures at your master-list companies. "
          "\"Matches closed role\" links a move to a posting that recently came down._", ""]
    if rows:
        md += ["| Company | Move | Person | Title / headline | Matches closed role | Source |", "|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['company']} | {r['move']} | {r['person']} | {r['title_or_headline']} | "
                      f"{r.get('matched_closed_role','')} | [{r['source']}]({r['url']}) |")
    else:
        md.append("_No new moves this run._")
    md += ["", "_Coverage: " + "; ".join(f"{k}: {v}" for k, v in notes.items()) + "_", ""]
    rep = out_dir / "report.md"
    text = rep.read_text() if rep.exists() else ""
    text = text.split("\n" + head)[0]  # replace an earlier section if re-run the same day
    rep.write_text(text + "\n".join(md))
    page = out_dir / "report.html"
    if page.exists():
        cells = "".join(
            f"<tr><td>{H.escape(r['company'])}</td><td>{H.escape(r['move'])}</td><td>{H.escape(r['person'])}</td>"
            f"<td>{H.escape(r['title_or_headline'])}</td><td>{H.escape(r.get('matched_closed_role',''))}</td>"
            f"<td><a href=\"{H.escape(r['url'])}\" target=\"_blank\">{H.escape(r['source'])}</a></td></tr>" for r in rows)
        sec = (f'<section id="people-moves"><h2>People moves - who got hired</h2>'
               + (f"<table><thead><tr><th>Company</th><th>Move</th><th>Person</th><th>Title / headline</th>"
                  f"<th>Matches closed role</th><th>Source</th></tr></thead><tbody>{cells}</tbody></table>"
                  if rows else "<p><i>No new moves this run.</i></p>")
               + f"<p><i>Coverage: {H.escape('; '.join(f'{k}: {v}' for k, v in notes.items()))}</i></p></section>")
        h = re.sub(r'<section id="people-moves">.*?</section>', "", page.read_text(), flags=re.S)
        page.write_text(h.replace("</body>", sec + "</body>"))


def latest_report_dir(today: dt.date) -> Path:
    """The most recent weekly job report (output/YYYY-MM-DD, not on-demand) from the last 7 days.
    If the job search hasn't produced one this week, people moves get their own folder for today."""
    best = None
    for d in OUT.glob("20??-??-??"):
        try:
            day = dt.date.fromisoformat(d.name)
        except ValueError:
            continue
        if 0 <= (today - day).days <= 7 and (d / "report.md").exists() and (best is None or day > best[0]):
            best = (day, d)
    return best[1] if best else OUT / today.isoformat()


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--only", default="", help="comma-separated sources: news,sec,leadership")
    ap.add_argument("--minutes", type=float, default=15, help="time budget for the whole step")
    ap.add_argument("--report-dir", default="", help="weekly report folder to add the section to (default: latest)")
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    start = time.time()
    cfg = load_json(CONFIG, {})
    pm = cfg.get("people_moves", {})
    sources = {s.strip() for s in (args.only or "news,sec,leadership").split(",")}
    leads = [l for l in load_leads() if (l.get("company") or "").strip()]
    seen = load_json(PEOPLE_HISTORY, {})
    snaps = load_json(SNAPSHOTS, {})
    history = load_json(HISTORY, {})
    found, notes = [], {}
    over = lambda: (time.time() - start) > args.minutes * 60

    if "news" in sources:
        ok = fail = 0
        for l in leads:
            if over():
                notes["news"] = f"time budget hit after {ok} companies"
                break
            try:
                found += news_moves(l, cfg, today); ok += 1
            except Exception as e:
                fail += 1
                print(f"  news    {l['company']:30} failed: {type(e).__name__}: {str(e)[:80]}", flush=True)
            time.sleep(float(pm.get("news_sleep", 1.0)))
        notes.setdefault("news", f"{ok} companies searched" + (f", {fail} failed" if fail else ""))
        print(f"  news    done: {notes['news']}", flush=True)

    if "sec" in sources and "@" not in sec_contact(cfg):
        notes["sec"] = "skipped - SEC_USER_AGENT secret not set"
        print("  sec     SKIPPED: add a repository secret named SEC_USER_AGENT "
              "(e.g. 'Your Name you@example.com') - the SEC requires a contact email", flush=True)
    elif "sec" in sources:
        cos = pm.get("public_companies", {})
        done = []
        for co, ticker in cos.items():
            try:
                mv, note = sec_moves(co, ticker, cfg, today)
                found += mv
                done.append(f"{ticker} {note}")
            except Exception as e:
                done.append(f"{ticker} failed ({type(e).__name__})")
            time.sleep(0.2)
        notes["sec"] = f"{len(cos)} public companies"
        print("  sec     " + " | ".join(done), flush=True)

    if "leadership" in sources:
        todo = [l for l in leads if l.get("website")]
        remaining = max(60, args.minutes * 60 - (time.time() - start))

        def one(l):
            try:
                return l["company"], leadership_moves(l, snaps, cfg, today)
            except Exception as e:
                return l["company"], ([], snaps.get(l["company"], {}), f"failed: {type(e).__name__}")

        t0, results = time.time(), []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(one, l) for l in todo]
            for f in futs:
                left = remaining - (time.time() - t0)
                try:
                    results.append(f.result(timeout=max(1, left)))
                except Exception:
                    break
        tracked = 0
        for co, (mv, entry, note) in results:
            found += mv
            snaps[co] = entry
            tracked += bool(entry.get("url"))
        notes["leadership pages"] = f"{tracked} companies with a readable page"
        print(f"  pages   {tracked} of {len(todo)} companies have a readable leadership page", flush=True)
        save_json(SNAPSHOTS, snaps)

    found = merge_duplicate_stories(found)
    # keep only moves not reported before, link to recently closed roles
    new = []
    for m in found:
        if m["key"] in seen:
            continue
        seen[m["key"]] = {"first_seen": today.isoformat(), "company": m["company"], "url": m["url"]}
        for k in m.pop("_dupe_keys", []):
            seen[k] = {"first_seen": today.isoformat(), "company": m["company"], "url": m["url"], "duplicate_of": m["key"]}
        m.pop("_dupes", None)
        m["matched_closed_role"] = match_closed_roles(m, history, today)
        new.append(m)
    new.sort(key=lambda m: (m["company"], m["date"]))
    save_json(PEOPLE_HISTORY, seen)

    out_dir = Path(args.report_dir) if args.report_dir else latest_report_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  report  adding People moves to {out_dir}", flush=True)
    write_csv(out_dir / "people_moves.csv", new,
              ["company", "move", "person", "title_or_headline", "role_group", "date", "source",
               "matched_closed_role", "url", "detail"])
    append_report(out_dir, new, notes)
    print(f"[{today}] people moves: {len(new)} new ({sum(1 for m in new if m.get('matched_closed_role'))} "
          f"matched to a closed role) -> {out_dir / 'people_moves.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
