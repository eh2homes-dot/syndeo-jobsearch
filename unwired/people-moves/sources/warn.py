"""WARN Act layoff notices — the candidate-supply signal.

Every US employer over a certain size must file a WARN notice before a mass
layoff or plant closing. That is a named list of companies about to release
people, published before it happens. For placement work that is often worth
more than a job posting: you know who is available before they are on the
market.

There is no federal database. The WARN Act is administered state by state and
the US DOL links out to state pages rather than hosting a consolidated one, so
this reads a normalized 48-state dataset (CC BY 4.0) instead of scraping ~50
portals in ~50 formats. Swap the URL in config/warn.yml for any other CSV with
company / state / date columns; the loader matches headers loosely.

ATTRIBUTION: the default dataset is CC BY 4.0 and asks for credit to "WARN
Feed". If a notice from it goes in the newsletter, credit it. The constant
below is rendered into the brief so this does not get forgotten.

MATCHING IS THE HARD PART
-------------------------
Company names in WARN filings are legal entity names, and they collide. The
archive contains many notices from "Compass Group USA" — a food service
company with no relationship to Compass the brokerage. Loose substring
matching would put a false layoff in the newsletter under a real company's
name, which is the worst error this whole pipeline can make.

So matching here is deliberately stricter than elsewhere:
  - the normalized WARN name must equal the master name, or begin with it
  - a state mismatch against the master list downgrades confidence
  - an exact match in the expected state is CONFIRMED; anything else is
    PROBABLE and says in the line itself that the identity needs checking
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import re

from lib.companies import normalize
from lib.http import get
from lib.move import Move, CONFIRMED, PROBABLE, LAYOFF, COMPANY
from lib.state import SeenStore, item_id

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "warn.yml"

DEFAULT_URL = (
    "https://raw.githubusercontent.com/APVentureEngine/warn-act-notices"
    "/main/data/warn_notices.csv"
)
DEFAULT_ATTRIBUTION = "WARN Feed (CC BY 4.0)"
DEFAULT_BROWSE = "https://approjects-warn-act-notices.static.hf.space/index.html"

# Never report a notice filed longer ago than this, however recently it entered
# the dataset. Without it the first run dumps the entire historical archive,
# because every row's first_seen is the day the dataset was first fetched.
DEFAULT_MAX_AGE_DAYS = 90

# Header -> canonical field, matched by substring, case-insensitive.
_HEADERS = [
    ("company_canonical", "company_canonical"),
    ("company_dba", "dba"),
    ("company", "company"),
    ("employer", "company"),
    ("state", "state"),
    ("location", "location"),
    ("employees_affected", "employees"),
    ("employees", "employees"),
    ("workers", "employees"),
    ("notice_date", "notice_date"),
    ("effective_date", "effective_date"),
    ("notice_type", "notice_type"),
    ("first_seen", "first_seen"),
    ("id", "id"),
]

# US state name -> postal code, because the master sheet stores full names and
# WARN data stores codes.
_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"url": DEFAULT_URL, "attribution": DEFAULT_ATTRIBUTION}
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {
        "url": data.get("url") or DEFAULT_URL,
        "browse_url": data.get("browse_url") or DEFAULT_BROWSE,
        "local_file": data.get("local_file"),
        "attribution": data.get("attribution") or DEFAULT_ATTRIBUTION,
        "min_employees": int(data.get("min_employees") or 0),
        "max_notice_age_days": int(
            data.get("max_notice_age_days") or DEFAULT_MAX_AGE_DAYS
        ),
    }


def _canonical_headers(fieldnames) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames or []:
        low = (raw or "").strip().lower()
        for needle, canon in _HEADERS:
            if needle in low and raw not in mapping:
                mapping[raw] = canon
                break
    return mapping


def _load_rows(cfg: dict) -> list[dict]:
    local = cfg.get("local_file")
    if local and Path(local).exists():
        text = Path(local).read_text(encoding="utf-8", errors="replace")
        log.info("WARN: reading local file %s", local)
    else:
        r = get(cfg["url"])
        if not r:
            log.error("WARN: could not fetch %s", cfg["url"])
            return []
        text = r.text

    reader = csv.DictReader(io.StringIO(text))
    headers = _canonical_headers(reader.fieldnames)
    if "company" not in headers.values() and "company_canonical" not in headers.values():
        log.error("WARN: no company column found in %s", reader.fieldnames)
        return []

    rows = []
    for raw in reader:
        rows.append({canon: (raw.get(col) or "").strip() for col, canon in headers.items()})
    log.info("WARN: loaded %d notices", len(rows))
    return rows


# Only true legal-entity suffixes are stripped when comparing WARN names. The
# general normalizer in lib/companies.py also strips words like "group",
# "properties" and "management", which is right for prose matching and wrong
# here: it turns "Compass Group USA" into "compass usa", which then looks like
# a match for "Compass".
_LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|lp|llp|plc|"
    r"pllc|pc|na|usa|us|holdings?|the)\b\.?",
    re.I,
)
_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def _legal_normalize(text: str) -> str:
    t = _NON_WORD.sub(" ", (text or "").lower())
    t = _LEGAL_SUFFIX.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def _expected_code(company) -> str:
    return _STATE_CODES.get((company.state or "").strip().lower(), "")


def _match(company, warn_name: str) -> bool:
    """Strict equality against the company name or any of its aliases.

    Prefix and substring matching are deliberately not used. The archive
    contains many "Compass Group USA" notices — a food service company — and
    attributing those layoffs to Compass the brokerage in a newsletter is the
    worst error this pipeline can make. A false negative just means a missed
    item; a false positive means publishing something untrue about a real
    company under its own name.

    To widen coverage, add the legal entity name to that company's Aliases
    column in the master sheet. Near misses are logged so you can see which
    aliases are worth adding.
    """
    candidate = _legal_normalize(warn_name)
    if not candidate or len(candidate) < 4:
        return False
    targets = {_legal_normalize(company.name)}
    targets.update(_legal_normalize(a) for a in company.aliases)
    return candidate in {t for t in targets if t and len(t) >= 4}


def _near_miss(company, warn_name: str) -> bool:
    """A name that starts with the company's — probably related, maybe not."""
    candidate = _legal_normalize(warn_name)
    target = _legal_normalize(company.name)
    if not candidate or not target or len(target) < 4:
        return False
    return candidate != target and candidate.startswith(target + " ")


def collect(companies, lookback_days: int, seen: SeenStore) -> list[Move]:
    cfg = load_config()
    rows = _load_rows(cfg)
    if not rows:
        return []

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).date().isoformat()
    age_floor = (
        now - timedelta(days=cfg["max_notice_age_days"])
    ).date().isoformat()
    moves: list[Move] = []
    stale = 0
    near_misses: list[tuple[str, str, str]] = []
    min_employees = cfg.get("min_employees") or 0

    for row in rows:
        # first_seen is when the notice entered the dataset, which is the right
        # field for "new this week". notice_date can be blank, and effective
        # dates are often in the future.
        seen_at = (row.get("first_seen") or "")[:10]
        notice_date = (row.get("notice_date") or "")[:10]
        if seen_at and seen_at < cutoff:
            continue
        if not seen_at and notice_date and notice_date < cutoff:
            continue

        # New to us, but is it new? On the first run every row looks newly
        # seen, so drop anything filed long ago regardless.
        if notice_date and notice_date < age_floor:
            stale += 1
            continue

        warn_name = row.get("company_canonical") or row.get("company") or ""
        if not warn_name:
            continue

        matched = None
        for company in companies:
            if _match(company, warn_name) or (
                row.get("dba") and _match(company, row["dba"])
            ):
                matched = company
                break
        if not matched:
            # Surface near misses in the log rather than the brief. If one is
            # genuinely the same company, add the legal name to its Aliases
            # column and it will match next week.
            for company in companies:
                if _near_miss(company, warn_name):
                    near_misses.append((company.name, warn_name, row.get("state", "")))
                    break
            continue

        employees = row.get("employees") or ""
        try:
            if min_employees and int(employees) < min_employees:
                continue
        except ValueError:
            pass

        warn_state = (row.get("state") or "").upper()
        expected = _expected_code(matched)
        exact = normalize(warn_name) == normalize(matched.name)
        state_ok = (not expected) or (not warn_state) or warn_state == expected

        iid = item_id("warn", row.get("id") or f"{warn_name}|{warn_state}|{notice_date}")
        if not seen.is_new(iid):
            continue
        seen.mark(iid)

        parts = [f"**{matched.name}** filed a WARN notice"]
        if warn_state:
            where = f"{row.get('location')}, {warn_state}" if row.get("location") else warn_state
            parts.append(f"in {where}")
        if employees:
            parts.append(f"— {employees} workers affected")
        if row.get("notice_type"):
            parts.append(f"({row['notice_type']})")
        if notice_date:
            parts.append(f"filed {notice_date}")

        summary = " ".join(parts) + "."
        if not (exact and state_ok):
            summary += (
                f" ⚠ Filed as \"{warn_name}\""
                + (f" in {warn_state}" if warn_state and not state_ok else "")
                + " — confirm this is the same company before publishing."
            )

        moves.append(
            Move(
                company=matched.name,
                source=f"WARN notice — {cfg['attribution']}",
                confidence=CONFIRMED if (exact and state_ok) else PROBABLE,
                kind=LAYOFF,
                category=COMPANY,
                summary=summary,
                url=cfg["browse_url"],
                published=notice_date or seen_at,
                score=40 + (10 if exact and state_ok else 0),
            )
        )

    if stale:
        log.info(
            "WARN: %d notice(s) skipped as older than %d days (normal on a "
            "first run, when the whole archive looks newly seen)",
            stale, cfg["max_notice_age_days"],
        )

    if near_misses:
        log.info(
            "WARN: %d near miss(es) not reported. Add the legal name to that "
            "company's Aliases column if it is genuinely the same company:",
            len(near_misses),
        )
        for master, warn_name, st in near_misses[:15]:
            log.info("    %-28s ~ %-48s [%s]", master, warn_name, st)

    log.info("WARN: %d moves", len(moves))
    return moves
