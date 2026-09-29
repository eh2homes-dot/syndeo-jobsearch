"""SEC EDGAR — Form 8-K, Item 5.02.

The most authoritative free source there is. Item 5.02 is "Departure of
Directors or Certain Officers; Election of Directors; Appointment of Certain
Officers", which is exactly a people move, filed by the company itself.

Limits worth knowing:
  - only public companies (about 25 on the master list)
  - only named executive officers and directors, so a new VP of Sales will
    not appear here
  - no API key, no account; SEC only requires a descriptive User-Agent, which
    lib/http.py sets

Everything from this source is CONFIRMED.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from lib.http import get
from lib.move import Move, CONFIRMED, ARRIVAL, DEPARTURE, UNKNOWN
from lib.people import extract_from_text, priority_score
from lib.state import SeenStore, item_id

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

_ITEM_START = re.compile(r"item\s*5\.02", re.I)
_ITEM_END = re.compile(r"item\s*\d\.\d{2}|^\s*signatures?\s*$", re.I | re.M)

_DEPARTURE_WORDS = re.compile(
    r"\b(resign|resignation|depart|departure|retire|retirement|terminat|"
    r"step(?:ped|s)? down|will no longer|separation)\b",
    re.I,
)
_ARRIVAL_WORDS = re.compile(
    r"\b(appoint|elect|named|promot|hire|join|succeed|assume the role)\b", re.I
)

_ticker_cache: dict[str, int] | None = None


def _ticker_to_cik() -> dict[str, int]:
    """SEC's official ticker -> CIK mapping, fetched once per run."""
    global _ticker_cache
    if _ticker_cache is not None:
        return _ticker_cache

    _ticker_cache = {}
    r = get(TICKER_MAP_URL, accept="application/json")
    if not r:
        log.error("could not fetch SEC ticker map; EDGAR source will be empty")
        return _ticker_cache

    try:
        for row in r.json().values():
            _ticker_cache[row["ticker"].upper()] = int(row["cik_str"])
    except (ValueError, KeyError) as exc:
        log.error("SEC ticker map was not in the expected shape: %s", exc)

    log.info("resolved %d tickers from SEC", len(_ticker_cache))
    return _ticker_cache


def _resolve_cik(company) -> int | None:
    if company.cik:
        digits = re.sub(r"\D", "", company.cik)
        if digits:
            return int(digits)
    if company.ticker:
        return _ticker_to_cik().get(company.ticker.strip().upper())
    return None


def _extract_item_502(html: str) -> str:
    """Pull just the Item 5.02 section out of a filing document."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    m = _ITEM_START.search(text)
    if not m:
        return ""

    tail = text[m.end():]
    end = _ITEM_END.search(tail)
    section = tail[: end.start()] if end else tail
    return section.strip()[:4000]


def _classify(section: str) -> str:
    dep = bool(_DEPARTURE_WORDS.search(section))
    arr = bool(_ARRIVAL_WORDS.search(section))
    if arr and not dep:
        return ARRIVAL
    if dep and not arr:
        return DEPARTURE
    return UNKNOWN


def collect(companies, lookback_days: int, seen: SeenStore) -> list[Move]:
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    moves: list[Move] = []

    targets = [c for c in companies if c.ticker or c.cik]
    log.info("EDGAR: checking %d public companies", len(targets))

    for company in targets:
        cik = _resolve_cik(company)
        if not cik:
            log.debug("  %s: no CIK resolved for ticker %r", company.name, company.ticker)
            continue

        r = get(SUBMISSIONS_URL.format(cik=cik), accept="application/json")
        if not r:
            continue

        try:
            recent = r.json()["filings"]["recent"]
        except (ValueError, KeyError):
            log.warning("  %s: unexpected submissions payload", company.name)
            continue

        forms = recent.get("form", [])
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            if (recent["filingDate"][i] or "") < cutoff:
                continue
            if "5.02" not in (recent.get("items", [""] * len(forms))[i] or ""):
                continue

            accession = recent["accessionNumber"][i]
            doc = recent["primaryDocument"][i]
            url = ARCHIVE_URL.format(
                cik=cik, accession=accession.replace("-", ""), doc=doc
            )

            iid = item_id("edgar", accession)
            if not seen.is_new(iid):
                continue
            seen.mark(iid)

            doc_resp = get(url)
            section = _extract_item_502(doc_resp.text) if doc_resp else ""
            people = (
                extract_from_text(section, exclude={company.name.lower()})
                if section
                else []
            )
            kind = _classify(section)

            if people:
                for person in people[:3]:
                    moves.append(
                        Move(
                            company=company.name,
                            source="SEC 8-K Item 5.02",
                            confidence=CONFIRMED,
                            kind=kind,
                            person=person.name,
                            title=person.title,
                            summary=section[:280],
                            url=url,
                            published=recent["filingDate"][i],
                            score=priority_score(person.title) + 20,
                        )
                    )
            else:
                moves.append(
                    Move(
                        company=company.name,
                        source="SEC 8-K Item 5.02",
                        confidence=CONFIRMED,
                        kind=kind,
                        summary=(
                            f"{company.name} filed an 8-K reporting an officer or "
                            f"director change. Filing text needs a read."
                        ),
                        url=url,
                        published=recent["filingDate"][i],
                        score=30,
                    )
                )

    log.info("EDGAR: %d moves", len(moves))
    return moves
