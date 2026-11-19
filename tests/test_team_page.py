"""The leadership page reader, against real pages captured on 2026-11-19
(tests/fixtures/team_pages, gzipped, scripts and styles stripped)."""
import gzip
from pathlib import Path

import pytest

from lib.team_page import clean_name, clean_title, extract_team, page_score, STAFF_TITLE

FIX = Path(__file__).parent / "fixtures" / "team_pages"


def page(name: str) -> str:
    with gzip.open(FIX / f"{name}.html.gz", "rt", encoding="utf-8") as fh:
        return fh.read()


def names(name: str) -> dict:
    return dict(extract_team(page(name)))


def test_reads_a_plain_leadership_grid():
    team = names("appfolio")
    assert team["Shane Trigg"] == "Chairman and Chief Executive Officer"
    assert team["Tim Eaton"] == "Chief Financial Officer"
    assert len(team) == 9
    # The old reader glued the "Read Bio" link onto the title.
    assert not any("Read Bio" in t for t in team.values())


def test_reads_cards_with_long_bios():
    # The old reader skipped any card over 600 characters and found one
    # "person": "Investment Banking (VP at Colony Capital ...)".
    team = names("kiavi")
    assert len(team) == 11
    assert team["Arvind Mohan"].startswith("Chief Executive Officer")
    assert "Investment Banking" not in team


def test_reads_a_page_the_old_reader_found_nobody_on():
    team = names("home365")
    assert {"Shai Alfandary", "Lior Ben-Ezer", "Tesh Patel"} <= set(team)


def test_no_page_words_or_split_names():
    team = names("measurabl")
    assert "Maureen Waters" in team
    for junk in ("Board Matt Ellis", "Data Ecosystem", "Data Sara Anzinger"):
        assert junk not in team


def test_customer_quote_pages_lose_to_team_pages():
    quotes_product = extract_team(page("resman-lead-management"))
    quotes_home = extract_team(page("knock-home"))
    team = extract_team(page("appfolio"))
    product = page_score("https://www.myresman.com/solutions/lead-management", quotes_product)
    home = page_score("https://www.knockcrm.com/", quotes_home)
    real = page_score("https://www.appfolio.com/company/who-we-are", team)
    assert product[0] == 0                      # a product page is never a team page
    assert home[1] == 0                         # "Director, <Customer>" isn't staff
    assert real > home > product or real > product > home


@pytest.mark.parametrize("text,expected", [
    ("Jane Doe", "Jane Doe"),
    ("JOHN SMITH", "John Smith"),
    ("Edward K. Aldag", "Edward K. Aldag"),
    ("Jane Doe, CPM", "Jane Doe"),
    ("José Núñez", "José Núñez"),
    ("Carmen Perez-Carlton", "Carmen Perez-Carlton"),
    ("Cookies These", None),
    ("Blog Events", None),
    ("Clorox Company", None),
    ("Meet Josh Theiss", None),
    ("Colony Capital", None),
    ("Envoy Appoints", None),
    ("Ali Co-Chief", None),
    ("Dave Dave", None),
    ("Financial Officer Brian", None),
])
def test_names(text, expected):
    assert clean_name(text) == expected


@pytest.mark.parametrize("text,ok", [
    ("Chief Executive Officer", True),
    ("VP, Finance", True),
    ("Managing Partner", True),
    ("Co-founder", True),
    ("Partner with us", False),
    ("VP at Trinity Financial Boston, MA", False),
    ("Our partners include the best in the business", False),
    ("Read more", False),
])
def test_titles(text, ok):
    assert (clean_title(text) is not None) == ok


def test_staff_titles_are_not_leadership_moves():
    assert STAFF_TITLE.search("Leasing Manager")
    assert STAFF_TITLE.search("Community Coordinator, Student Housing")
    assert not STAFF_TITLE.search("General Manager, US")
    assert not STAFF_TITLE.search("Vice President, Property Management")
    assert not STAFF_TITLE.search("Chief Financial Officer")
