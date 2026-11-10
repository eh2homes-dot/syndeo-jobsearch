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
    E.CHAPTERS_MAP = tmp / "narpm_chapters.json"
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
    # Wild Apricot writes day-month-year; NARPM's course list writes "12pm - 3pm EASTERN"
    assert p("18 Nov 2026, 11:00 AM - 1:00 PM") == (d(2026, 11, 18), d(2026, 11, 18))
    assert p("14 Jan 2027 8:00 AM - 15 Jan 2027 4:00 PM") == (d(2027, 1, 14), d(2027, 1, 15))
    assert E.is_mostly_date("18 Nov 2026, 11:00 AM - 1:00 PM", TODAY)
    assert E.is_mostly_date("Nov 17, 2026 12pm - 3pm EASTERN", TODAY)
    assert not E.is_mostly_date("Join us Nov 17, 2026 for the chapter lunch and awards", TODAY)
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


def test_narpm_national_and_chapters():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _sandbox(tmp)
    assert E.main(["--fixtures", str(FX), "--date", TODAY.isoformat(), "--only", "associations"]) == 0
    all_rows = _rows(tmp)
    rows = {r["title"]: r for r in all_rows}
    # national calendar, read through its iCal export
    assert rows["NARPM Southeast Regional Conference"]["end"] == "2026-12-04"            # all-day DTEND is exclusive
    assert rows["NARPM Southeast Regional Conference"]["kind"] == "conference"
    # course schedule: date line before the title, past course dropped, virtual vs in person
    doors = rows["The Art of Acquiring More Doors (Virtual)"]
    assert doors["start"] == "2026-10-27" and doors["kind"] == "education" and doors["location"] == "Online"
    assert doors["url"].endswith("/the-art-of-acquiring-more-doors-virtual-3")
    assert rows["Ethics (In-Person)"]["location"] == "Raleigh, NC"
    assert "Risk Management (Virtual)" not in rows
    assert rows["How to Successfully Market Your Company (Virtual)"]["location"] == "Online"   # not the footer address
    # webinars: real name from the bold line under "...sponsored by X:", date from the Time: line, always online
    assert rows["Hard Data and Proven Tactics to Level Up Leasing"]["location"] == "Online"
    assert rows["Hard Data and Proven Tactics to Level Up Leasing"]["start"] == "2026-11-10"
    assert rows["How to Use AI in Your Property Management Business"]["start"] == "2026-11-18"
    assert not any(t.endswith(":") for t in rows)
    # chapters: iCal feeds on NARPM-hosted sites
    oct_mtg = rows["NARPM Atlanta October Chapter Meeting"]
    assert oct_mtg["kind"] == "chapter" and oct_mtg["organizer"] == "NARPM Atlanta" and oct_mtg["city"] == "Atlanta"
    assert oct_mtg["time"] == "11:30" and oct_mtg["source"] == "narpm-chapters"
    assert "NARPM Atlanta June Chapter Meeting" not in rows                              # past
    assert "Chapter Board Meeting" not in rows                                            # excluded pattern
    assert "Fair Housing Update for Members" not in rows                                  # national category
    assert "2026 NARPM Annual Convention & Expo" not in rows                              # cross-post of national
    assert rows["2026 NARPM® Annual Convention and Expo"]["source"] == "narpm"
    assert rows["Georgia Property Management Conference & Expo"]["kind"] == "conference"  # promoted
    series = [r for r in all_rows if "52 Weeks" in r["title"]]
    assert len(series) == 1 and series[0]["start"] == "2026-09-29"                        # weekly series: next session only
    assert "2026 Calendar" not in rows
    assert rows["Atlanta Holiday Party"]["location"] == ""                                # "TBA, TBA, TBA, United States"
    assert rows["Interactive AI Learning Workshop"]["location"] == "Ravinia Club, Atlanta, GA"   # leading time removed
    # Wild Apricot (Houston): day-month-year dates, venue line; the same meeting on the Texas feed is kept once
    hou = [r for r in all_rows if r["title"] == "NARPM Houston November 2026 Chapter Meeting"]
    assert len(hou) == 1 and hou[0]["start"] == "2026-11-18" and hou[0]["state"] == "TX"
    assert hou[0]["organizer"] == "NARPM Houston"                                         # host chapter wins
    assert rows["NARPM Atlanta November Chapter Meeting"]["start"] == "2026-11-18"        # same day, other city: kept
    assert rows["Gulf Coast Property Management Summit"]["end"] == "2027-01-15"
    # Meetup chapter via its iCal feed
    assert rows["Baltimore/Washington NARPM Fall Happy Hour"]["state"] == "MD"
    # chapter map: built from the finder + chapter pages, seed entries kept
    cmap = json.loads(E.CHAPTERS_MAP.read_text())
    assert cmap["C099"]["site"] == "https://testvillenarpm.org/" and cmap["C099"]["name"] == "Testville"
    # own domain without /events: the events link was found from its homepage and saved for next week
    assert cmap["C099"]["events_url"].startswith("https://testvillenarpm.org/chapter-calendar")
    assert rows["Testville December Luncheon"]["organizer"] == "NARPM Testville"
    assert cmap["C002"]["site"] == "https://narpmhouston.wildapricot.org" and cmap["C002"]["method"] == "chapter finder"
    assert cmap["C005"]["site"] == "https://chapters.narpm.org/atlanta/" and cmap["C005"]["last_ok"] == TODAY.isoformat()
    assert "UA" not in cmap and "DCHR" not in cmap
    # status: unreachable chapters are listed but never counted as broken
    src = list(csv.DictReader(open(tmp / "output" / TODAY.isoformat() / "events_sources.csv")))
    summary = next(r for r in src if r["source"] == "narpm-chapters" and r["url"].endswith("/find/chapters/"))
    assert summary["status"] == "ok" and "5 chapters on the finder" in summary["note"]
    assert any(r["source"] == "narpm-chapters" and r["status"].startswith("skipped") and r["note"] == "Greater Portland"
               for r in src)
    assert all(r["status"] == "ok" for r in src if r["source"] in ("narpm-calendar", "narpm-courses", "narpm-webinars"))
    # newsletter: chapter block, one line per chapter; courses and webinars stay out
    nl = (tmp / "output" / "newsletter" / f"events-{TODAY.isoformat()}.md").read_text()
    block = nl.split("**NARPM chapter meetings**")[1].split("\n**")[0]
    assert block.count("chapters.narpm.org/atlanta") == 1 and "52 Weeks" in block   # Atlanta's soonest event only
    assert "Fall Happy Hour" in block
    assert "Acquiring More Doors" not in nl and "Level Up Leasing" not in nl
    assert "Georgia Property Management Conference" in nl.split("**NARPM chapter meetings**")[0]
    report = (tmp / "output" / TODAY.isoformat() / "events_report.md").read_text()
    assert "## NARPM chapter events" in report and "## Courses and webinars" in report


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


def test_sheet_csv_full_runs_only():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _sandbox(tmp, [{"company": "Rently", "website": "https://www.rently.com"}])
    fx = ["--fixtures", str(FX)]
    E.main(fx + ["--date", "2026-09-21", "--only", "associations"])      # first-ever run, a week earlier
    assert not (tmp / "output" / "events-latest.csv").exists()          # partial run leaves the sheet alone
    E.main(fx + ["--date", TODAY.isoformat(), "--detect"])               # full run writes it
    rows = list(csv.DictReader(open(tmp / "output" / "events-latest.csv")))
    assert list(rows[0])[:4] == ["Start", "End", "Event", "Type"]
    assert rows[0]["Start"] <= rows[1]["Start"] and rows[-1]["Start"] == "TBA"
    types = {r["Type"] for r in rows}
    assert types == {"Conference", "Company event", "NARPM chapter event", "Course / webinar"}   # attending left out
    assert any(r["Event"] == "Rently Happy Hour at NARPM" and r["Host"] == "Rently" for r in rows)
    new = {r["Host"] for r in rows if r["New this week"] == "yes"}
    assert "NRHC" not in new and "Bisnow" in new and "Rently" in new     # only what the first run hadn't seen
    nl = (tmp / "output" / "newsletter" / f"events-{TODAY.isoformat()}.md").read_text()
    just = nl.split("**Just announced**")[1] if "**Just announced**" in nl else ""
    assert "Triangle" in just and "IMN Single Family Rental West" not in just   # Bisnow is new; IMN was seen last week


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
