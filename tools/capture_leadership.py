#!/usr/bin/env python3
"""Save every plausible leadership / team page for the watched companies, as
plain downloads and as the headless browser sees them, so the name reader can
be built and tested against real pages offline.

Writes capture/<company-slug>/NN-plain.html and NN-rendered.html plus
capture/index.json (one entry per candidate URL: status, final URL, sizes).
Nothing in the repo is changed. Run by .github/workflows/capture-leadership.yml.

    python tools/capture_leadership.py                    # all targets
    python tools/capture_leadership.py --only "AppFolio,Entrata" -v
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import multiprocessing as mp
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from lib import companies as companies_lib  # noqa: E402

log = logging.getLogger("capture")
OUT = ROOT / "capture"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HINTS = [("leadership", 50), ("executive", 40), ("management-team", 40), ("meet-the-team", 40),
         ("our-team", 35), ("team", 30), ("our-people", 30), ("people", 20), ("management", 20),
         ("who-we-are", 15), ("about", 10), ("company", 5)]
SKIP = re.compile(
    r"(careers?|jobs?|blog|news|press|insights?|podcasts?|webinars?|events?|resources?|"
    r"features?|solutions?|products?|platform|pricing|demo|login|sign|privacy|terms|"
    r"cookie|support|contact|case-stud|customers?|reviews?|partners?/|integrations?|"
    r"\.pdf$|\.jpg$|\.png$|mailto:|tel:)", re.I)
COMMON = ["/leadership", "/about/leadership", "/about-us/leadership", "/company/leadership",
          "/leadership-team", "/our-team", "/team", "/about/team", "/about-us/our-team",
          "/meet-the-team", "/who-we-are", "/about", "/about-us"]
PER_COMPANY = 7
RENDERS_PER_COMPANY = 4


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def site_root(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


def same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb) or strip(ha).endswith("." + strip(hb)) or strip(hb).endswith("." + strip(ha))


def hint(url: str, text: str = "") -> int:
    blob = f"{urlparse(url).path} {text}".lower().replace(" ", "-")
    return max((s for w, s in HINTS if w in blob), default=0)


def slim(html: str) -> str:
    """Drop scripts (except JSON-LD), styles and SVG to keep the capture small."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["style", "svg", "noscript", "iframe"]):
        t.decompose()
    for t in soup("script"):
        if (t.get("type") or "").lower() != "application/ld+json":
            t.decompose()
    return str(soup)


def targets() -> list[dict]:
    """Companies to capture: the 74 discovery candidates, the 30 'no people'
    results, every company already watched, and the old scan's pages."""
    norm = companies_lib.normalize
    rows = {norm(r["Company Name"]): r for r in csv.DictReader(
        open(ROOT / "config" / "companies.csv", encoding="utf-8-sig"))}
    disc = {norm(r["company"]): r for r in csv.DictReader(open(ROOT / "out" / "leadership-discovery.csv"))}
    old = json.loads((ROOT / "data" / "leadership_snapshots.json").read_text())
    out: dict[str, dict] = {}

    def add(name, website="", *urls):
        k = norm(name)
        t = out.setdefault(k, {"company": name, "website": "", "seed": []})
        if website and not t["website"]:
            t["website"] = site_root(website)
        for u in urls:
            if u and u.startswith("http") and u not in t["seed"]:
                t["seed"].append(u)

    for k, d in disc.items():
        if d["status"] in ("found", "too few people (check by hand)",
                           "no people found (page missing or JS-rendered)", "site unreachable"):
            r = rows.get(k, {})
            add(d["company"], d["website"] or r.get("Website URL", ""), d["leadership_url"])
    for k, r in rows.items():
        if (r.get("Leadership URL") or "").strip():
            add(r["Company Name"], r.get("Website URL", ""), r["Leadership URL"].strip())
    for name, v in old.items():
        if v.get("url"):
            r = rows.get(norm(name), {})
            add(name, r.get("Website URL", "") or v["url"], v["url"])
    for t in out.values():
        if not t["website"] and t["seed"]:
            t["website"] = site_root(t["seed"][0])
    return sorted(out.values(), key=lambda t: t["company"].lower())


def plain(session: requests.Session, url: str):
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
        return r.status_code, r.url, r.text if "html" in r.headers.get("content-type", "html") else ""
    except Exception as exc:  # noqa: BLE001
        return 0, url, f"<!-- {type(exc).__name__}: {exc} -->"


def candidates(session, t: dict) -> list[tuple[str, int]]:
    root = t["website"]
    scores: dict[str, int] = {u: 100 for u in t["seed"]}  # seeds are always captured
    if root:
        status, final, html = plain(session, root)
        if status == 200 and html:
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(final, a["href"].strip()).split("#")[0].split("?")[0].rstrip("/")
                if not href.startswith("http") or not same_site(href, root) or SKIP.search(urlparse(href).path):
                    continue
                s = hint(href, a.get_text(" ", strip=True))
                if s >= 10:
                    scores[href] = max(scores.get(href, 0), s + 5)
        for p in COMMON:
            scores.setdefault(root + p, hint(root + p) - 5)
    return sorted(scores.items(), key=lambda kv: -kv[1])[:PER_COMPANY]


def capture_company(t: dict) -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    d = OUT / slug(t["company"])
    d.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, (url, score) in enumerate(candidates(session, t)):
        status, final, html = plain(session, url)
        e = {"company": t["company"], "n": i, "url": url, "score": score, "status": status,
             "final_url": final, "plain_bytes": 0, "rendered": False}
        if status == 200 and html:
            (d / f"{i:02d}-plain.html").write_text(slim(html), encoding="utf-8")
            e["plain_bytes"] = len(html)
        entries.append(e)
        time.sleep(0.4)
    return entries


def render_worker(jobs: list[dict], q) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US")
        for e in jobs:
            page = ctx.new_page()
            try:
                resp = page.goto(e["url"], wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:  # noqa: BLE001
                    pass
                for _ in range(6):  # lazy-loaded team grids
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(350)
                page.wait_for_timeout(800)
                html = "\n".join(f.content() for f in [page.main_frame] + [
                    f for f in page.frames if f != page.main_frame and f.url.startswith("http")][:3])
                d = OUT / slug(e["company"])
                (d / f"{e['n']:02d}-rendered.html").write_text(slim(html), encoding="utf-8")
                q.put({"company": e["company"], "n": e["n"], "rendered": True,
                       "render_status": resp.status if resp else 0, "render_final": page.url})
            except Exception as exc:  # noqa: BLE001
                q.put({"company": e["company"], "n": e["n"], "rendered": False,
                       "render_error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            finally:
                page.close()
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)-7s %(message)s")

    ts = targets()
    if a.only:
        want = {companies_lib.normalize(x) for x in a.only.split(",")}
        ts = [t for t in ts if companies_lib.normalize(t["company"]) in want]
    log.info("capturing %d companies", len(ts))
    OUT.mkdir(exist_ok=True)

    entries: list[dict] = []
    with ThreadPoolExecutor(8) as pool:
        for i, es in enumerate(pool.map(capture_company, ts), 1):
            entries += es
            if i % 20 == 0:
                log.info("  plain: %d/%d companies", i, len(ts))

    # Browser pass: seeds plus the best-hinted candidates, a few per company.
    jobs, per = [], {}
    for e in sorted(entries, key=lambda e: -e["score"]):
        if e["status"] in (404, 410):
            continue
        if per.get(e["company"], 0) >= RENDERS_PER_COMPANY:
            continue
        per[e["company"]] = per.get(e["company"], 0) + 1
        jobs.append(e)
    log.info("rendering %d pages in %d browsers", len(jobs), a.workers)
    q = mp.Queue()
    chunks = [jobs[i::a.workers] for i in range(a.workers)]
    procs = [mp.Process(target=render_worker, args=(c, q)) for c in chunks if c]
    for p in procs:
        p.start()
    by_key = {(e["company"], e["n"]): e for e in entries}
    done = 0
    while done < len(jobs):
        try:
            r = q.get(timeout=300)
        except Exception:  # noqa: BLE001
            if not any(p.is_alive() for p in procs):
                break
            continue
        by_key[(r.pop("company"), r.pop("n"))].update(r)
        done += 1
        if done % 50 == 0:
            log.info("  rendered %d/%d", done, len(jobs))
    for p in procs:
        p.join(timeout=30)

    (OUT / "index.json").write_text(json.dumps({"targets": ts, "entries": entries}, indent=1))
    ok = sum(1 for e in entries if e["status"] == 200)
    log.info("done: %d candidate URLs, %d loaded plainly, %d rendered", len(entries), ok,
             sum(1 for e in entries if e.get("rendered")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
