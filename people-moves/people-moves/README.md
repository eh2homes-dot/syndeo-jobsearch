# People Moves

Weekly scan for hires, departures and promotions at the companies on the master
leads list. Runs as its own GitHub Action alongside the job search, on the same
Sunday 6am ET cadence. Output is a markdown brief for the newsletter plus JSON.

Free sources only. No paid feeds, no API keys, nothing to sign up for.

## Install

```
people-moves/           <- this folder, drop it at the repo root
.github/workflows/people-moves.yml
```

Then:

1. Export the master sheet to CSV and save it as `people-moves/config/companies.csv`.
   A starter file is committed with 13 companies so you can see the shape —
   replace it with the real export.
2. Commit. The workflow runs itself on Sunday.
3. Run the one-off backfill once (see below) so week one has a baseline.

### The companies CSV

Column headers are matched loosely, so a re-export with slightly different
headers still works. Recognised columns:

| Column | Needed for | Notes |
|---|---|---|
| `Company Name` | everything | required |
| `Leadership URL` | leadership diffing | the single highest-value column — see below |
| `Ticker` or `CIK` | EDGAR | public companies only |
| `Careers Page URL` | links in the brief | |
| `Aliases` | news and trade press matching | semicolon-separated |

**The leadership URL is the column worth your time.** Most of the master list is
private and will never file with the SEC or issue a press release. Their team
page is the only place a new hire shows up. Every row you fill in is a company
that starts producing signal; every row you leave blank is one that never will.

## Sources

| Source | Covers | Confidence | Notes |
|---|---|---|---|
| `edgar` | ~25 public companies | Confirmed | 8-K Item 5.02. Officers and directors only — a new VP of Sales won't appear. |
| `formd` | private companies that have raised | Confirmed / Probable | Funding rounds *and* the officer roster named on the filing. Covers the part of the list EDGAR otherwise misses. |
| `warn` | any company filing layoffs | Confirmed / Probable | Mass-layoff notices. Candidate supply, not a people move. |
| `leadership` | any company with a team page | Probable | Weekly snapshot and diff. The workhorse. |
| `news` | all companies | Confirmed / Probable | Google News RSS, one query per company, verb-filtered. |
| `trade` | all companies | Confirmed / Probable | Publisher feeds in `config/feeds.yml`, filtered against the list. |
| `reqs` | companies on the job board | Inferred | Derived from the job-search output. Describes companies, names nobody. |

### Two sections, not one

The brief splits into **Company moves** (funding rounds, layoff notices) and
**People moves** (named individuals). They are different newsletter content and
carry different risk, so they never get mixed.

A Form D round is a hiring wave 60-90 days out — worth watching before the reqs
appear. A WARN notice is the opposite signal: people about to become available,
which for placement work is often worth more than a job posting.

### Confidence tiers

The brief is grouped by tier because that is the editorial decision:

- **Confirmed** — filed or announced by the company. Name the person.
- **Probable** — a team page changed, or a headline implies it. Verify on
  LinkedIn first.
- **Inferred** — a req closed or a cluster opened. Describe the company, name
  nobody.

Never promote a tier because an item looks convincing. These are people Evan
knows and may place; a wrong name costs more than the item was worth.

## Usage

```bash
cd people-moves
pip install -r requirements.txt

python run.py                             # weekly run, all sources
python run.py --sources leadership -v     # one source, verbose
python run.py --sources formd,warn        # just the new company-move sources
python run.py --lookback-days 14          # wider news window
python run.py --limit 10                  # first 10 companies, for testing
python run.py --check-feeds               # validate trade press feed URLs
python run.py --backfill                  # one-off Wayback baseline (slow)
```

### Run the backfill once, first

Leadership diffing needs two snapshots before it produces anything, so a cold
start reports nothing for a month. `--backfill` pulls an archived copy of each
team page from ~120 days ago via the Wayback Machine, diffs it against today,
and seeds the baseline in one pass.

It is throttled to one request per second against archive.org, so budget 20-40
minutes for the full list. Run it from the Actions tab (workflow_dispatch →
backfill: true) and forget about it.

## Two gotchas worth knowing

**WARN company matching is deliberately strict.** Legal entity names collide.
The archive contains many notices from "Compass Group USA" — a food service
company with no relationship to Compass the brokerage. Publishing that as a
Compass layoff is the worst error this pipeline can make, so `warn` matches on
strict equality against the company name and its aliases, never on substrings
or prefixes. Near misses are logged rather than reported. If one is genuinely
the same company, add its legal name to that company's `Aliases` column and it
will match next week.

**The first WARN run would otherwise dump the whole archive.** Every row's
"first seen" date is the day the dataset was first fetched, so on run one all
61,000 notices look new. `max_notice_age_days` in `config/warn.yml` (default 90)
drops anything filed longer ago than that, however recently it landed.

**Attribution.** The default WARN dataset is CC BY 4.0 and asks for credit to
"WARN Feed". The credit line is rendered into every brief so it does not get
lost — carry it into the newsletter if you publish a notice from it.

## Two integration points to check

**1. The job-search output path.** The `reqs` source reads whatever your job
search writes. Set `JOBS_RESULTS_PATH` in the workflow to the real path. The
loader accepts a list of role dicts or an object with a `roles` / `jobs` /
`results` key, and looks for company and title fields under several common
names. If your shapes differ, `_load_roles()` in `sources/req_signals.py` is the
one function to edit. Until it is pointed somewhere real, that source quietly
returns nothing and the other four still run.

**2. State is committed back to the repo.** Runners are ephemeral, so snapshots
live in `people-moves/state/` and the workflow pushes them after each run. That
also gives you a free audit trail: `git log -p people-moves/state/leadership/evernest.json`
shows every change to that company's team page over time. If your repo protects
the default branch, the push will fail — either exempt the bot or switch the
workflow to `actions/cache`.

## Maintenance

- **Feed URLs rot.** Run `python run.py --check-feeds` after any edit to
  `config/feeds.yml`, and once a quarter regardless. Anything reported DEAD
  should be found again or disabled rather than left to fail silently.
- **JavaScript-rendered team pages parse as empty.** The leadership source skips
  any page yielding fewer than 2 people and logs a warning. Those companies need
  a different URL, or accept that news and trade press are their only coverage.
- **Page redesigns look like mass turnover.** If more than 60% of a page's names
  change at once, the run treats it as a redesign, re-baselines, and reports
  nothing. Tune `MAX_CHURN_RATIO` in `sources/leadership.py` if that fires too
  often or not often enough.

## What this does not do

It does not touch LinkedIn. Job-change alerts sit behind Sales Navigator, and
scraping profiles violates their terms and gets addresses blocked. LinkedIn is
the verification step at the end — confirm the move before it goes in the
newsletter — not a discovery source.

Nothing in the brief is publication-ready as written. Every item carries its
source link precisely so the manual verification step has something to click.
