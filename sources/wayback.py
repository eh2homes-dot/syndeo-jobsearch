"""Wayback Machine backfill — run once, then forget about it.

Leadership diffing only starts producing signal after you have two snapshots.
Rather than wait a month, pull an archived copy of each team page from N days
ago and diff it against today. That gives a populated "who changed recently"
list on day one.

Free, no API key. The CDX index is rate-limited, so this is slow by design
(lib/http.py throttles web.archive.org to one request per second). Expect
20-40 minutes for the full list. Run it with `--backfill` once, not weekly.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from lib.http import get
from lib.move import Move, PROBABLE, ARRIVAL, DEPARTURE, PROMOTION
from lib.people import extract_from_html, diff_people, priority_score
from lib.state import load_snapshot, save_snapshot, page_hash

log = logging.getLogger(__name__)

CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url={url}&output=json&filter=statuscode:200"
    "&from={frm}&to={to}&collapse=timestamp:6&limit=5"
)
# The `id_` modifier returns the original page without the Wayback toolbar.
SNAPSHOT_URL = "https://web.archive.org/web/{ts}id_/{url}"


def _closest_snapshot(url: str, days_back: int) -> tuple[str, str] | None:
    """Return (timestamp, html) for an archived copy roughly days_back ago."""
    target = date.today() - timedelta(days=days_back)
    window_start = (target - timedelta(days=45)).strftime("%Y%m%d")
    window_end = (target + timedelta(days=15)).strftime("%Y%m%d")

    r = get(CDX_URL.format(url=url, frm=window_start, to=window_end), accept="application/json")
    if not r:
        return None

    try:
        rows = r.json()
    except ValueError:
        return None

    if not rows or len(rows) < 2:
        return None

    header, *entries = rows
    try:
        ts_idx = header.index("timestamp")
    except ValueError:
        ts_idx = 1

    timestamp = entries[-1][ts_idx]
    snap = get(SNAPSHOT_URL.format(ts=timestamp, url=url))
    if not snap:
        return None
    return timestamp, snap.text


def collect(companies, days_back: int = 120, seen=None) -> list[Move]:
    moves: list[Move] = []
    targets = [c for c in companies if c.leadership_url]
    log.info(
        "Wayback backfill: %d pages, looking ~%d days back (this is slow)",
        len(targets), days_back,
    )

    for company in targets:
        live = get(company.leadership_url)
        if not live:
            continue
        current = extract_from_html(live.text)
        if len(current) < 2:
            continue

        archived = _closest_snapshot(company.leadership_url, days_back)
        if not archived:
            log.info("  %s: no usable archive snapshot", company.name)
            # Still record today as the baseline so weekly diffing can start.
            save_snapshot(
                company.slug,
                company.leadership_url,
                [p.to_dict() for p in current],
                page_hash(live.text),
            )
            continue

        timestamp, old_html = archived
        old_people = extract_from_html(old_html)
        if len(old_people) < 2:
            continue

        added, removed, retitled = diff_people(old_people, current)
        archive_link = SNAPSHOT_URL.format(ts=timestamp, url=company.leadership_url)
        window = f"since {timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"

        for person in added:
            moves.append(
                Move(
                    company=company.name, source="Wayback backfill",
                    confidence=PROBABLE, kind=ARRIVAL,
                    person=person.name, title=person.title,
                    summary=f"Added to {company.name}'s team page {window}.",
                    url=archive_link,
                    score=priority_score(person.title) + 10,
                )
            )
        for person in removed:
            moves.append(
                Move(
                    company=company.name, source="Wayback backfill",
                    confidence=PROBABLE, kind=DEPARTURE,
                    person=person.name, title=person.title,
                    summary=f"Removed from {company.name}'s team page {window}.",
                    url=archive_link,
                    score=priority_score(person.title),
                )
            )
        for old, new in retitled:
            moves.append(
                Move(
                    company=company.name, source="Wayback backfill",
                    confidence=PROBABLE, kind=PROMOTION,
                    person=new.name, title=new.title, previous_title=old.title,
                    summary=f"Title changed at {company.name} {window}.",
                    url=archive_link,
                    score=priority_score(new.title) + 5,
                )
            )

        # Seed the baseline from today so the weekly job takes over cleanly.
        save_snapshot(
            company.slug,
            company.leadership_url,
            [p.to_dict() for p in current],
            page_hash(live.text),
        )

    log.info("Wayback backfill: %d moves", len(moves))
    return moves
