"""Every test writes to a scratch folder, never to the repo's real history, baseline or reports."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(autouse=True)
def _scratch_state(tmp_path, monkeypatch):
    import opco_search
    from jobsearch import run
    state = tmp_path / "_state" / "opco"
    monkeypatch.setattr(opco_search, "STATE", state)
    monkeypatch.setattr(opco_search, "BASELINE_PATH", state / "baseline.json")
    monkeypatch.setattr(opco_search, "LIVE_COPY", state / "opco_live.csv")
    data, out = tmp_path / "_data", tmp_path / "_output"
    for name, value in (("DATA", data), ("OUT", out), ("ATS_MAP", data / "ats_map.json"),
                        ("HISTORY", data / "history.json"), ("NEEDS_MAP", data / "needs_manual_mapping.csv")):
        monkeypatch.setattr(run, name, value)
    yield
