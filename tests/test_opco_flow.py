"""Offline tests for the OpCo search (opco_search.py) on top of the shared readers."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fakenet            # noqa: E402
import opco_search as O   # noqa: E402

ROUTES = json.loads((ROOT / "tests/fixtures/readers/payloads.json").read_text())
EMBED = {"text": "<html><body><h1>Careers</h1><script src='https://boards.greenhouse.io/embed/job_board/js?for=acme'></script></body></html>"}
HEADER = "Company Name,Website URL,State,Careers Page URL,Sample Open Roles / Focus,Industry Segment,Verification Notes,Verified\n"
CONFIG = {"include": {"executive": ["vice president", "VP", "head of", "director"], "sales": ["sales", "account executive"],
                      "engineering": ["engineer\\w*"]},
          "exclude": ["maintenance"]}


def routes(**extra):
    r = dict(ROUTES)
    r.update(extra)
    return r


def company(name, column_d):
    return O.Company(name=name, website="https://www.acme.test", careers_url=column_d,
                     careers_from="Column D" if column_d else "")


def read(name, column_d, baseline=None, **extra_routes):
    with fakenet.serve(routes(**extra_routes)):
        return O.read_company(company(name, column_d), O.RoleFilter(CONFIG), baseline or _baseline({}))


def _baseline(data):
    b = O.Baseline("2026-10-11")
    b.data = data
    return b


def test_column_d_job_board_link():
    r = read("Acme", "https://job-boards.greenhouse.io/acme")
    assert r["status"] == "ok" and r["system"] == "greenhouse" and r["via"] == ""
    assert [x["id"] for x in r["roles"]] == ["gh-4012345", "gh-4012346"]
    assert [x["category"] for x in r["filtered"]] == ["sales", "engineering"]


def test_column_d_careers_page_is_read_through_the_board_it_loads():
    r = read("Acme", "https://www.acme.test/careers", **{"GET https://www\\.acme\\.test/careers$": EMBED})
    assert r["status"] == "ok" and r["system"] == "page"
    assert r["via"] == "greenhouse:acme:{}" and r["via_url"] == "https://job-boards.greenhouse.io/acme"
    assert [x["id"] for x in r["roles"]] == ["gh-4012345", "gh-4012346"]
    md = "\n".join(O._attention({"Acme": r}))
    assert "paste these into Column D" in md and "https://job-boards.greenhouse.io/acme" in md


def test_newly_supported_systems():
    r = read("Atlas", "https://recruiting.paylocity.com/recruiting/jobs/All/5e5fc082-a90b-4d93-8b73-46f77d40e081")
    assert r["status"] == "ok" and [x["id"] for x in r["roles"]] == [
        "paylocity-5e5fc082-a90b-4d93-8b73-46f77d40e081-4375500", "paylocity-5e5fc082-a90b-4d93-8b73-46f77d40e081-4561431"]
    r = read("RealPage", "https://careers-acme.icims.com/jobs/search?ss=1")
    assert r["status"] == "ok" and r["total"] == 3 and r["filtered"][-1]["title"] == "VP, Engineering"


def test_empty_and_non_links():
    assert read("A", "")["reason"] == "Column D is empty"
    assert read("B", "TBD - ask HR")["status"] == "needs-link"
    assert "homepage" in read("C", "https://www.acme.test/", **{"GET https://www\\.acme\\.test/$": EMBED})["reason"]


def test_job_system_with_no_reader_is_reported_as_unsupported():
    r = read("Old Co", "https://oldco.taleo.net/careersection/2/jobsearch.ftl")
    assert r["status"] == "unsupported" and r["reason"].startswith("taleo isn't supported yet")


def test_failure_carries_last_weeks_roles_forward():
    prev = {"Acme": {"fetched_on": "2026-10-04", "board": "greenhouse:acme:{}", "roles": [
        {"id": "gh-1", "title": "Director of Sales", "url": "u", "location": "", "department": "", "posted": ""}]}}
    down = {"GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": {"status": 503, "text": "busy"}}
    r = read("Acme", "https://job-boards.greenhouse.io/acme", _baseline(prev), **down)
    assert r["status"] == "stale" and [x["id"] for x in r["roles"]] == ["gh-1"]


def test_a_board_that_no_longer_exists_is_a_link_to_fix():
    r = read("Acme", "https://jobs.lever.co/ghost")
    assert r["status"] == "needs-link" and "isn't there" in r["reason"]


def test_a_page_that_switches_job_system_starts_a_fresh_baseline():
    b = _baseline({"Acme": {"fetched_on": "2026-10-04", "board": 'page::{"url": "https://www.acme.test/careers"}',
                            "via": "lever:acme:{}", "roles": [{"id": "lv-1", "title": "Director of Sales", "url": "u"}]}})
    r = read("Acme", "https://www.acme.test/careers", b, **{"GET https://www\\.acme\\.test/careers$": EMBED})
    assert r["status"] == "ok" and r.get("spot_check")                     # treated as a first read
    assert O.compute_diff({"Acme": r}, b, O.RoleFilter(CONFIG)) == {"new": [], "closed": []}


def test_weekly_run_end_to_end(tmp_path, monkeypatch):
    import yaml
    state = tmp_path / "state" / "opco"
    monkeypatch.setattr(O, "STATE", state)
    monkeypatch.setattr(O, "BASELINE_PATH", state / "baseline.json")
    monkeypatch.setattr(O, "LIVE_COPY", state / "opco_live.csv")
    monkeypatch.setattr(O, "remove_legacy_state", lambda: None)
    csv_path, cfg_path, out = tmp_path / "opco.csv", tmp_path / "cfg.yml", tmp_path / "out"
    csv_path.write_text(HEADER + "Acme,https://www.acme.test,NC,https://www.acme.test/careers,,REIT,,TRUE\n"
                                 "Lamar,https://lamar.test,LA,https://recruiting2.ultipro.com/ACM1000ACME/JobBoard/82898216-ca7a-4621-b01b-bf3410c919b2/,,REIT,,TRUE\n"
                                 "NoLink,https://nolink.test,TX,,,REIT,,FALSE\n")
    cfg_path.write_text(yaml.safe_dump(CONFIG))
    argv = ["opco_search.py", "--companies", str(csv_path), "--config", str(cfg_path), "--out", str(out), "--no-browser"]

    monkeypatch.setattr(sys, "argv", argv)
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED})):
        assert O.main() == 0
    first = json.loads((out / "opco-jobs-latest.json").read_text())
    assert {n: c["status"] for n, c in first["coverage"].items()} == {"Acme": "ok", "Lamar": "ok", "NoLink": "needs-link"}
    assert len(first["roles"]) == 4 and first["new"] == [] and first["closed"] == []     # first run is the baseline

    week2 = json.loads(json.dumps(ROUTES["GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs"]))
    week2["json"]["jobs"][0] = {"id": 4099999, "title": "Head of Sales", "location": {"name": "Remote"},
                                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4099999"}
    with fakenet.serve(routes(**{"GET https://www\\.acme\\.test/careers$": EMBED,
                                 "GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": week2})):
        assert O.main() == 0
    second = json.loads((out / "opco-jobs-latest.json").read_text())
    assert [r["id"] for r in second["new"]] == ["gh-4099999"]
    assert [r["id"] for r in second["closed"]] == ["gh-4012345"]
    brief = (out / "opco-jobs-latest.md").read_text()
    assert "Read through the page's job board" in brief and "Head of Sales" in brief


def test_blip_at_the_board_behind_a_careers_page_keeps_last_weeks_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "BASELINE_PATH", tmp_path / "baseline.json")
    """Column D is a careers page read through Greenhouse. Greenhouse is down this week: the
    company must be 'stale' with its roles carried forward, not 'needs a link' with history wiped."""
    key = 'page::{"url": "https://www.acme.test/careers"}'
    b = _baseline({"Acme": {"fetched_on": "2026-10-04", "board": key, "via": "greenhouse:acme:{}",
                            "roles": [{"id": "gh-4012345", "title": "Account Executive", "url": "u"}]}})
    down = {"GET https://boards-api\\.greenhouse\\.io/v1/boards/acme/jobs": {"status": 503, "text": "busy"}}
    r = read("Acme", "https://www.acme.test/careers", b, **{"GET https://www\\.acme\\.test/careers$": EMBED, **down})
    assert r["status"] == "stale" and [x["id"] for x in r["roles"]] == ["gh-4012345"]
    b.save({"Acme": r})
    assert b.data["Acme"]["roles"][0]["id"] == "gh-4012345"      # baseline untouched


def test_sudden_zero_is_believed_the_second_week():
    roles = [{"id": f"lv-{i}", "title": "Director of Sales", "url": "u"} for i in range(6)]
    b = _baseline({"Quiet": {"fetched_on": "2026-10-04", "board": "lever:quiet:{}", "roles": roles}})
    first = read("Quiet", "https://jobs.lever.co/quiet", b)
    assert first["status"] == "stale" and len(first["roles"]) == 6
    b.run_date = "2026-10-18"
    second = read("Quiet", "https://jobs.lever.co/quiet", b)
    assert second["status"] == "ok" and second["roles"] == []
    assert len(O.compute_diff({"Quiet": second}, b, O.RoleFilter(CONFIG))["closed"]) == 6


def test_out_of_time_skips_pages_but_still_reads_job_boards():
    key = 'page::{"url": "https://www.acme.test/careers"}'
    b = _baseline({"Acme": {"fetched_on": "2026-10-04", "board": key, "via": "",
                            "roles": [{"id": "op-/x", "title": "Head of Sales", "url": "u"}]}})
    with fakenet.serve(ROUTES) as calls:
        page = O.read_company(company("Acme", "https://www.acme.test/careers"), O.RoleFilter(CONFIG), b, out_of_time=True)
        board = O.read_company(company("B", "https://job-boards.greenhouse.io/acme"), O.RoleFilter(CONFIG), b, out_of_time=True)
    assert page["status"] == "stale" and len(page["filtered"]) == 1 and "time budget" in page["reason"]
    assert board["status"] == "ok" and not any("acme.test" in c for c in calls)


def test_closures_are_reported_when_a_page_read_through_a_board_goes_to_zero():
    key = 'page::{"url": "https://www.acme.test/careers"}'
    roles = [{"id": f"gh-{i}", "title": "Director of Sales", "url": "u"} for i in range(3)]
    b = _baseline({"Acme": {"fetched_on": "2026-10-04", "board": key, "via": "greenhouse:acme:{}", "roles": roles}})
    page = {"text": "<html><body><h1>Careers</h1><p>We have no open positions right now.</p></body></html>"}
    r = read("Acme", "https://www.acme.test/careers", b, **{"GET https://www\\.acme\\.test/careers$": page})
    assert r["status"] == "ok" and r["roles"] == []
    assert len(O.compute_diff({"Acme": r}, b, O.RoleFilter(CONFIG))["closed"]) == 3
