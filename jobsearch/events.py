"""Weekly events pull: association conferences, Bisnow, and events hosted by master-list companies.

    python -m jobsearch.events                        # everything
    python -m jobsearch.events --only associations     # or bisnow, companies (comma-separated)
    python -m jobsearch.events --detect                # also look for events pages for companies not mapped yet
    python -m jobsearch.events --fixtures tests/fixtures/events --date 2026-09-28 --dry-run   # offline test

Inputs
  data/event_sources.json     which association / IMN / Bisnow pages to read, filters, newsletter settings
  leads/master_leads.csv      the companies whose own events we track (no discovery beyond this list)
  data/events_map.json        company -> events page. Found by --detect or typed in by hand; manual entries win
  data/events.json            every event ever seen, with first_seen / last_seen / status
  data/narpm_chapters.json    NARPM chapter -> website + feed. Built from narpm.org's chapter finder and refreshed
                              monthly; entries with "manual": true are never overwritten

Outputs (output/YYYY-MM-DD/)
  events.csv                  every upcoming event in the horizon: when, where, who, link
  events_sources.csv          per-source status, so a broken page never looks like "no events"
  events_report.md / .html    readable list
  debug/<source>.html         raw page for any source that loaded but gave zero events (to fix the parser)
  output/newsletter/events-YYYY-MM-DD.md / .html   paste-ready "Upcoming events" section
  output/events-latest.csv    same address every week, for a Google Sheet:
                              =IMPORTDATA("https://raw.githubusercontent.com/eh2homes-dot/syndeo-jobsearch/main/output/events-latest.csv")
                              Only full runs (all sources) overwrite it, so a partial manual run never empties the sheet.
  data/events_needs_manual.csv   companies where no events page was found
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html as H
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment

from .adapters import UA
from .run import LEADS, OUT, ROOT, draft_html, load_json, save_json

SOURCES = ROOT / "data" / "event_sources.json"
HISTORY = ROOT / "data" / "events.json"
EVENTS_MAP = ROOT / "data" / "events_map.json"
CHAPTERS_MAP = ROOT / "data" / "narpm_chapters.json"
NEEDS_MAP = ROOT / "data" / "events_needs_manual.csv"
TIMEOUT = 20
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head", "title", "iframe", "select", "option"}
HEADINGS = ["h1", "h2", "h3", "h4", "h5"]


# ================================================================ fetching
class Fetcher:
    """GET with a short retry. Never raises: returns (status, text, final_url); status 0 = network error.
    In fixture mode, URLs are looked up in <fixtures>/index.json and read from disk."""

    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures
        self.index = load_json(fixtures / "index.json", {}) if fixtures else {}
        self.headers = {**UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9"}

    def get(self, url: str, timeout: int = TIMEOUT, tries: int = 2) -> tuple[int, str, str]:
        if self.fixtures is not None:
            name = self.index.get(url) or self.index.get(url.rstrip("/")) or self.index.get(url.rstrip("/") + "/")
            return (200, (self.fixtures / name).read_text(), url) if name else (404, "", url)
        last = (0, "", url)
        for i in range(tries):
            try:
                r = requests.get(url, headers=self.headers, timeout=timeout, allow_redirects=True)
                if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                    time.sleep(3 * (i + 1))
                    continue
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.status_code, r.text, r.url
            except requests.RequestException as e:
                last = (0, f"{type(e).__name__}: {str(e)[:160]}", url)
                if i < tries - 1:
                    time.sleep(2)
        return last


# ================================================================ dates
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_WD = r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YR = r"(20\d{2})"
_DASH = r"\s*(?:[-\u2010-\u2015]|to|through|thru|\u00bb)\s*"
RANGE_RE = re.compile(rf"\b{_WD}{_MON}\s+{_DAY}(?:,?\s*{_YR})?{_DASH}{_WD}(?:{_MON}\s+)?{_DAY}(?:,?\s*{_YR})?(?!\d)", re.I)
SINGLE_RE = re.compile(rf"\b{_WD}{_MON}\s+{_DAY}(?:,?\s*{_YR})?(?![\d:])", re.I)
NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})(?:\s*(?:[-\u2013\u2014]|\u00bb|to)\s*(\d{1,2})/(\d{1,2})/(20\d{2}))?")
ISO_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})")
# day-month-year, as Wild Apricot writes it: "18 Nov 2026, 11:00 AM - 1:00 PM" or "18 Nov 2026 9:00 AM - 19 Nov 2026 5:00 PM"
DMY_RE = re.compile(rf"\b{_DAY}\s+{_MON},?\s+{_YR}\b", re.I)
LABEL_PREFIX = re.compile(r"^\s*(?:dates?|when|location|where|venue)\s*:\s*", re.I)
LABEL_ONLY = re.compile(r"^\s*(?:dates?|when|location|where|venue|time)\s*:?\s*$", re.I)


def _mon(s: str | None) -> int:
    return ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(s[:3].lower()) + 1


def _safe(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def parse_dates(text: str, today: dt.date, default_year: int | None = None):
    """First date or date range in text -> (start, end, has_year) or None."""
    t = (text or "").replace("\xa0", " ").replace("\u2009", " ")
    if len(t) > 400:
        t = t[:400]
    m = RANGE_RE.search(t)
    if m:
        mo1, d1, y1, mo2, d2, y2 = m.groups()
        m1, m2 = _mon(mo1), (_mon(mo2) if mo2 else _mon(mo1))
        y1, y2 = (int(y1) if y1 else None), (int(y2) if y2 else None)
        has_year = bool(y1 or y2 or default_year)
        if y1 is None and y2 is not None:
            y1 = y2 - 1 if m1 > m2 else y2
        if y1 is None:
            y1 = default_year or _infer_year(m1, int(d1), today)
        if y2 is None:
            y2 = y1 + 1 if m2 < m1 else y1
        s, e = _safe(y1, m1, int(d1)), _safe(y2, m2, int(d2))
        if s and e and 0 <= (e - s).days <= 31:
            return s, e, has_year
        if s:
            return s, s, has_year
    m = NUM_RE.search(t)
    if m:
        s = _safe(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        e = _safe(int(m.group(6)), int(m.group(4)), int(m.group(5))) if m.group(4) else s
        if s:
            return s, (e if e and 0 <= (e - s).days <= 31 else s), True
    ms = list(DMY_RE.finditer(t))
    if ms:
        d1, mo1, y1 = ms[0].groups()
        s = _safe(int(y1), _mon(mo1), int(d1))
        e = None
        if len(ms) > 1:
            d2, mo2, y2 = ms[1].groups()
            e = _safe(int(y2), _mon(mo2), int(d2))
        if s:
            return s, (e if e and 0 <= (e - s).days <= 31 else s), True
    m = SINGLE_RE.search(t)
    if m:
        mo, d, y = m.groups()
        mm = _mon(mo)
        yy = int(y) if y else (default_year or _infer_year(mm, int(d), today))
        s = _safe(yy, mm, int(d))
        if s:
            return s, s, bool(y or default_year)
    m = ISO_RE.search(t)
    if m:
        s = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if s:
            return s, s, True
    return None


def _infer_year(month: int, day: int, today: dt.date) -> int:
    d = _safe(today.year, month, day) or _safe(today.year, month, 28)
    return today.year + 1 if d and d < today - dt.timedelta(days=60) else today.year


def is_mostly_date(text: str, today: dt.date) -> bool:
    """True when a line is basically just a date (plus maybe a time/label), not a sentence with a date in it."""
    t = LABEL_PREFIX.sub("", text)
    if len(t) > 60 or not parse_dates(t, today):
        return False
    rest = RANGE_RE.sub("", t) if RANGE_RE.search(t) else SINGLE_RE.sub("", NUM_RE.sub("", DMY_RE.sub("", t)))
    rest = re.sub(r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|"
                  r"\b(?:am|pm|[ecmp][sd]t|et|ct|mt|pt|bst|gmt|cet|noon|eastern|central|mountain|pacific)\b|[|,\-\u2013\u2014@()]",
                  " ", rest, flags=re.I)
    return len(rest.split()) <= 2


def fmt_range(s: dt.date, e: dt.date, with_year: bool = False) -> str:
    y = f", {e.year}" if with_year else ""
    if s == e:
        return f"{s.strftime('%b')} {s.day}{y}"
    if s.month == e.month:
        return f"{s.strftime('%b')} {s.day}\u2013{e.day}{y}"
    return f"{s.strftime('%b')} {s.day}\u2013{e.strftime('%b')} {e.day}{y}"


# ================================================================ locations
STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
          "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
          "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
STATE_NAMES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
               "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
               "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
               "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
               "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
               "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
               "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
               "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
               "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
               "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
CITY_ST = re.compile(r"([A-Z][A-Za-z.'\u2019\- ]{1,40}?),?\s+(" + "|".join(sorted(STATES)) + r"|D\.C\.)(?![A-Za-z])")
CITY_STNAME = re.compile(r"([A-Z][A-Za-z.'\u2019\- ]{1,40}?),\s*(" + "|".join(n.title() for n in STATE_NAMES) + r")\b")
ONLINE = re.compile(r"\b(online|virtual|webinar|zoom|livestream|on-demand|web event)\b", re.I)


def city_state(text: str) -> tuple[str, str] | None:
    t = (text or "").replace("Washington, D.C.", "Washington, DC").replace("Washington D.C.", "Washington, DC")
    best = None
    for m in CITY_ST.finditer(t):
        city = m.group(1).strip(" ,-")
        if city.lower() in {"in", "at", "the", "and"}:
            continue
        best = (city, "DC" if m.group(2) == "D.C." else m.group(2))
    if best is None:
        for m in CITY_STNAME.finditer(t):
            best = (m.group(1).strip(" ,-"), STATE_NAMES[m.group(2).lower()])
    return best


def clean_loc(parts: list[str]) -> str:
    s = ", ".join(p.strip(" ,|\u00b7") for p in parts if p and p.strip(" ,|\u00b7"))
    s = re.sub(r"\s*\|\s*", ", ", s)
    s = re.sub(r"(,\s*){2,}", ", ", s)
    return s.strip(" ,")[:140]


# ================================================================ HTML helpers
def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def tokens(soup: BeautifulSoup) -> list[dict]:
    """Visible text nodes in document order, each with its link, heading, and bold context."""
    out = []
    for s in soup.find_all(string=True):
        if isinstance(s, Comment) or s.find_parent(list(SKIP_TAGS)) is not None:
            continue
        txt = " ".join(s.split())
        if not txt:
            continue
        a = s.find_parent("a")
        h = s.find_parent(HEADINGS)
        out.append({"t": txt, "href": (a.get("href") or None) if a else None, "h": h, "b": s.find_parent(["strong", "b"]) is not None})
    return out


def heading_text(toks: list[dict], j: int) -> tuple[str, str | None]:
    """Full text + first link of the heading that token j belongs to."""
    h = toks[j]["h"]
    lo = j
    while lo > 0 and toks[lo - 1]["h"] is h:
        lo -= 1
    hi = j
    while hi + 1 < len(toks) and toks[hi + 1]["h"] is h:
        hi += 1
    parts = toks[lo:hi + 1]
    href = next((p["href"] for p in parts if p["href"]), None)
    if href is None and h is not None:
        a = h.find_parent("a") or h.find("a")
        href = a.get("href") if a else None
    return " ".join(p["t"] for p in parts).strip(), href


ACCESS_RE = re.compile(r"\b(members?[- ]only|invitation only|invite only|by invitation)\b", re.I)
NOISE_RE = re.compile(r"^(log in for details|register( now| here| today)?|learn more|read more|view (details|event|more)|details|"
                      r"more info|click here|coming soon|sold out|add to calendar|\||\u00b7|\*|save the date|rsvp|buy tickets|tickets)$", re.I)


def good_href(href: str | None, base: str) -> str:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return ""
    return urljoin(base, href)


# ================================================================ event records
LEAD_TIME = re.compile(r"^\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-\u2013\u2014to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*,?\s*", re.I)


def clean_location(location: str) -> str:
    """'10:30 AM - 12:30 PM, NRH Grand Hall, ...' -> 'NRH Grand Hall, ...'; 'TBA, TBA, TBA, United States' -> ''."""
    loc = LEAD_TIME.sub("", " ".join(H.unescape(location or "").split()))
    parts = [p.strip() for p in loc.split(",")]
    parts = [p for p in parts if p and p.upper() not in {"TBA", "TBD", "UNITED STATES", "USA", "US"}]
    return ", ".join(parts)


def make_event(source: str, organizer: str, title: str, start, end, location: str, url: str, **kw) -> dict:
    title = " ".join(H.unescape(title or "").split()).strip(" -\u2013|*\"\u201c\u201d")
    location = clean_location(location)
    cs = city_state(location)
    online = bool(ONLINE.search(location)) or bool(ONLINE.search(kw.get("fmt", "") or ""))
    ev = {"source": source, "organizer": organizer, "title": title,
          "start": start.isoformat() if start else "", "end": (end or start).isoformat() if start else "",
          "location": location or ("Online" if online else ""), "city": cs[0] if cs else "", "state": cs[1] if cs else "",
          "format": "Online" if online and not cs else "In person",
          "access": kw.get("access", "open"), "url": url, "kind": kw.get("kind", "conference"),
          "company": kw.get("company", ""), "topic": kw.get("topic", ""), "time": kw.get("time", ""),
          "method": kw.get("method", ""), "tba": bool(kw.get("tba"))}
    return ev


def norm_title(t: str) -> str:
    t = re.sub(r"[\u00ae\u2122]", "", (t or "").lower())
    t = re.sub(r"\b20\d{2}\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(w for w in t.split() if w not in {"the", "and", "of", "a", "annual", "conference", "event"})


def event_id(e: dict) -> str:
    key = f"{e['source']}|{e.get('company', '')}|{norm_title(e['title'])}|{e['start']}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def title_sim(a: str, b: str) -> float:
    A, B = set(norm_title(a).split()), set(norm_title(b).split())
    return len(A & B) / max(1, min(len(A), len(B))) if A and B else 0.0


# ================================================================ parsers (one per page layout)
def parse_title_date(html: str, base: str, today: dt.date, src: dict) -> list[dict]:
    """Heading (title, usually linked) followed by a date, then venue / city lines.
    Used for NMHC's upcoming meetings page and NARPM's national and state conference pages."""
    toks = tokens(soup_of(html))
    out, seen = [], set()
    for i, tk in enumerate(toks):
        if tk["h"] is not None or not is_mostly_date(tk["t"], today):
            continue
        d = parse_dates(LABEL_PREFIX.sub("", tk["t"]), today)
        j = next((j for j in range(i - 1, max(-1, i - 9), -1) if toks[j]["h"] is not None), None)
        if j is None:
            continue
        title, href = heading_text(toks, j)
        if title.endswith(":"):
            # "NARPM® Partner Webinar sponsored by ShowMojo:" then the real name in bold on the next line
            k = j
            while k + 1 < i and toks[k + 1]["h"] is toks[j]["h"]:
                k += 1
            nxt = toks[k + 1] if k + 1 < i else None
            if nxt and nxt["b"] and nxt["h"] is None and len(nxt["t"]) < 150 and not is_mostly_date(nxt["t"], today):
                title = nxt["t"]
        if src.get("date_from_time_line"):
            # NARPM's webinar blocks sometimes carry a stale Date: field; the Time: line has the real date
            for k in range(i + 1, min(len(toks) - 1, i + 8)):
                if re.match(r"^\s*time\s*:?\s*$", toks[k]["t"], re.I):
                    td = parse_dates(toks[k + 1]["t"], today)
                    if td and td[2]:
                        d = td
                    break
        if not title or (title, d[0]) in seen:
            continue
        seen.add((title, d[0]))
        loc, access = [], "open"
        for k in range(i + 1, min(len(toks), i + 12)):
            t = toks[k]
            if t["h"] is not None or is_mostly_date(t["t"], today):
                break
            s = LABEL_PREFIX.sub("", t["t"])
            am = ACCESS_RE.search(s)
            if am:
                access = "members only" if "member" in am.group(1).lower() else "invitation only"
                continue
            if LABEL_ONLY.match(s) or NOISE_RE.match(s) or len(s) > 90:
                continue
            if not (city_state(" ".join(loc)) or ONLINE.search(" ".join(loc))):
                loc.append(s)
        out.append(make_event(src["id"], src["name"], title, d[0], d[1], clean_loc(loc), good_href(href, base) or base,
                              access=access, method="page"))
    return out


def parse_date_title(html: str, base: str, today: dt.date, src: dict) -> list[dict]:
    """Date first, then title, then city, then a register link. Used for NRHC's public events page."""
    toks = tokens(soup_of(html))
    date_idx = [i for i, tk in enumerate(toks) if is_mostly_date(tk["t"], today)]
    out = []
    for n, i in enumerate(date_idx):
        stop = date_idx[n + 1] if n + 1 < len(date_idx) else min(len(toks), i + 12)
        d = parse_dates(LABEL_PREFIX.sub("", toks[i]["t"]), today)
        body = [t for t in toks[i + 1:stop] if not NOISE_RE.match(t["t"])]
        if not body:
            continue
        title = body[0]["t"]
        loc = body[1]["t"] if len(body) > 1 and (city_state(body[1]["t"]) or ONLINE.search(body[1]["t"]) or len(body[1]["t"]) < 50) else ""
        near = toks[i:min(stop, i + 6)]
        href = next((good_href(t["href"], base) for t in near
                     if good_href(t["href"], base) and "email-protection" not in t["href"]), "") or base
        out.append(make_event(src["id"], src["name"], title, d[0], d[1], loc, href, method="page"))
    return out


def parse_date_heading(html: str, base: str, today: dt.date, src: dict) -> list[dict]:
    """A date line followed by a heading with the title, then a description. Used for NARPM's course schedule:
    "Nov 17, 2026 12pm - 3pm EASTERN" / <h3><a>How to Successfully Market Your Company (Virtual)</a></h3> / blurb."""
    toks = tokens(soup_of(html))
    out, seen = [], set()
    for i, tk in enumerate(toks):
        if tk["h"] is not None or not is_mostly_date(tk["t"], today):
            continue
        j = next((j for j in range(i + 1, min(len(toks), i + 6)) if toks[j]["h"] is not None), None)
        if j is None:
            continue
        title, href = heading_text(toks, j)
        d = parse_dates(LABEL_PREFIX.sub("", tk["t"]), today)
        if not title or (title, d[0]) in seen:
            continue
        seen.add((title, d[0]))
        if ONLINE.search(f"{title} {tk['t']}"):
            loc = "Online"
        else:   # look for a city only inside this course's block, never past the next course or into the footer
            nxt = next((k for k in range(j + 1, len(toks)) if is_mostly_date(toks[k]["t"], today)), len(toks))
            nxt_h = next((k for k in range(j + 1, len(toks)) if toks[k]["h"] is not None and toks[k]["h"] is not toks[j]["h"]),
                         len(toks))
            loc = next((toks[k]["t"] for k in range(j + 1, min(nxt, nxt_h, j + 15))
                        if len(toks[k]["t"]) <= 90 and city_state(toks[k]["t"])), "")
        tm = re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", tk["t"], re.I)
        out.append(make_event(src["id"], src["name"], title, d[0], d[1], loc, good_href(href, base) or base,
                              time=tm.group(0) if tm else "", method="page"))
    return out


def parse_naa(html: str, base: str, today: dt.date, src: dict) -> list[dict]:
    """NAA's upcoming-events page: year headings, then title / (Invitation Only) / date without year / venue | city."""
    toks = tokens(soup_of(html))
    out, year = [], None
    for i, tk in enumerate(toks):
        if re.fullmatch(r"20\d{2}", tk["t"]):
            year = int(tk["t"])
            continue
        if year is None or len(tk["t"]) > 50 or not is_mostly_date(tk["t"], today):
            continue
        d = parse_dates(tk["t"], today, default_year=year)
        title, access = "", "open"
        for j in range(i - 1, max(-1, i - 4), -1):
            s = toks[j]["t"].strip(" *")
            if ACCESS_RE.search(s):
                access = "invitation only"
                continue
            if not s or re.fullmatch(r"20\d{2}", s) or is_mostly_date(s, today):
                break
            title = s
            break
        if not title:
            continue
        nxt = toks[i + 1]["t"] if i + 1 < len(toks) else ""
        loc = "" if (re.fullmatch(r"20\d{2}", nxt) or is_mostly_date(nxt, today) or len(nxt) > 100) else clean_loc([nxt])
        out.append(make_event(src["id"], src["name"], title, d[0], d[1], loc, base, access=access, method="page"))
    return out


BISNOW_CARD = re.compile(r"^(?:https?://(?:www\.)?bisnow\.com)?/(?:events|webinar)/[^/?#]+/[^/?#]+/[^/?#]+-\d+/?$")


def card_box(a, href_ok) -> object:
    """Smallest ancestor of link `a` that contains no other card's link: the card itself."""
    target = a.get("href").split("?")[0].rstrip("/")
    node = a
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        hrefs = {x.get("href").split("?")[0].rstrip("/") for x in node.parent.find_all("a", href=True) if href_ok(x.get("href"))}
        if hrefs - {target}:
            break
        node = node.parent
    return node


def parse_bisnow(html: str, base: str, today: dt.date, src: dict) -> list[dict]:
    soup = soup_of(html)
    ok = lambda h: bool(h) and bool(BISNOW_CARD.match(h.split("?")[0]))
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if not ok(a["href"]):
            continue
        href = urljoin(base, a["href"].split("?")[0])
        if href in seen:
            continue
        seen.add(href)
        box = card_box(a, ok)
        lines = [" ".join(s.split()) for s in box.find_all(string=True)
                 if not isinstance(s, Comment) and s.find_parent(list(SKIP_TAGS)) is None and s.strip()]
        fmt = next((l for l in lines if l.lower() in {"in person", "webinar", "virtual", "hybrid", "livestream"}), "")
        meta = next((l for l in lines if "|" in l and not re.search(r"\d", l)), "")
        dline = next((l for l in lines if parse_dates(l, today)), "")
        if not dline:
            continue
        d = parse_dates(dline, today)
        h = box.find(HEADINGS)
        title = " ".join(h.get_text(" ").split()) if h else ""
        if not title:
            rest = [l for l in lines if l not in (fmt, meta, dline) and not NOISE_RE.match(l)]
            title = max(rest, key=len) if rest else ""
        market, topic = ([p.strip() for p in meta.split("|", 1)] + [""])[:2] if meta else ("", "")
        tm = dline.split("|", 1)[1].strip() if "|" in dline else ""
        tz = (tm.split()[-1] if tm else "").upper()
        if market in src.get("non_us_markets", []) or tz in {"BST", "GMT", "CET", "CEST", "IST", "WET"}:
            continue
        if src.get("topics") and topic and topic not in src["topics"]:
            continue
        if topic in src.get("topics_needing_keywords", []) and not re.search("|".join(src.get("keywords", ["$^"])), title, re.I):
            continue
        online = fmt.lower() in {"webinar", "virtual", "livestream"}
        loc = "Online" if online else market
        out.append(make_event(src["id"], src["name"], title, d[0], d[1], loc, href, topic=topic, time=tm,
                              fmt="Online" if online else "", method="page"))
    return out


def parse_informa_page(html: str, url: str, today: dt.date, src: dict, name: str = "") -> dict | None:
    """One IMN event site on informaconnect.com. Header reads e.g. 'May 24-26, 2027|Loews Miami Beach, Miami, FL'."""
    soup = soup_of(html)
    if not name:
        og = soup.find("meta", property="og:title")
        name = (og.get("content") if og else "") or (soup.title.get_text() if soup.title else "")
        name = re.split(r"\s+[|\-\u2013]\s+", name)[0].strip()
    toks = tokens(soup)[:500]
    for i, tk in enumerate(toks):
        d = parse_dates(tk["t"], today)
        if not d or not d[2]:
            continue
        loc = tk["t"].split("|", 1)[1].strip() if "|" in tk["t"] else ""
        k = i + 1
        parts = [loc] if loc else []
        while not city_state(" ".join(parts)) and k < min(len(toks), i + 4):
            s = toks[k]["t"]
            if s != "|" and not is_mostly_date(s, today):
                parts.append(s.strip("| "))
            k += 1
        return make_event(src["id"], src["name"], name, d[0], d[1], clean_loc(parts), url, method="page")
    if any(re.search(r"\b(dates? and venue )?TBA\b|to be announced", t["t"], re.I) for t in toks):
        return make_event(src["id"], src["name"], name, None, None, "", url, tba=True, method="page")
    return None


# ---------------------------------------------------------------- structured data (JSON-LD, iCal, WordPress events API)
def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _ld_location(loc) -> str:
    if isinstance(loc, list):
        loc = loc[0] if loc else ""
    if isinstance(loc, str):
        return loc
    if not isinstance(loc, dict):
        return ""
    if "VirtualLocation" in str(loc.get("@type", "")):
        return "Online"
    addr = loc.get("address")
    if isinstance(addr, dict):
        city, region = addr.get("addressLocality", ""), addr.get("addressRegion", "")
        addr = ", ".join(p for p in (city, region) if p)
    return clean_loc([loc.get("name", ""), addr if isinstance(addr, str) else ""])


def jsonld_events(html: str, base: str) -> list[dict]:
    soup = soup_of(html)
    out = []
    for s in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        for o in _walk(data):
            types = o.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(isinstance(t, str) and t.endswith("Event") for t in types):
                continue
            sd = str(o.get("startDate") or "")
            m = ISO_RE.search(sd)
            if not m:
                continue
            start = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            me = ISO_RE.search(str(o.get("endDate") or ""))
            end = _safe(int(me.group(1)), int(me.group(2)), int(me.group(3))) if me else start
            online = "Online" in str(o.get("eventAttendanceMode", ""))
            loc = _ld_location(o.get("location")) or ("Online" if online else "")
            tm = sd[11:16] if len(sd) >= 16 and sd[10] == "T" else ""
            out.append({"title": str(o.get("name") or ""), "start": start, "end": end or start, "location": loc,
                        "url": good_href(str(o.get("url") or ""), base) or base, "time": tm})
    return out


def parse_ics(text: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text or "")
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = {}
        for line in block.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                f[k.split(";")[0].upper()] = v.replace("\\,", ",").replace("\\n", " ").replace("\\;", ";").strip()
        ds = re.match(r"(\d{4})(\d{2})(\d{2})", f.get("DTSTART", ""))
        if not ds:
            continue
        s = _safe(int(ds.group(1)), int(ds.group(2)), int(ds.group(3)))
        de = re.match(r"(\d{4})(\d{2})(\d{2})", f.get("DTEND", ""))
        e = _safe(int(de.group(1)), int(de.group(2)), int(de.group(3))) if de else s
        if e and s and "T" not in f.get("DTEND", "T") and e > s:
            e = e - dt.timedelta(days=1)   # all-day DTEND is exclusive
        tm = re.match(r"\d{8}T(\d{2})(\d{2})", f.get("DTSTART", ""))
        out.append({"title": f.get("SUMMARY", ""), "start": s, "end": e or s, "location": f.get("LOCATION", ""),
                    "url": f.get("URL", ""), "time": f"{tm.group(1)}:{tm.group(2)}" if tm and tm.group(1) != "00" else "",
                    "categories": f.get("CATEGORIES", "")})
    return out


def tribe_events(fetcher: Fetcher, page_url: str, today: dt.date) -> list[dict]:
    """WordPress 'The Events Calendar' plugin exposes a public JSON API. Many association and company sites use it."""
    p = urlparse(page_url)
    api = f"{p.scheme}://{p.netloc}/wp-json/tribe/events/v1/events?start_date={today.isoformat()}&per_page=50"
    status, text, _ = fetcher.get(api, tries=1)
    if status != 200:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for ev in data.get("events", []):
        m = ISO_RE.search(ev.get("start_date", ""))
        me = ISO_RE.search(ev.get("end_date", ""))
        if not m:
            continue
        s = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        e = _safe(int(me.group(1)), int(me.group(2)), int(me.group(3))) if me else s
        v = ev.get("venue") if isinstance(ev.get("venue"), dict) else {}
        loc = clean_loc([v.get("venue", ""), v.get("city", ""), v.get("stateprovince") or v.get("state") or v.get("province", "")])
        if not loc and ev.get("is_virtual"):
            loc = "Online"
        out.append({"title": H.unescape(ev.get("title", "")), "start": s, "end": e or s, "location": loc,
                    "url": ev.get("url", ""), "time": (ev.get("start_date", "")[11:16])})
    return out


def detail_page_event(html: str, url: str, today: dt.date) -> dict | None:
    """A single event's own page: JSON-LD if present, otherwise the H1 plus the first upcoming date under it."""
    ld = [e for e in jsonld_events(html, url) if e["start"] and e["start"] >= today]
    if ld:
        e = ld[0]
        e["url"] = e["url"] if e["url"] and e["url"] != url else url
        return e
    soup = soup_of(html)
    h1 = soup.find("h1")
    if not h1:
        return None
    title = " ".join(h1.get_text(" ").split())
    toks = tokens(soup)
    try:
        start_i = next(i for i, t in enumerate(toks) if t["h"] is h1)
    except StopIteration:
        start_i = 0
    for i in range(start_i, min(len(toks), start_i + 150)):
        d = parse_dates(toks[i]["t"], today)
        if d and d[2] and d[0] >= today:
            loc = next((toks[k]["t"] for k in range(i + 1, min(len(toks), i + 10))
                        if city_state(toks[k]["t"]) or ONLINE.search(toks[k]["t"])), "")
            return {"title": title, "start": d[0], "end": d[1], "location": LABEL_PREFIX.sub("", loc)[:140], "url": url, "time": ""}
    return None


# ================================================================ association + Bisnow runners
PARSERS = {"title_date": parse_title_date, "date_title": parse_date_title, "date_heading": parse_date_heading,
           "naa": parse_naa, "bisnow": parse_bisnow}


def apply_source_options(evs: list[dict], src: dict) -> list[dict]:
    """Per-source settings from event_sources.json: "kind" (e.g. education) and "online": true for webinar pages."""
    for e in evs:
        if src.get("kind"):
            e["kind"] = src["kind"]
        if src.get("online") and not e["city"]:
            e["location"], e["format"] = "Online", "Online"
    return evs


def run_page_source(fetcher: Fetcher, src: dict, today: dt.date, debug_dir: Path | None) -> tuple[list[dict], list[dict]]:
    events, status_rows = [], []
    urls = src.get("urls") or [src["url"]]
    for url in urls:
        status, html, final = fetcher.get(url)
        if status != 200:
            unverified = url in src.get("unverified_urls", [])
            status_rows.append({"source": src["id"], "url": url, "events": 0, "note": "",
                                "status": "skipped (unverified URL not found)" if unverified and status == 404
                                else (f"http {status}" if status else html[:120])})
            continue
        parser = PARSERS.get(src.get("parser", ""), None)
        evs = parser(html, final, today, src) if parser else []
        if not evs and src.get("parser") == "generic":
            evs = generic_page_events(fetcher, final, html, today, src["id"], src["name"], kind="conference")
        if not evs:
            evs = [make_event(src["id"], src["name"], e["title"], e["start"], e["end"], e["location"], e["url"] or final,
                              time=e.get("time", ""), method="json-ld") for e in jsonld_events(html, final) if e["start"]]
        evs = apply_source_options(evs, src)
        if not evs and src.get("may_be_empty"):   # a calendar that is often empty: zero is normal, not a broken parser
            status_rows.append({"source": src["id"], "url": url, "status": "ok", "events": 0, "note": "nothing listed right now"})
            continue
        if not evs and debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{src['id']}-{hashlib.sha1(url.encode()).hexdigest()[:6]}.html").write_text(html)
        status_rows.append({"source": src["id"], "url": url, "status": "ok" if evs else "loaded, 0 events (page saved to debug/)",
                            "events": len(evs), "note": ""})
        events += evs
    return events, status_rows


def run_informa(fetcher: Fetcher, src: dict, today: dt.date, debug_dir: Path | None) -> tuple[list[dict], list[dict]]:
    base = "https://informaconnect.com/"
    watch = {e["slug"]: e for e in src.get("events", [])}
    rows, events = [], []
    # look for IMN event sites we don't know about yet (kept only if the name matches our keywords)
    discovered = set()
    if src.get("index_url"):
        status, html, _ = fetcher.get(src["index_url"])
        if status == 200:
            discovered = set(re.findall(r"informaconnect\.com/(imn-[a-z0-9-]+)", html)) - set(watch)
        rows.append({"source": src["id"], "url": src["index_url"], "status": "ok" if status == 200 else f"http {status}",
                     "events": 0, "note": f"{len(discovered)} other IMN event sites linked" if status == 200 else ""})
    kw = re.compile("|".join(src.get("discover_keywords", ["$^"])), re.I)
    for slug in list(watch) + sorted(discovered):
        url = f"{base}{slug}/"
        status, html, final = fetcher.get(url)
        meta = watch.get(slug, {})
        if status != 200:
            rows.append({"source": src["id"], "url": url, "events": 0, "note": "",
                         "status": "skipped (unverified slug not found)" if meta.get("unverified") and status == 404
                         else (f"http {status}" if status else html[:120])})
            continue
        ev = parse_informa_page(html, url, today, src, name=meta.get("name", ""))
        if slug in discovered and (ev is None or not kw.search(ev["title"])):
            continue   # an IMN event outside our space (e.g. bank special assets); ignore quietly
        if ev is None and debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"imn-{slug}.html").write_text(html)
        rows.append({"source": src["id"], "url": url, "status": "ok" if ev else "loaded, no date found (page saved to debug/)",
                     "events": 1 if ev else 0, "note": "dates TBA" if ev and ev["tba"] else ("new IMN event" if slug in discovered else "")})
        if ev:
            events.append(ev)
    return events, rows


# ================================================================ NARPM chapters
# ~55 local and state chapters, each with its own site. Most are NARPM-hosted microsites (chapters.narpm.org/<slug>/
# or <name>.narpm.org) running the WordPress "Events Manager" plugin: its listing loads by script and its pages write
# dates as DD/MM/YYYY, so we read the plugin's iCal feed (<site>/events.ics), which has unambiguous dates.
# The rest (Wild Apricot, Meetup, own sites) go through the generic reader.
CHAPTER_CODE = re.compile(r"\b([CS]\d{3})\s*[-–]\s*([^<>\n\"|]{2,80})")
SOCIAL = re.compile(r"facebook\.|twitter\.|x\.com|instagram\.|linkedin\.|youtube\.|google\.com/maps|maps\.google|tiktok\.", re.I)
CONF_TITLE = re.compile(r"\b(conference|convention|trade ?show|expo|summit|symposium)\b", re.I)


def chapter_root(site: str) -> str:
    """https://chapters.narpm.org/atlanta/about/ -> https://chapters.narpm.org/atlanta/ ; other hosts -> scheme://host/"""
    if not site.startswith("http"):
        site = "https://" + site
    p = urlparse(site)
    if p.netloc.lower() == "chapters.narpm.org":
        slug = next((x for x in p.path.split("/") if x), "")
        return f"https://chapters.narpm.org/{slug}/" if slug else ""
    return f"{p.scheme}://{p.netloc}/"


def site_key(site: str) -> str:
    """One key per chapter site: the microsite root, or the group for Meetup (many chapters share meetup.com)."""
    m = re.search(r"meetup\.com/([^/?#]+)", site or "", re.I)
    return f"meetup:{m.group(1).lower()}" if m else chapter_root(site)


def chapter_website(html: str, base: str) -> str:
    """The chapter's own site from its narpm.org chapter-info page (shown after a "Website:" label)."""
    soup = soup_of(html)
    for lab in soup.find_all(string=re.compile(r"^\s*website\s*:?\s*$", re.I)):
        a = lab.find_next("a", href=True)
        if a and a["href"].startswith("http") and not SOCIAL.search(a["href"]):
            return a["href"].strip()
    links = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)]
    hosted = [h for h in links if re.match(r"https?://(chapters\.narpm\.org/[a-z0-9-]+|(?!www\.)[a-z0-9-]+\.narpm\.org)", h, re.I)]
    if hosted:
        return hosted[0]
    m = re.search(r"website\s*:?\s*</?[^>]*>\s*(?:<a[^>]+href=\")?(https?://[^\"<\s]+)", html, re.I)
    return m.group(1) if m and not SOCIAL.search(m.group(1)) else ""


def discover_chapters(fetcher: Fetcher, src: dict, cmap: dict, today: dt.date, budget: int) -> tuple[int, int]:
    """Read every chapter code from narpm.org's chapter finder, then fill in / refresh each chapter's website.
    Returns (codes found on the finder, chapter pages looked up)."""
    status, html, _ = fetcher.get(src["finder_url"])
    codes = {}
    if status == 200:
        for code, name in CHAPTER_CODE.findall(html):
            codes.setdefault(code, " ".join(H.unescape(name).split()).strip(" -"))
        # any chapter microsite linked straight from the finder (sometimes in a data attribute or script)
        for h in set(re.findall(r"https?://(?:chapters\.narpm\.org/[a-z0-9-]+|(?!www\.)[a-z0-9-]+\.narpm\.org)", html, re.I)):
            root = chapter_root(h)
            if root and not any(chapter_root(c.get("site", "")) == root for c in cmap.values()):
                cmap[f"site:{root}"] = {"name": "", "site": root, "method": "finder link", "checked": today.isoformat()}
    stale = (today - dt.timedelta(days=src.get("refresh_days", 28))).isoformat()
    todo = [c for c in codes if c not in cmap or (not cmap[c].get("manual") and cmap[c].get("checked", "") < stale)]
    looked = 0
    for code in sorted(todo)[:budget]:
        url = src["info_url"].format(code=code)
        st, page, fin = fetcher.get(url, tries=1)
        looked += 1
        prev = cmap.get(code, {})
        entry = {**prev, "name": codes[code] or prev.get("name", ""), "checked": today.isoformat()}
        if st == 200:
            site = chapter_website(page, fin)
            if site:
                entry.update(site=site, method="chapter finder")
                entry.pop("note", None)
            elif not prev.get("site"):
                entry["note"] = "no website listed on narpm.org"
        elif not prev.get("site"):
            entry["note"] = f"chapter page http {st}" if st else "chapter page unreachable"
        cmap[code] = entry
    for code, name in codes.items():   # keep names current for entries that came from the seed list
        if code in cmap and not cmap[code].get("name"):
            cmap[code]["name"] = name
    return len(codes), looked


def narpm_hosted(site: str) -> bool:
    """chapters.narpm.org / texas.narpm.org, but not a chapter's own domain like trianglenarpm.org."""
    host = urlparse(chapter_root(site)).netloc.lower()
    return host == "narpm.org" or host.endswith(".narpm.org")


def chapter_feed_urls(site: str) -> list[str]:
    root = chapter_root(site)
    if narpm_hosted(site):
        return [root + "events.ics"]
    m = re.search(r"meetup\.com/([^/?#]+)", site, re.I)
    if m:
        return [f"https://www.meetup.com/{m.group(1)}/events/ical/"]
    return []


def chapter_listing_url(site: str) -> str:
    """Where to look when there is no feed: an explicit events page if the map has one, else <root>/events."""
    if re.search(r"/(events?|calendar)(-\d+)?/?$", site, re.I) or "meetup.com" in site.lower():
        return site
    return chapter_root(site) + "events"


def read_chapter(fetcher: Fetcher, key: str, entry: dict, src: dict, today: dt.date) -> tuple[list[dict], dict]:
    site = entry.get("events_url") or entry.get("site", "")
    name = entry.get("name") or key
    short = re.sub(r"(?i)\s*-\s*chapter of narpm.*$", "", name)
    org = short if short.lower().startswith("narpm") else f"NARPM {short}"
    raw, method, reached = [], "", False
    for feed in chapter_feed_urls(entry.get("site", site)):
        st, text, _ = fetcher.get(feed, tries=1)
        if st == 200 and "BEGIN:VCALENDAR" in text:
            raw, method, reached = parse_ics(text), "ical", True
            break
    found_url = ""
    if not reached:
        listing = chapter_listing_url(site)
        st, html, fin = fetcher.get(listing, tries=1)
        if st == 404 and not entry.get("events_url") and not narpm_hosted(site):
            # own domain with no /events page: find the events link from its homepage (saved for next time)
            det = detect_events_page(fetcher, {"website": chapter_root(site)}, today)
            if det.get("url"):
                found_url = det["url"]
                st, html, fin = fetcher.get(found_url, tries=1)
        if st == 200:
            reached = True
            got = generic_page_events(fetcher, fin, html, today, src["id"], org, kind="chapter", max_detail=4)
            raw = [{"title": e["title"], "start": dt.date.fromisoformat(e["start"]), "end": dt.date.fromisoformat(e["end"]),
                    "location": e["location"], "url": e["url"], "time": e["time"], "categories": ""} for e in got if e["start"]]
            method = "page"
        else:
            return [], {"source": src["id"], "url": listing, "events": 0, "note": name,
                        "status": f"http {st}" if st else (html[:100] or "unreachable")}
    out, series = [], set()
    for r in sorted((r for r in raw if r.get("start") and r.get("title")), key=lambda r: r["start"]):
        if re.search(r"\bnational\b", r.get("categories", ""), re.I):
            continue   # NARPM national events cross-posted on every chapter calendar
        if (r["end"] or r["start"]) >= today:
            # a weekly series ("52 Weeks of Property Management Mastery") shows once: the next session
            k = norm_title(r["title"])
            if k in series:
                continue
            series.add(k)
        kind = "conference" if CONF_TITLE.search(r["title"]) else "chapter"
        out.append(make_event(src["id"], org, r["title"], r["start"], r["end"], r["location"],
                              r["url"] or chapter_root(site), kind=kind, time=r.get("time", ""), method=method))
    upcoming = [e for e in out if e["end"] >= today.isoformat()]
    return out, {"source": src["id"], "url": entry.get("site", site), "status": "ok", "events": len(upcoming), "note": name,
                 "found_url": found_url}


def run_narpm_chapters(fetcher: Fetcher, src: dict, today: dt.date, debug_dir: Path | None,
                       save: bool = True) -> tuple[list[dict], list[dict]]:
    cmap = load_json(CHAPTERS_MAP, {})
    for s in src.get("seed", []):   # known chapter sites, so the first run works even if the finder changes
        key = s.get("code") or f"site:{site_key(s['site'])}"
        if key not in cmap or not cmap[key].get("site"):
            cmap[key] = {**cmap.get(key, {}), "name": s.get("name", ""), "site": s["site"], "method": "seed",
                         **({"events_url": s["events_url"]} if s.get("events_url") else {})}
    found, looked = discover_chapters(fetcher, src, cmap, today, src.get("max_lookups_per_run", 80))
    # one entry per site (a code and a seed can point at the same microsite)
    by_root, todo = {}, []
    for key, entry in sorted(cmap.items()):
        root = site_key(entry.get("site", "")) if entry.get("site") else ""
        if not root or entry.get("disabled") or root in by_root:
            continue
        by_root[root] = key
        todo.append((key, entry))
    events, fail_rows, read_ok = [], [], 0
    with ThreadPoolExecutor(max_workers=src.get("workers", 6)) as pool:
        futs = {pool.submit(read_chapter, fetcher, k, e, src, today): (k, e) for k, e in todo}
        for f in as_completed(futs):
            key, entry = futs[f]
            try:
                evs, row = f.result()
            except Exception as err:   # one broken chapter never stops the rest
                evs, row = [], {"source": src["id"], "url": entry.get("site", ""), "status": f"error: {err}"[:160],
                                "events": 0, "note": entry.get("name", key)}
            new_events_url = row.pop("found_url", "")
            if new_events_url:
                cmap[key]["events_url"] = new_events_url
            if row["status"] == "ok":
                read_ok += 1
                cmap[key]["last_ok"] = today.isoformat()
                events += evs
            elif cmap[key].get("last_ok"):
                fail_rows.append(row)   # worked before, broken now: worth a look
            else:
                row["status"] = f"skipped (never reached: {row['status']})"
                fail_rows.append(row)
    if save:
        save_json(CHAPTERS_MAP, cmap)
    # stable order whatever finished first; when two chapters list one event, the host chapter (named in the title) wins
    def _host_first(e):
        city = e["organizer"].replace("NARPM", "").replace("State", "").strip().lower()
        return (e["start"], norm_title(e["title"]), 0 if city and city in e["title"].lower() else 1, e["organizer"])
    events.sort(key=_host_first)
    broken = [r for r in fail_rows if not r["status"].startswith("skipped")]
    with_events = len({e["organizer"] for e in events if e["end"] >= today.isoformat()})
    summary = {"source": src["id"], "url": src["finder_url"], "events": len([e for e in events if e["end"] >= today.isoformat()]),
               "status": "ok" if not broken else f"{len(broken)} chapter sites that used to work failed",
               "note": f"{len(todo)} chapter sites, {read_ok} read, {with_events} with upcoming events; "
                       f"{found} chapters on the finder, {looked} looked up this run"}
    return events, [summary] + sorted(fail_rows, key=lambda r: r["note"])


# ================================================================ company events
EVENTISH = re.compile(r"\b(events?|webinars?|conferences?|summits?|meetups?|user conference|roadshow)\b", re.I)
CANDIDATE_PATHS = ["/events", "/webinars", "/company/events", "/resources/events", "/resources/webinars",
                   "/about/events", "/events-and-webinars", "/events-webinars", "/news-events", "/community/events"]


def load_companies() -> list[dict]:
    with open(LEADS, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("company")]


def detect_events_page(fetcher: Fetcher, company: dict, today: dt.date) -> dict:
    """Find a company's events/webinars page: first from links on its homepage, then common paths."""
    site = (company.get("website") or "").strip()
    if not site:
        return {"url": "", "method": "none", "checked": today.isoformat(), "note": "no website in master list"}
    if not site.startswith("http"):
        site = "https://" + site
    root = urlparse(site)
    host = root.netloc.replace("www.", "")
    status, html, final = fetcher.get(site, timeout=15, tries=1)
    cands = []
    if status == 200:
        for a in soup_of(html).find_all("a", href=True):
            href = urljoin(final, a["href"].split("#")[0])
            p = urlparse(href)
            if host not in p.netloc.replace("www.", "") or not p.path or p.path == "/":
                continue
            text = " ".join(a.get_text(" ").split())
            last = p.path.rstrip("/").split("/")[-1]
            if EVENTISH.fullmatch(last.replace("-", " ")) or (EVENTISH.search(text) and len(text) < 30):
                depth = len([x for x in p.path.split("/") if x])
                score = (0 if EVENTISH.fullmatch(last.replace("-", " ")) else 1, depth, len(href))
                cands.append((score, href.rstrip("/")))
    for _, href in sorted(set(cands))[:3]:
        st, page, fin = fetcher.get(href, timeout=15, tries=1)
        if st == 200 and looks_like_events_page(page, fin, today):
            return {"url": fin, "method": "homepage link", "checked": today.isoformat()}
    for path in CANDIDATE_PATHS:
        url = f"{root.scheme}://{root.netloc}{path}"
        st, page, fin = fetcher.get(url, timeout=12, tries=1)
        if st == 200 and urlparse(fin).path.rstrip("/") not in ("", "/") and looks_like_events_page(page, fin, today):
            return {"url": fin, "method": f"probe {path}", "checked": today.isoformat()}
    return {"url": "", "method": "none", "checked": today.isoformat(),
            "note": "homepage blocked or unreachable" if status != 200 else "no events page found"}


def looks_like_events_page(html: str, url: str, today: dt.date) -> bool:
    if jsonld_events(html, url):
        return True
    low = html.lower()
    if "tribe-events" in low or ".ics" in low or "eventbrite.com/e/" in low or "lu.ma/" in low:
        return True
    toks = tokens(soup_of(html))
    upcoming = sum(1 for t in toks if len(t["t"]) < 80 and (d := parse_dates(t["t"], today)) and d[2] and d[0] >= today)
    return upcoming >= 1 and bool(EVENTISH.search(" ".join(t["t"] for t in toks[:400])))


def generic_page_events(fetcher: Fetcher, url: str, html: str, today: dt.date, source: str, organizer: str,
                        kind: str = "company", company: str = "", max_detail: int = 8) -> list[dict]:
    """Events listed on an arbitrary page, trying the most reliable format first."""
    def wrap(items, method):
        return [make_event(source, organizer, e["title"], e["start"], e["end"], e["location"], e["url"] or url,
                           kind=kind, company=company, time=e.get("time", ""), method=method)
                for e in items if e.get("start") and e.get("title")]

    ev = wrap(jsonld_events(html, url), "json-ld")
    if ev:
        return ev
    low = html.lower()
    if "tribe-events" in low or "/wp-json/tribe/" in low:
        ev = wrap(tribe_events(fetcher, url, today), "events api")
        if ev:
            return ev
    soup = soup_of(html)
    ics = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
           if a["href"].lower().split("?")[0].endswith(".ics") or "ical=1" in a["href"].lower() or a["href"].startswith("webcal:")]
    for link in ics[:2]:
        st, text, _ = fetcher.get(link.replace("webcal:", "https:"), tries=1)
        if st == 200 and "BEGIN:VEVENT" in text:
            ev = wrap(parse_ics(text), "ical")
            if ev:
                return ev
    # event platforms that publish structured data on each event page
    ext = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if re.search(r"eventbrite\.[a-z.]+/e/|lu\.ma/[a-z0-9-]+$|luma\.com/[a-z0-9-]+$|goldcast\.io/events/|on24\.com/", h, re.I):
            ext.append(h.split("?")[0])
    got = []
    for link in list(dict.fromkeys(ext))[:max_detail]:
        st, page, fin = fetcher.get(link, tries=1)
        if st == 200:
            e = detail_page_event(page, fin, today)
            if e:
                got.append(e)
    if got:
        return wrap(got, "event platform")
    # plain text on the listing page
    src = {"id": source, "name": organizer}
    txt = parse_title_date(html, url, today, src) or parse_date_title(html, url, today, src)
    txt = [e for e in txt if e["start"] and e["start"] >= today.isoformat()]
    if txt:
        for e in txt:
            e.update(kind=kind, company=company, method="page text")
        return txt
    # listing with no dates: open the individual event pages on the same site
    p = urlparse(url)
    detail = []
    for a in soup.find_all("a", href=True):
        h = urljoin(url, a["href"].split("#")[0].split("?")[0])
        q = urlparse(h)
        if q.netloc == p.netloc and q.path.rstrip("/") != p.path.rstrip("/") and \
                re.search(r"/(events?|webinars?)/[^/]+", q.path) and len(q.path) > len(p.path.rstrip("/")) + 2:
            detail.append(h)
    got = []
    for link in list(dict.fromkeys(detail))[:max_detail]:
        st, page, fin = fetcher.get(link, tries=1)
        if st == 200:
            e = detail_page_event(page, fin, today)
            if e:
                got.append(e)
    return wrap(got, "event pages")


def classify_company_event(e: dict, conferences: list[dict], cfg: dict) -> str:
    """'hosted' unless the listing is really the company attending someone else's conference."""
    t = e["title"].lower()
    if any(m in t for m in cfg.get("hosted_markers", [])):
        return "hosted"
    if any(m in t for m in cfg.get("attending_markers", [])):
        return "attending"
    for c in conferences:
        if c["start"] and c["start"] <= e["end"] and e["start"] <= c["end"] and title_sim(e["title"], c["title"]) >= 0.5:
            return "attending"
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in cfg.get("known_conferences", [])):
        return "attending"
    return "hosted"


def run_companies(fetcher: Fetcher, cfg: dict, today: dt.date, detect: bool, minutes: float, save: bool,
                  conferences: list[dict]) -> tuple[list[dict], list[dict]]:
    ccfg = cfg.get("companies", {})
    companies = load_companies()
    emap = load_json(EVENTS_MAP, {})
    deadline = time.time() + minutes * 60
    rows = []
    if detect:
        stale = today - dt.timedelta(days=ccfg.get("recheck_none_after_days", 28))
        todo = [c for c in companies if c["company"] not in emap or (
            emap[c["company"]].get("method") == "none" and dt.date.fromisoformat(emap[c["company"]].get("checked", "2000-01-01")) < stale)]
        todo = [c for c in todo if emap.get(c["company"], {}).get("method") != "manual"]
        print(f"  detect: looking for events pages at {len(todo)} companies", flush=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(detect_events_page, fetcher, c, today): c for c in todo}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    emap[c["company"]] = f.result()
                except Exception as err:   # one bad site never stops the run
                    emap[c["company"]] = {"url": "", "method": "none", "checked": today.isoformat(), "note": f"error: {err}"[:120]}
                if time.time() > deadline:
                    print("  detect: time budget reached; the rest will be checked next run", flush=True)
                    for g in futs:
                        g.cancel()
                    break
        if save:
            save_json(EVENTS_MAP, emap)
            with open(NEEDS_MAP, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["company", "website", "why", "how to fix"])
                for c in companies:
                    m = emap.get(c["company"], {})
                    if not m.get("url") and m.get("method") != "manual":
                        w.writerow([c["company"], c.get("website", ""), m.get("note", "not checked yet"),
                                    'if they have an events page, add it to data/events_map.json as {"url": "...", "method": "manual"}'])

    mapped = [(c, emap[c["company"]]) for c in companies if emap.get(c["company"], {}).get("url")]
    print(f"  companies: reading {len(mapped)} events pages", flush=True)

    def one(c, m):
        st, html, fin = fetcher.get(m["url"])
        if st != 200:
            return [], {"source": "company", "url": m["url"], "status": f"http {st}" if st else html[:120], "events": 0, "note": c["company"]}
        evs = generic_page_events(fetcher, fin, html, today, "company", c["company"], kind="company", company=c["company"],
                                  max_detail=ccfg.get("max_detail_pages", 8))
        return evs, {"source": "company", "url": m["url"], "status": "ok", "events": len(evs), "note": c["company"]}

    events = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one, c, m) for c, m in mapped]
        for f in as_completed(futs):
            try:
                evs, row = f.result()
            except Exception as err:
                evs, row = [], {"source": "company", "url": "", "status": f"error: {err}"[:120], "events": 0, "note": ""}
            events += evs
            rows.append(row)
    for e in events:
        e["kind"] = "company_" + classify_company_event(e, conferences, ccfg)
    return events, rows


# ================================================================ combine, history, outputs
def keep(e: dict, today: dt.date, horizon: dt.date, cfg: dict) -> bool:
    if e["tba"]:
        return True
    if not e["start"] or e["end"] < today.isoformat() or e["start"] > horizon.isoformat():
        return False
    pats = cfg.get("exclude_title_patterns", [])
    return not any(re.search(p, e["title"], re.I) for p in pats)


def dedupe(events: list[dict], priority: dict) -> list[dict]:
    """Same event listed by two sources (e.g. an NAA event on a state association's calendar): keep the better source."""
    events = sorted(events, key=lambda e: (priority.get(e["source"], 9), e["start"] or "9999"))
    out = []
    for e in events:
        dup = next((o for o in out if o["start"] and o["start"] == e["start"] and title_sim(o["title"], e["title"]) >= 0.6
                    and o["kind"] == e["kind"]
                    # "NARPM Atlanta November Chapter Meeting" and "NARPM Houston November Chapter Meeting" on the
                    # same day are two events: different cities never match
                    and not (o["city"] and e["city"] and o["city"].lower() != e["city"].lower())), None)
        if dup is None:
            out.append(e)
    return out


def drop_crossposts(events: list[dict], cfg: dict) -> list[dict]:
    """A chapter calendar re-listing an event we already have from a national source (any kind), e.g. a NARPM
    webinar or the Broker/Owner conference posted on a chapter site. The national listing wins."""
    chapter_srcs = {s["id"] for s in cfg.get("sources", []) if s.get("parser") == "narpm_chapters"}
    national = [e for e in events if e["source"] not in chapter_srcs and e["start"]]
    return [e for e in events if e["source"] not in chapter_srcs or not any(
        n["start"] == e["start"] and title_sim(n["title"], e["title"]) >= 0.6 for n in national)]


def update_history(events: list[dict], ok_sources: set[str], today: dt.date) -> tuple[dict, list[dict], list[dict]]:
    hist = load_json(HISTORY, {})
    new, moved = [], []
    seen = {event_id(e) for e in events if not e["tba"]}
    for e in events:
        if e["tba"]:
            continue
        eid = event_id(e)
        if eid in hist:
            hist[eid].update({**e, "last_seen": today.isoformat(), "status": "upcoming"})
            e["first_seen"] = hist[eid]["first_seen"]
        else:
            e["first_seen"] = today.isoformat()
            hist[eid] = {**e, "first_seen": today.isoformat(), "last_seen": today.isoformat(), "status": "upcoming"}
            old = next((h for k, h in hist.items() if k not in seen and h["status"] == "upcoming" and h["source"] == e["source"]
                        and h.get("company", "") == e.get("company", "") and norm_title(h["title"]) == norm_title(e["title"])), None)
            if old:
                old["status"] = "rescheduled"
                e["note"] = f"date changed from {old['start']}"
                moved.append(e)
            else:
                new.append(e)
        e["id"] = eid
    for k, h in hist.items():
        if h["status"] != "upcoming" or k in seen:
            continue
        if h["end"] < today.isoformat():
            h["status"] = "past"
        elif h["source"] in ok_sources or (h["source"] == "company" and h.get("company") in ok_sources):
            h["status"] = "removed"   # the page loaded fine this week and the event is gone: cancelled or taken down
    return hist, new, moved


FIELDS = ["start", "end", "title", "organizer", "company", "kind", "location", "city", "state", "format", "access", "time",
          "url", "source", "topic", "first_seen", "method", "note"]


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


SHEET_FIELDS = ["Start", "End", "Event", "Type", "Host", "Where", "City", "State", "Format", "Access", "Time", "Link",
                "New this week", "First seen", "Note", "Updated"]


def is_recent(e: dict, today: dt.date, baseline: str) -> bool:
    """First seen in the last 7 days, and not part of the very first run (when everything is 'new')."""
    fs = e.get("first_seen", "")
    return bool(fs) and fs > baseline and fs > (today - dt.timedelta(days=7)).isoformat()


def write_sheet_csv(path: Path, events: list[dict], today: dt.date, baseline: str):
    """One tidy table for the Google Sheet: conferences and company-hosted events, soonest first, TBA at the bottom.
    Company listings for other people's conferences are left out (they're in the weekly report)."""
    label = {"conference": "Conference", "company_hosted": "Company event", "chapter": "NARPM chapter event",
             "education": "Course / webinar"}
    rows = []
    for e in sorted(events, key=lambda e: (e["tba"], e["start"] or "9999", e["title"])):
        if e["kind"] not in label:
            continue
        rows.append({"Start": e["start"] or "TBA", "End": e["end"], "Event": e["title"], "Type": label[e["kind"]],
                     "Host": e["company"] or e["organizer"], "Where": e["location"], "City": e["city"], "State": e["state"],
                     "Format": e["format"], "Access": e["access"], "Time": e["time"], "Link": e["url"],
                     "New this week": "yes" if is_recent(e, today, baseline) else "",
                     "First seen": e.get("first_seen", ""), "Note": e.get("note", ""), "Updated": today.isoformat()})
    write_csv(path, rows, SHEET_FIELDS)


def _line(e: dict, with_year: bool = False, show_org: bool = True) -> str:
    s, en = dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"])
    org = e["company"] or e["organizer"]
    who = f" ({org})" if show_org and org and org.lower().split()[0] not in e["title"].lower() else ""
    where = e["location"]
    if e["city"] and e["state"] and len(where) > 45:
        where = f"{e['city']}, {e['state']}"
    extra = " _(members only)_" if e["access"] == "members only" else (" _(invitation only)_" if e["access"] == "invitation only" else "")
    return f"- **{fmt_range(s, en, with_year)}**: [{e['title']}]({e['url']}){who}" + (f". {where}" if where else "") + extra


def write_newsletter(events: list[dict], today: dt.date, cfg: dict, out_root: Path, baseline: str,
                     priority: dict | None = None) -> Path:
    ncfg = cfg.get("newsletter", {})
    until = (today + dt.timedelta(weeks=ncfg.get("weeks", 8))).isoformat()
    caps = ncfg.get("max_per_source", {})
    window = [e for e in events if not e["tba"] and e["start"] <= until and e["kind"] == "conference"]
    counts, shown = {}, []
    for e in sorted(window, key=lambda e: (e["start"], e["title"])):
        counts[e["source"]] = counts.get(e["source"], 0) + 1
        if counts[e["source"]] <= caps.get(e["source"], 99):
            shown.append(e)
    hosted = sorted([e for e in events if e["kind"] == "company_hosted" and not e["tba"] and e["start"] <= until],
                    key=lambda e: (e["start"], e["company"]))
    later = sorted([e for e in events if e["kind"] == "conference" and not e["tba"] and e["start"] > until
                    and is_recent(e, today, baseline)], key=lambda e: ((priority or {}).get(e["source"], 5), e["start"]))
    md = ["## Upcoming events", "",
          f"_Conferences and meetups across proptech and scattered site rental operations over the next "
          f"{ncfg.get('weeks', 8)} weeks._", ""]
    if shown:
        month = None
        for e in shown:
            m = dt.date.fromisoformat(e["start"]).strftime("%B")
            if m != month:
                md += ([""] if month else []) + [f"**{m}**", ""]
                month = m
            md.append(_line(e))
        md.append("")
    if hosted:
        md += ["**Hosted by companies we track**", ""] + [_line(e) for e in hosted[:ncfg.get("max_company_events", 10)]] + [""]
    # local NARPM chapter meetings: soonest first, one per chapter so a single busy chapter can't fill the block
    chapters, seen_org = [], set()
    for e in sorted([e for e in events if e["kind"] == "chapter" and not e["tba"] and e["start"] <= until],
                    key=lambda e: (e["start"], e["organizer"])):
        if e["organizer"] not in seen_org:
            seen_org.add(e["organizer"])
            chapters.append(e)
    if chapters and ncfg.get("max_chapter_events", 6):
        md += ["**NARPM chapter meetings**", ""] + [_line(e) for e in chapters[:ncfg.get("max_chapter_events", 6)]] + [""]
    if later:
        md += ["**Just announced**", ""] + [_line(e, with_year=True) for e in later[:ncfg.get("max_just_announced", 5)]] + [""]
    if not (shown or hosted or later or chapters):
        md.append("_No events in the next few weeks from the sources we track._")
    nl = out_root / "newsletter"
    nl.mkdir(parents=True, exist_ok=True)
    path = nl / f"events-{today.isoformat()}.md"
    path.write_text("\n".join(md).strip() + "\n")
    path.with_suffix(".html").write_text(draft_html("\n".join(md), f"Upcoming events - {today.isoformat()}"))
    return path


def write_report(out_dir: Path, events: list[dict], new: list[dict], moved: list[dict], removed: list[dict],
                 rows: list[dict], today: dt.date) -> str:
    conf = [e for e in events if e["kind"] == "conference" and not e["tba"]]
    hosted = [e for e in events if e["kind"] == "company_hosted"]
    attending = [e for e in events if e["kind"] == "company_attending"]
    chapter = [e for e in events if e["kind"] == "chapter" and not e["tba"]]
    education = [e for e in events if e["kind"] == "education" and not e["tba"]]
    tba = [e for e in events if e["tba"]]
    md = [f"# Events report, {today.isoformat()}", "",
          f"{len(conf)} conferences, {len(hosted)} company-hosted events, {len(chapter)} NARPM chapter events and "
          f"{len(education)} courses/webinars in the next 6 months. "
          f"{len(new)} new this week, {len(moved)} rescheduled, {len(removed)} taken down.", ""]
    month = None
    md += ["## Conferences", ""]
    for e in sorted(conf, key=lambda e: e["start"]):
        m = dt.date.fromisoformat(e["start"]).strftime("%B %Y")
        if m != month:
            md += ["", f"**{m}**", ""]
            month = m
        md.append(_line(e) + (" **NEW**" if e in new else "") + (f" _({e.get('note')})_" if e.get("note") else ""))
    md += ["", "## Hosted by master-list companies", ""]
    md += [_line(e) + (" **NEW**" if e in new else "") for e in sorted(hosted, key=lambda e: e["start"])] or ["_None found._"]
    md += ["", "## NARPM chapter events", ""]
    md += [_line(e) + (" **NEW**" if e in new else "") for e in sorted(chapter, key=lambda e: (e["start"], e["organizer"]))] \
        or ["_None found._"]
    if education:
        md += ["", "## Courses and webinars", "", "_Online education; in the sheet, not in the newsletter._", ""]
        md += [_line(e) + (" **NEW**" if e in new else "") for e in sorted(education, key=lambda e: e["start"])]
    if tba:
        md += ["", "## Dates not announced yet", ""] + [f"- [{e['title']}]({e['url']}) ({e['organizer']})" for e in tba]
    if removed:
        md += ["", "## Taken down since last week", "", "_The page loaded fine but the event is gone: likely cancelled or moved._", ""]
        md += [f"- {e['start']}: {e['title']} ({e.get('company') or e['organizer']})" for e in removed]
    if attending:
        md += ["", "## Left out: companies attending other conferences", "",
               "_Listed on a company's events page but it's someone else's conference, so it's not in the newsletter._", ""]
        md += [_line(e) for e in sorted(attending, key=lambda e: e["start"])]
    md += ["", "## Source status", "", "| source | status | events | note | url |", "|---|---|---|---|---|"]
    md += [f"| {r['source']} | {r['status']} | {r['events']} | {r.get('note', '')} | {r['url']} |" for r in rows]
    text = "\n".join(md) + "\n"
    (out_dir / "events_report.md").write_text(text)
    (out_dir / "events_report.html").write_text(draft_html(text.replace("| ", "").replace(" |", ""), f"Events - {today.isoformat()}"))
    return text


# ================================================================ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="associations, bisnow, companies (comma-separated); blank = all")
    ap.add_argument("--detect", action="store_true", help="look for events pages for companies not mapped yet")
    ap.add_argument("--fixtures", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--minutes", type=float, default=15, help="time budget for company detection")
    ap.add_argument("--dry-run", action="store_true", help="write nothing to data/; outputs go to a temp folder")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    cfg = load_json(SOURCES, {})
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()} or {"associations", "bisnow", "companies"}
    fetcher = Fetcher(Path(args.fixtures) if args.fixtures else None)
    out_root = Path(tempfile.mkdtemp(prefix="events-")) if args.dry_run else OUT
    out_dir = out_root / today.isoformat()
    debug_dir = out_dir / "debug"
    horizon = today + dt.timedelta(days=cfg.get("horizon_days", 183))
    save = not args.dry_run

    events, rows = [], []
    for src in cfg.get("sources", []) + cfg.get("apartment_associations", []):
        if src.get("group", "associations") not in only or src.get("disabled"):
            continue
        try:
            if src.get("parser") == "narpm_chapters":
                evs, r = run_narpm_chapters(fetcher, src, today, debug_dir, save=save)
            else:
                runner = run_informa if src.get("parser") == "informa" else run_page_source
                evs, r = runner(fetcher, src, today, debug_dir)
        except Exception as err:   # one broken source never stops the run
            evs, r = [], [{"source": src["id"], "url": src.get("url", ""), "status": f"error: {err}"[:160], "events": 0, "note": ""}]
        print(f"  {src['id']}: {len(evs)} events", flush=True)
        events += evs
        rows += r

    conferences = [e for e in events if e["start"]]
    if "companies" in only:
        cevs, crows = run_companies(fetcher, cfg, today, args.detect, args.minutes, save, conferences)
        events += cevs
        rows += crows

    priority = {s["id"]: s.get("priority", 5) for s in cfg.get("sources", []) + cfg.get("apartment_associations", [])}
    events = drop_crossposts([e for e in events if keep(e, today, horizon, cfg)], cfg)
    events = dedupe(events, priority)
    healthy = lambda st: st == "ok" or st.startswith("skipped")
    by_src = {}
    for r in rows:
        if r["source"] != "company":
            by_src.setdefault(r["source"], []).append(healthy(r["status"]))
    ok_sources = {k for k, v in by_src.items() if v and all(v)} | \
                 {r["note"] for r in rows if r["source"] == "company" and r["status"] == "ok"}
    hist, new, moved = update_history(events, ok_sources, today)
    baseline = min((h["first_seen"] for h in hist.values()), default=today.isoformat())   # date of the very first run
    removed = [h for h in hist.values() if h["status"] == "removed" and h.get("last_seen", "") >= (today - dt.timedelta(days=8)).isoformat()]

    out_dir.mkdir(parents=True, exist_ok=True)
    events.sort(key=lambda e: (e["tba"], e["start"] or "9999", e["title"]))
    write_csv(out_dir / "events.csv", events, FIELDS)
    write_csv(out_dir / "events_sources.csv", rows, ["source", "status", "events", "note", "url"])
    report = write_report(out_dir, events, new, moved, removed, rows, today)
    nl = write_newsletter(events, today, cfg, out_root, baseline, priority=priority)
    if only >= {"associations", "bisnow", "companies"}:
        write_sheet_csv(out_root / "events-latest.csv", events, today, baseline)
    if save:
        save_json(HISTORY, hist)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"# Upcoming events draft\n\nPaste-ready file: `{nl.relative_to(out_root.parent) if not args.dry_run else nl}`\n\n"
                    f"---\n\n{nl.read_text()}\n\n---\n\n{report}")
    broken = [r for r in rows if not healthy(r["status"]) and r["source"] != "company"]
    print(f"events: {len(events)} kept | new {len(new)} | rescheduled {len(moved)} | removed {len(removed)} | "
          f"sources needing a look: {len(broken)} -> {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
