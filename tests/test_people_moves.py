"""People Moves: which companies are watched, and the sources that were idle or noisy."""
import csv
import json
import pathlib
from types import SimpleNamespace

import pytest

from lib import companies as C
from lib import state as state_lib

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _scratch_people_state(tmp_path, monkeypatch):
    from sources import req_signals
    st = tmp_path / "_pm_state"
    monkeypatch.setattr(state_lib, "STATE_DIR", st)
    monkeypatch.setattr(state_lib, "SEEN_PATH", st / "seen.json")
    monkeypatch.setattr(req_signals, "PREVIOUS_PATH", st / "previous_reqs.json")
    if hasattr(state_lib, "SNAPSHOT_DIR"):
        monkeypatch.setattr(state_lib, "SNAPSHOT_DIR", st / "leadership")
    yield


def _csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return str(path)


# ---------- company lists ----------

def test_watches_the_real_lists_not_the_sample():
    import run
    companies = C.load_lists([str(run.MASTER_LEADS), ("", str(run.OPCO_CSV))], enrich=run.ENRICHMENT)
    names = {C.normalize(c.name) for c in companies}
    assert len(companies) > 100                      # the old sample list had 13
    assert C.normalize("Greystar") in names and C.normalize("Evernest") in names
    tickers = {c.name: c.ticker for c in companies if c.ticker}
    assert tickers.get("AppFolio") == "APPF" and tickers.get("Opendoor") == "OPEN"
    assert sum(1 for c in companies if c.leadership_url) >= 13


def test_lists_merge_dedupe_and_enrichment_only_adds_details(tmp_path):
    master = _csv(tmp_path / "m.csv", [
        {"company": "Acme Homes", "website": "https://acme.test", "careers_url": ""},
        {"company": "Beta Rentals", "website": "", "careers_url": ""},
    ])
    opco = _csv(tmp_path / "o.csv", [
        {"Company Name": "Acme Homes Inc", "Website URL": "", "Careers Page URL": ""},  # same company
        {"Company Name": "Gamma Property Management", "Website URL": "", "Careers Page URL": ""},
    ])
    enrich = _csv(tmp_path / "e.csv", [
        {"Company Name": "Acme Homes", "Ticker": "ACME", "Leadership URL": "https://acme.test/team", "Aliases": "Acme"},
        {"Company Name": "Not On Any List", "Ticker": "NOPE", "Leadership URL": "", "Aliases": ""},
    ])
    got = C.load_lists([master, ("https://unreachable.invalid/sheet.csv", opco)], enrich=enrich)
    assert [c.name for c in got] == ["Acme Homes", "Beta Rentals", "Gamma Property Management"]
    acme = got[0]
    assert acme.ticker == "ACME" and acme.leadership_url == "https://acme.test/team"
    assert all(c.name != "Not On Any List" for c in got)


# ---------- job-search signals ----------

def test_job_signals_read_the_real_outputs(tmp_path):
    from sources import req_signals
    companies = [C.Company(name="Acme Homes", careers_url="https://acme.test/jobs")]

    def week(titles_weekly, titles_opco, tag):
        weekly = _csv(tmp_path / f"all_open_roles_{tag}.csv",
                      [{"company": "Acme Homes", "title": t, "url": ""} for t in titles_weekly])
        opco = tmp_path / f"opco_{tag}.json"
        opco.write_text(json.dumps({"run_date": "x", "roles": [
            {"company": "Gamma PM", "title": t} for t in titles_opco]}))
        return f"{weekly},{opco}"

    first = req_signals.collect(companies, jobs_file=week(["VP Sales", "Engineer"], ["Director of Ops", "Analyst"], 1))
    assert first == []                                         # first run is the baseline
    second = req_signals.collect(companies, jobs_file=week(["Engineer"], ["Analyst"], 2))
    summaries = " ".join(m.summary for m in second)
    assert "Acme Homes closed 1 role" in summaries and "VP Sales" in summaries
    assert "Gamma PM closed 1 role" in summaries               # OpCo roles count too


def test_job_signals_skip_cleanly_without_output(monkeypatch):
    from sources import req_signals
    monkeypatch.delenv("JOBS_RESULTS_PATH", raising=False)
    assert req_signals.collect([], jobs_file=None) == []


# ---------- news ----------

_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>CBRE hires South Florida leader from Related Ross - CoStar</title>
<link>https://news.test/1</link><pubDate>{d}</pubDate></item>
<item><title>CoStar Group names Jane Smith Chief Revenue Officer - Business Wire</title>
<link>https://news.test/2</link><pubDate>{d}</pubDate></item>
</channel></rss>"""


def test_news_ignores_a_company_that_is_only_the_publisher(monkeypatch):
    from email.utils import format_datetime
    from datetime import datetime, timezone
    from sources import news_rss
    rss = _RSS.format(d=format_datetime(datetime.now(timezone.utc)))
    monkeypatch.setattr(news_rss, "get", lambda url, **kw: SimpleNamespace(content=rss.encode()))
    seen = state_lib.SeenStore()
    moves = news_rss.collect([C.Company(name="CoStar Group")], 8, seen)
    assert [m.url for m in moves] == ["https://news.test/2"]


# ---------- leadership pages ----------

_TEAM = """<html><body><ul>
<li>Jane Smith, Chief Executive Officer</li>
<li>Robert Jones, Chief Financial Officer</li>
<li>Maria Garcia, Vice President of Sales</li>
</ul></body></html>"""


def test_leadership_reads_script_loaded_pages_in_the_browser(monkeypatch):
    from sources import leadership
    monkeypatch.setattr(leadership, "get", lambda url, **kw: SimpleNamespace(text="<html><body><div id=app></div></body></html>"))
    monkeypatch.setattr(leadership, "_rendered_html", lambda url: _TEAM)
    company = C.Company(name="Acme Homes", leadership_url="https://acme.test/team")
    html, people = leadership._read_people(company)
    assert len(people) >= 2 and html == _TEAM
    assert leadership.collect([company]) == []                 # first sighting: baseline only
    assert state_lib.load_snapshot(company.slug).get("people")


def test_leadership_skips_the_browser_when_the_plain_page_works(monkeypatch):
    from sources import leadership
    monkeypatch.setattr(leadership, "get", lambda url, **kw: SimpleNamespace(text=_TEAM))

    def no_browser(url):
        raise AssertionError("browser should not be needed")
    monkeypatch.setattr(leadership, "_rendered_html", no_browser)
    _, people = leadership._read_people(C.Company(name="Acme", leadership_url="https://acme.test/team"))
    assert len(people) >= 2
