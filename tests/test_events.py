"""Offline test for the events pull: python tests/test_events.py  (or python -m pytest tests/)

The pages in tests/fixtures/events/ copy the layout of each live site as of Sept 2026. If a site redesigns,
the weekly run saves the new page to output/<date>/debug/ - copy it here and update the parser."""
import csv, datetime as dt, json, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jobsearch import events as E  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "events"
TODAY = dt.date(2026, 9, 28)


def _sandbox(tmp: pathlib.Path, leads_rows=None):
    E.HISTORY, E.EVENTS_MAP, E.NEEDS_MAP, E.OUT = tmp / "events.json", tmp / "events_map.json", tmp / "needs.csv", tmp / "output"
    if leads_rows is not None:
        E.LEADS = tmp / "leads.csv"
        with open(E.LEADS, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["company", "website"])
            w.writeheader()
            w.writerows(leads_rows)


def _rows(tmp, name="events.csv"):
    return list(csv.DictReader(open(tmp / "output" / TODAY.isoformat() / name)))


def test_dates():
    p = lambda s, y=None: E.parse_dates(s, TODAY, y)[:2]
    d = dt.date
    assert p("October 6, 2026 | 9:00 AM PDT") == (d(2026, 10, 6), d(2026, 10, 6))
    assert p("November 18 – November 19, 2026") == (d(2026, 11, 18), d(2026, 11, 19))
    assert p("Sept. 13-15, 2027") == (d(2027, 9, 13), d(2027, 9, 15))
    assert p("May 24-26, 2027|Loews Miami Beach, Miami, FL") == (d(2027, 5, 24), d(2027, 5, 26))
    assert p("September 29-October 1, 2025") == (d(2025, 9, 29), d(2025, 10, 1))
    assert p("January 14-15", 2027) == (d(2027, 1, 14), d(2027, 1, 15))
    assert p("Dec 30, 2026 - Jan 2, 2027") == (d(2026, 12, 30), d(2027, 1, 2))
    assert p("5/20/2026 » 5/22/2026") == (d(2026, 5, 20), d(2026, 5, 22))
    assert p("Thursday, November 12, 2026 · 1:00 PM ET") == (d(2026, 11, 12), d(2026, 11, 12))
    assert E.parse_dates("March 2026 market update", TODAY) is None
    assert E.city_state("Loews Miami Beach, Miami, FL") == ("Miami", "FL")
    assert E.city_state("Grand Hyatt Washington | Washington, DC") == ("Washington", "DC")


def test_associations_and_bisnow():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _sandbox(tmp)
    assert E.main(["--fixtures", str(FX), "--date", TODAY.isoformat(), "--only", "associations,bisnow"]) == 0
    rows = {r["title"]: r for r in _rows(tmp)}
    # NRHC: date-first layout, past event dropped, "Coming Soon" event kept with the page as its link
    assert rows["NRHC – Georgia Annual Conference"]["start"] == "2026-10-15"
    assert rows["NRHC – Georgia Annual Conference"]["url"].endswith("/georgia-annual-conference-2026/")
    assert rows["Dinner In The Desert"]["location"] == "Scottsdale, AZ"
    assert rows["Dinner In The Desert"]["url"] == "https://rentalhomecouncil.org/events/"   # not a footer link
    assert "NRHC – North Carolina Annual Conference" not in rows                            # Sept 17 is past
    # NARPM: heading + Dates:/Location: labels
    assert rows["2026 NARPM® Annual Convention and Expo"]["location"] == "Mandalay Bay Resort & Casino, Las Vegas, NV"
    assert rows["Nevada State Conference"]["state"] == "NV"
    # NMHC: members-only flag, venue + city, committee/board and Emerging Leaders filtered
    assert rows["OPTECH 2026"]["access"] == "members only"
    assert rows["2026 NMHC Student Housing Conference"]["location"] == "Grand Hyatt Scottsdale Resort, Scottsdale, AZ"
    assert not any("Emerging Leaders" in t or "Board" in t for t in rows)
    # NAA: year headings, stale 2025 entries dropped, venue separator cleaned
    assert rows["Advocate"]["start"] == "2027-03-16" and rows["Advocate"]["location"] == "Grand Hyatt Washington, Washington, DC"
    assert "IRO Summit" not in rows
    # IMN: watchlist + discovered SFR event kept, unrelated IMN event ignored, TBA kept for the report only
    assert rows["IMN Single Family Rental West"]["location"] == "Fairmont Scottsdale Princess, Scottsdale, AZ"
    assert "Bank Special Assets Forum" not in rows
    assert rows["IMN SFR/BTR Property Management & Operations"]["start"] == ""
    # Bisnow: US only, webinars online, multi-day ranges
    assert "Ireland's Residential Investment And Development Conference" not in rows
    assert not any("Toronto" in t for t in rows)
    assert rows["Reimagining Multifamily Operations"]["location"] == "Online"
    assert rows["Southern California Multifamily Annual Conference"]["end"] == "2026-11-19"
    nl = (tmp / "output" / "newsletter" / f"events-{TODAY.isoformat()}.md").read_text()
    assert "## Upcoming events" and "scattered site rental operations" in nl
    assert "Just announced" not in nl        # first run: everything is new, so nothing is called just announced
    assert "SFR" not in nl.split("\n")[2]    # our own intro line never says SFR


def test_companies_detect_and_classify():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _sandbox(tmp, [{"company": "Rently", "website": "https://www.rently.com"},
                   {"company": "HiredHelpr", "website": "https://www.hiredhelpr.com"},
                   {"company": "Nowhere Co", "website": "https://nowhere.example"}])
    assert E.main(["--fixtures", str(FX), "--date", TODAY.isoformat(), "--only", "associations,companies", "--detect"]) == 0
    emap = json.loads(E.EVENTS_MAP.read_text())
    assert emap["Rently"]["url"] == "https://www.rently.com/company/events" and emap["Rently"]["method"] == "homepage link"
    assert emap["HiredHelpr"]["url"] == "https://www.hiredhelpr.com/webinars"
    assert emap["Nowhere Co"]["method"] == "none"
    assert "Nowhere Co" in E.NEEDS_MAP.read_text()
    rows = {(r["company"], r["title"]): r for r in _rows(tmp) if r["company"]}
    assert rows[("Rently", "Rently Happy Hour at NARPM")]["kind"] == "company_hosted"
    assert rows[("Rently", "Rently Happy Hour at NARPM")]["location"] == "Mandalay Bay, Las Vegas, NV"
    assert rows[("Rently", "NARPM Annual Convention and Expo")]["kind"] == "company_attending"
    assert rows[("Rently", "Self-Guided Tours 101 Webinar")]["location"] == "Online"
    assert ("Rently", "Old Webinar") not in rows
    assert rows[("HiredHelpr", "AI Leasing Playbook for Operators")]["start"] == "2026-11-12"
    assert ("HiredHelpr", "Q3 Product Recap") not in rows
    nl = (tmp / "output" / "newsletter" / f"events-{TODAY.isoformat()}.md").read_text()
    assert "Hosted by companies we track" in nl and "Happy Hour" in nl
    assert "NARPM Annual Convention and Expo](https://rently.com" not in nl   # attending isn't listed as hosted


def test_history_second_run():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _sandbox(tmp)
    args = ["--fixtures", str(FX), "--date", TODAY.isoformat(), "--only", "associations"]
    E.main(args)
    h1 = json.loads(E.HISTORY.read_text())
    E.main(args)
    h2 = json.loads(E.HISTORY.read_text())
    assert set(h1) == set(h2) and all(v["status"] == "upcoming" for v in h2.values())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
