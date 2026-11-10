"""Renders collected moves into a newsletter-ready markdown brief plus JSON.

The markdown is organised by confidence tier, because that is the decision the
editor makes: confirmed and probable items can name a person, inferred items
cannot. Every item carries its source link so the manual verification step has
something to click.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from lib.move import (
    Move, CONFIRMED, PROBABLE, INFERRED, DEPARTURE, FUNDING, LAYOFF,
    PEOPLE, COMPANY,
)

TIER_HEADINGS = {
    CONFIRMED: (
        "Confirmed",
        "Filed or announced by the company. Safe to name the person.",
    ),
    PROBABLE: (
        "Probable",
        "A team page changed or a headline implies it. Verify on LinkedIn "
        "before naming anyone.",
    ),
    INFERRED: (
        "Inferred",
        "Derived from job board activity. Describe the company, name nobody.",
    ),
}


KIND_MARKER = {FUNDING: "\u25b2", LAYOFF: "\u25bc", DEPARTURE: "\u2198"}


def _dedupe(moves: list[Move]) -> list[Move]:
    """Collapse the same person-at-company reported by several sources."""
    best: dict[tuple, Move] = {}
    for m in moves:
        key = (
            m.company.lower(),
            (m.person or m.summary[:60]).lower(),
            m.kind,
        )
        existing = best.get(key)
        if existing is None or m.sort_key < existing.sort_key:
            if existing is not None:
                m.summary = m.summary or existing.summary
            best[key] = m
    return sorted(best.values(), key=lambda m: m.sort_key)


def render_markdown(moves: list[Move], run_date: str, top_n: int = 0) -> str:
    moves = _dedupe(moves)
    if top_n:
        moves = moves[:top_n]

    people = [m for m in moves if m.category == PEOPLE]
    company = [m for m in moves if m.category == COMPANY]

    by_tier: dict[str, list[Move]] = defaultdict(list)
    for m in people:
        by_tier[m.confidence].append(m)

    lines = [
        f"# People Moves — week of {run_date}",
        "",
        f"{len(people)} people move{'s' if len(people) != 1 else ''} and "
        f"{len(company)} company event{'s' if len(company) != 1 else ''} across "
        f"{len({m.company for m in moves})} companies.",
        "",
        "> Every item below needs its link opened and checked before it goes in "
        "the newsletter. Nothing here is publication-ready as written.",
        "",
    ]

    if company:
        lines += [
            f"## Company moves ({len(company)})",
            "",
            "*Funding rounds and layoff notices. A round is a hiring wave 60-90 "
            "days out; a WARN notice is candidate supply.*",
            "",
        ]
        for m in sorted(company, key=lambda x: x.sort_key):
            marker = KIND_MARKER.get(m.kind, "\u2022")
            lines.append(f"- {marker} {m.display()}")
            meta = m.source + (f", {m.published}" if m.published else "")
            lines.append(f"  {f'[{meta}]({m.url})' if m.url else f'*{meta}*'}")
        lines.append("")

    if people:
        lines += [f"## People moves ({len(people)})", ""]

    for tier in (CONFIRMED, PROBABLE, INFERRED):
        items = by_tier.get(tier)
        if not items:
            continue
        heading, guidance = TIER_HEADINGS[tier]
        lines += [f"### {heading} ({len(items)})", "", f"*{guidance}*", ""]

        for m in items:
            marker = "↘" if m.kind == DEPARTURE else "↗"
            lines.append(f"- {marker} {m.display()}")
            detail = []
            if m.summary and m.summary != m.display():
                detail.append(m.summary.strip())
            meta = m.source + (f", {m.published}" if m.published else "")
            detail.append(f"*{meta}*" if not m.url else f"[{meta}]({m.url})")
            lines.append(f"  {' — '.join(detail)}")
        lines.append("")

    if not moves:
        lines += [
            "No moves detected this week.",
            "",
            "If this repeats, check that the leadership pages are parsing "
            "(`python run.py --sources leadership -v`) and that the trade feeds "
            "are alive (`python run.py --check-feeds`).",
            "",
        ]

    return "\n".join(lines)


def write(moves: list[Move], out_dir: str | Path = "out", top_n: int = 0) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = date.today().isoformat()

    md_path = out_dir / f"people-moves-{run_date}.md"
    json_path = out_dir / f"people-moves-{run_date}.json"
    latest = out_dir / "people-moves-latest.md"

    markdown = render_markdown(moves, run_date, top_n=top_n)
    md_path.write_text(markdown, encoding="utf-8")
    latest.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "run_date": run_date,
                "count": len(moves),
                "moves": [m.to_dict() for m in _dedupe(moves)],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return md_path, json_path
