"""Offline regression test: python -m pytest tests/  (or just python tests/test_pipeline.py)

Runs the weekly search twice against recorded job-board answers and checks new/closed detection.
History and reports go to a scratch folder (JOBSEARCH_DATA / JOBSEARCH_OUT), so the repo's real
data/ and output/ are never touched.
"""
import csv, json, os, shutil, subprocess, sys, pathlib, tempfile
ROOT = pathlib.Path(__file__).resolve().parent.parent
def run(fx, date, env):
    r = subprocess.run([sys.executable, "-m", "jobsearch.run", "--fixtures", f"tests/fixtures/{fx}", "--date", date,
                        "--only", "AppFolio,SmartRent,Entrata,Belong"], cwd=ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
def test_new_and_closed_detection(tmp_path=None):
    tmp = pathlib.Path(tmp_path or tempfile.mkdtemp())
    data, out = tmp / "data", tmp / "output"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "tests" / "fixtures" / "ats_map.json", data / "ats_map.json")
    env = {**os.environ, "JOBSEARCH_DATA": str(data), "JOBSEARCH_OUT": str(out)}
    run("baseline", "2026-09-16", env); run("today", "2026-09-23", env)
    d = sorted((out / "on-demand").glob("2026-09-23_*"))[-1]   # --only runs are on-demand runs
    closed = list(csv.DictReader(open(d / "closed_this_week.csv")))
    new = list(csv.DictReader(open(d / "new_this_week.csv")))
    h = json.loads((data / "history.json").read_text())
    # closure is always recorded in history...
    assert h["jobvite:appfolio-internal:oo2uAfwe"]["status"] == "closed"
    # ...but this support role is outside Sales/GTM/Engineering/VP+, so it's not in the report
    assert closed == []
    allopen = list(csv.DictReader(open(d / "all_open_roles.csv")))
    assert len(allopen) >= len(list(csv.DictReader(open(d / "open_roles.csv"))))
    assert [n["job_key"] for n in new] == ["greenhouse:smartrent:6192943004"]
    status = {r["company"]: r["status"] for r in csv.DictReader(open(d / "company_status.csv"))}
    assert status["Belong"].startswith("failed")  # and Belong's roles must NOT be closed
    assert all(v["status"] == "open" for k, v in h.items() if v["company"] == "Belong")
if __name__ == "__main__":
    test_new_and_closed_detection(); print("ok")
