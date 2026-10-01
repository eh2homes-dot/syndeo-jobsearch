"""Writes a finished people-moves run to Airtable.

Mirrors lib/supabase_sink.py so the two can run side by side on the same data:
same dedupe key, same row contents, same coverage logic. The difference is the
API: Airtable takes at most 10 records per write and 5 requests per second, so
a run costs roughly 20-30 calls instead of 6.

Call budget: a weekly run against ~150 companies uses about 25 calls (15 of
them are the coverage rows). The Free plan allows 1,000 calls a month per
workspace, so weekly runs fit; daily runs would not.

Needs two environment variables, stored as GitHub repo secrets:
  AIRTABLE_TOKEN    personal access token with data.records:read and
                    data.records:write on the base
  AIRTABLE_BASE_ID  appnWM0QuCZS3xgYN  ("Syndeo Job Search (Trial)")
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from lib.companies import Company, normalize
from lib.move import Move
from lib.supabase_sink import _dedupe, _move_row, _coverage_result, _coverage_note

log = logging.getLogger(__name__)

API = "https://api.airtable.com/v0"
T_RUNS = "Scrape Runs"
T_COMPANIES = "Companies"
T_MOVES = "People Moves"
T_COVERAGE = "Move Coverage"

_MIN_GAP = 0.22  # seconds between calls: stays under 5 requests/second


class AirtableError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE_ID"))


class _Client:
    def __init__(self) -> None:
        self.base = f"{API}/{os.environ['AIRTABLE_BASE_ID'].strip()}"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN'].strip()}",
            "Content-Type": "application/json",
        })
        self.calls = 0
        self._last = 0.0

    def call(self, method: str, table: str, *, params=None, body=None) -> dict:
        for attempt in range(1, 4):
            gap = time.time() - self._last
            if gap < _MIN_GAP:
                time.sleep(_MIN_GAP - gap)
            self._last = time.time()
            self.calls += 1
            resp = self.session.request(
                method, f"{self.base}/{requests.utils.quote(table)}",
                params=params, json=body, timeout=60,
            )
            if resp.status_code == 429:
                # Airtable asks clients to wait 30 seconds after a 429.
                log.warning("airtable: rate limited, waiting 30s (attempt %d)", attempt)
                time.sleep(30)
                continue
            if resp.status_code >= 300:
                raise AirtableError(f"{method} {table} -> HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.content else {}
        raise AirtableError(f"{method} {table}: still rate limited after 3 attempts")

    def list_all(self, table: str, params: dict) -> list[dict]:
        out, offset = [], None
        while True:
            p = dict(params, pageSize=100)
            if offset:
                p["offset"] = offset
            page = self.call("GET", table, params=p)
            out += page.get("records", [])
            offset = page.get("offset")
            if not offset:
                return out

    def create(self, table: str, field_rows: list[dict]) -> list[dict]:
        created = []
        for i in range(0, len(field_rows), 10):
            chunk = [{"fields": f} for f in field_rows[i:i + 10]]
            got = self.call("POST", table, body={"records": chunk, "typecast": True})
            created += got.get("records", [])
        return created


def _clean(fields: dict) -> dict:
    """Drop empty values: Airtable rejects null for some field types."""
    return {k: v for k, v in fields.items() if v not in (None, "", [])}


def _quote(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


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
    now = datetime.now(timezone.utc).isoformat()

    # 1. Open the run.
    run = db.create(T_RUNS, [_clean({
        "Workflow": "recently_hired",
        "Started": now,
        "Period Start": (today - timedelta(days=lookback_days)).isoformat(),
        "Period End": today.isoformat(),
        "Notes": "GitHub Action people-moves run",
    })])[0]
    run_id = run["id"]
    run_no = run.get("fields", {}).get("Run", run_id)
    log.info("airtable: opened run %s", run_no)

    try:
        # 2-3. Match CSV companies to Airtable companies; add missing ones.
        existing = db.list_all(T_COMPANIES, {"fields[]": "Name"})
        by_norm = {normalize(r["fields"].get("Name", "")): r["id"] for r in existing if r["fields"].get("Name")}
        missing = [c for c in companies if normalize(c.name) not in by_norm]
        if missing:
            added = db.create(T_COMPANIES, [
                _clean({"Name": c.name, "Careers URL": c.careers_url or None, "Active": True})
                for c in missing
            ])
            for r in added:
                by_norm[normalize(r["fields"].get("Name", ""))] = r["id"]
            log.info("airtable: added %d new companies", len(added))

        # 4. Moves: skip keys already in the table (first sighting wins).
        deduped = _dedupe(moves)
        keys = [k for k, *_ in deduped]
        already: set[str] = set()
        for i in range(0, len(keys), 40):
            formula = "OR(" + ",".join(f"{{Move Key}}={_quote(k)}" for k in keys[i:i + 40]) + ")"
            for r in db.list_all(T_MOVES, {"fields[]": "Move Key", "filterByFormula": formula}):
                already.add(r["fields"].get("Move Key"))

        new_rows = []
        for k, m, names, urls in deduped:
            if k in already:
                continue
            row = _move_row(k, m, names, urls, run_id=0, company_id=None)
            cid = by_norm.get(normalize(m.company))
            new_rows.append(_clean({
                "Person": row["person_name"],
                "New Title": row["new_title"],
                "Previous Title": row["previous_title"],
                "Previous Company": row["previous_company"],
                "New Company": row["new_company"],
                "Company": [cid] if cid else None,
                "Change Type": row["change_type"],
                "Category": row["category"],
                "Kind": row["kind"],
                "Confidence": row["confidence"],
                "Source": row["source"],
                "Source URL": m.url or (row["sources"] or [None])[0],
                "Sources": "\n".join(row["sources"] or []),
                "Announcement Date": row["announcement_date"],
                "Date Evidence": row["date_evidence"],
                "Summary": row["summary"],
                "Score": row["score"],
                "Move Key": k,
                "Detected At": now,
                "Run": [run_id],
            }))
        db.create(T_MOVES, new_rows)

        # 5. Coverage: one row per company.
        per_company: dict[str, list[Move]] = {}
        for _, m, _, _ in deduped:
            per_company.setdefault(normalize(m.company), []).append(m)
        coverage_note = (
            f"Sources run: {', '.join(sources_ok) or 'none'}"
            + (f"; failed: {', '.join(sources_failed)}" if sources_failed else "")
            + f"; lookback {lookback_days} days"
        )
        cov_rows, seen = [], set()
        for c in companies:
            cid = by_norm.get(normalize(c.name))
            if not cid or cid in seen:
                continue
            seen.add(cid)
            found = per_company.get(normalize(c.name), [])
            cov_rows.append(_clean({
                "Key": f"Run {run_no} / {c.name}",
                "Run": [run_id],
                "Company": [cid],
                "Result": _coverage_result(found),
                "Research Note": _coverage_note(found),
                "Finding Sources": "\n".join(sorted({m.url for m in found if m.url})),
                "Search Coverage": coverage_note,
            }))
        db.create(T_COVERAGE, cov_rows)

        # 6. Close the run.
        notes = (
            f"GitHub Action people-moves run. {len(cov_rows)} companies checked; "
            f"{len(deduped)} moves found, {len(new_rows)} new to the table. "
            f"{db.calls + 1} Airtable API calls."
            + (f" Failed sources: {', '.join(sources_failed)}." if sources_failed else "")
        )
        db.call("PATCH", T_RUNS, body={"records": [{"id": run_id, "fields": {
            "Finished": datetime.now(timezone.utc).isoformat(),
            "Rows Found": len(deduped),
            "Notes": notes,
        }}]})
    except Exception as exc:
        try:
            db.call("PATCH", T_RUNS, body={"records": [{"id": run_id, "fields": {
                "Notes": f"FAILED during write: {str(exc)[:400]}"}}]})
        except Exception:
            pass
        raise

    return {
        "run_id": run_no,
        "moves": len(deduped),
        "new_moves": len(new_rows),
        "coverage_rows": len(cov_rows),
        "api_calls": db.calls,
    }
