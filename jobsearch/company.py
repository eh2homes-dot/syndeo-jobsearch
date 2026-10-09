"""Reading one company for the weekly search.

A company is read from its own job board. Which board that is comes from
data/ats_map.json; when a company has no entry there (or its entry has stopped
working) the board is worked out from the company's own careers page, by
reading what that page loads. Nothing is guessed from the company's name and
no third-party listing site is used.

    read_company(lead, mapping, ...) -> Read(jobs, status, note, mapping)

status is one of
    ok                          read cleanly (possibly zero jobs)
    excluded                    marked excluded in ats_map.json
    covered by parent: <name>   the company hires through its parent's board,
                                which is on the list in its own right
    unmapped                    no board on file and page reading was off
    needs-link: <reason>        the careers link couldn't be read; fix the link
    failed:<reason>             something went wrong this run; last run's roles
                                are NOT treated as closed
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import boards as B
from .adapters import ADAPTERS, to_weekly
from .boards import Board, classify
from .http import FetchError, NotFound
from .pages import NeedsLink, PageDown, read_page, read_rendered_board

SUSPICIOUS_DROP = 5     # a company with this many open roles last run doesn't go to zero quietly...
#                         ...once. If it reads as zero again on a later run, it is believed.
_BOARD_PARAMS = ("host", "site", "wd", "board", "cc", "origin", "key")


@dataclass
class Read:
    jobs: list = field(default_factory=list)
    status: str = "ok"
    note: str = ""
    mapping: Optional[dict] = None     # set when ats_map.json should be updated for this company


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def board_of(mapping: dict) -> Board:
    return Board(system=mapping.get("ats", ""), slug=mapping.get("slug", ""),
                 params={k: mapping[k] for k in _BOARD_PARAMS if mapping.get(k)})


def _mapping(board: Board, how: str, confidence: str, today: str) -> dict:
    return {"ats": board.system, "slug": board.slug, **board.params, "confidence": confidence,
            "evidence": how[:240], "detected_on": today}


def _weekly(jobs, lead: dict, system: str = "", slug: str = "") -> B.JobList:
    """Shared-reader jobs in the weekly search's shape. Jobs read off a web page
    have no job system or slug of their own, so they take the company's."""
    company_slug = slugify(lead["company"])
    out = B.JobList()
    for j in jobs:
        if not (j.get("title") or "").strip():
            continue
        if j["system"] in ("page", "jsonld") and system:
            j = {**j, "system": system, "slug": slug}
        out.append(to_weekly(j, slug=company_slug))
    out.truncated = getattr(jobs, "truncated", "")
    return out


def from_careers_page(lead: dict, browser, today: str, url: str = "", has_history: bool = False) -> Read:
    """Work out and read the company's board from its careers link."""
    url = (url or lead.get("careers_url") or "").strip()
    board = classify(url)
    if not board.system:
        return Read(status=f"needs-link: the careers link can't be used ({board.problem})")
    try:
        if board.readable:
            jobs = ADAPTERS[board.system](board.slug, **board.params)
            note = f"cut short: {jobs.truncated}" if getattr(jobs, "truncated", "") else ""
            return Read(jobs, "ok", note,
                        _mapping(board, f"the careers link is this job board: {url}", "link", today))
        if board.rendered:
            page = read_rendered_board(board, browser)
            ident = board.slug or board.params.get("key", "")
            return Read(_weekly(page.jobs, lead, board.system, ident), "ok", page.truncated,
                        _mapping(board, f"the careers link is this job board: {url}", "link", today))
        page = read_page(url, browser=browser, label="careers page", workday_max_pages=100)
    except NeedsLink as exc:
        return Read(status=f"needs-link: {exc}")
    except PageDown as exc:
        # Didn't load this run. A bad week if we have roles from it; a link to look at if we never have.
        return Read(status=f"failed:{exc}" if has_history else f"needs-link: {exc}")
    except FetchError as exc:
        return Read(status=f"failed:{exc}")
    except Exception as exc:  # noqa: BLE001 - one company never sinks the run
        return Read(status=f"failed:{type(exc).__name__}: {str(exc)[:140]}")

    note = page.how + (f"; cut short: {page.truncated}" if page.truncated else "")
    if page.board is not None:
        b = page.board
        if b.rendered:
            return Read(_weekly(page.jobs, lead, b.system, b.slug or b.params.get("key", "")), "ok", note,
                        _mapping(b, page.how, "page", today))
        return Read(_weekly(page.jobs, lead), "ok", note, _mapping(b, page.how, "page", today))
    return Read(_weekly(page.jobs, lead), "ok", note,
                {"ats": "page", "slug": slugify(lead["company"]), "url": url, "confidence": "page",
                 "evidence": page.how[:240], "detected_on": today})


def _heal(lead: dict, mapping: dict, browser, today: str, why: str) -> Optional[Read]:
    """The board on file is gone or empty. If the company's careers page now
    loads its jobs from somewhere else, read that and remember it."""
    old = board_of(mapping)
    if classify(lead.get("careers_url", "")).identity() == old.identity():
        return None                      # the careers link IS the board on file; nothing else to look at
    fresh = from_careers_page(lead, browser, today)
    if fresh.status != "ok" or not fresh.jobs or fresh.mapping is None:
        return None
    if board_of(fresh.mapping).identity() == old.identity():
        return None                      # the page points at the same board, spelled differently
    was = f"{mapping.get('ats')}: {mapping.get('slug')}"
    now = f"{fresh.mapping.get('ats')}: {fresh.mapping.get('slug')}"
    fresh.mapping.update({"confidence": "page-moved", "moved_on": today,
                          "previous": {k: mapping[k] for k in ("ats", "slug", *_BOARD_PARAMS, "confidence")
                                       if mapping.get(k)}})
    fresh.note = f"job board changed ({was} -> {now}; {why}); " + fresh.note
    return fresh


def _zero_guard(jobs, mapping: dict, open_before: int, today: str):
    """A company with several open roles last run that now shows none is far
    more often a bad read than a company that filled everything at once. So the
    first zero is treated as a failed read (nothing closes) and remembered; if
    the next run is zero as well, it is believed and the roles close then.

    Returns (status or None, mapping to save or None)."""
    since = mapping.get("zero_since", "")
    if jobs:
        return (None, {k: v for k, v in mapping.items() if k != "zero_since"}) if since else (None, None)
    if open_before < SUSPICIOUS_DROP:
        return None, None
    if since and since < today:
        return None, {k: v for k, v in mapping.items() if k != "zero_since"}    # second zero running: real
    return (f"failed:dropped to 0 roles from {open_before} last run; treated as a read failure "
            "unless the next run agrees"), {**mapping, "zero_since": since or today}


def read_company(lead: dict, mapping: dict, *, fixtures: Optional[Path] = None, browser=None,
                 today: str = "", allow_page: bool = True, open_before: int = 0) -> Read:
    mapping = mapping or {}
    ats = mapping.get("ats", "")
    if mapping.get("confidence") == "excluded":
        return Read(status="excluded")
    if mapping.get("parent"):
        return Read(status=f"covered by parent: {mapping['parent']}",
                    note=mapping.get("evidence", ""))

    if fixtures is not None:             # offline test run: recorded answers, no network
        if not ats:
            return Read(status="unmapped")
        fx = fixtures / f"{slugify(lead['company'])}.json"
        if not fx.exists():
            return Read(status="failed:no fixture")
        raw = json.loads(fx.read_text())
        return Read(raw if isinstance(raw, list) else raw.get("jobs", []), "ok")

    if ats in ADAPTERS:
        kwargs = {k: v for k, v in mapping.items() if k not in ("ats", "slug")}
        kwargs.setdefault("careers_url", lead.get("careers_url", ""))
        jobs, gone = [], ""
        try:
            jobs = ADAPTERS[ats](mapping.get("slug", ""), **kwargs)
        except NotFound as exc:          # the board on file no longer exists
            gone = str(exc)
        except Exception as exc:  # noqa: BLE001 - a blip is a failed read: nothing closes, nothing is re-mapped
            return Read(status=f"failed:{type(exc).__name__}: {str(exc)[:140]}")
        if (gone or not jobs) and allow_page and ats in B.READERS:
            healed = _heal(lead, mapping, browser, today, gone or "the board on file lists nothing")
            if healed is not None:
                return healed
        if gone:
            return Read(status=f"failed:{gone}")
        status, save = _zero_guard(jobs, mapping, open_before, today)
        if status:
            return Read(status=status, mapping=save)
        note = f"cut short: {jobs.truncated}" if getattr(jobs, "truncated", "") else ""
        return Read(jobs, "ok", note, save)

    if ats == "page" or ats in B.RENDERED:
        url = mapping.get("url", "") if ats == "page" else board_of(mapping).listing_url
        fresh = from_careers_page(lead, browser, today, url=url, has_history=open_before > 0)
        # keep hand-written entries as they are; only record a board the page turned out to load
        if fresh.mapping is not None and (board_of(fresh.mapping).identity() == board_of(mapping).identity()
                                          or fresh.mapping.get("ats") == "page"):
            fresh.mapping = None
        if fresh.status == "ok":
            status, save = _zero_guard(fresh.jobs, mapping, open_before, today)
            if status:
                return Read(status=status, mapping=save)
            if save is not None and fresh.mapping is None:
                fresh.mapping = save
        return fresh

    # No usable board on file (never mapped, or an old Built In / unsupported entry).
    if not ats and mapping.get("confidence") == "manual":
        return Read(status="unmapped", note="on hold: " + mapping.get("evidence", "marked by hand in ats_map.json"))
    if not allow_page:
        return Read(status="unmapped", note="no job board on file; careers-page reading was off or out of time")
    if not (lead.get("careers_url") or "").strip():
        return Read(status="needs-link: no careers link on file")
    return from_careers_page(lead, browser, today, has_history=open_before > 0)
