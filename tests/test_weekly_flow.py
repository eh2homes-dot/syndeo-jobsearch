"""Offline tests for how the weekly search reads a company (jobsearch/company.py)
and how a change of job board is carried through history (jobsearch/run.py).
"""
import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fakenet                                   # noqa: E402
from jobsearch import company, run               # noqa: E402

ROUTES = json.loads((ROOT / "tests/fixtures/readers/payloads.json").read_text())
EMBED = {"text": "<html><body><h1>Careers</h1><script src='https://boards.greenhouse.io/embed/job_board/js?for=acme'></script></body></html>"}
LINKS_ASHBY = {"text": "<html><body><h1>Careers</h1><a href='https://jobs.ashbyhq.com/acme'>View openings</a></body></html>"}
PLAIN = {"text": "<html><body><h1>Careers</h1><p>We are a great place to work.</p></body></html>"}


def lead(name="Acme", careers="https://www.acme.test/careers"):
    return {"company": name, "website": "https://www.acme.test", "state": "NC", "careers_url": careers,
            "segment": "PropTech", "tier": "A", "sheet_verified": "TRUE", "notes": ""}


def routes(**extra):
    r = dict(ROUTES)
    r.update(extra)
    return r


def test_board_on_file_is_read():
    with fakenet.serve(ROUTES):
        r = company.read_company(lead(), {"ats": "greenhouse", "slug": "acme", "confidence": "manual"}, today="2026-10-11")
    assert r.status == "ok" and r.mapping is None
    assert [j["job_key"] for j in r.jobs] == ["greenhouse:acme:4012345", "greenhouse:acme:4012346"]


def test_no_board_on_file_reads_the_careers_page_and_remembers():
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED})):
        r = company.read_company(lead(), {}, today="2026-10-11")
    assert r.status == "ok" and len(r.jobs) == 2
    assert (r.mapping["ats"], r.mapping["slug"], r.mapping["confidence"]) == ("greenhouse", "acme", "page")


def test_old_built_in_entry_is_ignored_and_the_companys_own_board_is_used():
    old = {"ats": "builtin", "slug": "acme", "confidence": "builtin-domain-match"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED})) as calls:
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status == "ok" and r.mapping["ats"] == "greenhouse"
    assert not any("builtin.com" in c for c in calls)


def test_missing_board_is_replaced_by_the_one_the_careers_page_points_to():
    old = {"ats": "lever", "slug": "ghost", "confidence": "manual"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": LINKS_ASHBY})):
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status == "ok" and [j["job_key"] for j in r.jobs] == ["ashby:acme:aaaa1111-0000-4000-8000-000000000001"]
    assert r.mapping["ats"] == "ashby" and r.mapping["previous"] == {"ats": "lever", "slug": "ghost", "confidence": "manual"}
    assert "job board changed (lever: ghost -> ashby: acme" in r.note


def test_missing_board_with_no_replacement_is_a_failure_not_a_zero():
    old = {"ats": "lever", "slug": "ghost", "confidence": "manual"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": PLAIN})):
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status.startswith("failed:Lever has no board named 'ghost'") and r.jobs == []


def test_empty_board_stays_a_real_zero_when_the_page_offers_nothing_else():
    old = {"ats": "lever", "slug": "quiet", "confidence": "manual"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": PLAIN})):
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status == "ok" and r.jobs == [] and r.mapping is None


def test_sudden_zero_is_treated_as_a_failure():
    old = {"ats": "lever", "slug": "quiet", "confidence": "manual"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": PLAIN})):
        r = company.read_company(lead(), old, today="2026-10-11", open_before=12)
    assert r.status.startswith("failed:dropped to 0 roles from 12")


def test_parent_and_excluded_are_not_read():
    with fakenet.serve(ROUTES) as calls:
        a = company.read_company(lead("Mynd"), {"parent": "Roofstock"}, today="2026-10-11")
        b = company.read_company(lead("X"), {"ats": "", "confidence": "excluded"}, today="2026-10-11")
    assert a.status == "covered by parent: Roofstock" and b.status == "excluded" and calls == []


def test_third_party_listing_site_is_never_read():
    with fakenet.serve(ROUTES) as calls:
        r = company.read_company(lead(careers="https://builtin.com/company/acme/jobs"), {}, today="2026-10-11")
    assert r.status.startswith("needs-link") and "third-party listing site" in r.status and calls == []


def test_page_jobs_get_a_key_from_the_company_and_the_posting_path():
    page = {"text": "<html><body><h1>Open Positions</h1>"
                    "<div><h3>Asset Manager</h3><p>Birmingham, AL</p><a href='/careers/jobs/asset-manager'>Learn more</a></div>"
                    "<div><h3>Financial Analyst</h3><a href='/careers/jobs/financial-analyst'>Learn more</a></div></body></html>"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": page})):
        r = company.read_company(lead("Acme Realty"), {}, today="2026-10-11")
    assert [j["job_key"] for j in r.jobs] == ["page:acme-realty:/careers/jobs/asset-manager",
                                              "page:acme-realty:/careers/jobs/financial-analyst"]
    assert r.mapping["ats"] == "page" and r.jobs[0]["url"] == "https://www.acme.test/careers/jobs/asset-manager"


# ---------------------------------------------------------------- end to end
def _setup(tmp_path, monkeypatch, history):
    data, out = tmp_path / "data", tmp_path / "output"
    data.mkdir()
    leads = tmp_path / "leads.csv"
    with leads.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lead().keys()))
        w.writeheader()
        w.writerow(lead())
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "builtin", "slug": "acme", "confidence": "builtin-domain-match"}}))
    (data / "history.json").write_text(json.dumps(history))
    for name, value in (("DATA", data), ("OUT", out), ("LEADS", leads), ("ATS_MAP", data / "ats_map.json"),
                        ("HISTORY", data / "history.json"), ("NEEDS_MAP", data / "needs.csv")):
        monkeypatch.setattr(run, name, value)
    return data, out


def _rows(path):
    return list(csv.DictReader(open(path)))


def test_switching_a_company_to_its_own_board_reports_nothing_false(tmp_path, monkeypatch):
    """A company moves from Built In to its own Greenhouse board. Its Built In roles must not be
    reported as closed, and its Greenhouse roles must not be reported as new - until one really is."""
    old = {"builtin:acme:111": {"company": "Acme", "title": "Account Executive", "location": "Remote",
                                "url": "https://builtin.com/job/x/111", "posted_at": "", "ats": "builtin", "focus": "sales",
                                "first_seen": "2026-09-28", "last_seen": "2026-09-28", "status": "open"}}
    data, out = _setup(tmp_path, monkeypatch, old)
    args = ["--detect", "--no-verify", "--no-vc", "--no-browser", "--sleep", "0"]

    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED})):
        assert run.main(args + ["--date", "2026-10-11"]) == 0
    d = out / "2026-10-11"
    assert _rows(d / "new_this_week.csv") == [] and _rows(d / "closed_this_week.csv") == []
    assert [r["job_key"] for r in _rows(d / "open_roles.csv")] == ["greenhouse:acme:4012345", "greenhouse:acme:4012346"]
    h = json.loads((data / "history.json").read_text())
    assert h["builtin:acme:111"]["status"] == "retired"
    assert h["greenhouse:acme:4012345"]["baseline"] is True
    assert json.loads((data / "ats_map.json").read_text())["Acme"]["ats"] == "greenhouse"
    status = _rows(d / "company_status.csv")[0]
    assert status["status"] == "ok" and "embeds" in status["note"]
    assert "0 new" in (out / "newsletter" / "now-hiring-2026-10-11.md").read_text()

    # A week later: one role gone, one added. Now they ARE news.
    week2 = json.loads(json.dumps(ROUTES["GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs"]))
    week2["json"]["jobs"] = [week2["json"]["jobs"][1],
                             {"id": 4099999, "title": "VP, Engineering", "location": {"name": "Raleigh, NC"},
                              "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4099999",
                              "first_published": "2026-10-14T10:00:00-04:00"}]
    with fakenet.serve(routes(**{"GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": week2})):
        assert run.main(args + ["--date", "2026-10-18"]) == 0
    d = out / "2026-10-18"
    assert [r["job_key"] for r in _rows(d / "new_this_week.csv")] == ["greenhouse:acme:4099999"]
    assert [r["job_key"] for r in _rows(d / "closed_this_week.csv")] == ["greenhouse:acme:4012345"]


def test_a_blip_on_the_board_is_a_failed_read_and_changes_nothing():
    """503 from the board on file: no re-mapping from the careers page, even if that page lists jobs."""
    old = {"ats": "greenhouse", "slug": "acme", "confidence": "manual"}
    page = {"text": "<html><body><h1>Open Positions</h1><div><h3>Asset Manager</h3>"
                    "<a href='/careers/jobs/asset-manager'>Learn more</a></div></body></html>"}
    down = {"GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": {"status": 503, "text": "busy"}}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": page, **down})):
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status.startswith("failed:") and r.mapping is None and r.jobs == []


def test_same_board_spelled_differently_is_not_a_board_change():
    """Workday on file as {"wd": ...}; the careers page links to the same board by host name."""
    old = {"ats": "workday", "slug": "acme", "wd": "wd5", "site": "External", "confidence": "html"}
    page = {"text": "<html><body><a href='https://acme.wd5.myworkdayjobs.com/en-US/External'>Search jobs</a></body></html>"}
    empty = {"POST https://acme\\.wd5\\.myworkdayjobs\\.com/wday/cxs/acme/External/jobs": {"json": {"total": 0, "jobPostings": []}}}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": page, **empty})):
        r = company.read_company(lead(), old, today="2026-10-11")
    assert r.status == "ok" and r.jobs == [] and r.mapping is None


def test_sudden_zero_is_believed_the_second_time():
    old = {"ats": "lever", "slug": "quiet", "confidence": "manual"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": PLAIN})):
        first = company.read_company(lead(), old, today="2026-10-11", open_before=12)
        assert first.status.startswith("failed:dropped to 0") and first.mapping["zero_since"] == "2026-10-11"
        second = company.read_company(lead(), first.mapping, today="2026-10-18", open_before=12)
    assert second.status == "ok" and second.jobs == [] and "zero_since" not in second.mapping


def test_renaming_a_board_on_the_same_job_system_reports_nothing_false(tmp_path, monkeypatch):
    """greenhouse:oldacme -> greenhouse:acme. Same roles, new ids: neither 'new' nor 'closed'."""
    hist = {f"greenhouse:oldacme:{i}": {"company": "Acme", "title": t, "location": "Remote", "url": "u", "posted_at": "",
                                         "ats": "greenhouse", "focus": "", "first_seen": "2026-09-28",
                                         "last_seen": "2026-10-04", "status": "open"}
            for i, t in ((1, "Account Executive"), (2, "Staff Software Engineer"))}
    data, out = _setup(tmp_path, monkeypatch, hist)
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "greenhouse", "slug": "oldacme", "confidence": "html"}}))
    args = ["--detect", "--no-verify", "--no-vc", "--no-browser", "--sleep", "0", "--date", "2026-10-11"]
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED})):
        assert run.main(args) == 0
    d = out / "2026-10-11"
    assert _rows(d / "new_this_week.csv") == [] and _rows(d / "closed_this_week.csv") == []
    h = json.loads((data / "history.json").read_text())
    assert h["greenhouse:oldacme:1"]["status"] == "retired" and h["greenhouse:acme:4012345"]["baseline"] is True
    saved = json.loads((data / "ats_map.json").read_text())["Acme"]
    assert saved["slug"] == "acme" and saved["previous"]["slug"] == "oldacme"
    assert "job board changed" in (d / "report.md").read_text()


# --------------------------------------------- what closes, and what is checked
def _history_role(key, title="Director of Sales", url="https://x.test/p"):
    return {key: {"company": "Acme", "title": title, "location": "Remote", "url": url, "posted_at": "",
                  "ats": key.split(":")[0], "focus": "sales", "first_seen": "2026-09-20",
                  "last_seen": "2026-10-04", "status": "open"}}


def test_a_role_the_job_boards_feed_no_longer_lists_is_closed_even_if_its_page_still_loads(tmp_path, monkeypatch):
    """Most job systems answer a closed posting's address with a normal page. That must not re-open it."""
    hist = _history_role("greenhouse:acme:999", url="https://job-boards.greenhouse.io/acme/jobs/999")
    data, out = _setup(tmp_path, monkeypatch, hist)
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "greenhouse", "slug": "acme", "confidence": "html"}}))
    still_loads = {"GET https://job-boards\\.greenhouse\\.io/acme/jobs/": {"text": "<html><body>" + "A job page. " * 500 + "</body></html>"}}
    with fakenet.serve(routes(**still_loads)):
        assert run.main(["--no-vc", "--no-browser", "--sleep", "0", "--date", "2026-10-11"]) == 0
    d = out / "2026-10-11"
    closed = _rows(d / "closed_this_week.csv")
    assert [c["job_key"] for c in closed] == ["greenhouse:acme:999"]
    assert closed[0]["link_status"] == "no longer listed by the job board"
    assert _rows(d / "scraper_misses.csv") == []


def test_a_role_missing_from_a_careers_page_stays_open_while_its_posting_still_loads(tmp_path, monkeypatch):
    """A page read can miss a job; there, a posting that still loads means "we missed it"."""
    hist = _history_role("page:acme:/careers/jobs/director-of-sales", url="https://www.acme.test/careers/jobs/director-of-sales")
    data, out = _setup(tmp_path, monkeypatch, hist)
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "page", "slug": "acme", "url": "https://www.acme.test/careers", "confidence": "page"}}))
    page = {"text": "<html><body><h1>Open Positions</h1><div><h3>Asset Manager</h3>"
                    "<a href='/careers/jobs/asset-manager'>Learn more</a></div></body></html>"}
    posting = {"text": "<html><body>" + "Director of Sales posting. " * 300 + "</body></html>"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": page,
                                 "GET https://www\\.acme\\.test/careers/jobs/": posting})):
        assert run.main(["--no-vc", "--no-browser", "--sleep", "0", "--date", "2026-10-11"]) == 0
    d = out / "2026-10-11"
    assert _rows(d / "closed_this_week.csv") == []
    assert [m["job_key"] for m in _rows(d / "scraper_misses.csv")] == ["page:acme:/careers/jobs/director-of-sales"]


def test_roles_the_old_link_check_had_been_holding_open_close_quietly_once(tmp_path, monkeypatch):
    hist = {**_history_role("greenhouse:acme:777", "VP of Sales"), **_history_role("greenhouse:acme:888", "Head of Growth")}
    data, out = _setup(tmp_path, monkeypatch, hist)
    (data / "ats_map.json").write_text(json.dumps({"Acme": {"ats": "greenhouse", "slug": "acme", "confidence": "html"}}))
    (data / "last_run.json").write_text(json.dumps({"date": "2026-10-04"}))          # written by the old code
    prev = out / "2026-10-04"
    prev.mkdir(parents=True)
    (prev / "scraper_misses.csv").write_text("company,title,url,link_status,first_seen,ats,job_key\n"
                                             "Acme,VP of Sales,u,live,2026-09-20,greenhouse,greenhouse:acme:777\n")
    with fakenet.serve(ROUTES):
        assert run.main(["--no-vc", "--no-browser", "--no-verify", "--sleep", "0", "--date", "2026-10-11"]) == 0
    closed = [c["job_key"] for c in _rows(out / "2026-10-11" / "closed_this_week.csv")]
    assert closed == ["greenhouse:acme:888"]                    # 777 came down weeks ago: closed, not reported
    h = json.loads((data / "history.json").read_text())
    assert h["greenhouse:acme:777"]["status"] == "closed" and h["greenhouse:acme:777"]["late"] is True
    assert json.loads((data / "last_run.json").read_text())["feed_decides"] is True


def test_workday_postings_are_checked_against_workdays_own_data():
    from jobsearch import verify
    base = "https://acme\\.wd5\\.myworkdayjobs\\.com/wday/cxs/acme/External/job/Remote/"
    r = {f"GET {base}Open_R1$": {"json": {"jobPostingInfo": {"title": "Director", "posted": True}}},
         f"GET {base}Gone_R2$": {"status": 403, "json": {"errorCode": "S22", "message": "permission denied"}},
         "GET https://acme\\.wd5\\.myworkdayjobs\\.com/External/job/": {"text": "<html>app shell</html>"}}
    with fakenet.serve(r) as calls:
        assert verify.check("https://acme.wd5.myworkdayjobs.com/External/job/Remote/Open_R1") == ("live", 200)
        assert verify.check("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Gone_R2") == ("gone", 403)
    assert all("/wday/cxs/" in c for c in calls)                # the always-200 public page is never asked


def test_migration_is_applied_once_and_never_overwrites_later_edits(tmp_path):
    ats_map = {"Placer.ai": {"ats": "builtin", "slug": "placerai"}, "Robin": {"ats": "builtin", "slug": "robin"},
               "Mason": {"ats": "", "confidence": "manual"}, "Entrata": {"ats": "lever", "slug": "entrata"}}
    applied = tmp_path / "applied.json"
    assert run.apply_migrations(ats_map, applied) == ["2026-10-own-boards"]
    assert ats_map["Placer.ai"]["ats"] == "greenhouse" and ats_map["Mynd"] == {**ats_map["Mynd"], "parent": "Roofstock"}
    assert "Robin" not in ats_map                               # Built In entry dropped: read from its careers page
    assert ats_map["Mason"]["confidence"] == "manual" and ats_map["Entrata"]["slug"] == "entrata"
    assert not any(v.get("ats") == "builtin" for v in ats_map.values())
    ats_map["Placer.ai"] = {"ats": "lever", "slug": "edited-by-hand"}
    assert run.apply_migrations(ats_map, applied) == []
    assert ats_map["Placer.ai"]["slug"] == "edited-by-hand"
