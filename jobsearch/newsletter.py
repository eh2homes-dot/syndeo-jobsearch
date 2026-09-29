"""Assemble this week's newsletter draft from the two automated sections.

    python -m jobsearch.newsletter                 # after jobsearch.run and jobsearch.hires
    python -m jobsearch.newsletter --date 2026-09-27

Reads the most recent drafts from the last 7 days:
    output/newsletter/now-hiring-YYYY-MM-DD.md      (weekly job search)
    output/newsletter/people-moves-YYYY-MM-DD.md    (recently hired)
    output/newsletter/events-YYYY-MM-DD.md          (weekly events)
and writes one combined issue:
    output/newsletter/issue-YYYY-MM-DD.md           (also used as the GitHub issue body, which GitHub emails)
    output/newsletter/issue-YYYY-MM-DD.html         (open in a browser, copy, paste into beehiiv)
It also shows the combined draft on the GitHub run page.

This is the automated, structured draft. The voice-written version of an issue is still done by hand.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from .run import OUT, ROOT, draft_html

NL = OUT / "newsletter"
MAX_ISSUE_CHARS = 60000  # GitHub issue bodies are capped at 65,536 characters


def latest(prefix: str, today: dt.date) -> Path | None:
    best = None
    for p in NL.glob(f"{prefix}-20??-??-??.md"):
        try:
            day = dt.date.fromisoformat(p.stem[len(prefix) + 1:])
        except ValueError:
            continue
        if 0 <= (today - day).days <= 7 and (best is None or day > best[0]):
            best = (day, p)
    return best[1] if best else None


def build(today: dt.date) -> tuple[str, list[str]]:
    """Returns (markdown, notes about what was missing)."""
    parts, notes = [], []
    people = latest("people-moves", today)
    jobs = latest("now-hiring", today)
    if people:
        parts.append(people.read_text().strip())
    else:
        notes.append("No People on the move draft from the last 7 days (did Recently hired run?).")
    if jobs:
        parts.append(jobs.read_text().strip())
    else:
        notes.append("No Now hiring draft from the last 7 days (did the weekly job search run as a full run?).")
    events = latest("events", today)
    if events:
        parts.append(events.read_text().strip())
    else:
        notes.append("No Upcoming events draft from the last 7 days (did Weekly events run?).")
    week = today.strftime("%B %-d, %Y")
    md = [f"# Newsletter draft: week of {week}", ""]
    if notes:
        md += ["> " + n for n in notes] + [""]
    md.append("\n\n---\n\n".join(parts) if parts else "_Nothing to report this week._")
    return "\n".join(md).strip() + "\n", notes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--mention", default=os.environ.get("NEWSLETTER_MENTION", ""),
                    help="GitHub username to @mention in the issue so GitHub emails it (e.g. eh2homes-dot)")
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    NL.mkdir(parents=True, exist_ok=True)

    md, notes = build(today)
    md_path = NL / f"issue-{today.isoformat()}.md"
    html_path = md_path.with_suffix(".html")
    md_path.write_text(md)
    html_path.write_text(draft_html(md, f"Newsletter draft - {today.isoformat()}"))

    # GitHub issue body: a short how-to on top, then the draft. The @mention makes GitHub email it.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    html_rel = html_path.relative_to(ROOT).as_posix()
    how = [f"{'@' + args.mention + ' ' if args.mention else ''}This week's newsletter draft is ready.", "",
           "**To paste into beehiiv:** copy it from this page (the links come with it), or download "
           + (f"[`{html_rel}`](https://github.com/{repo}/blob/main/{html_rel}) (use the Download raw file button), "
              if repo else f"`{html_rel}`, ")
           + "open it in a browser, select all, copy, paste.", "", "---", ""]
    body = "\n".join(how) + "\n" + md
    if len(body) > MAX_ISSUE_CHARS:
        body = body[:MAX_ISSUE_CHARS] + "\n\n_(Cut short to fit GitHub's limit. Full draft: " + md_path.name + ")_\n"
    (NL / f"issue-{today.isoformat()}.github.md").write_text(body)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"# Newsletter draft ready\n\nPaste-ready file: `{html_rel}`\n\n---\n\n{md}")
    print(f"  issue   {md_path.relative_to(ROOT)} (+ .html)" + (f" | missing: {' '.join(notes)}" if notes else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
