"""Rules that keep the 'Now hiring' draft from being dominated by the biggest companies."""
import datetime as dt

from jobsearch import run


def test_vice_president_is_not_c_suite_and_field_vps_rank_with_directors():
    assert run._seniority("Chief Revenue Officer") == 0
    assert run._seniority("President") == 0
    assert run._seniority("Vice President of Sales") == 2
    assert run._seniority("Regional Vice President of Sales, East Region") == run.DIRECTOR_LEVEL
    assert run._seniority("Area VP, Enterprise") == run.DIRECTOR_LEVEL
    assert run._seniority("Director, Engineering") == run.DIRECTOR_LEVEL


def test_us_filter():
    for loc in ["Remote-USA", "Dallas, Texas", "", "Multiple locations", "New Mexico", "Remote - US; Toronto",
                "Boca Raton; Boston; New York City; Seattle"]:
        assert run._is_us(loc), loc
    for loc in ["Bengaluru", "Mexico City", "Remote-Germany", "Toronto, Canada", "London, United Kingdom",
                "Kraków, Lesser Poland Voivodeship, Poland", "Latin America"]:
        assert not run._is_us(loc), loc


def test_issue_window_starts_at_last_wednesday():
    wed = 2
    assert run._issue_cutoff(dt.date(2026, 10, 11), wed) == dt.date(2026, 10, 7)   # Sunday -> last Wed
    assert run._issue_cutoff(dt.date(2026, 10, 14), wed) == dt.date(2026, 10, 14)  # Wed itself -> next issue
    assert run._issue_cutoff(dt.date(2026, 10, 13), wed) == dt.date(2026, 10, 7)   # Tuesday


def _job(company, title, tier="B", loc="Remote-USA", group="gtm", first_seen="2026-10-11", n=0):
    return {"company": company, "title": title, "tier": tier, "location": loc, "role_group": group,
            "first_seen": first_seen, "url": f"https://x.test/{company}/{n}"}


def _ledger(tmp_path, monkeypatch, issues):
    import json
    monkeypatch.setattr(run, "OUT", tmp_path / "out")
    monkeypatch.setattr(run, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "featured_companies.json").write_text(json.dumps({"issues": issues}))
    return tmp_path / "data" / "featured_companies.json"


def test_draft_ranks_by_tier_and_rotation_not_volume(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, {"2026-09-16": {"source": "published", "companies": ["BigCo Group"]}})
    jobs = ([_job("BigCo", f"Director, Sales {i}", n=i) for i in range(20)]
            + [_job("BigCo", "Director, Sales Mexico", loc="Mexico City", n=99)]
            + [_job("SmallCo", "Account Executive", tier="A")]
            + [_job("MidCo", "Sales Manager")]
            + [_job("OldCo", "VP Sales", first_seen="2026-10-04")])          # already went out on Oct 7
    path = run.write_jobs_newsletter(jobs, [], {}, dt.date(2026, 10, 11), {})
    lines = [l for l in path.read_text().splitlines() if l.startswith("- **")]
    # tier A first, then never featured, then BigCo (featured 4 weeks ago, so back in rotation but last)
    assert [l.split("**")[1] for l in lines] == ["SmallCo", "MidCo", "BigCo"]
    assert lines[2].count("](") == 2                     # at most 2 roles per company line
    text = path.read_text()
    assert "OldCo" not in text and "Mexico" not in text
    assert "_22 new" in text                             # 20 BigCo US + SmallCo + MidCo


def test_cooldown_holds_companies_out_for_three_issues(tmp_path, monkeypatch):
    import json
    led = _ledger(tmp_path, monkeypatch, {
        "2026-09-23": {"source": "published", "companies": ["Zillow"]},        # 3 issues before Oct 14: still out
        "2026-10-07": {"source": "published", "companies": ["Procore"]},       # last issue: out
        "2026-10-14": {"source": "published", "companies": ["Hand Added Co"]},
    })
    jobs = [_job("Zillow Group", "Account Executive"), _job("Procore Technologies", "Software Engineer", group="product_engineering"),
            _job("FreshCo", "VP Marketing", group="executive"), _job("BackCo", "Sales Manager")]
    text = run.write_jobs_newsletter(jobs, [], {}, dt.date(2026, 10, 11), {}).read_text()
    assert "Zillow" not in text and "Procore" not in text
    assert "FreshCo" in text and "BackCo" in text
    assert any("Held back" in n and "Zillow Group" in n and "Procore Technologies" in n for n in run.LAST_DRAFT_NOTES)
    issues = json.loads(led.read_text())["issues"]
    assert issues["2026-10-14"] == {"source": "published", "companies": ["Hand Added Co"]}   # published wins over draft

    # A week later (issue Oct 21) the Sept 23 feature is 4 weeks old: Zillow is back; the draft is recorded.
    run.write_jobs_newsletter([_job("Zillow Group", "Account Executive", first_seen="2026-10-18")], [], {},
                              dt.date(2026, 10, 18), {})
    issues = json.loads(led.read_text())["issues"]
    assert issues["2026-10-21"] == {"source": "draft", "companies": ["Zillow Group"]}


def test_three_buckets_follow_evans_rules():
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text())
    expect = {
        # Leadership: VP and up, Head of, GM. Directors stay in their function; field VPs are sales.
        "VP, Engineering": "executive", "Head of Product": "executive", "Chief Revenue Officer": "executive",
        "Director of Engineering": "product_engineering", "Director of Sales": "gtm",
        "Regional Vice President of Sales, East Region": "gtm",
        # GTM & Sales, including technical-sounding GTM titles and GTM analysts
        "Sales Engineer": "gtm", "Product Marketing Manager": "gtm", "Customer Success Manager": "gtm",
        "Senior Data Analyst, GTM Analytics": "gtm", "Senior Analyst, Growth & Marketing Analytics": "gtm",
        # Product & Engineering: software, product, design, data builders
        "Senior Software Engineer": "product_engineering", "Senior Product Manager": "product_engineering",
        "Product Designer": "product_engineering", "Staff Data Scientist": "product_engineering",
        "Senior Analytics Engineer": "product_engineering", "Sr. Manager, Data Science (Marketing)": "product_engineering",
        "Sr. Software Engineer - Accounting Payables": "product_engineering",
        # Out: hardware/IT/AV, recruiting, legal, coordinators, part-time, generic analysts
        "Senior Device Engineer": "", "Firmware Engineer": "", "Mechanical Engineer": "", "AV Engineer": "",
        "Senior Network Engineer": "", "Lead Senior Talent Acquisition(GTM)": "", "Associate Counsel, Business & Growth": "",
        "Marketing Coordinator": "", "Part-Time Social Media Content Specialist": "", "Data Analyst": "",
        "Senior Financial Analyst, GTM": "", "Senior Accountant": "",
    }
    got = {t: run.classify_role(t, cfg) for t in expect}
    assert got == expect
