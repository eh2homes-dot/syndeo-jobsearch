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


def _job(company, title, tier="B", loc="Remote-USA", group="sales", first_seen="2026-10-11", n=0):
    return {"company": company, "title": title, "tier": tier, "location": loc, "role_group": group,
            "first_seen": first_seen, "url": f"https://x.test/{company}/{n}"}


def test_draft_ranks_by_tier_and_rotation_not_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OUT", tmp_path)
    nl = tmp_path / "newsletter"
    nl.mkdir()
    (nl / "now-hiring-2026-10-04.md").write_text("## Now hiring\n\n**Sales & GTM**\n\n- **BigCo**: [x](u)\n")
    jobs = ([_job("BigCo", f"Director, Sales {i}", n=i) for i in range(20)]
            + [_job("BigCo", "Director, Sales Mexico", loc="Mexico City", n=99)]
            + [_job("SmallCo", "Account Executive", tier="A")]
            + [_job("MidCo", "Sales Manager")]
            + [_job("OldCo", "VP Sales", first_seen="2026-10-04")])          # already went out on Oct 7
    path = run.write_jobs_newsletter(jobs, [], {}, dt.date(2026, 10, 11), {})
    lines = [l for l in path.read_text().splitlines() if l.startswith("- **")]
    assert [l.split("**")[1] for l in lines] == ["SmallCo", "MidCo", "BigCo"]
    assert lines[2].count("](") == 2                     # at most 2 roles per company line
    text = path.read_text()
    assert "OldCo" not in text and "Mexico" not in text
    assert "_22 new" in text                             # 20 BigCo US + SmallCo + MidCo
