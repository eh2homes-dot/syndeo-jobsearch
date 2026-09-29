"""Leadership / team page diffing.

The highest-value source on the list, because most of the master list is
private and will never file anything or issue a press release. Snapshot the
team page weekly, diff the names, and a new hire shows up with their title
already attached.

First run for a company records a baseline and reports nothing — that is
correct, not a bug. Use `--backfill` (sources/wayback.py) to get a populated
baseline on day one instead of waiting.

Everything from this source is PROBABLE: a page edit is strong evidence but
not an announcement.
"""

from __future__ import annotations

import logging

from lib.http import get
from lib.move import Move, PROBABLE, ARRIVAL, DEPARTURE, PROMOTION
from lib.people import Person, extract_from_html, diff_people, priority_score
from lib.state import load_snapshot, save_snapshot, page_hash

log = logging.getLogger(__name__)

# A page that suddenly yields almost nobody is usually a failed fetch or a
# redesign, not fifteen simultaneous departures. Guard against reporting that.
MIN_PLAUSIBLE = 2
MAX_CHURN_RATIO = 0.6


def collect(companies, seen=None) -> list[Move]:
    moves: list[Move] = []
    targets = [c for c in companies if c.leadership_url]
    log.info("Leadership: checking %d pages", len(targets))

    for company in targets:
        r = get(company.leadership_url)
        if not r:
            continue

        html = r.text
        current = extract_from_html(html)
        if len(current) < MIN_PLAUSIBLE:
            log.warning(
                "  %s: only %d people parsed from %s, skipping (likely JS-rendered)",
                company.name, len(current), company.leadership_url,
            )
            continue

        prior = load_snapshot(company.slug)
        prior_people = [Person(**p) for p in prior.get("people", [])]

        # First sighting: record the baseline, report nothing.
        if not prior_people:
            save_snapshot(
                company.slug,
                company.leadership_url,
                [p.to_dict() for p in current],
                page_hash(html),
            )
            log.info("  %s: baseline recorded (%d people)", company.name, len(current))
            continue

        # Page unchanged byte-for-byte: nothing to do.
        if prior.get("page_hash") == page_hash(html):
            continue

        added, removed, retitled = diff_people(prior_people, current)

        # Sanity gate: a redesign can make everyone look new.
        churn = (len(added) + len(removed)) / max(len(prior_people), 1)
        if churn > MAX_CHURN_RATIO:
            log.warning(
                "  %s: %.0f%% churn on %s — treating as a page redesign, "
                "re-baselining without reporting",
                company.name, churn * 100, company.leadership_url,
            )
            save_snapshot(
                company.slug,
                company.leadership_url,
                [p.to_dict() for p in current],
                page_hash(html),
            )
            continue

        for person in added:
            moves.append(
                Move(
                    company=company.name,
                    source="Leadership page",
                    confidence=PROBABLE,
                    kind=ARRIVAL,
                    person=person.name,
                    title=person.title,
                    summary=f"New name on {company.name}'s team page.",
                    url=company.leadership_url,
                    score=priority_score(person.title) + 10,
                )
            )

        for person in removed:
            moves.append(
                Move(
                    company=company.name,
                    source="Leadership page",
                    confidence=PROBABLE,
                    kind=DEPARTURE,
                    person=person.name,
                    title=person.title,
                    summary=f"Name removed from {company.name}'s team page.",
                    url=company.leadership_url,
                    score=priority_score(person.title),
                )
            )

        for old, new in retitled:
            moves.append(
                Move(
                    company=company.name,
                    source="Leadership page",
                    confidence=PROBABLE,
                    kind=PROMOTION,
                    person=new.name,
                    title=new.title,
                    previous_title=old.title,
                    summary=f"Title change at {company.name}.",
                    url=company.leadership_url,
                    score=priority_score(new.title) + 5,
                )
            )

        save_snapshot(
            company.slug,
            company.leadership_url,
            [p.to_dict() for p in current],
            page_hash(html),
        )

    log.info("Leadership: %d moves", len(moves))
    return moves
