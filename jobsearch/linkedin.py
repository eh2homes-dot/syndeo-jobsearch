"""Each company's LinkedIn page, taken from the company's own website.

    python -m jobsearch.linkedin            # check companies that are due, write the review file
    python -m jobsearch.linkedin --all      # check every company now

Nothing here ever requests a page from LinkedIn. LinkedIn blocks automated
requests and its terms forbid scraping, so the only thing read is each
company's own homepage (and its careers page if the homepage has no link), the
same pages any visitor sees. The LinkedIn address is taken from the link the
company itself puts there, usually in the footer. Names are never guessed.

What it writes:

  data/linkedin_found.json     what was found for each company and when it was last checked
  output/linkedin-review.csv   one row per company for Evan to review: the link found,
                               what to check, and what the list already has

Found links are suggestions only. The job search uses a company's LinkedIn page
only once it is in the company list itself: a column headed "LinkedIn" on the
master list (leads/master_leads.csv) or on the OpCo tab. Nothing is written to
either list from here.

Each company is checked again every four weeks (every week if its site
couldn't be read last time), so the weekly run only re-reads a handful of
homepages.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .http import FetchError, get

log = logging.getLogger("jobsearch.linkedin")

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("JOBSEARCH_DATA") or ROOT / "data")
OUT = Path(os.environ.get("JOBSEARCH_OUT") or ROOT / "output")
FOUND = DATA / "linkedin_found.json"
REVIEW = OUT / "linkedin-review.csv"
LEADS = ROOT / "leads" / "master_leads.csv"

RECHECK_DAYS = 28          # a site's LinkedIn link rarely changes
RETRY_DAYS = 7             # but a site we couldn't read is tried again next week

# A column on either company list holding the approved LinkedIn page.
_HEADER = re.compile(r"^\s*(company\s*)?linked\s*in(\s*(url|page|link|profile))?\s*$", re.I)

_LI_HOST = re.compile(r"^(?:[a-z]{2,3}\.|www\.|m\.)?linkedin\.com$", re.I)
_LI_PATH = re.compile(r"^/(company|showcase|school)/([^/?#]+)", re.I)
_SHORT_HOST = re.compile(r"^(?:www\.)?lnkd\.in$", re.I)
# LinkedIn addresses written anywhere in a page's code (a script, a data blob).
_IN_CODE = re.compile(r"""(?:https?:)?(?:\\?/){2}(?:[a-z]{2,3}\.|www\.)?linkedin\.com(?:\\?/)(?:company|showcase|school)(?:\\?/)[^\s"'<>\\?#]+""", re.I)

# Where on the page a link sat, most trustworthy first.
WHERE_RANK = {"site data": 4, "footer": 3, "header": 2, "page": 1, "page code": 0}


# ------------------------------------------------------------------ links
def normalize(href: str) -> tuple[str, str, str]:
    """A LinkedIn link -> (kind, slug, clean url), or ("", "", "") if it isn't a page link.

    kind is company / showcase / school, or "short" for a lnkd.in link (which
    can't be resolved without asking LinkedIn). Share buttons, people's
    profiles, posts and job links are not pages and come back empty.
    """
    href = (href or "").strip().replace("\\/", "/")
    if not href:
        return "", "", ""
    if href.startswith("//"):
        href = "https:" + href
    elif not re.match(r"^[a-z]+:", href, re.I):
        href = "https://" + href.lstrip("/")
    try:
        u = urlparse(href)
    except ValueError:
        return "", "", ""
    host = (u.hostname or "").lower()
    if _SHORT_HOST.match(host):
        return "short", "", f"https://lnkd.in{u.path}"
    if not _LI_HOST.match(host):
        return "", "", ""
    m = _LI_PATH.match(u.path or "")
    if not m:
        return "", "", ""
    kind, slug = m.group(1).lower(), unquote(m.group(2)).strip().lower()
    if not slug or slug in ("undefined", "null", "none", "your-company", "company"):
        return "", "", ""
    return kind, slug, f"https://www.linkedin.com/{kind}/{slug}/"


def approved(value: str) -> str:
    """A list cell -> the clean LinkedIn page address, or "" if the cell isn't one."""
    kind, _, url = normalize(value)
    return url if kind in ("company", "showcase", "school") else ""


def approved_column(header: list) -> Optional[int]:
    """Index of the list's LinkedIn column, if it has one."""
    for i, h in enumerate(header):
        if _HEADER.match(h or ""):
            return i
    return None


def approved_from_row(row: dict) -> str:
    """The approved LinkedIn page from a csv.DictReader row of a company list."""
    for k, v in row.items():
        if k and _HEADER.match(k):
            return approved(v or "")
    return ""


@dataclass
class Candidate:
    url: str
    kind: str
    slug: str
    where: str
    count: int = 1


def _where(tag) -> str:
    for parent in [tag, *tag.parents]:
        name = getattr(parent, "name", "") or ""
        if name == "footer":
            return "footer"
        if name in ("header", "nav"):
            return "header"
        attrs = getattr(parent, "attrs", None) or {}
        marks = " ".join([str(attrs.get("id", "")), " ".join(attrs.get("class", []) or []),
                          str(attrs.get("role", ""))]).lower()
        if "footer" in marks or "contentinfo" in marks:
            return "footer"
    return "page"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def extract(html: str, page_url: str = "") -> list[Candidate]:
    """Every LinkedIn page the HTML links to, strongest first."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, Candidate] = {}

    def add(href, where):
        kind, slug, url = normalize(href)
        if not kind:
            return
        c = found.get(url)
        if c is None:
            found[url] = Candidate(url, kind, slug, where)
        else:
            c.count += 1
            if WHERE_RANK[where] > WHERE_RANK[c.where]:
                c.where = where

    # The site's own description of itself (schema.org Organization "sameAs").
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            same = node.get("sameAs")
            for href in (same if isinstance(same, list) else [same] if isinstance(same, str) else []):
                if isinstance(href, str):
                    add(href, "site data")
    for a in soup.find_all(["a", "link"], href=True):
        if "linkedin" in a["href"].lower() or "lnkd.in" in a["href"].lower():
            add(a["href"], _where(a))
    if not any(c.kind != "short" for c in found.values()):
        # Pages built by scripts often keep their footer links in a data blob.
        for m in _IN_CODE.finditer(html or ""):
            add(m.group(0), "page code")
    return sorted(found.values(), key=lambda c: (-WHERE_RANK[c.where], -c.count, c.url))


# ------------------------------------------------------------------ does the page look like this company?
_SUFFIX = re.compile(r"(inc|llc|ltd|corp|corporation|company|co|hq|official|group|global|usa|us|team)$")


def _key(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _site_name(website: str) -> str:
    host = (urlparse(website if "//" in (website or "") else "https://" + (website or "")).hostname or "").lower()
    parts = [p for p in host.split(".") if p and p != "www"]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def name_matches(slug: str, company: str, website: str = "") -> Optional[bool]:
    """True / False, or None when the page address is just a number."""
    s = _key(slug)
    if not s or s.isdigit():
        return None
    s = re.sub(r"\d+$", "", s) or s
    keys = {_key(company), _key(_site_name(website))}
    words = [w for w in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(w) >= 4]
    if words:
        keys.add(words[0])
    keys.discard("")
    trimmed = _SUFFIX.sub("", s) or s
    for k in keys:
        k2 = _SUFFIX.sub("", k) or k
        for a, b in ((s, k), (trimmed, k2)):
            if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
                return True
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
                return True
    return False


# ------------------------------------------------------------------ finding one company's page
@dataclass
class Found:
    url: str = ""
    kind: str = ""
    found_on: str = ""          # e.g. "homepage footer"
    page: str = ""              # the company page it was read from
    others: list = field(default_factory=list)
    checks: list = field(default_factory=list)    # what to look at before approving
    error: str = ""             # why the site couldn't be read (nothing found, nothing known)


def _homepage(website: str) -> str:
    w = (website or "").strip()
    if not w:
        return ""
    if not re.match(r"^https?://", w, re.I):
        w = "https://" + w
    u = urlparse(w)
    return f"{u.scheme}://{u.netloc}/" if u.netloc else ""


def _static(url: str) -> tuple[str, str]:
    r = get(url, accept="text/html,application/xhtml+xml", timeout=20, retries=2)
    return r.text, r.url


def _choose(cands: list[Candidate], company: str, website: str) -> tuple[Optional[Candidate], list]:
    pages = [c for c in cands if c.kind != "short"]
    if not pages:
        return None, []

    def score(c):
        m = name_matches(c.slug, company, website)
        return (c.kind == "company", m is True, m is None, WHERE_RANK[c.where], c.count)

    pages.sort(key=score, reverse=True)
    return pages[0], [c.url for c in pages[1:]]


def find(company: str, website: str, careers_url: str = "", *,
         fetch: Callable[[str], tuple[str, str]] = _static, browser=None) -> Found:
    """Look for the company's LinkedIn page on its homepage, then its careers page."""
    pages = []
    home = _homepage(website)
    if home:
        pages.append(("homepage", home))
    if careers_url and careers_url.strip().rstrip("/") != (home or "").rstrip("/"):
        pages.append(("careers page", careers_url.strip()))
    if not pages:
        return Found(error="no website or careers page in the list")

    errors, shorts = [], []
    for label, url in pages:
        html, final = "", url
        try:
            html, final = fetch(url)
        except FetchError as exc:
            errors.append(f"{label}: {exc}")
        cands = extract(html, final) if html else []
        if not any(c.kind != "short" for c in cands) and browser is not None:
            # Some sites only build their footer with scripts, and some turn away
            # simple requests. A real browser sees what a visitor sees.
            try:
                r = browser.render(url, max_clicks=0)
                rendered = r.frames[0][1] if r.frames else ""
                more = extract(rendered, r.final_url or url)
                if more:
                    cands = more
                    if errors and errors[-1].startswith(label):
                        errors.pop()
            except Exception as exc:  # noqa: BLE001 - a browser problem is "couldn't check", never fatal
                log.debug("browser failed on %s: %s", url, exc)
        shorts += [c.url for c in cands if c.kind == "short"]
        best, others = _choose(cands, company, website)
        if best is None:
            continue
        f = Found(url=best.url, kind=best.kind, page=url, others=others,
                  found_on=f"{label} {best.where}" if best.where in ("footer", "header") else
                  f"{label} ({'the site describes itself' if best.where == 'site data' else 'in the page code, not a visible link' if best.where == 'page code' else 'body'})")
        if best.kind == "showcase":
            f.checks.append("this is a showcase page (usually a product or division), not a company page")
        elif best.kind == "school":
            f.checks.append("this is a school page, not a company page")
        m = name_matches(best.slug, company, website)
        if m is False:
            f.checks.append(f"page name '{best.slug}' doesn't look like {company}: check it isn't a parent, "
                            "an old name or another company")
        elif m is None:
            f.checks.append("page address is a number, not a name: open it to check it's the right company")
        if label == "careers page":
            f.checks.append("found on the careers page, not the main site")
        if best.where == "page code":
            f.checks.append("found in the page code, not a visible link")
        if others:
            f.checks.append("the site also links to " + ", ".join(others[:3]))
        return f

    f = Found()
    if shorts:
        f.checks.append(f"the site only has a lnkd.in short link ({shorts[0]}): open it to see which page it is")
    if len(errors) == len(pages):
        f.error = "; ".join(errors)
    return f


# ------------------------------------------------------------------ the company lists
@dataclass
class Listed:
    name: str
    website: str
    careers_url: str
    approved: str
    lists: list


def _weekly_companies(path: Optional[Path] = None) -> list[Listed]:
    path = path or LEADS
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [Listed(r["company"].strip(), (r.get("website") or "").strip(), (r.get("careers_url") or "").strip(),
                       approved_from_row(r), ["Weekly"]) for r in csv.DictReader(f) if (r.get("company") or "").strip()]


def _opco_companies() -> list[Listed]:
    """The OpCo tab: the live sheet if it's set up, else the repo copy. Never fatal."""
    try:
        sys.path.insert(0, str(ROOT))
        import yaml
        import opco_search as opco
        cfg_path = ROOT / "opco_config.yml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        url = str((cfg or {}).get("sheet_csv_url") or "").strip()
        try:
            companies, _ = opco.load_live_companies(url) if url else (None, "")
        except (FetchError, ValueError):
            companies = None
        if companies is None:
            companies = opco.load_companies(opco.find_companies_file(None))
        return [Listed(c.name, c.website, c.careers_url, getattr(c, "linkedin", ""), ["OpCo"]) for c in companies]
    except Exception as exc:  # noqa: BLE001
        log.warning("couldn't load the OpCo list, skipping it: %s", exc)
        return []


def merged(*groups: list[Listed]) -> list[Listed]:
    """One entry per company; a company on both lists is checked once."""
    out: dict[str, Listed] = {}
    for g in groups:
        for c in g:
            k = _key(c.name)
            if k in out:
                cur = out[k]
                cur.lists = cur.lists + [l for l in c.lists if l not in cur.lists]
                cur.website = cur.website or c.website
                cur.careers_url = cur.careers_url or c.careers_url
                cur.approved = cur.approved or c.approved
            else:
                out[k] = Listed(c.name, c.website, c.careers_url, c.approved, list(c.lists))
    return list(out.values())


# ------------------------------------------------------------------ the run
def _due(entry: Optional[dict], c: Listed, today: dt.date) -> bool:
    if not entry or entry.get("website", "") != c.website or entry.get("careers_url", "") != c.careers_url:
        return True
    try:
        checked = dt.date.fromisoformat(entry.get("checked", ""))
    except ValueError:
        return True
    wait = RETRY_DAYS if entry.get("error") else RECHECK_DAYS
    return (today - checked).days >= wait


def check(companies: list[Listed], state: dict, today: dt.date, *, fetch=_static, browser=None,
          everything: bool = False, minutes: float = 15) -> int:
    """Check the companies that are due. Updates `state` in place; returns how many were checked."""
    started, n = time.time(), 0
    for c in companies:
        k = _key(c.name)
        if not everything and not _due(state.get(k), c, today):
            continue
        if time.time() - started > minutes * 60:
            log.info("time's up after %d companies; the rest are checked next run", n)
            break
        prev = state.get(k) or {}
        f = find(c.name, c.website, c.careers_url, fetch=fetch, browser=browser)
        n += 1
        entry = {"company": c.name, "website": c.website, "careers_url": c.careers_url,
                 "checked": today.isoformat(), "url": f.url, "kind": f.kind, "found_on": f.found_on,
                 "page": f.page, "others": f.others, "checks": f.checks, "error": f.error,
                 "first_found": prev.get("first_found", "") if prev.get("url") == f.url and f.url else
                 (today.isoformat() if f.url else "")}
        if f.error and prev.get("url"):
            # Couldn't read the site this time: keep what we knew, try again next week.
            entry.update({kk: prev.get(kk, entry[kk]) for kk in ("url", "kind", "found_on", "page", "others",
                                                                  "checks", "first_found")})
        elif prev.get("url") and f.url and prev["url"] != f.url:
            entry["previous"] = prev["url"]
        state[k] = entry
        log.info("  %-34s %s", c.name[:34], f.url or f.error or "no LinkedIn link on the site")
    return n


ACTIONS = ("differs from list: check", "check, then add", "add to list", "couldn't check",
           "not found", "in list")


def review_rows(companies: list[Listed], state: dict) -> list[dict]:
    """One row per company for the review file, the ones needing a look first."""
    by_url: dict[str, list] = {}
    for c in companies:
        e = state.get(_key(c.name)) or {}
        if e.get("url"):
            by_url.setdefault(e["url"], []).append(c.name)

    rows = []
    for c in companies:
        e = state.get(_key(c.name)) or {}
        url = e.get("url", "")
        checks = list(e.get("checks") or [])
        shared = [n for n in by_url.get(url, []) if n != c.name]
        if shared:
            checks.append("same page found for " + ", ".join(shared) + ": one of them is probably the parent's")
        if e.get("previous"):
            checks.append(f"the site used to link to {e['previous']}")
        if c.approved and url and c.approved == url:
            action = "in list"
        elif c.approved and url:
            action = "differs from list: check"
        elif c.approved:
            action = "in list"
            if e.get("checked") and not e.get("error"):
                checks.append("the site doesn't link to it any more")
        elif url:
            action = "check, then add" if checks else "add to list"
        elif e.get("error"):
            action = "couldn't check"
            checks.insert(0, e["error"])
        elif e:
            action = "not found"
        else:
            action = "couldn't check"
            checks.insert(0, "not checked yet (out of time this run)")
        rows.append({"list": " + ".join(c.lists), "company": c.name, "website": c.website,
                     "linkedin_found": url, "action": action, "what_to_check": "; ".join(checks),
                     "found_on": e.get("found_on", ""), "in_list_now": c.approved,
                     "checked": e.get("checked", "")})
    rows.sort(key=lambda r: (ACTIONS.index(r["action"]), r["company"].lower()))
    return rows


REVIEW_COLS = ["action", "company", "linkedin_found", "what_to_check", "found_on", "in_list_now",
               "website", "list", "checked"]


def write_review(rows: list[dict], path: Optional[Path] = None) -> Path:
    path = path or REVIEW
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="check every company now, not just the ones due")
    ap.add_argument("--only", default="", help="comma-separated company names")
    ap.add_argument("--no-browser", action="store_true", help="never open the headless browser")
    ap.add_argument("--minutes", type=float, default=15, help="stop checking after this long")
    ap.add_argument("--date", default="", help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    companies = merged(_weekly_companies(), _opco_companies())
    if args.only:
        wanted = {_key(n) for n in args.only.split(",") if n.strip()}
        companies = [c for c in companies if _key(c.name) in wanted]
    state = json.loads(FOUND.read_text()) if FOUND.exists() else {}

    browser = None
    if not args.no_browser:
        from . import browser as browser_mod
        browser = browser_mod.Browser(max_pages=60, max_seconds=args.minutes * 60)
    try:
        n = check(companies, state, today, browser=browser, everything=args.all or bool(args.only),
                  minutes=args.minutes)
    finally:
        if browser is not None:
            browser.close()
    FOUND.parent.mkdir(parents=True, exist_ok=True)
    FOUND.write_text(json.dumps(dict(sorted(state.items())), indent=2) + "\n")
    rows = review_rows(companies, state)
    path = write_review(rows)
    counts = {a: sum(1 for r in rows if r["action"] == a) for a in ACTIONS}
    line = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    print(f"[{today}] LinkedIn pages: checked {n} of {len(companies)} companies -> {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  ({line})")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(f"\n## LinkedIn pages for review\n\n{line}. Open `output/linkedin-review.csv`; "
                     "approved links go in a column headed \"LinkedIn\" on the company list.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
