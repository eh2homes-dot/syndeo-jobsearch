"""Inference signals read out of the existing job-search output.

This is the only source that depends on your other workflow. It reads the
job-search results file, compares this week against last week, and derives:

  - roles that closed while the company kept hiring  -> likely filled
  - a cluster of new reqs under one function         -> a leader probably
                                                        started and is building
                                                        a team

Everything here is INFERRED. It describes companies, never people. A closed
req does not tell you who filled it, and guessing is how a community
newsletter gets a name wrong.

INTEGRATION POINT
-----------------
Set JOBS_RESULTS_PATH (env var or --jobs-file) to whatever the job-search
workflow writes. The loader accepts either a list of role dicts or an object
with a "roles"/"jobs"/"results" key, and looks for company/title fields under
several common names. If your shapes differ, _load_roles() is the one function
to edit.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from lib.move import Move, INFERRED, ARRIVAL
from lib.people import PRIORITY_FUNCTIONS
from lib.state import STATE_DIR, _read_json, _write_json

log = logging.getLogger(__name__)

PREVIOUS_PATH = STATE_DIR / "previous_reqs.json"

_COMPANY_KEYS = ("company", "company_name", "employer", "org", "organization")
_TITLE_KEYS = ("title", "role", "job_title", "position", "name")

CLUSTER_THRESHOLD = 3  # new reqs in one function that imply a new leader


def _first_key(row: dict, keys) -> str:
    for k in keys:
        val = row.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _load_roles(path: Path) -> dict[str, set[str]]:
    """Return {company: {role title, ...}} from the job-search output."""
    if not path.exists():
        log.info("no job-search output at %s; skipping inference signals", path)
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}

    if isinstance(data, dict):
        for key in ("roles", "jobs", "results", "postings", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        log.warning("%s was not a list of roles; skipping", path)
        return {}

    out: dict[str, set[str]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        company = _first_key(row, _COMPANY_KEYS)
        title = _first_key(row, _TITLE_KEYS)
        if company and title:
            out.setdefault(company, set()).add(title)

    log.info("loaded %d companies / %d roles from job-search output",
             len(out), sum(len(v) for v in out.values()))
    return out


def _function_of(title: str) -> str | None:
    m = PRIORITY_FUNCTIONS.search(title or "")
    return m.group(0).lower() if m else None


def collect(companies, jobs_file: str | None = None, seen=None) -> list[Move]:
    path = Path(jobs_file or os.environ.get("JOBS_RESULTS_PATH", "out/jobs-latest.json"))
    current = _load_roles(path)
    if not current:
        return []

    previous = {k: set(v) for k, v in _read_json(PREVIOUS_PATH, {}).items()}
    known = {c.name.lower(): c for c in companies}
    moves: list[Move] = []

    if previous:
        for company_name, prev_titles in previous.items():
            now_titles = current.get(company_name, set())
            closed = prev_titles - now_titles

            # No reqs at all now: a freeze or a scrape failure, not a hire.
            if not now_titles:
                continue
            if not closed:
                continue

            display = known.get(company_name.lower())
            label = display.name if display else company_name
            senior = [t for t in closed if PRIORITY_FUNCTIONS.search(t)]
            headline = sorted(senior or closed)[:3]

            moves.append(
                Move(
                    company=label,
                    source="Job board (req closed)",
                    confidence=INFERRED,
                    kind=ARRIVAL,
                    summary=(
                        f"{label} closed {len(closed)} role"
                        f"{'s' if len(closed) != 1 else ''} while still hiring "
                        f"({len(now_titles)} open): {', '.join(headline)}"
                        f"{'…' if len(closed) > 3 else ''}. Likely filled."
                    ),
                    url=display.careers_url if display else "",
                    score=12 + min(len(closed), 5) * 2,
                )
            )

            # Cluster signal: several new reqs in one function at once.
            new_titles = now_titles - prev_titles
            by_function: dict[str, int] = {}
            for t in new_titles:
                fn = _function_of(t)
                if fn:
                    by_function[fn] = by_function.get(fn, 0) + 1
            for fn, count in by_function.items():
                if count >= CLUSTER_THRESHOLD:
                    moves.append(
                        Move(
                            company=label,
                            source="Job board (req cluster)",
                            confidence=INFERRED,
                            kind=ARRIVAL,
                            summary=(
                                f"{label} opened {count} new {fn} roles this week. "
                                f"A new {fn} leader has often already started when "
                                f"this happens — worth a look."
                            ),
                            url=display.careers_url if display else "",
                            score=18,
                        )
                    )

    _write_json(PREVIOUS_PATH, {k: sorted(v) for k, v in current.items()})
    log.info("Req signals: %d moves", len(moves))
    return moves
