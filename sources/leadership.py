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
# A plain download that lists fewer than this many people is read again in the
# headless browser; team grids often load the rest with scripts.
BROWSER_BELOW = 3
# Leadership pages get their own browser and budget, so a slow week on job
# boards can't starve them (the shared one stops at 150 pages / 20 minutes).
BROWSER_PAGES = 200
BROWSER_MINUTES = 40

_browser = None


def _get_browser():
    global _browser
    if _browser is None:
        from jobsearch.browser import Browser
        _browser = Browser(max_pages=BROWSER_PAGES, max_seconds=BROWSER_MINUTES * 60)
    return _browser


def _close_browser():
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None


def is_leadership_change(*titles: str) -> bool:
    """A change worth reporting: at least one of the titles involved is above
    staff level. Staff names stay in the snapshot so the page reads the same
    week to week, but a leasing manager joining isn't a leadership move."""
    from lib.team_page import STAFF_TITLE
    return any(t and not STAFF_TITLE.search(t) for t in titles)


def _rendered_html(url: str) -> str:
    """The page as a visitor sees it, after its scripts run. Blank if the
    headless browser isn't installed or the page won't load in it."""
    try:
        from jobsearch.browser import BrowserUnavailable
        browser = _get_browser()
    except Exception:  # noqa: BLE001 - the browser is optional
        return ""
    try:
        page = browser.render(url)
    except BrowserUnavailable as exc:
        log.info("    browser couldn't read %s: %s", url, exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.info("    browser failed on %s: %s", url, exc)
        return ""
    return "\n".join(html for _, html in page.frames)


def _read_people(company) -> tuple[str, list]:
    """Download the leadership page; if the plain download lists too few people
    (the names load with scripts, or the site refuses plain downloads), read it
    again in the headless browser. Returns (html, people)."""
    r = get(company.leadership_url)
    html = r.text if r else ""
    people = extract_from_html(html) if html else []
    if len(people) >= BROWSER_BELOW:
        return html, people
    rendered = _rendered_html(company.leadership_url)
    if rendered:
        browser_people = extract_from_html(rendered)
        if len(browser_people) > len(people):
            log.info("  %s: read in the browser (%d people)", company.name, len(browser_people))
            return rendered, browser_people
    return html, people


def collect(companies, seen=None) -> list[Move]:
    moves: list[Move] = []
    targets = [c for c in companies if c.leadership_url]
    log.info("Leadership: checking %d pages", len(targets))

    try:
        for company in targets:
            try:
                moves += _check(company)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                log.warning("  %s: leadership check failed (%s)", company.name, exc)
    finally:
        _close_browser()

    log.info("Leadership: %d moves", len(moves))
    return moves


def _check(company) -> list[Move]:
    moves: list[Move] = []
    html, current = _read_people(company)
    if len(current) < MIN_PLAUSIBLE:
        log.warning(
            "  %s: only %d people parsed from %s, skipping (even in the browser)",
            company.name, len(current), company.leadership_url,
        )
        return moves

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
        return moves

    # Page unchanged byte-for-byte: nothing to do.
    if prior.get("page_hash") == page_hash(html):
        return moves

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
        return moves

    added = [p for p in added if is_leadership_change(p.title)]
    removed = [p for p in removed if is_leadership_change(p.title)]
    retitled = [(o, n) for o, n in retitled if is_leadership_change(o.title, n.title)]

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
    return moves
