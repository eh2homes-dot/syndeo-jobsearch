"""Offline regression test: python -m pytest tests/  (or just python tests/test_pipeline.py)"""
import csv, json, shutil, subprocess, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
def run(fx, date):
    r = subprocess.run([sys.executable, "-m", "jobsearch.run", "--fixtures", f"tests/fixtures/{fx}", "--date", date,
                        "--only", "AppFolio,SmartRent,Entrata,Belong"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
def test_new_and_closed_detection(tmp_path):
    hist = ROOT / "data" / "history.json"; backup = hist.read_text() if hist.exists() else None
    try:
        if hist.exists(): hist.unlink()
        run("baseline", "2026-09-16"); run("today", "2026-09-23")
        d = sorted((ROOT / "output/on-demand").glob("2026-09-23_*"))[-1]   # --only runs are on-demand runs
        closed = list(csv.DictReader(open(d / "closed_this_week.csv")))
        new = list(csv.DictReader(open(d / "new_this_week.csv")))
        h0 = json.loads((ROOT / "data/history.json").read_text())
        # closure is always recorded in history...
        assert h0["jobvite:appfolio-internal:oo2uAfwe"]["status"] == "closed"
        # ...but this support role is outside Sales/GTM/Engineering/VP+, so it's not in the report
        assert closed == []
        allopen = list(csv.DictReader(open(d / "all_open_roles.csv")))
        assert len(allopen) >= len(list(csv.DictReader(open(d / "open_roles.csv"))))
        assert [n["job_key"] for n in new] == ["greenhouse:smartrent:6192943004"]
        status = {r["company"]: r["status"] for r in csv.DictReader(open(d / "company_status.csv"))}
        assert status["Belong"].startswith("failed")  # and Belong's roles must NOT be closed
        h = json.loads(hist.read_text())
        assert all(v["status"] == "open" for k, v in h.items() if v["company"] == "Belong")
    finally:
        if backup is not None: hist.write_text(backup)
        shutil.rmtree(ROOT / "output/on-demand", ignore_errors=True)
if __name__ == "__main__":
    test_new_and_closed_detection(None); print("ok")
