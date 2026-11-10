"""SEC Form D — private funding rounds, and the officers named on them.

This is the source that covers the part of the master list EDGAR otherwise
misses. 8-K Item 5.02 only reaches the ~25 public companies. Form D reaches
every private one that has raised money, which is most of the proptech side.

A company filing a private (Reg D) offering must file Form D within 15 days of
the first sale. The filing does double duty:

  1. a company move — the round itself, with the amount actually sold
  2. a people list — Item 3 "Related Persons" names every executive officer,
     director and promoter, with their relationship to the issuer

So a single free filing gives you both a funding event to write about and a
current executive roster to diff against last time.

Discovery works off EDGAR's daily index rather than full-text search: one
request per calendar day covers every Form D filed that day across all
issuers, which is both cheaper and more complete than querying company by
company. Weekends and holidays have no index and are skipped.

No API key. SEC only requires a descriptive User-Agent, set in lib/http.py.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from lib.http import get
from lib.move import (
    Move, CONFIRMED, PROBABLE, ARRIVAL, FUNDING, PEOPLE, COMPANY,
)
from lib.people import priority_score
from lib import state as state_lib
from lib.state import SeenStore, item_id, _read_json, _write_json

log = logging.getLogger(__name__)

DAILY_INDEX = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/master.{ymd}.idx"
)
PRIMARY_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/primary_doc.xml"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"

_RANK = {"cik": 0, "name": 1, "related": 2}   # best match first

# Form D and its amendments. Amendments matter: they are often where a new
# officer first appears.
FORM_TYPES = {"D", "D/A"}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find(node, path: str):
    """ElementTree find that ignores XML namespaces."""
    current = [node]
    for part in path.split("/"):
        nxt = []
        for el in current:
            nxt.extend(c for c in el if _strip_ns(c.tag) == part)
        current = nxt
        if not current:
            return None
    return current[0] if current else None


def _find_all(node, path: str) -> list:
    parent_path, _, leaf = path.rpartition("/")
    parent = _find(node, parent_path) if parent_path else node
    if parent is None:
        return []
    return [c for c in parent if _strip_ns(c.tag) == leaf]


def _text(node, path: str) -> str:
    el = _find(node, path) if path else node
    return (el.text or "").strip() if el is not None and el.text else ""


def _money(raw: str) -> str:
    """Format an amount for a newsletter line: 12000000 -> $12.0M."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _daily_index_rows(day: date) -> list[tuple[str, str, str, str, str]]:
    """Return (cik, company, form, filed, filename) for one calendar day."""
    url = DAILY_INDEX.format(
        year=day.year, qtr=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d")
    )
    r = get(url)
    if not r:
        # Weekends, federal holidays and not-yet-published days all 404.
        return []

    rows = []
    for line in r.text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form, filed, filename = (p.strip() for p in parts)
        if not cik.isdigit():
            continue  # header and separator lines
        if form in FORM_TYPES:
            rows.append((cik, company, form, filed, filename))
    return rows


def _parse_form_d(xml_text: str) -> dict | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.debug("  Form D XML would not parse: %s", exc)
        return None

    issuer = _find(root, "primaryIssuer")
    if issuer is None:
        return None

    people = []
    for info in _find_all(root, "relatedPersonsList/relatedPersonInfo"):
        name_el = _find(info, "relatedPersonName")
        if name_el is None:
            continue
        name = " ".join(
            part
            for part in (
                _text(name_el, "firstName"),
                _text(name_el, "middleName"),
                _text(name_el, "lastName"),
            )
            if part
        ).strip()
        if not name:
            continue

        rel_el = _find(info, "relatedPersonRelationshipList")
        roles = []
        if rel_el is not None:
            roles = [
                (c.text or "").strip()
                for c in rel_el
                if _strip_ns(c.tag) == "relationship" and c.text
            ]
        clarification = _text(info, "relationshipClarification")
        people.append(
            {"name": name, "roles": roles, "clarification": clarification}
        )

    amounts = _find(root, "offeringData/offeringSalesAmounts")
    sold = _text(amounts, "totalAmountSold") if amounts is not None else ""
    offered = _text(amounts, "totalOfferingAmount") if amounts is not None else ""

    return {
        "entity": _text(issuer, "entityName"),
        "state": _text(issuer, "issuerAddress/stateOrCountryDescription"),
        "city": _text(issuer, "issuerAddress/city"),
        "industry": _text(root, "offeringData/industryGroup/industryGroupType"),
        "sold": sold,
        "offered": offered,
        "submission_type": _text(root, "submissionType"),
        "people": people,
    }


def _roster_path(slug: str):
    # Looked up at call time so the tests' scratch state folder is honoured.
    return state_lib.STATE_DIR / "formd" / f"{slug}.json"


# ---------------------------------------------------------------------------
# Which company a filing belongs to
# ---------------------------------------------------------------------------
#
# A few thousand Form Ds are filed every week, most of them by investment funds
# with names like "Haven Capital Fund III LP". Matching those the way a headline
# is matched ("the company's name appears in the text") would put a stranger's
# fundraise in the brief under a watched company's name, marked Confirmed. So a
# filing only counts when the issuer IS the company:
#
#   "cik"      the issuer's SEC number is the one on file for the company
#   "name"     the issuer's name, minus its legal form (Inc, LLC...) and any
#              holding-company wrapping (Holdings, Group), is exactly the
#              company's name or one of its aliases
#   "related"  the issuer's name starts with the company's name and goes on
#              ("Evernest Fund II"). It may be the company's own fund or a
#              stranger. It is reported as Probable, only when the issuer's
#              state matches the company's, and never feeds the officer roster.

# Legal form only. Descriptive words (Partners, Capital, Realty...) are part of
# who a company is: "Atlas Real Estate Partners" is not "Atlas Real Estate".
_LEGAL_FORM = re.compile(r"\s+(inc|incorporated|llc|l l c|lp|l p|llp|ltd|limited|corp|corporation|co|company|pbc|plc)$")
# A holding entity of the same name is the same company.
_HOLDING = re.compile(r"\s+(holdings?|holdco|parent|topco|intermediate|group)$")

_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "district of columbia": "dc",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}


def _state(value: str) -> str:
    """'California', 'CALIFORNIA' or 'CA' -> 'ca'. Unknown or blank -> ''."""
    v = (value or "").strip().lower()
    if v in _STATES:
        return _STATES[v]
    return v if v in _STATES.values() else ""


def _plain(name: str) -> str:
    """Lowercase, no punctuation, legal form taken off the end:
    'Belong Home, Inc.' -> 'belong home'."""
    n = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (name or "").lower())).strip()
    while True:
        shorter = _LEGAL_FORM.sub("", n)
        if shorter == n:
            return n
        n = shorter


def _core(name: str) -> str:
    """An issuer's name with holding-company wrapping taken off too:
    'Evernest Holdings, LLC' -> 'evernest'."""
    n = _plain(name)
    while True:
        shorter = _plain(_HOLDING.sub("", n))
        if shorter == n:
            return n
        n = shorter


def _exact_names(company) -> set[str]:
    names = {_plain(company.name), *(_plain(a) for a in company.aliases)}
    return {n for n in names if n}


def _own_cik(company) -> int:
    digits = "".join(ch for ch in str(getattr(company, "cik", "") or "") if ch.isdigit())
    return int(digits) if digits else 0


def match_issuer(company, cik: str, issuer_name: str, *, names=None, own_cik=None,
                 plain=None, core=None) -> str:
    """How a filing's issuer relates to a watched company: 'cik', 'name',
    'related' or '' (not this company). The keyword arguments are the same
    values worked out ahead of time, for the weekly scan's inner loop."""
    own_cik = _own_cik(company) if own_cik is None else own_cik
    if own_cik and cik.isdigit() and own_cik == int(cik):
        return "cik"
    names = _exact_names(company) if names is None else names
    full = _plain(issuer_name) if plain is None else plain
    if full in names or (_core(issuer_name) if core is None else core) in names:
        return "name"
    if any(len(n) >= 5 and full.startswith(n + " ") for n in names):
        return "related"
    return ""


def collect(companies, lookback_days: int, seen: SeenStore) -> list[Move]:
    moves: list[Move] = []
    today = date.today()
    days = [today - timedelta(days=i) for i in range(lookback_days + 1)]

    log.info("Form D: scanning %d days of EDGAR daily index", len(days))

    watched = [(c, _exact_names(c), _own_cik(c)) for c in companies]
    candidates: list[tuple] = []
    days_read = filings = 0
    for day in days:
        if day.weekday() >= 5:
            continue   # the SEC publishes no index at weekends
        rows = _daily_index_rows(day)
        if rows:
            days_read += 1
            filings += len(rows)
            log.debug("  %s: %d Form D filings", day, len(rows))
        for cik, issuer_name, form, filed, filename in rows:
            best = None
            plain, core = _plain(issuer_name), _core(issuer_name)
            for company, names, own_cik in watched:
                how = match_issuer(company, cik, issuer_name, names=names, own_cik=own_cik,
                                   plain=plain, core=core)
                if how and (best is None or _RANK[how] < _RANK[best[1]]):
                    best = (company, how)
            if best:
                candidates.append((best[0], best[1], cik, issuer_name, form, filed, filename))

    if not days_read:
        log.warning("Form D: no daily index could be read for the last %d days "
                    "(SEC unreachable, or it refused the User-Agent)", lookback_days)
    log.info("Form D: %d filings in %d days, %d matched a watched company",
             filings, days_read, len(candidates))

    for company, how, cik, issuer_name, form, filed, filename in candidates:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", filename)
        if not m:
            continue
        accession = m.group(1)

        iid = item_id("formd", accession)
        if not seen.is_new(iid):
            continue
        seen.mark(iid)

        folder = accession.replace("-", "")
        doc = get(PRIMARY_DOC.format(cik=int(cik), folder=folder), accept="application/xml")
        if not doc:
            continue

        parsed = _parse_form_d(doc.text)
        if not parsed:
            continue

        link = FILING_INDEX.format(cik=int(cik), folder=folder)
        amount = _money(parsed["sold"]) or _money(parsed["offered"])
        amended = parsed["submission_type"] == "D/A"
        entity = parsed["entity"] or issuer_name

        ours, theirs = _state(company.state), _state(parsed["state"])
        if how == "related":
            # A longer name that starts with the company's. Keep it only when the
            # issuer is in the company's own state, and say plainly it is unchecked.
            if not ours or ours != theirs:
                log.debug("  %s: skipped '%s' (similar name, state %s vs %s)",
                          company.name, entity, theirs or "?", ours or "?")
                continue
        # Same name, but the filing gives a different state from the one on the
        # list: two companies can share a name, so this one is not Confirmed.
        elsewhere = how == "name" and bool(ours) and bool(theirs) and ours != theirs

        # 1. The company move: the round itself.
        if how == "related":
            bits = [f"**{entity}** filed a Form D"]
        else:
            bits = [f"**{company.name}** filed a Form D"]
        if amended:
            bits.append("(amendment)")
        if amount:
            sold_label = "raised" if _money(parsed["sold"]) else "is offering"
            bits.append(f"— {sold_label} {amount}")
        if parsed["industry"]:
            bits.append(f"[{parsed['industry']}]")
        summary = " ".join(bits) + "."
        if how == "related":
            summary += (f" Similar name to **{company.name}** and the same state; "
                        "check it is theirs before using it.")
        elif _plain(entity) != _plain(company.name):
            summary += f" Filed as {entity}."
        if elsewhere:
            summary += (f" The filing gives {parsed['state'].title()} as its state, not "
                        f"{company.state}; check it is the same company.")
        moves.append(
            Move(
                company=company.name,
                source="SEC Form D",
                confidence=PROBABLE if (how == "related" or elsewhere) else CONFIRMED,
                kind=FUNDING,
                category=COMPANY,
                summary=summary,
                url=link,
                published=filed,
                score=(45 if not amended else 25) - (20 if how == "related" else 0),
            )
        )
        if how == "related" or elsewhere:
            continue   # someone else's officers must never land in this company's roster

        # 2. The people: officers named on this filing who were not named on
        #    the last one. Probable rather than confirmed — an officer can be
        #    newly *listed* without being newly *hired*, since an earlier
        #    filing may simply have omitted them.
        roster_file = _roster_path(company.slug)
        previous = set(_read_json(roster_file, {}).get("people", []))
        current_names = [p["name"] for p in parsed["people"]]

        if previous:
            for person in parsed["people"]:
                if person["name"] in previous:
                    continue
                role = ", ".join(person["roles"]) or "Related person"
                if person["clarification"]:
                    role = f"{role} ({person['clarification']})"
                moves.append(
                    Move(
                        company=company.name,
                        source="SEC Form D (Item 3)",
                        confidence=PROBABLE,
                        kind=ARRIVAL,
                        category=PEOPLE,
                        person=person["name"],
                        title=role,
                        summary=(
                            f"Named on {company.name}'s latest Form D but not on "
                            f"the previous one."
                        ),
                        url=link,
                        published=filed,
                        score=priority_score(role) + 15,
                    )
                )
        else:
            log.info(
                "  %s: first Form D roster recorded (%d people)",
                company.name, len(current_names),
            )

        _write_json(
            roster_file,
            {
                "entity": parsed["entity"],
                "cik": cik,
                "last_filing": accession,
                "last_filed": filed,
                "people": sorted(set(current_names)),
            },
        )

    log.info("Form D: %d moves", len(moves))
    return moves
