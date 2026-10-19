#!/usr/bin/env python3
"""
NARPM Affiliate Member directory scraper.

Source: https://www.narpm.org/find/affiliate-members/
  - ~227 national affiliates (vendors to residential property managers)
  - 30 results per page, paginated with a 0-indexed `pgno` query param
  - Emails are NOT published; each listing links to a NARPM contact form
    keyed by MLS_ID, which we keep as the stable record ID.

Usage:
  pip install requests beautifulsoup4
  python narpm_affiliates.py                          # all affiliates -> narpm_affiliates.csv
  python narpm_affiliates.py --out data/narpm.csv
  python narpm_affiliates.py --type "Software"        # one business type only
  python narpm_affiliates.py --delay 3                # seconds between page requests

How parsing works:
  The page is flattened into an ordered stream of text/link/image tokens.
  Every listing has the same shape:
      <Company> <Business Type> "More" <photo> <Contact name>
      <address lines...> "See Map" Phone/Fax/Email/website "Refer a Client Lead to Member"
      <optional description...>
  so we split on the "More" toggle and read fields by their link targets
  (tel:, fax:, contact_member?MLS_ID=, maps.google.com, external http).
  This avoids depending on CSS class names that can change in a redesign.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup, Comment, NavigableString

BASE_URL = "https://www.narpm.org/find/affiliate-members"
PER_PAGE = 30  # the site's fixed page size

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Types shown in the site's filter dropdown, plus ones seen on listings.
KNOWN_TYPES = {
    "Accounting", "Advertising", "Banking/Financial Services",
    "Business Products and Services", "Cleaning/Removal Services",
    "Credit Card Processing", "Property Mgmt. Franchise Co.", "Inspections",
    "Insurance", "Internet Tools and Marketing", "Legal Services",
    "Maintenance", "Merchant Services", "Other", "Software", "Tenant Screening",
}

PLACEHOLDER_PHONES = {"999-999-9999", "000-000-0000"}
DEFAULT_AVATAR_MARKER = "user-avatar"
REFER_TEXT = "Refer a Client Lead to Member"

FIELDS = [
    "mls_id", "company", "business_type", "contact_name",
    "street", "city", "state", "zip",
    "phone", "fax", "website", "website_domain",
    "description", "photo_url", "contact_form_url", "referral_url", "map_url",
    "source_page", "scraped_at",
]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def page_url(pgno: int, business_type: str = "") -> str:
    params = {
        "a": "affiliates",
        "sort": "Office_Name",
        "submitted": "true",
        "toresults": "1",
        "resultsperpage": str(PER_PAGE),
        "company": "",
        "type": business_type,
        "pgno": str(pgno),
    }
    return f"{BASE_URL}?{urlencode(params)}"


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and "Search Results" in r.text:
                return r.text
            last_err = f"HTTP {r.status_code}" + (
                "" if r.status_code != 200 else " (no 'Search Results' on page — possible bot challenge)"
            )
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

@dataclass
class Tok:
    kind: str            # "text" | "img"
    text: str = ""
    href: str = ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def linearize(root) -> list[Tok]:
    """Flatten the DOM into ordered text/img tokens; text keeps its enclosing link."""
    toks: list[Tok] = []
    for el in root.descendants:
        if isinstance(el, Comment):
            continue
        if isinstance(el, NavigableString):
            if el.parent and el.parent.name in ("script", "style", "noscript", "option", "select"):
                continue
            t = _clean(str(el))
            if not t:
                continue
            a = el.find_parent("a")
            toks.append(Tok("text", t, (a.get("href") or "").strip() if a else ""))
        elif getattr(el, "name", None) == "img":
            toks.append(Tok("img", href=(el.get("src") or "").strip()))
    return toks


def results_window(toks: list[Tok]) -> tuple[list[Tok], int | None]:
    """Slice tokens to the results section; return (tokens, total_count)."""
    start, total = 0, None
    for i, t in enumerate(toks):
        m = re.search(r"Found\s+(\d+)\s+results?", t.text, re.I)
        if m:
            start, total = i + 1, int(m.group(1))
            break
    end = len(toks)
    for i in range(start, len(toks)):
        if toks[i].text.startswith("Follow Us"):
            end = i
            break
    # Drop pagination links (they carry pgno= in the href).
    window = [t for t in toks[start:end] if "pgno=" not in t.href]
    return window, total


def _mls_id(href: str) -> str:
    q = parse_qs(urlparse(href).query)
    return (q.get("MLS_ID") or [""])[0]


def _is_external_site(href: str) -> bool:
    if not href.lower().startswith(("http://", "https://")):
        return False
    host = urlparse(href).netloc.lower()
    return not any(h in host for h in ("narpm.org", "maps.google", "google.com/maps",
                                        "internetmemberservices.com"))


def _phone(text: str) -> str:
    p = _clean(text)
    return "" if p in PLACEHOLDER_PHONES else p


def parse_listing(seg: list[Tok], company: str, btype: str) -> dict:
    rec = {k: "" for k in FIELDS}
    rec["company"], rec["business_type"] = company, btype

    i, n = 0, len(seg)

    # Photo (optional) then contact name
    while i < n and seg[i].kind == "img":
        if DEFAULT_AVATAR_MARKER not in seg[i].href:
            rec["photo_url"] = seg[i].href
        i += 1
    if i < n and seg[i].kind == "text" and "maps.google" not in seg[i].href:
        rec["contact_name"] = seg[i].text
        i += 1

    # Address lines until "See Map"
    addr = []
    while i < n and "maps.google" not in seg[i].href:
        if seg[i].kind == "text" and not seg[i].href:
            addr.append(seg[i].text.rstrip(",").strip())
        if seg[i].text.startswith(("Phone", "Email")):  # no map link at all
            break
        i += 1
    if i < n and "maps.google" in seg[i].href:
        rec["map_url"] = seg[i].href
        i += 1
    addr = [a for a in addr if a]
    if len(addr) >= 4:
        rec["street"] = ", ".join(addr[:-3])
        rec["city"], rec["state"], rec["zip"] = addr[-3], addr[-2], addr[-1]
    elif addr:
        rec["street"] = ", ".join(addr)

    # Contact block until the referral link
    while i < n:
        t = seg[i]
        h = t.href.lower()
        if t.text == REFER_TEXT or "referral?mls_id" in h:
            rec["referral_url"] = t.href
            rec["mls_id"] = rec["mls_id"] or _mls_id(t.href)
            i += 1
            break
        if h.startswith("tel:"):
            rec["phone"] = rec["phone"] or _phone(t.text)
        elif h.startswith("fax:"):
            rec["fax"] = rec["fax"] or _phone(t.text)
        elif "contact_member" in h:
            rec["contact_form_url"] = t.href
            rec["mls_id"] = _mls_id(t.href)
        elif _is_external_site(t.href) and not rec["website"]:
            rec["website"] = t.href
        i += 1

    # Everything after the referral link is the free-text description.
    desc = [t.text for t in seg[i:] if t.kind == "text"]
    while desc and re.fullmatch(r"[\d>< ]+|Pages:?|,", desc[-1]):  # trailing pager crumbs
        desc.pop()
    rec["description"] = "\n".join(desc).strip()

    if rec["website"]:
        rec["website_domain"] = re.sub(r"^www\.", "", urlparse(rec["website"]).netloc.lower())
    return rec


def parse_page(html: str) -> tuple[list[dict], int | None, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup
    toks, total = results_window(linearize(body))

    more_idx = [i for i, t in enumerate(toks) if t.kind == "text" and t.text == "More"]
    records, warnings = [], []
    for k, mi in enumerate(more_idx):
        if mi < 2:
            continue
        company, btype = toks[mi - 2].text, toks[mi - 1].text
        seg_end = (more_idx[k + 1] - 2) if k + 1 < len(more_idx) else len(toks)
        rec = parse_listing(toks[mi + 1: seg_end], company, btype)
        if btype not in KNOWN_TYPES:
            warnings.append(f"Unrecognized business type {btype!r} for {company!r} — check this row")
        if not rec["mls_id"]:
            warnings.append(f"No MLS_ID found for {company!r}")
        records.append(rec)
    return records, total, warnings


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def scrape(business_type: str = "", delay: float = 2.0) -> list[dict]:
    session = requests.Session()
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen, out = set(), []

    pgno, total, pages = 0, None, None
    while True:
        url = page_url(pgno, business_type)
        html = fetch(session, url)
        recs, page_total, warnings = parse_page(html)
        if total is None:
            total = page_total
            pages = math.ceil(total / PER_PAGE) if total else None
            print(f"Directory reports {total} results across {pages} pages", file=sys.stderr)

        new = 0
        for r in recs:
            key = r["mls_id"] or (r["company"], r["contact_name"])
            if key in seen:
                continue
            seen.add(key)
            r["source_page"] = pgno + 1
            r["scraped_at"] = scraped_at
            out.append(r)
            new += 1
        for w in warnings:
            print(f"  ! page {pgno + 1}: {w}", file=sys.stderr)
        print(f"Page {pgno + 1}: {len(recs)} listings ({new} new)", file=sys.stderr)

        pgno += 1
        if new == 0 or (pages is not None and pgno >= pages):
            break
        time.sleep(delay)

    if total is not None and len(out) != total:
        print(f"WARNING: scraped {len(out)} but site reports {total}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape the NARPM affiliate member directory")
    ap.add_argument("--out", default="narpm_affiliates.csv", help="CSV output path")
    ap.add_argument("--type", default="", help='Filter to one business type, e.g. "Software"')
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between page requests")
    args = ap.parse_args()

    rows = scrape(args.type, args.delay)
    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
