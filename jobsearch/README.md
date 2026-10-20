# Job search

Two weekly searches share one set of readers.

| | Weekly search | OpCo search |
|---|---|---|
| Run with | `python -m jobsearch.run --detect` | `python opco_search.py` |
| Company list | `leads/master_leads.csv` | Column D of the sheet's OpCo tab |
| Which job board | `data/ats_map.json` | whatever Column D is |
| Remembers | `data/history.json` | `state/opco/baseline.json` |
| Writes | `output/<date>/`, `output/newsletter/` | `out/opco-jobs-*.md/.csv/.json` |

## How a company is read

Every company is read from **its own job board**. No company is looked up by
name, and listing sites (Built In, LinkedIn job pages, Glassdoor, Indeed...)
are never used as a source.

1. **A job-board link** is read with that job system's reader.
2. **A careers page** is read in this order, stopping at the first that works:
   1. the job board the page loads, embeds or links to (most careers pages are
      a marketing page with a job board behind them);
   2. structured job data on the page;
   3. job links written on the page, following its "next page" links;
   4. the same in a headless browser, for pages that only show jobs after their
      scripts run. The browser also clicks "Load more" / "Next" until the list
      stops growing.
   A page that shows only a few featured jobs and links to its full list
   ("View all jobs") has that list read instead.
3. Whatever was worked out is saved (weekly: in `data/ats_map.json`; OpCo: the
   brief lists the board's link so it can be pasted into Column D).

### Job systems

Read from their data feed: Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee, Breezy, BambooHR, Rippling, Workday, UKG, ADP, Dayforce, Jobvite,
Paylocity, iCIMS, Comeet.

Read by opening the board in the headless browser: Paycom, isolved,
ApplicantPro, Gem, JazzHR, Teamtailor, TriNet Hire, Pinpoint.

Recognised but not readable yet (reported by name): Paradox, Taleo,
SuccessFactors, Paycor, Hireology, Apploi.

### Pagination

| Source | How all pages are read |
|---|---|
| Greenhouse, Lever, Ashby, Workable, BambooHR, Breezy, Recruitee, Rippling, Paylocity | one answer holds every posting |
| Workday | 20 per request until the total is reached. Capped at 2,000 (weekly) / 1,200 per search (OpCo); a cut-short read says so |
| SmartRecruiters, UKG, ADP, Dayforce | page after page until the reported total |
| iCIMS, Jobvite | page after page until a page adds nothing |
| Careers page, plain | follows "next" links, up to 15 pages |
| Careers page, in the browser | clicks "Load more" / "Next", up to 20 times |

## When something isn't read

Each run says why, per company (`company_status.csv` / the brief's "Needs your
attention"):

- **needs-link** — the link on file can't be read. Fix: open the company's
  careers page, click any job, and use the address it lands on (without the
  part naming the one job). Weekly: put it in `data/ats_map.json`, or fix
  `careers_url` in the leads file. OpCo: paste it into Column D.
- **failed** — something went wrong this run (site down, rate limit). Last
  run's roles are kept and nothing is reported closed.
- **covered by parent** — the company hires through its parent's board, which
  is on the list itself (`{"parent": "RealPage"}` in `ats_map.json`).

"Not hiring" is only ever concluded from a positive sign: a job board that
answers with an empty list, or a page that says nothing is open. A page where
no jobs could be found is a link to fix, never a company that stopped hiring.

A company that had 5+ open roles and suddenly shows none is treated as a
failed read the first time; if the next run agrees, it is believed.

## New and closed

A role is **new** the first run it appears, and **closed** the first run it is
gone from a company that was read cleanly.

What a job system's own feed no longer lists has closed, whether or not the
posting's page still loads: most job systems answer a closed posting's address
with a normal-looking page. Only where the list itself may be incomplete (a
careers page, LinkedIn, a VC board, a read that was cut short) does a posting
that still loads keep the role open, as a "scraper miss". Workday closures are
each checked against Workday's own data.

Two cases are deliberately *not* news:

- **First read of a company from a job system** (newly covered, or its board
  changed). Its roles were already open; only ones the board dates within the
  last 7 days count as new.
- **A company that changed job systems.** Its roles under the old system are
  retired quietly rather than reported as closed.

## `data/ats_map.json`

```json
"Kasa":      {"ats": "greenhouse", "slug": "kasa"},
"Zillow":    {"ats": "workday", "slug": "zillow", "wd": "wd5", "site": "Zillow_Group_External"},
"Yardi":     {"ats": "page", "url": "https://careers.yardi.com/openings/"},
"Mynd":      {"parent": "Roofstock"},
"GreenLite": {"ats": "", "confidence": "excluded"}
```

A company with no entry is read from its `careers_url`, and the result is
saved here. An entry whose board has gone quiet or missing is re-checked
against the careers page; if the page now loads a different board, that board
is read and saved, with the old one kept under `previous`.

One-time changes to this file that ship with a code change live in
`jobsearch/migrations/*.json` and are applied once by the next run (recorded in
`data/migrations_applied.json`). That keeps a code branch from colliding with
the weekly run's own commit of this file.

## Code

| File | What it holds |
|---|---|
| `boards.py` | one reader per job system; `classify(url)`; the history key formats |
| `pages.py` | reading a careers page (the steps above) |
| `browser.py` | the headless browser (Playwright; optional) |
| `company.py` | weekly search: reading one company, replacing a dead board |
| `adapters.py` | weekly search's view of the readers, plus `generic` and `linkedin` |
| `run.py` | weekly search: history, new/closed, reports, newsletter draft |
| `../opco_search.py` | OpCo search |
| `http.py` | polite HTTP client, throttled per site |

## Tests

```
pip install -r requirements.txt pytest
python -m playwright install chromium     # optional; browser tests skip without it
python -m pytest -q tests
```

Tests are offline: job systems answer from `tests/fixtures/readers/`, careers
pages are served from `tests/fixtures/pages/`. `test_history_keys_are_unchanged`
pins the key formats to what both searches stored before the readers were
merged; if it fails, tracked roles would look closed-and-reopened.

The **Tests** workflow runs this on every branch and pull request. Its "Live
check" job runs both searches for real without saving anything, and attaches
what they found.

## Founder-led hiring (off by default)

The small-company slice of what the two searches already found, drafted in the
founder-item format for the founder newsletter. No scraping of its own.

```
python -m jobsearch.founders --force          # try it without flipping the toggle
python -m jobsearch.founders --days 14 --max-headcount 75 --force
python -m jobsearch.founders --record         # log the picked companies for the cooldown
```

| | |
|---|---|
| Reads | `data/history.json`, `out/opco-jobs-latest.json`, `leads/founders.csv` |
| Rule | a role first seen in the last `window_days` at a company with `headcount ≤ max_headcount`, or `founder_led = TRUE`, is founder-led |
| Writes | `output/founders/founder-roles-<date>.md` (draft), `.csv` / `.json` (every eligible role, scored), `needs-review-<date>.csv` |
| Cooldown | `data/founder_featured.json`, its own ledger — Origins' `featured_companies.json` is untouched |
| Toggles | `config.json → founder_hiring.enabled` (the script) and the `FOUNDER_HIRING` repo variable (the Sunday schedule). Manual runs from the Actions tab pass `--force`. |

**`leads/founders.csv` is the only source of headcount, stage and founder details.**
It is seeded with every company name from the master list and the OpCo tab, all
blank. A company with fresh roles and no headcount on file is listed under "Needs
review" in the draft rather than guessed at; fill in `headcount` (or set
`founder_led` to TRUE/FALSE) and it is in or out next run. Anything still blank
when an item is drafted shows up as `[VERIFY …]`, never as a value.

Columns: `company, headcount, stage, founder_name, founder_linkedin, founder_x, founder_led, notes`.
`stage` places the item in a bucket (pre-seed/seed → Build -1→0; Series A–C → 0→1;
growth/established/operator → 1→beyond).

### VC portfolio companies

`python -m jobsearch.vc_companies` (or the "VC portfolio companies" workflow) lists every
company on the Getro boards in `config.json → vc_boards` into `leads/vc_portfolio_companies.csv`
with headcount bucket, stage and tags. The founder-led draft reads `leads/founders.csv`, which
carries those companies with a `domain` column, so VC-board roles in `worth_adding.csv` match
them even though they aren't on the master list. Getro's API needs `Accept: application/json`
or it answers 406; both the jobs and companies readers send it.
