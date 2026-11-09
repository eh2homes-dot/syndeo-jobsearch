"""Offline tests for finding each company's LinkedIn page on its own website (jobsearch/linkedin.py),
and for carrying an approved LinkedIn page through to the job search outputs."""
import csv
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fakenet                                   # noqa: E402
import opco_search                               # noqa: E402
from jobsearch import linkedin as li             # noqa: E402
from jobsearch import run                        # noqa: E402
from jobsearch.http import FetchError            # noqa: E402

TODAY = dt.date(2026, 10, 18)


def page(body="", head=""):
    return f"<html><head>{head}</head><body>{body}</body></html>"


def footer(*hrefs):
    return "<footer>" + "".join(f"<a href='{h}'>in</a>" for h in hrefs) + "</footer>"


def site(pages: dict):
    """A fake fetch: url -> html, or an exception to raise."""
    def fetch(url):
        v = pages.get(url)
        if v is None:
            raise FetchError("HTTP 404")
        if isinstance(v, Exception):
            raise v
        return v, url
    return fetch


# ---------------------------------------------------------------- reading links
def test_page_links_are_cleaned_up_and_everything_else_is_ignored():
    assert li.normalize("https://www.linkedin.com/company/AppFolio-Inc/?trk=footer") == \
        ("company", "appfolio-inc", "https://www.linkedin.com/company/appfolio-inc/")
    assert li.normalize("//uk.linkedin.com/company/yardi-systems/jobs/")[2] == "https://www.linkedin.com/company/yardi-systems/"
    assert li.normalize("linkedin.com/showcase/rentcafe")[0] == "showcase"
    assert li.normalize("https://lnkd.in/abc123")[0] == "short"
    for not_a_page in ("https://www.linkedin.com/in/someone/", "https://www.linkedin.com/shareArticle?url=x",
                       "https://www.linkedin.com/sharing/share-offsite/?url=x", "https://www.linkedin.com/feed/",
                       "https://www.linkedin.com/jobs/view/123", "https://notlinkedin.com/company/x",
                       "https://www.linkedin.com/company/", "mailto:hi@acme.test"):
        assert li.normalize(not_a_page) == ("", "", ""), not_a_page


def test_the_sites_own_description_and_footer_beat_a_link_in_the_body():
    html = page(head="<script type='application/ld+json'>{\"@type\":\"Organization\",\"sameAs\":"
                     "[\"https://twitter.com/acme\",\"https://www.linkedin.com/company/acme-homes\"]}</script>",
                body="<p>Read our <a href='https://www.linkedin.com/company/partner-co/'>partner</a></p>"
                     "<a href='https://www.linkedin.com/shareArticle?mini=true'>share</a>"
                     "<div class='site-footer'><a href='https://www.linkedin.com/company/acme-homes/'>LinkedIn</a></div>")
    found = li.extract(html)
    assert [(c.url, c.where) for c in found] == [("https://www.linkedin.com/company/acme-homes/", "site data"),
                                                  ("https://www.linkedin.com/company/partner-co/", "page")]


def test_a_link_kept_in_the_page_code_is_found_when_there_is_no_visible_one():
    html = page(body="<div id='root'></div><script>window.__DATA__={\"social\":"
                     "{\"li\":\"https:\\/\\/www.linkedin.com\\/company\\/acme-homes\"}}</script>")
    assert [(c.url, c.where) for c in li.extract(html)] == [("https://www.linkedin.com/company/acme-homes/", "page code")]


def test_name_check():
    assert li.name_matches("appfolio-inc", "AppFolio", "https://www.appfolio.com")
    assert li.name_matches("placer-ai", "Placer.ai", "https://placer.ai")
    assert li.name_matches("yardi-systems-inc", "Yardi Systems", "https://www.yardi.com")
    assert li.name_matches("urbancompass", "Compass", "https://www.compass.com")
    assert li.name_matches("realpage", "Buildium", "https://www.buildium.com") is False
    assert li.name_matches("10101010", "Acme", "") is None


# ---------------------------------------------------------------- finding one company's page
def test_found_in_the_homepage_footer_needs_no_checks():
    f = li.find("Acme Homes", "https://www.acmehomes.test", "https://www.acmehomes.test/careers",
                fetch=site({"https://www.acmehomes.test/": page(footer("https://www.linkedin.com/company/acme-homes"))}))
    assert (f.url, f.found_on, f.checks, f.error) == ("https://www.linkedin.com/company/acme-homes/",
                                                      "homepage footer", [], "")


def test_homepage_without_a_link_falls_back_to_the_careers_page():
    f = li.find("Acme Homes", "https://www.acmehomes.test/about", "https://jobs.acmehomes.test/",
                fetch=site({"https://www.acmehomes.test/": page("<p>Hello</p>"),
                            "https://jobs.acmehomes.test/": page(footer("https://www.linkedin.com/company/acme-homes/"))}))
    assert f.url == "https://www.linkedin.com/company/acme-homes/"
    assert f.found_on == "careers page footer"
    assert f.checks == ["found on the careers page, not the main site"]


def test_parent_company_page_is_flagged_not_trusted():
    f = li.find("Buildium", "https://www.buildium.com", "",
                fetch=site({"https://www.buildium.com/": page(footer("https://www.linkedin.com/company/realpage/"))}))
    assert f.url == "https://www.linkedin.com/company/realpage/"
    assert "doesn't look like Buildium" in f.checks[0]


def test_own_page_preferred_over_a_showcase_and_others_listed():
    f = li.find("Acme Homes", "https://www.acmehomes.test", "",
                fetch=site({"https://www.acmehomes.test/": page(footer(
                    "https://www.linkedin.com/showcase/acme-rent-app/", "https://www.linkedin.com/company/acme-homes/"))}))
    assert f.url == "https://www.linkedin.com/company/acme-homes/"
    assert f.others == ["https://www.linkedin.com/showcase/acme-rent-app/"]
    assert f.checks == ["the site also links to https://www.linkedin.com/showcase/acme-rent-app/"]


def test_only_a_showcase_page_is_flagged():
    f = li.find("Acme Homes", "https://www.acmehomes.test", "",
                fetch=site({"https://www.acmehomes.test/": page(footer("https://www.linkedin.com/showcase/acme-homes/"))}))
    assert f.kind == "showcase" and "showcase page" in f.checks[0]


def test_unreadable_site_is_an_error_not_a_miss():
    f = li.find("Acme Homes", "https://www.acmehomes.test", "",
                fetch=site({"https://www.acmehomes.test/": FetchError("HTTP 403 bot protection (Cloudflare)")}))
    assert f.url == "" and "403" in f.error


def test_short_link_only_is_reported_for_a_human_to_open():
    f = li.find("Acme Homes", "https://www.acmehomes.test", "",
                fetch=site({"https://www.acmehomes.test/": page(footer("https://lnkd.in/eXyZ"))}))
    assert f.url == "" and f.error == "" and "lnkd.in" in f.checks[0]


def test_a_browser_is_used_when_the_plain_page_shows_no_link():
    class FakeBrowser:
        opened = []

        def render(self, url, max_clicks=20):
            self.opened.append(url)
            return type("R", (), {"frames": [(url, page(footer("https://www.linkedin.com/company/acme-homes/")))],
                                  "final_url": url})()

    b = FakeBrowser()
    f = li.find("Acme Homes", "https://www.acmehomes.test", "",
                fetch=site({"https://www.acmehomes.test/": FetchError("HTTP 403")}), browser=b)
    assert f.url == "https://www.linkedin.com/company/acme-homes/" and f.error == ""
    assert b.opened == ["https://www.acmehomes.test/"]


# ---------------------------------------------------------------- the review file
def L(name, website, approved="", lists=("Weekly",)):
    return li.Listed(name, website, "", approved, list(lists))


def test_review_rows_say_what_to_do():
    companies = [L("Acme Homes", "https://www.acmehomes.test"),
                 L("Beta Rentals", "https://beta.test"),
                 L("Gamma", "https://gamma.test", approved="https://www.linkedin.com/company/gamma/"),
                 L("Delta", "https://delta.test", approved="https://www.linkedin.com/company/delta/"),
                 L("Mynd", "https://mynd.test"), L("Roofstock", "https://roofstock.test"),
                 L("Omega", "https://omega.test")]
    pages = {"https://www.acmehomes.test/": page(footer("https://www.linkedin.com/company/acme-homes/")),
             "https://beta.test/": page("<p>no social links</p>"),
             "https://gamma.test/": page(footer("https://www.linkedin.com/company/gamma")),
             "https://delta.test/": page(footer("https://www.linkedin.com/company/delta-property-group/")),
             "https://mynd.test/": page(footer("https://www.linkedin.com/company/roofstock/")),
             "https://roofstock.test/": page(footer("https://www.linkedin.com/company/roofstock/")),
             "https://omega.test/": FetchError("HTTP 503")}
    state = {}
    assert li.check(companies, state, TODAY, fetch=site(pages)) == 7
    rows = {r["company"]: r for r in li.review_rows(companies, state)}
    assert rows["Acme Homes"]["action"] == "add to list" and rows["Acme Homes"]["what_to_check"] == ""
    assert rows["Beta Rentals"]["action"] == "not found"
    assert rows["Gamma"]["action"] == "in list"
    assert rows["Delta"]["action"] == "differs from list: check"
    assert rows["Mynd"]["action"] == "check, then add"
    assert "same page found for Roofstock" in rows["Mynd"]["what_to_check"]
    assert "same page found for Mynd" in rows["Roofstock"]["what_to_check"]
    assert rows["Omega"]["action"] == "couldn't check" and "503" in rows["Omega"]["what_to_check"]
    order = [r["action"] for r in li.review_rows(companies, state)]
    assert order == sorted(order, key=li.ACTIONS.index)       # things to look at come first


def test_companies_are_only_rechecked_when_due_and_a_failed_read_keeps_what_was_known():
    c = [L("Acme Homes", "https://www.acmehomes.test")]
    good = site({"https://www.acmehomes.test/": page(footer("https://www.linkedin.com/company/acme-homes/"))})
    down = site({"https://www.acmehomes.test/": FetchError("HTTP 503")})
    moved = site({"https://www.acmehomes.test/": page(footer("https://www.linkedin.com/company/acme-homes-inc/"))})
    state = {}
    li.check(c, state, TODAY, fetch=good)
    assert li.check(c, state, TODAY + dt.timedelta(days=7), fetch=down) == 0          # not due yet
    assert li.check(c, state, TODAY + dt.timedelta(days=28), fetch=down) == 1
    e = state["acmehomes"]
    assert e["url"] == "https://www.linkedin.com/company/acme-homes/" and "503" in e["error"]
    assert e["first_found"] == TODAY.isoformat()
    assert li.check(c, state, TODAY + dt.timedelta(days=34), fetch=moved) == 0        # retried weekly after an error
    assert li.check(c, state, TODAY + dt.timedelta(days=35), fetch=moved) == 1
    e = state["acmehomes"]
    assert e["url"] == "https://www.linkedin.com/company/acme-homes-inc/"
    assert e["previous"] == "https://www.linkedin.com/company/acme-homes/"
    assert "used to link to" in li.review_rows(c, state)[0]["what_to_check"]
    # a new website in the list means a fresh look straight away
    c[0].website = "https://acme.test"
    assert li.check(c, state, TODAY + dt.timedelta(days=36), fetch=site({})) == 1


def test_a_company_on_both_lists_is_checked_once():
    both = li.merged([L("Acme Homes", "https://www.acmehomes.test")],
                     [L("ACME Homes", "", approved="https://www.linkedin.com/company/acme-homes/", lists=("OpCo",))])
    assert len(both) == 1 and both[0].lists == ["Weekly", "OpCo"]
    assert both[0].approved == "https://www.linkedin.com/company/acme-homes/"


def test_command_line_run_writes_state_and_review_never_asking_linkedin(tmp_path, monkeypatch):
    leads = tmp_path / "leads.csv"
    leads.write_text("company,website,state,careers_url,segment,tier,sheet_verified,notes\n"
                     "Acme Homes,https://www.acmehomes.test,NC,https://www.acmehomes.test/careers,PropTech,A,TRUE,\n")
    monkeypatch.setattr(li, "LEADS", leads)
    monkeypatch.setattr(li, "FOUND", tmp_path / "data" / "linkedin_found.json")
    monkeypatch.setattr(li, "REVIEW", tmp_path / "output" / "linkedin-review.csv")
    monkeypatch.setattr(li, "_opco_companies", lambda: [L("Flock Homes", "https://flockhomes.test", lists=("OpCo",))])
    routes = {"GET https://www\\.acmehomes\\.test/$": {"text": page(footer("https://www.linkedin.com/company/acme-homes"))},
              "GET https://flockhomes\\.test/$": {"text": page(footer("https://www.linkedin.com/company/flock-homes"))}}
    with fakenet.serve(routes) as calls:
        assert li.main(["--no-browser", "--date", TODAY.isoformat()]) == 0
    assert not [c for c in calls if "linkedin.com" in c]
    rows = list(csv.DictReader(open(tmp_path / "output" / "linkedin-review.csv")))
    assert {(r["company"], r["linkedin_found"], r["list"]) for r in rows} == {
        ("Acme Homes", "https://www.linkedin.com/company/acme-homes/", "Weekly"),
        ("Flock Homes", "https://www.linkedin.com/company/flock-homes/", "OpCo")}
    assert set(json.loads((tmp_path / "data" / "linkedin_found.json").read_text())) == {"acmehomes", "flockhomes"}


# ---------------------------------------------------------------- approved pages reach the outputs
def test_the_approved_column_is_found_by_its_header():
    assert li.approved_from_row({"company": "A", "LinkedIn": "linkedin.com/company/acme"}) == \
        "https://www.linkedin.com/company/acme/"
    assert li.approved_from_row({"company": "A", "LinkedIn URL": "n/a"}) == ""
    assert li.approved_from_row({"company": "A", "notes": "https://www.linkedin.com/company/acme"}) == ""
    assert li.approved_column(["Company Name", "Website URL", "Company LinkedIn Page"]) == 2


def test_opco_tab_with_a_linkedin_column():
    text = ("Company Name,Website URL,State,Careers Page URL,LinkedIn\n"
            "Flock Homes,https://www.flockhomes.com,California,https://www.flockhomes.com/careers,"
            "https://www.linkedin.com/company/flock-homes\n"
            "Other Co,https://other.test,NC,https://other.test/careers,\n")
    cs = opco_search.parse_companies(text, "test")
    assert [c.linkedin for c in cs] == ["https://www.linkedin.com/company/flock-homes/", ""]
    assert cs[0].careers_url == "https://www.flockhomes.com/careers"          # Column D unaffected


def test_weekly_outputs_carry_the_approved_page(tmp_path, monkeypatch):
    data, out = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    leads = tmp_path / "leads.csv"
    leads.write_text("company,website,state,careers_url,segment,tier,sheet_verified,notes,LinkedIn\n"
                     "Acme,https://www.acme.test,NC,https://www.acme.test/careers,PropTech,A,TRUE,,"
                     "https://www.linkedin.com/company/acme\n")
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "greenhouse", "slug": "acme", "confidence": "manual"}}))
    (data / "history.json").write_text("{}")
    for name, value in (("DATA", data), ("OUT", out), ("LEADS", leads), ("ATS_MAP", data / "ats_map.json"),
                        ("HISTORY", data / "history.json"), ("NEEDS_MAP", data / "needs.csv")):
        monkeypatch.setattr(run, name, value)
    routes = json.loads((ROOT / "tests/fixtures/readers/payloads.json").read_text())
    with fakenet.serve(routes):
        assert run.main(["--no-verify", "--no-vc", "--no-browser", "--sleep", "0", "--date", "2026-10-11"]) == 0
    d = out / "2026-10-11"
    rows = list(csv.DictReader(open(d / "all_open_roles.csv")))
    assert rows and {r["company_linkedin"] for r in rows} == {"https://www.linkedin.com/company/acme/"}
    assert list(csv.DictReader(open(d / "company_status.csv")))[0]["linkedin"] == "https://www.linkedin.com/company/acme/"
