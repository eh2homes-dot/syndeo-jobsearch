# People Moves: built, but not switched on

These files were written for the People Moves scan and are **not run by
anything**. They sat in older copies of the module under `people-moves/`, which
the workflow stopped using when the scan moved to the repo root. They are kept
here so they aren't lost; nothing imports this folder.

| File | What it is | Was hooked in by |
|---|---|---|
| `sources/warn.py`, `config/warn.yml` | WARN Act layoff notices, from a 48-state dataset (CC BY 4.0, credit "WARN Feed"). | `reference/run_2026-09-30_with_form_d_and_warn.py` (source name `warn`); the credit line is in `reference/report_2026-09-30_with_warn_credit.py` |
| `lib/airtable_sink.py` | Writes each run's moves and coverage to Airtable. | `reference/run_2026-10-02_with_database_sinks.py` |
| `lib/supabase_sink.py` | The same, to Supabase. | same file |

## What the weekly run does today

`run.py` at the repo root runs six sources: `edgar`, `formd`, `leadership`,
`news`, `trade`, `reqs`. (`formd`, SEC Form D, used to sit in this folder; it
was switched on in November 2026 and now lives in `sources/form_d.py`.) The
run writes the markdown and JSON brief to `out/` and nothing else. The workflow still passes the Airtable and Supabase secrets, but the
live `run.py` has no code that uses them, so no People Moves run writes to
either database.

## To switch one on

1. Move the file back beside its siblings (`sources/` or `lib/`, and
   `config/warn.yml` into `config/`).
2. Copy its hook from the reference runner into the root `run.py`: a source is
   one `elif name == ...` branch plus its name in `ALL_SOURCES`; the database
   writers are the block after the brief is written.
3. These were written against the September/October versions of `lib/`. Run
   the tests and a manual workflow run before trusting the output.

The reference files are old whole copies of `run.py` and `report.py`. They are
here to copy from, not to run.
