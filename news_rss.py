"""Google News RSS, one query per company.

Free, no API key, returns clean XML. The query pairs the company name with
move verbs so we are not reading every mention of the company, only the ones
that look like a hire, an appointment or a departure.

Results are CONFIRMED when the item clearly names a person and a title,
otherwise PROBABLE — a headline is not a filing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser

from lib.http import get
from lib.move import Move, CONFIRMED, PROBABLE, ARRIVAL, DEPARTURE, PROMOTION, UNKNOWN
from lib.people import extract_from_text, priority_score, MOVE_VERBS
from lib.state import SeenStore, item_id

log = logging.getLogger(__name__)

FEED_URL = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
QUERY_TEMPLATE = (
    '"{company}" (hires OR appoints OR names OR joins OR promotes OR '
    '"steps down" OR "chief executive" OR "vice president" OR "head of")'
)

_DEPARTURE = re.compile(r"steps? down|resign|depart|exits?|retires?|out as", re.I)
_PROMOTION = re.compile(r"promot|elevat|expands? role|takes? over as", re.I)


def _entry_date(entry) -> str | None:
    parsed = getattr(entry, "published_parsed", None)
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc).date().isoformat()


def _kind(text: str) -> str:
    if _DEPARTURE.search(text):
        return DEPARTURE
    if _PROMOTION.search(text):
        return PROMOTION
    if MOVE_VERBS.search(text):
        return ARRIVAL
    return UNKNOWN


def collect(companies, lookback_days: int, seen: SeenStore) -> list[Move]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    moves: list[Move] = []
    log.info("Google News: querying %d companies", len(companies))

    for company in companies:
        query = QUERY_TEMPLATE.format(company=company.name)
        r = get(FEED_URL.format(query=quote_plus(query)), accept="application/rss+xml")
        if not r:
            continue

        feed = feedparser.parse(r.content)
        for entry in feed.entries[:15]:
            title = getattr(entry, "title", "") or ""
            published = _entry_date(entry)
            if published and published < cutoff:
                continue
            if not MOVE_VERBS.search(title):
                continue

            # Google News titles end in " - Publisher"; keep it for provenance
            # but match against the headline only.
            headline = title.rsplit(" - ", 1)[0]
            if not company.mentioned_in(headline) and not company.mentioned_in(title):
                continue

            link = getattr(entry, "link", "") or ""
            iid = item_id("gnews", link or title)
            if not seen.is_new(iid):
                continue
            seen.mark(iid)

            people = extract_from_text(
                headline, exclude={company.name.lower(), *(a.lower() for a in company.aliases)}
            )
            person = people[0] if people else None

            moves.append(
                Move(
                    company=company.name,
                    source="Google News",
                    confidence=CONFIRMED if person else PROBABLE,
                    kind=_kind(headline),
                    person=person.name if person else None,
                    title=person.title if person else None,
                    summary=title,
                    url=link,
                    published=published,
                    score=(priority_score(person.title) if person else 5) + 8,
                )
            )

    log.info("Google News: %d moves", len(moves))
    return moves
