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
        closed = list(csv.DictReader(open(ROOT / "output/2026-09-23/closed_this_week.csv")))
        new = list(csv.DictReader(open(ROOT / "output/2026-09-23/new_this_week.csv")))
        assert [c["job_key"] for c in closed] == ["jobvite:appfolio-internal:oo2uAfwe"]
        assert [n["job_key"] for n in new] == ["greenhouse:smartrent:6192943004"]
        status = {r["company"]: r["status"] for r in csv.DictReader(open(ROOT / "output/2026-09-23/company_status.csv"))}
        assert status["Belong"].startswith("failed")  # and Belong's roles must NOT be closed
        h = json.loads(hist.read_text())
        assert all(v["status"] == "open" for k, v in h.items() if v["company"] == "Belong")
    finally:
        if backup is not None: hist.write_text(backup)
if __name__ == "__main__":
    test_new_and_closed_detection(None); print("ok")
