"""Offline tests for the careers-page reader (jobsearch/pages.py) and the
headless browser (jobsearch/browser.py).

Pages are served from tests/fixtures/pages by a local web server; job-system
answers come from the fake network in tests/fixtures/readers/payloads.json.
The browser tests are skipped when Playwright isn't installed.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fakenet                                           # noqa: E402
import localsite                                         # noqa: E402
from jobsearch import boards as B                        # noqa: E402
from jobsearch.browser import Browser, BrowserUnavailable  # noqa: E402
from jobsearch.http import FetchError                    # noqa: E402
from jobsearch.pages import NeedsLink, read_page, _SAYS_NONE, _posting_id   # noqa: E402

ROUTES = json.loads((ROOT / "tests/fixtures/readers/payloads.json").read_text())


@pytest.fixture(scope="module")
def site():
    with localsite.serve() as base:
        yield base


@pytest.fixture(scope="module")
def browser():
    b = Browser()
    try:
        b._start()
    except BrowserUnavailable as exc:
        pytest.skip(f"no headless browser here: {exc}")
    yield b
    b.close()


def titles(result):
    return [j["title"] for j in result.jobs]


# ---------------------------------------------------------------- plain download
def test_static_list_follows_next_page(site):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/static_list")
    assert titles(r) == ["Asset Manager", "Financial Analyst", "Vice President, Acquisitions"]
    assert r.jobs[0]["location"] == "Birmingham, AL"
    assert r.jobs[0]["url"] == f"{site}/careers/jobs/asset-manager"
    assert B.opco_id(r.jobs[0]) == "op-/careers/jobs/asset-manager"     # same id format as before the rebuild
    assert "2 pages" in r.how and not r.rendered and r.board is None


def test_deep_job_paths_and_next_link(site):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/deep_paths")
    assert titles(r) == ["Vice President, Investments", "Director, Real Estate", "Sr. Manager, Marketing"]
    assert r.jobs[1]["location"] == "Dallas, TX"


def test_structured_job_data(site):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/jsonld")
    assert titles(r) == ["Portfolio Manager"] and r.jobs[0]["location"] == "Charlotte, NC"
    assert B.opco_id(r.jobs[0]) == "ld-PM-1-Portfolio Manager"


def test_culture_cards_are_not_published_as_jobs(site):
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink) as exc:
            read_page(f"{site}/careers/culture_only")
    assert "don't look like job titles" in str(exc.value)


def test_homepage_is_refused(site):
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink) as exc:
            read_page(f"{site}/")
    assert "homepage" in str(exc.value)


# ------------------------------------------------- the job board a page loads
def test_embedded_board_is_read_directly(site):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/embed_greenhouse")
    assert r.board is not None and r.board.key == "greenhouse:acme:{}"
    assert titles(r) == ["Account Executive", "Staff Software Engineer"]
    assert "embeds" in r.how and "job-boards.greenhouse.io/acme" in r.how


def test_stale_links_lose_to_the_board_that_answers(site):
    """Three old links to a Lever board that no longer exists, one to the live Ashby board."""
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/links_to_board")
    assert r.board.key == "ashby:acme:{}" and titles(r) == ["Forward Deployed Engineer"]


def test_redirect_to_a_job_board_reads_the_board():
    routes = dict(ROUTES)
    routes["GET https://www\\.acme\\.test/careers$"] = {
        "url": "https://recruiting.paylocity.com/recruiting/jobs/All/5e5fc082-a90b-4d93-8b73-46f77d40e081",
        "text": "<html><body>board</body></html>"}
    with fakenet.serve(routes):
        r = read_page("https://www.acme.test/careers")
    assert r.board.system == "paylocity" and len(r.jobs) == 2
    assert "redirects to recruiting.paylocity.com" in r.how


def test_breezy_board_on_the_companys_own_address():
    routes = dict(ROUTES)
    routes["GET https://jobs\\.acmehomes\\.com/$"] = {"text": (
        "<html><head><link href='https://assets.breezy.hr/x.css'></head><body>"
        "<a href='/p/aa2515c48932-photographer'><h2>Photographer</h2></a></body></html>")}
    with fakenet.serve(routes):
        r = read_page("https://jobs.acmehomes.com/")
    assert r.board.system == "breezy" and titles(r) == ["Photographer"]


def test_board_named_in_the_pages_own_code(site):
    """No browser needed when the page's source names the feed it loads."""
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/mentions_board")
    assert r.board.key == "ashby:acme:{}" and not r.rendered


def test_follow_boards_can_be_turned_off(site):
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink):
            read_page(f"{site}/careers/embed_greenhouse", follow_boards=False)


def test_without_a_browser_script_pages_say_so(site):
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink) as exc:
            read_page(f"{site}/careers/js_list")
    assert "browser" in str(exc.value)


# ------------------------------------------------------------- headless browser
def test_script_list_with_load_more(site, browser):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/js_list", browser=browser)
    assert titles(r) == ["Leasing Director", "Senior Software Engineer", "Regional Manager",
                         "Marketing Coordinator", "Data Analyst"]
    assert r.rendered and "3 pages of results" in r.how
    assert r.jobs[0]["location"] == "Tampa, FL" and r.jobs[0]["url"].endswith("/jobs/101")


def test_board_recognised_from_what_the_page_loads(site, browser):
    """The page's script calls Ashby's feed; nothing in the HTML names the board."""
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/js_network", browser=browser)
    assert r.board.key == "ashby:acme:{}" and "loads its jobs from" in r.how
    assert titles(r) == ["Forward Deployed Engineer"]


def test_jobs_inside_a_frame(site, browser):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/careers/iframe_page", browser=browser)
    assert titles(r) == ["Community Manager", "Director of Development"]


def test_card_that_is_one_big_link(site, browser):
    """Paycom-style: heading, type, location and blurb all inside the link."""
    rx = B.RENDERED["paycom"]["links"]
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/card_links.html", link_regex=rx, browser=browser, follow_boards=False)
    assert titles(r) == ["Property Manager", "Senior Accountant"]
    assert r.jobs[0]["location"] == "Atlanta Office - Atlanta, GA 30305"


def test_link_with_title_and_location_side_by_side(site, browser):
    """Gem-style: two spans in a link, no heading."""
    rx = B.RENDERED["gem"]["links"]
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/span_cards.html", link_regex=rx, browser=browser, follow_boards=False)
    assert titles(r) == ["Back End Engineer (All Levels)", "Lead Product Designer"]
    assert r.jobs[0]["location"] == "New York • In office"


def test_spent_browser_budget_is_couldnt_check_not_fix_the_link(site):
    b = Browser(max_pages=0)
    with fakenet.serve(ROUTES):
        with pytest.raises(FetchError) as exc:
            read_page(f"{site}/careers/js_list", browser=b)
    assert "limit" in str(exc.value) and not isinstance(exc.value, NeedsLink)


def test_single_posting_on_a_job_systems_board_keeps_its_own_title(site, browser):
    """isolved-style board with one job: the page heading must not become the title."""
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/one_job_board.html", link_regex=B.RENDERED["isolved"]["links"],
                      browser=browser, follow_boards=False)
    assert titles(r) == ["Compliance Specialist - Affordable Housing"]


def test_job_systems_board_that_says_nothing_is_open_is_a_real_zero(site, browser):
    with fakenet.serve(ROUTES):
        r = read_page(f"{site}/empty_board.html", link_regex=B.RENDERED["isolved"]["links"],
                      browser=browser, follow_boards=False)
    assert r.jobs == [] and "says nothing is open" in r.how


def test_board_with_no_recognisable_postings_is_not_assumed_empty(site, browser):
    """No postings found and no "nothing open" message: the layout may have changed, so this
    must not be read as "not hiring" (which would report every tracked role as closed)."""
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink) as exc:
            read_page(f"{site}/blank_board.html", link_regex=B.RENDERED["isolved"]["links"],
                      browser=browser, follow_boards=False)
    assert "layout may have changed" in str(exc.value)


def test_board_that_is_not_found_is_not_assumed_empty(site, browser):
    with fakenet.serve(ROUTES):
        with pytest.raises(NeedsLink) as exc:
            read_page(f"{site}/no_such_board.html", link_regex=B.RENDERED["isolved"]["links"],
                      browser=browser, follow_boards=False)
    assert "doesn't load" in str(exc.value)


def test_own_list_shown_by_scripts_beats_a_stray_link(site, browser):
    with fakenet.serve(ROUTES) as calls:
        r = read_page(f"{site}/careers/js_list_with_stray_link", browser=browser)
    assert r.board is None and titles(r) == ["Leasing Director", "Regional Manager"]
    assert not any("lever.co" in c for c in calls)        # the linked board was never needed


# --------------------------------- nothing open / couldn't check / fix the link
def page_route(html):
    return {"GET https://www\\.acme\\.test/careers$": {"text": f"<html><body>{html}</body></html>"}}


OWN_JOBS = ("<h1>Open Positions</h1>"
            "<div><h3>Asset Manager</h3><a href='/careers/jobs/asset-manager'>Learn more</a></div>"
            "<div><h3>Financial Analyst</h3><a href='/careers/jobs/financial-analyst'>Learn more</a></div>")


def test_the_pages_own_list_beats_a_stray_link_to_a_board():
    """A footer link to some Lever board must not replace the jobs the page itself lists."""
    html = OWN_JOBS + "<footer><a href='https://jobs.lever.co/acme'>Partner careers</a></footer>"
    with fakenet.serve({**ROUTES, **page_route(html)}):
        r = read_page("https://www.acme.test/careers")
    assert r.board is None and titles(r) == ["Asset Manager", "Financial Analyst"]


def test_a_linked_board_with_nothing_on_it_is_not_taken_as_not_hiring():
    html = "<h1>Careers</h1><a href='https://jobs.lever.co/quiet'>See openings</a>"
    with fakenet.serve({**ROUTES, **page_route(html)}):
        with pytest.raises(NeedsLink) as exc:
            read_page("https://www.acme.test/careers")
    assert "lists nothing" in str(exc.value)


def test_an_embedded_board_with_nothing_on_it_is_not_hiring():
    html = "<h1>Careers</h1><iframe src='https://jobs.lever.co/quiet'></iframe>"
    with fakenet.serve({**ROUTES, **page_route(html)}):
        r = read_page("https://www.acme.test/careers")
    assert r.jobs == [] and r.board.key == "lever:quiet:{}" and "lists no openings" in r.how


def test_page_that_says_nothing_is_open():
    html = "<h1>Team and careers</h1><p>No open roles right now. Check back soon!</p>"
    with fakenet.serve({**ROUTES, **page_route(html)}):
        r = read_page("https://www.acme.test/careers")
    assert r.jobs == [] and "says nothing is open" in r.how


def test_a_failing_stray_link_does_not_block_nothing_open():
    """The page says nothing is open and also links to some old board that errors."""
    html = ("<h1>Careers</h1><p>We have no open positions right now.</p>"
            "<a href='https://oldacme.bamboohr.com/careers'>Previous job site</a>")
    broken = {"GET https://oldacme\\.bamboohr\\.com/careers/list": {"text": "<html>moved</html>"}}
    with fakenet.serve({**ROUTES, **page_route(html), **broken}):
        r = read_page("https://www.acme.test/careers")
    assert r.jobs == [] and "says nothing is open" in r.how


def test_board_behind_the_page_not_answering_is_couldnt_check():
    """A blip at the job system must not look like a broken link (which would wipe history)."""
    html = "<h1>Careers</h1><script src='https://boards.greenhouse.io/embed/job_board/js?for=acme'></script>"
    down = {"GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": {"status": 503, "text": "busy"}}
    with fakenet.serve({**ROUTES, **page_route(html), **down}):
        with pytest.raises(FetchError) as exc:
            read_page("https://www.acme.test/careers")
    assert not isinstance(exc.value, NeedsLink) and "didn't answer" in str(exc.value)


def test_redirect_to_a_boards_front_page_reads_the_board():
    routes = dict(ROUTES)
    routes["GET https://www\\.acme\\.test/careers$"] = {"url": "https://acme.breezy.hr/", "text": "<html><body>board</body></html>"}
    with fakenet.serve(routes):
        r = read_page("https://www.acme.test/careers")
    assert r.board.system == "breezy" and len(r.jobs) == 1


def test_vendor_marketing_and_asset_links_are_not_boards():
    html = ("<h1>Careers</h1><link href='https://assets-cdn.breezy.hr/x.css'><img src='https://images4.bamboohr.com/logo.png'>"
            "<a href='https://www.bamboohr.com/'>Powered by BambooHR</a><a href='https://community.icims.com/'>iCIMS</a>"
            "<script src='https://jobs.dayforcehcm.com/_next/static/chunk.js'></script>")
    with fakenet.serve({**ROUTES, **page_route(html)}) as calls:
        with pytest.raises(NeedsLink):
            read_page("https://www.acme.test/careers")
    assert calls == ["GET https://www.acme.test/careers"]          # nothing else was even tried


@pytest.mark.parametrize("href,expected", [
    ("https://x.test/careers/jobs/asset-manager", "/careers/jobs/asset-manager"),
    ("https://x.test/careers/jobs/asset-manager/?utm_source=li", "/careers/jobs/asset-manager"),
    ("https://x.test/jobs/view?id=11", "/jobs/view?id=11"),
    ("https://x.test/jobs/view?utm=a&jobId=77&lang=en", "/jobs/view?jobId=77"),
])
def test_posting_id_looks_at_one_link_only(href, expected):
    assert _posting_id(href) == expected


@pytest.mark.parametrize("text,is_none", [
    ("No open roles right now", True), ("There are currently no openings.", True),
    ("We are not currently hiring", True), ("We don't have any open positions", True),
    ("No positions available at this time", True), ("We have no open positions right now.", True),
    ("There are no open positions at this time. Please check back.", True),
    ("No role is too small here", False), ("no jobs are beneath us", False),
    ("We are hiring! See open roles below", False), ("no experience required", False),
    ("There is no limit to the opportunities you'll find here", False),
    ("If no positions match your skills, send us your resume", False),
    ("No openings fit? Join our talent network", False),
    ("We are not currently hiring for interns, but we have many other roles", False),
    ("No open positions match your search", False), ("No results found", False),
])
def test_says_none(text, is_none):
    assert bool(_SAYS_NONE.search(text)) is is_none
