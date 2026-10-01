"""Writes a finished people-moves run to Supabase.

One run costs about six HTTP requests no matter how many companies or moves
there are, so there is no rate limit to worry about:

  1. create a scrape_runs row
  2. read the companies table
  3. insert any companies from the CSV that Supabase does not have yet
  4. upsert every move in one batch
  5. write one move_coverage row per company
  6. close out the scrape_runs row with totals

Moves are keyed on company + person + kind (or company + URL when nobody is
named). The first sighting wins: a move already in the table is never
overwritten, so its original run and detection date stay put.

Needs two environment variables, both stored as GitHub repo secrets:
  SUPABASE_URL          https://<project-ref>.supabase.co
  SUPABASE_SERVICE_KEY  the project's service_role (or sb_secret_...) key
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests

from lib.companies import Company, normalize
from lib.move import (
    Move, ARRIVAL, DEPARTURE, PROMOTION, PEOPLE,
)

log = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH = 500

CHANGE_TYPE = {
    ARRIVAL: "New employer",
    DEPARTURE: "Departure only",
    PROMOTION: "Promotion",
}


class SupabaseError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


class _Client:
    def __init__(self) -> None:
        self.base = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
        key = os.environ["SUPABASE_SERVICE_KEY"].strip()
        self.session = requests.Session()
        self.session.headers.update({"apikey": key, "Content-Type": "application/json"})
        # Legacy service_role keys are JWTs and also go in Authorization.
        # The newer sb_secret_ keys are not JWTs and must not.
        if key.startswith("eyJ"):
            self.session.headers["Authorization"] = f"Bearer {key}"
        self.calls = 0

    def call(self, method: str, table: str, *, params=None, body=None, prefer: str | None = None):
        headers = {"Prefer": prefer} if prefer else {}
        self.calls += 1
        resp = self.session.request(
            method, f"{self.base}/{table}",
            params=params, json=body, headers=headers, timeout=60,
        )
        if resp.status_code >= 300:
            raise SupabaseError(f"{method} {table} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso_or_none(value: str | None) -> str | None:
    return value if value and _ISO_DATE.match(value) else None


def _move_key(m: Move) -> str:
    if m.category == PEOPLE and m.person:
        raw = f"p|{normalize(m.company)}|{m.person.strip().lower()}|{m.kind}"
    else:
        raw = f"u|{normalize(m.company)}|{m.url or m.summary[:80]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _dedupe(moves: list[Move]) -> list[tuple[str, Move, list[str], list[str]]]:
    """One entry per move_key: the highest-confidence Move plus every source
    name and URL that reported it."""
    best: dict[str, Move] = {}
    names: dict[str, set[str]] = {}
    urls: dict[str, set[str]] = {}
    for m in moves:
        key = _move_key(m)
        names.setdefault(key, set()).add(m.source)
        if m.url:
            urls.setdefault(key, set()).add(m.url)
        if key not in best or m.sort_key < best[key].sort_key:
            best[key] = m
    return [
        (k, best[k], sorted(names.get(k, ())), sorted(urls.get(k, ())))
        for k in best
    ]


def _move_row(key: str, m: Move, source_names: list[str], source_urls: list[str],
              run_id: int, company_id: int | None) -> dict:
    published = _iso_or_none(m.published)
    people = m.category == PEOPLE
    leaving = people and m.kind == DEPARTURE
    if published:
        evidence = f"{m.source} dated {published}"
    else:
        evidence = f"No publish date; first observed {m.observed} via {m.source}"
    # Every row carries the same keys: PostgREST rejects mixed-shape batches.
    return {
        "move_key": key,
        "run_id": run_id,
        "company_id": company_id,
        "category": m.category,
        "kind": m.kind,
        "confidence": m.confidence,
        "source": ", ".join(source_names),
        "person_name": m.person if people else None,
        # For a departure, the title is the one they held at the company they left.
        "new_title": m.title if people and not leaving else None,
        "previous_title": (m.title or m.previous_title) if leaving else (m.previous_title if people else None),
        "change_type": CHANGE_TYPE.get(m.kind) if people else None,
        "new_company": m.company if people and m.kind in (ARRIVAL, PROMOTION) else None,
        "previous_company": m.company if people and m.kind == DEPARTURE else None,
        "announcement_date": published,
        "date_evidence": evidence,
        "sources": source_urls or None,
        "summary": m.summary or None,
        "score": m.score,
    }


def _coverage_result(company_moves: list[Move]) -> str:
    people = [m for m in company_moves if m.category == PEOPLE]
    if any(m.kind in (ARRIVAL, PROMOTION) for m in people):
        return "New role found"
    if any(m.kind == DEPARTURE for m in people):
        return "Departure only"
    if people:
        return "Move found (type unclear)"
    if company_moves:
        return "Company event only"
    return "No dated change found"


def _coverage_note(company_moves: list[Move]) -> str | None:
    if not company_moves:
        return None
    parts = []
    for m in company_moves[:5]:
        who = m.person or (m.summary[:80] if m.summary else m.kind)
        parts.append(f"{who} ({m.confidence}, {m.source})")
    more = len(company_moves) - len(parts)
    return "; ".join(parts) + (f"; +{more} more" if more > 0 else "")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def write_run(
    companies: list[Company],
    moves: list[Move],
    *,
    lookback_days: int,
    sources_ok: list[str],
    sources_failed: list[str],
) -> dict:
    db = _Client()
    today = date.today()
    period_start = (today - timedelta(days=lookback_days)).isoformat()

    # 1. Open the run.
    run = db.call(
        "POST", "scrape_runs",
        body={
            "workflow": "recently_hired",
            "period_start": period_start,
            "period_end": today.isoformat(),
            "notes": "GitHub Action people-moves run",
        },
        prefer="return=representation",
    )[0]
    run_id = run["id"]
    log.info("supabase: opened run #%s (%s to %s)", run_id, period_start, today)

    try:
        # 2-3. Match CSV companies to Supabase companies by normalized name;
        # insert only the ones that are genuinely missing.
        existing = db.call("GET", "companies", params={"select": "id,name"}) or []
        by_norm = {normalize(r["name"]): r["id"] for r in existing}
        missing = [c for c in companies if normalize(c.name) not in by_norm]
        if missing:
            added = db.call(
                "POST", "companies",
                params={"on_conflict": "name"},
                body=[{"name": c.name, "careers_url": c.careers_url or None} for c in missing],
                prefer="resolution=ignore-duplicates,return=representation",
            ) or []
            for r in added:
                by_norm[normalize(r["name"])] = r["id"]
            log.info("supabase: added %d new companies", len(added))

        # 4. Upsert moves.
        deduped = _dedupe(moves)
        rows = [
            _move_row(k, m, names, urls, run_id, by_norm.get(normalize(m.company)))
            for k, m, names, urls in deduped
        ]
        inserted = 0
        for i in range(0, len(rows), _BATCH):
            got = db.call(
                "POST", "people_moves",
                params={"on_conflict": "move_key", "select": "id"},
                body=rows[i:i + _BATCH],
                prefer="resolution=ignore-duplicates,return=representation",
            ) or []
            inserted += len(got)

        # 5. Coverage: one row per company checked this run.
        per_company: dict[str, list[Move]] = {}
        for _, m, _, _ in deduped:
            per_company.setdefault(normalize(m.company), []).append(m)

        coverage_note = (
            f"Sources run: {', '.join(sources_ok) or 'none'}"
            + (f"; failed: {', '.join(sources_failed)}" if sources_failed else "")
            + f"; lookback {lookback_days} days"
        )
        coverage = []
        seen_ids: set[int] = set()
        for c in companies:
            cid = by_norm.get(normalize(c.name))
            if cid is None or cid in seen_ids:
                continue
            seen_ids.add(cid)
            found = per_company.get(normalize(c.name), [])
            coverage.append({
                "run_id": run_id,
                "company_id": cid,
                "result": _coverage_result(found),
                "research_note": _coverage_note(found),
                "finding_sources": sorted({m.url for m in found if m.url}) or None,
                "search_coverage": coverage_note,
            })
        for i in range(0, len(coverage), _BATCH):
            db.call(
                "POST", "move_coverage",
                params={"on_conflict": "run_id,company_id"},
                body=coverage[i:i + _BATCH],
                prefer="resolution=ignore-duplicates,return=minimal",
            )

        # 6. Close the run.
        notes = (
            f"GitHub Action people-moves run. {len(coverage)} companies checked; "
            f"{len(rows)} moves found, {inserted} new to the table."
            + (f" Failed sources: {', '.join(sources_failed)}." if sources_failed else "")
        )
        db.call(
            "PATCH", "scrape_runs",
            params={"id": f"eq.{run_id}"},
            body={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "rows_found": len(rows),
                "notes": notes,
            },
        )
    except Exception as exc:
        # Leave a trace on the run row so a half-written run is obvious.
        try:
            db.call(
                "PATCH", "scrape_runs",
                params={"id": f"eq.{run_id}"},
                body={"notes": f"FAILED during write: {str(exc)[:400]}"},
            )
        except Exception:
            pass
        raise

    return {
        "run_id": run_id,
        "moves": len(rows),
        "new_moves": inserted,
        "coverage_rows": len(coverage),
        "api_calls": db.calls,
    }
