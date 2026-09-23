# Syndeo weekly job search — prototype v0.1

Tracks hiring at the companies in **`leads/master_leads.csv` only** (the SYNDEO Real Estate
Hiring Leads master list). Every week it pulls open roles straight from each company's ATS,
diffs against last week, and reports:

- **open roles** with a direct link to every posting
- **new this week**
- **closed since last run** = the "recently hired / likely filled" signal
- per-company scrape status, so a failed scrape never masquerades as "all roles filled"

No company discovery. No LLM classification. No HTML guessing where a JSON API exists.

## How it runs (you are browser-only, so this lives in GitHub Actions)

1. Create a repo (or a folder in `FoundersandFriends`) and upload this whole directory.
2. `.github/workflows/weekly-jobsearch.yml` runs every **Monday 08:00 ET** and on demand
   (Actions tab → "Weekly job search" → Run workflow). It commits `data/` and `output/`
   back to the repo and attaches the report as an artifact.
3. Open `output/<date>/report.html` (or `report.md`, or the CSVs).

First real run: use **Run workflow** with `detect = true`. That fetches each careers page,
identifies the ATS from the raw HTML, and writes `data/ats_map.json`. Anything it can't
resolve lands in `data/needs_manual_mapping.csv` — fill those in by hand in `ats_map.json`
(`{"ats": "greenhouse", "slug": "xyz"}`). Manual entries are never overwritten.

## Files

| path | what |
|---|---|
| `leads/master_leads.csv` | the universe. company, careers_url, segment, tier (A = SFR/PM core, B = adjacent) |
| `jobsearch/adapters.py` | 12 ATS adapters: greenhouse, lever, ashby, workable, smartrecruiters, bamboohr, breezy, recruitee, rippling, workday, jobvite, generic |
| `jobsearch/detect.py` | finds ATS + slug from a careers page; probes APIs with domain-based slug guesses as fallback |
| `jobsearch/run.py` | orchestrates scrape → diff → outputs |
| `data/ats_map.json` | company → ATS/slug. Hand-verified entries marked `confidence: manual` |
| `data/history.json` | every job ever seen (first_seen / last_seen / status). This is the "recently hired" engine |
| `data/link_verification_log.csv` | what was checked by hand on 2026-09-23 and what was found |
| `config.json` | focus-keyword tags (sales / customer / product / leadership / ops / eng). Labels only |
| `tests/` | offline regression test with recorded fixtures: `python tests/test_pipeline.py` |

## Interpreting "closed / likely filled"

A role is marked closed when it was open last run, is absent this run, **and** the company's
scrape succeeded this run. `days_open` is the gap between first and last sighting. Roles that
close in < 14 days are often pulled/reposted rather than filled — treat those with more scepticism.
This is the best signal available without LinkedIn data; wiring PhantomBuster/Clay "new hire"
signals in later would let the two be cross-checked.

## Known gaps in this prototype

- ATS with no public JSON feed (iCIMS, Paylocity, UKG, ADP, Paradox) are detected but not scraped.
  Big operators (Greystar, Invitation Homes-style) tend to sit here — needs a Playwright step or
  a paid feed later.
- Workday needs `wd` and `site` filled in `ats_map.json` (detector captures both when the careers
  page links to myworkdayjobs.com directly).
- No alerting/email yet — the report is a file in the repo. Easy add once the output shape is agreed.
- No secrets, auth, or rate-limit backoff beyond a 0.5s sleep — fine for ~100 companies weekly.
