# syndeo-jobsearch

Weekly automations behind the newsletter and the talent data: two job searches,
people moves, events, and the founder-hiring pass. Everything runs as a GitHub
Action and commits its results back to this repo. Free sources only.

## What runs, and when

Times are Eastern. Every workflow can also be started by hand from the Actions tab.

| Workflow | When | Runs | Reads | Writes |
|---|---|---|---|---|
| **Weekly job search** (proptech) | Sun 4am | `python -m jobsearch.run --detect` | `leads/master_leads.csv` | `output/<date>/`, `output/newsletter/`, `data/` |
| **Weekly events** | Sun 5am | `python -m jobsearch.events` | sources in `data/event_sources.json` | `output/`, `data/` |
| **OpCo Job Search** (property operations) | Sun 6am, with two backup slots | `python opco_search.py` | Column D of the sheet's OpCo tab | `out/opco-jobs-*`, `state/opco/` |
| **People Moves** | Sun 6am, with two backup slots | `python run.py` | both company lists, plus `config/companies.csv` | `out/people-moves-*`, `state/` |
| **Founder hiring** | Sun late morning, after both searches | `python -m jobsearch.founders` | `leads/founders.csv` and both searches' results | `output/founders/` |
| **VC portfolio companies** | by hand | `python -m jobsearch.vc_companies` | VC job boards in `config.json` | `leads/vc_portfolio_companies.csv` |
| **NARPM affiliates scrape** | by hand | `python narpm_affiliates.py` | NARPM's affiliate directory | `data/narpm_affiliates.csv` |
| **Tests** | every branch push and pull request | `pytest tests` | recorded samples, no network | nothing |

## Where things are

| Path | What |
|---|---|
| `jobsearch/` | The job searches' shared code: job-system readers, careers-page reader, the weekly search, events, founder hiring. Details in [`jobsearch/README.md`](jobsearch/README.md). |
| `opco_search.py`, `opco_config.yml` | The property operations search and its settings (role filter, the sheet link). |
| `run.py`, `report.py`, `discover_leadership.py`, `lib/`, `sources/` | People Moves. Details in [`docs/people-moves.md`](docs/people-moves.md). |
| `config.json` | Settings for the weekly search: role filter, newsletter draft, per-company rules. |
| `leads/` | Company lists. `master_leads.csv` is the proptech list; `proposed_*.csv` files are suggestions waiting for review and are read by nothing. |
| `config/` | People Moves enrichment (`companies.csv`) and trade press feeds (`feeds.yml`). |
| `data/`, `state/` | What the runs remember between weeks. Written by the workflows; don't edit by hand mid-run. |
| `output/`, `out/` | Results. `output/` is the weekly search, events and founder hiring; `out/` is OpCo and People Moves. |
| `unwired/` | Code that was written but is not run by anything. See the README inside. |
| `tests/` | Offline tests. They run on every branch before it is merged. |

## Changing something

- **A company's careers link**: on the OpCo tab, fix Column D in the sheet; the next run uses it. For the master list, fix `careers_url` in `leads/master_leads.csv` (or the board in `data/ats_map.json`). The sheet's PropTech and Master File tabs are only synced once they are published; `opco_config.yml` says how.
- **Which roles are reported**: `config.json` -> `role_filter` for the proptech search, `opco_config.yml` for property operations.
- **Code**: on a branch, through a pull request. The Tests workflow checks it before it reaches `main`.
