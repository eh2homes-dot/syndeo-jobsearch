#!/usr/bin/env python3
"""Look for a readable leadership page for every watched company that lacks
one, and check Internet Archive coverage for pages the scan can't read.

Uses the scan's own fetcher (lib/http.py) and headless browser as they are:
nothing here disguises itself to get past a site that refuses automated
readers. For such sites it only reads copies the Internet Archive already
holds; it never asks the Archive to fetch a page on our behalf.

Writes probe/results.json (one entry per company: candidate URLs tried, how
each read, Archive coverage) and probe/<slug>-best.html for the best page.

    python tools/probe_leadership.py
    python tools/probe_leadership.py --only "Atlas Real Estate,VTS" -v
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

import run as people_moves  # noqa: E402
from lib import companies as companies_lib  # noqa: E402
from lib.http import SESSION, _throttle  # noqa: E402


class _Resp:
    def __init__(self, r):
        self.status_code, self.text, self.content, self.url = r.status_code, r.text, r.content, r.url


def get(url: str, timeout: int = 20, retries: int = 1):
    """The scan's session and throttle, but keeping the status code (the
    shared helper returns None for a 403 or 404)."""
    from urllib.parse import urlparse as _up
    for _ in range(max(1, retries)):
        _throttle(_up(url).netloc)
        try:
            return _Resp(SESSION.get(url, timeout=timeout))
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return None
from lib.team_page import NOT_TEAM_PATH, TEAM_PATH, extract_team, page_score  # noqa: E402

log = logging.getLogger("probe")
OUT = ROOT / "probe"
CDX = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{ts}id_/{url}"

# Watched, have a URL, but the scan can't read it (Nov 19 baseline run), plus
# the companies whose dead links were cleared.
RECHECK = {"VTS", "WeWork", "Redfin", "Atlas Real Estate", "CoStar Group", "Yardi Systems",
           "Picerne Real Estate Group", "Rocket Companies", "Kite Realty Group Trust",
           "Greystar", "Entrata", "Second Nature", "Renters Warehouse"}
WORDS = re.compile(r"leader|team|management|executive|people|who-we-are|our-story|about|board|founder", re.I)
PER_COMPANY = 6


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def root_of(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


def same_site(a: str, b: str) -> bool:
    strip = lambda h: h.lower()[4:] if h.lower().startswith("www.") else h.lower()  # noqa: E731
    ha, hb = strip(urlparse(a).netloc), strip(urlparse(b).netloc)
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def team_like(url: str) -> bool:
    path = urlparse(url).path or "/"
    return bool(WORDS.search(path)) and not NOT_TEAM_PATH.search(path) and path.count("/") <= 4


def sitemap_urls(root: str) -> list[str]:
    """Team-looking URLs from the site's sitemaps (robots.txt, /sitemap.xml)."""
    maps, seen, found = [], set(), []
    r = get(root + "/robots.txt", timeout=15, retries=1)
    if r is not None and r.status_code == 200:
        maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
    maps += [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    while maps and len(seen) < 12:
        m = maps.pop(0)
        if m in seen:
            continue
        seen.add(m)
        r = get(m, timeout=20, retries=1)
        if r is None or r.status_code != 200 or "<" not in r.text[:200]:
            continue
        try:
            tree = ElementTree.fromstring(r.content)
        except ElementTree.ParseError:
            continue
        for loc in tree.iter():
            if not loc.tag.endswith("loc") or not loc.text:
                continue
            u = loc.text.strip()
            if u.endswith(".xml") or "sitemap" in u.lower():
                # nested sitemaps: follow the ones likely to hold pages
                if re.search(r"page|main|company|about|team|people|sitemap\.xml$", u, re.I):
                    maps.append(u)
            elif same_site(u, root) and team_like(u):
                found.append(u.rstrip("/"))
    return list(dict.fromkeys(found))


def home_links(root: str, browser) -> list[str]:
    """Team-looking links on the homepage, plain and as the browser shows it
    (menus built by scripts only show up in the second)."""
    links = []
    sources = []
    r = get(root, timeout=15, retries=1)
    if r is not None and r.status_code == 200:
        sources.append((r.url or root, r.text))
    if browser is not None:
        try:
            page = browser.render(root)
            sources += [(page.final_url or root, html) for _, html in page.frames[:1]]
        except Exception as exc:  # noqa: BLE001
            log.debug("browser homepage %s: %s", root, exc)
    for base, html in sources:
        for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
            u = urljoin(base, a["href"].strip()).split("#")[0].split("?")[0].rstrip("/")
            if u.startswith("http") and same_site(u, root) and team_like(u):
                links.append(u)
    return list(dict.fromkeys(links))


def read(url: str, browser) -> dict:
    out = {"url": url, "status": 0, "how": "", "people": [], "score": [0, 0, 0]}
    r = get(url, timeout=20, retries=1)
    html = ""
    if r is not None:
        out["status"] = r.status_code
        if r.status_code == 200:
            html, out["how"] = r.text, "plain"
            out["url"] = (r.url or url).rstrip("/")
    people = extract_team(html) if html else []
    if len(people) < 3 and browser is not None and out["status"] not in (404, 410):
        try:
            page = browser.render(url)
            rendered = "\n".join(h for _, h in page.frames)
            more = extract_team(rendered)
            if len(more) > len(people):
                people, html, out["how"] = more, rendered, "browser"
                out["url"] = (page.final_url or url).rstrip("/")
            out["browser_status"] = page.status
        except Exception as exc:  # noqa: BLE001
            out["browser_error"] = str(exc)[:120]
    out["people"] = people
    out["score"] = list(page_score(out["url"], people))
    out["_html"] = html
    return out


def archive(url: str) -> dict:
    """How often the Archive has copied `url` this year, and the newest copy read."""
    q = f"{CDX}?url={url}&from=2026&output=json&fl=timestamp,statuscode&filter=statuscode:200"
    r = get(q, timeout=30, retries=1)
    out = {"url": url, "copies_2026": 0, "latest": "", "people": []}
    if r is None or r.status_code != 200:
        out["error"] = f"cdx {getattr(r, 'status_code', 'no answer')}"
        return out
    try:
        rows = json.loads(r.text or "[]")[1:]
    except ValueError:
        rows = []
    out["copies_2026"] = len(rows)
    if rows:
        ts = rows[-1][0]
        out["latest"] = ts
        months = sorted({row[0][:6] for row in rows})
        out["months"] = months
        s = get(SNAPSHOT.format(ts=ts, url=url), timeout=30, retries=1)
        if s is not None and s.status_code == 200:
            out["people"] = extract_team(s.text)
            out["_html"] = s.text
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--max-minutes", type=float, default=95)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)-7s %(message)s")

    watched = companies_lib.load_lists(people_moves.default_company_sources(), enrich=people_moves.ENRICHMENT)
    todo = [c for c in watched if not c.leadership_url or c.name in RECHECK]
    if a.only:
        want = {companies_lib.normalize(x) for x in a.only.split(",")}
        todo = [c for c in todo if companies_lib.normalize(c.name) in want]
    log.info("probing %d companies", len(todo))

    try:
        from jobsearch.browser import Browser
        browser = Browser(max_pages=600, max_seconds=80 * 60)
    except Exception:  # noqa: BLE001
        browser = None

    OUT.mkdir(exist_ok=True)
    results, started = [], time.time()
    for i, c in enumerate(todo, 1):
        if time.time() - started > a.max_minutes * 60:
            log.warning("time budget used after %d companies", i - 1)
            break
        root = root_of(c.website or c.leadership_url or "")
        res = {"company": c.name, "website": root, "current": c.leadership_url or "",
               "candidates": [], "archive": []}
        if root:
            cands = ([c.leadership_url] if c.leadership_url else []) + sitemap_urls(root) + home_links(root, browser)
            # Leadership-ish words first, then shorter paths.
            cands = sorted(dict.fromkeys(u.rstrip("/") for u in cands if u),
                           key=lambda u: (not re.search(r"leader|team|management|executive", u, re.I),
                                          not TEAM_PATH.search(urlparse(u).path), len(u)))
            res["found_candidates"] = len(cands)
            for u in cands[:PER_COMPANY]:
                res["candidates"].append(read(u, browser))
            best = max(res["candidates"], key=lambda x: x["score"], default=None)
            if c.name in RECHECK or not best or best["score"][1] < 2:
                arch_urls = [x["url"] for x in res["candidates"] if x["status"] in (0, 403, 429, 503)][:2]
                if c.leadership_url and c.leadership_url not in arch_urls:
                    arch_urls.insert(0, c.leadership_url)
                for u in arch_urls[:3]:
                    res["archive"].append(archive(u))
            if best and best.get("_html"):
                (OUT / f"{slug(c.name)}-best.html").write_text(best["_html"], encoding="utf-8")
        for x in res["candidates"] + res["archive"]:
            x.pop("_html", None)
        best = max(res["candidates"], key=lambda x: x["score"], default=None)
        log.info("[%d/%d] %-30s %s", i, len(todo), c.name[:30],
                 f"{best['score']} {best['url']}" if best else "no site")
        results.append(res)
        (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str))
    if browser is not None:
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
