from jobsearch import sheet_sync as S


def test_tab_links_reads_name_and_column_d():
    text = (
        "Company Name,Website URL,State,Careers Page URL\n"
        "Property Meld,https://propertymeld.com,,https://careers.propertymeld.com/jobs\n"
        "Blank Co,https://blank.co,,\n"
        "Not A Link,https://x.co,,see notes\n"
    )
    assert S.tab_links(text) == {"property meld": "https://careers.propertymeld.com/jobs"}


def test_tab_links_without_header_row():
    # The PropTech tab has no header row.
    text = "Measurabl,https://measurabl.com,,https://ats.rippling.com/measurabljobs/jobs\n"
    assert S.tab_links(text) == {"measurabl": "https://ats.rippling.com/measurabljobs/jobs"}


def test_tab_links_rejects_sign_in_page():
    import pytest
    with pytest.raises(ValueError):
        S.tab_links("<!DOCTYPE html><html>sign in</html>")


def test_apply_changes_only_matching_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "ROOT", tmp_path)
    f = tmp_path / "leads.csv"
    original = (
        "company,website,state,careers_url,notes\r\n"
        "Robin,https://robinpowered.com,MA,https://robinpowered.com/careers,\"a, b\"\r\n"
        "AppFolio,https://appfolio.com,CA,https://appfolio.com/careers,x\r\n"
    )
    f.write_bytes(original.encode())
    links = {"robin": "https://careers.kula.ai/robin", "unknown co": "https://u.co/jobs"}
    changes = S.apply(f, "company", "careers_url", links, write=True)
    assert len(changes) == 1
    lines = f.read_bytes().decode().splitlines(keepends=True)
    assert lines[0] == original.splitlines(keepends=True)[0]
    assert "https://careers.kula.ai/robin" in lines[1] and lines[1].endswith("\r\n")
    assert '"a, b"' in lines[1]
    assert lines[2] == original.splitlines(keepends=True)[2]


def test_check_mode_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "ROOT", tmp_path)
    f = tmp_path / "c.csv"
    f.write_text("Company Name,Careers Page URL\nKasa Living,https://www.kasa.com/careers\n")
    before = f.read_text()
    changes = S.apply(f, "Company Name", "Careers Page URL",
                      {"kasa living": "https://kasa.com/careers"}, write=False)
    assert changes and f.read_text() == before
