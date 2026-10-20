"""Founder-led hiring: the small-company slice of what the searches already found."""
import datetime as dt
import json

from jobsearch import founders, run

TODAY = dt.date(2026, 10, 19)


def _registry(tmp_path, rows):
    import csv
    p = tmp_path / "founders.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=founders.FOUNDER_COLS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in founders.FOUNDER_COLS} for r in rows)
    return founders.load_founders(p)


def _hist(company, title, first_seen, posted_at="", baseline=False, status="open", loc="Remote-USA", n=0):
    key = f"greenhouse:{company.lower()}:{n}"
    row = {"company": company, "title": title, "url": f"https://x.test/{company}/{n}", "location": loc,
           "first_seen": first_seen, "last_seen": TODAY.isoformat(), "status": status, "ats": "greenhouse"}
    if posted_at:
        row["posted_at"] = posted_at
    if baseline:
        row["baseline"] = True
    return key, row


def test_eligibility_is_headcount_or_explicit_flag():
    assert founders.eligible(None, 50) == (False, "not in leads/founders.csv")
    assert founders.eligible({"headcount_n": None, "founder_led_b": None}, 50)[0] is False
    assert founders.eligible({"headcount_n": 12, "founder_led_b": None}, 50)[0] is True
    assert founders.eligible({"headcount_n": 51, "founder_led_b": None}, 50)[0] is False
    assert founders.eligible({"headcount_n": 900, "founder_led_b": True}, 50)[0] is True     # override in
    assert founders.eligible({"headcount_n": 5, "founder_led_b": False}, 50)[0] is False     # override out


def test_registry_parses_headcount_and_flag_loosely(tmp_path):
    reg = _registry(tmp_path, [{"company": "Doorline", "headcount": "~25", "founder_led": "yes"},
                               {"company": "Zillow Group", "headcount": "6,500", "founder_led": "FALSE"}])
    assert reg[run._norm_co("Doorline")]["headcount_n"] == 25
    assert reg[run._norm_co("Doorline")]["founder_led_b"] is True
    assert reg[run._norm_co("Zillow")]["headcount_n"] == 6500      # 'Zillow Group' matches 'Zillow'
    assert reg[run._norm_co("Zillow")]["founder_led_b"] is False


def test_fresh_roles_window_and_baseline_rule():
    hist = dict([
        _hist("Doorline", "Founding Engineer", "2026-10-15", n=1),                 # in window
        _hist("Doorline", "Old role", "2026-09-01", n=2),                          # too old
        _hist("Doorline", "Baseline, undated", "2026-10-15", baseline=True, n=3),  # baseline w/o posted date -> skip
        _hist("Doorline", "Baseline, dated", "2026-10-15", posted_at="2026-10-14", baseline=True, n=4),  # posted recently -> keep
        _hist("Doorline", "Closed", "2026-10-15", status="closed", n=5),
    ])
    opco = {"run_date": "2026-10-19", "new": ["o1"], "roles": [
        {"id": "o1", "company": "Atlas Real Estate", "title": "BD Manager", "url": "https://o.test/1", "location": "Las Vegas, NV",
         "posted": "", "status": "ok"},                                            # new this run, no date -> run_date
        {"id": "o2", "company": "Atlas Real Estate", "title": "Old", "url": "https://o.test/2", "location": "", "posted": "2026-08-01"},
        {"id": "o3", "company": "Atlas Real Estate", "title": "Dup", "url": "https://x.test/Doorline/1", "posted": "2026-10-18"},  # same URL -> deduped
    ]}
    roles = founders.fresh_roles(hist, opco, TODAY, 7)
    titles = sorted(r["title"] for r in roles)
    assert titles == ["BD Manager", "Baseline, dated", "Founding Engineer"]


def test_select_ranks_founding_roles_first_and_flags_unknown_companies(tmp_path):
    reg = _registry(tmp_path, [{"company": "Doorline", "headcount": "12", "stage": "seed", "founder_name": "Maya Chen",
                                "founder_linkedin": "https://linkedin.com/in/maya"},
                               {"company": "Keyframe", "headcount": "40", "stage": "Series A"},
                               {"company": "Zillow Group", "headcount": "6500"}])
    cfg = run.load_json(run.CONFIG, {})
    s = founders.settings(cfg)
    roles = [
        {"source": "weekly", "company": "Doorline", "title": "Account Executive", "url": "u1", "location": "Charlotte, NC", "first_seen": "2026-10-18", "key": "k1"},
        {"source": "weekly", "company": "Doorline", "title": "Founding Engineer", "url": "u2", "location": "Remote-USA", "first_seen": "2026-10-16", "key": "k2"},
        {"source": "weekly", "company": "Keyframe", "title": "Head of Customer Success", "url": "u3", "location": "Dallas, TX", "first_seen": "2026-10-17", "key": "k3"},
        {"source": "weekly", "company": "Keyframe", "title": "Recruiter", "url": "u4", "location": "Dallas, TX", "first_seen": "2026-10-17", "key": "k4"},   # excluded by shared list
        {"source": "weekly", "company": "Keyframe", "title": "Sales Engineer", "url": "u5", "location": "London, UK", "first_seen": "2026-10-17", "key": "k5"},  # non-US
        {"source": "weekly", "company": "Zillow Group", "title": "VP Engineering", "url": "u6", "location": "Seattle", "first_seen": "2026-10-17", "key": "k6"},  # too big
        {"source": "opco", "company": "Mystery Co", "title": "COO", "url": "u7", "location": "Austin", "first_seen": "2026-10-17", "key": "k7"},       # not on file
    ]
    items, scored, review = founders.select(roles, reg, s, TODAY, cfg)
    assert [i["company"] for i in items] == ["Doorline", "Keyframe"]
    assert items[0]["title"] == "Founding Engineer"          # the founding role beats the AE at the same company
    assert items[0]["more_count"] == 1
    assert items[1]["title"] == "Head of Customer Success"
    assert items[1]["more_count"] == 0                       # recruiter and non-US role never counted
    assert [r["company"] for r in review] == ["Mystery Co"]
    assert "Zillow" not in {r["company"] for r in review}    # known-too-big is a decision, not a review item


def test_cooldown_uses_its_own_ledger_and_exempts(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "DATA", tmp_path / "data")
    led = {"issues": {"2026-10-05": {"companies": ["Doorline", "Second Nature"]}, "2026-09-28": {"companies": ["Keyframe"]}}}
    assert founders.in_cooldown(led, "Doorline", TODAY, 3, ["Second Nature"])
    assert founders.in_cooldown(led, "Keyframe", TODAY, 3, [])
    assert not founders.in_cooldown(led, "Keyframe", TODAY, 1, [])          # only the most recent issue counts
    assert not founders.in_cooldown(led, "Second Nature", TODAY, 3, ["Second Nature"])
    founders.record(led, TODAY, ["Newco"])
    assert json.loads(founders.featured_path().read_text())["issues"][TODAY.isoformat()]["companies"] == ["Newco"]


def test_draft_never_fills_a_blank_with_a_value(tmp_path):
    reg = _registry(tmp_path, [{"company": "Doorline", "headcount": "12"}])
    cfg = run.load_json(run.CONFIG, {})
    s = founders.settings(cfg)
    roles = [{"source": "weekly", "company": "Doorline", "title": "Founding Engineer", "url": "https://x.test/1",
              "location": "Remote-USA", "first_seen": "2026-10-16", "key": "k"}]
    items, _, review = founders.select(roles, reg, s, TODAY, cfg)
    md = founders.draft_md(items, review, TODAY, s, len(roles))
    assert "[VERIFY founder name]" in md and "[VERIFY LinkedIn]" in md
    assert "Stage unknown [VERIFY]" in md
    assert "Thin week" in md                                 # 1 item < min_items


def test_main_is_a_no_op_when_the_toggle_is_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "CONFIG", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"founder_hiring": {"enabled": False}}))
    assert founders.main([]) == 0
    assert "founder_hiring is off" in capsys.readouterr().out
    assert not (run.OUT / "founders").exists()
