"""Trade press people columns, filtered against the master list.

These publications run dedicated people-move columns. We pull the feed once
and filter every entry against the company list, so one request covers all
130 companies instead of one request each.

Feed URLs live in config/feeds.yml because they change. `python run.py
--check-feeds` validates every one of them and tells you which are dead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

from lib.http import get
from lib.move import Move, CONFIRMED, PROBABLE, ARRIVAL, DEPARTURE, PROMOTION, UNKNOWN
from lib.people import extract_from_text, priority_score, MOVE_VERBS
from lib.state import SeenStore, item_id

log = logging.getLogger(__name__)

FEEDS_PATH = Path(__file__).resolve().parent.parent / "config" / "feeds.yml"


def load_feeds() -> list[dict]:
    if not FEEDS_PATH.exists():
        log.error("no feeds config at %s", FEEDS_PATH)
        return []
    data = yaml.safe_load(FEEDS_PATH.read_text(encoding="utf-8")) or {}
    return [f for f in data.get("feeds", []) if f.get("enabled", True)]


def _entry_date(entry) -> str | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc).date().isoformat()


def _kind(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ("steps down", "resign", "departs", "exits", "retires")):
        return DEPARTURE
    if any(w in low for w in ("promot", "elevat")):
        return PROMOTION
    if MOVE_VERBS.search(text):
        return ARRIVAL
    return UNKNOWN


def check_feeds() -> None:
    """Validate every configured feed. Run this after any feed edit."""
    for feed in load_feeds():
        r = get(feed["url"], accept="application/rss+xml")
        if not r:
            print(f"  DEAD    {feed['name']:<38} {feed['url']}")
            continue
        parsed = feedparser.parse(r.content)
        count = len(parsed.entries)
        status = "OK" if count else "EMPTY"
        print(f"  {status:<7} {feed['name']:<38} {count:>3} entries")


def collect(companies, lookback_days: int, seen: SeenStore) -> list[Move]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    feeds = load_feeds()
    moves: list[Move] = []
    log.info("Trade press: %d feeds against %d companies", len(feeds), len(companies))

    for feed in feeds:
        r = get(feed["url"], accept="application/rss+xml")
        if not r:
            log.warning("  feed unreachable: %s", feed["name"])
            continue

        parsed = feedparser.parse(r.content)
        people_column = bool(feed.get("people_column"))

        for entry in parsed.entries:
            published = _entry_date(entry)
            if published and published < cutoff:
                continue

            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            blob = f"{title} {summary}"

            # A dedicated people column is already all moves; a general feed
            # needs a verb filter so we don't ingest market-trend articles.
            if not people_column and not MOVE_VERBS.search(blob):
                continue

            matched = [c for c in companies if c.mentioned_in(blob)]
            if not matched:
                continue

            link = getattr(entry, "link", "") or ""
            for company in matched[:3]:
                iid = item_id("trade", f"{link}|{company.slug}")
                if not seen.is_new(iid):
                    continue
                seen.mark(iid)

                people = extract_from_text(
                    f"{title}. {summary}",
                    exclude={company.name.lower(), *(a.lower() for a in company.aliases)},
                )
                person = people[0] if people else None

                moves.append(
                    Move(
                        company=company.name,
                        source=feed["name"],
                        confidence=CONFIRMED if person else PROBABLE,
                        kind=_kind(blob),
                        person=person.name if person else None,
                        title=person.title if person else None,
                        summary=title,
                        url=link,
                        published=published,
                        score=(priority_score(person.title) if person else 5)
                        + (12 if people_column else 6),
                    )
                )

    log.info("Trade press: %d moves", len(moves))
    return moves
