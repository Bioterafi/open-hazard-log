import importlib
import re

import pytest
from fastapi.testclient import TestClient

from ohl import db
from ohl import service as S



@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    monkeypatch.setenv("OHL_DB", str(path))
    c = db.connect(path)
    cso = S.add_user(c, email="cso@example.org", name="Test CSO", role="cso", registration="GMC 0000000",
                     actor="cli:test")
    S.add_user(c, email="viewer@example.org", name="Test Viewer", role="viewer", actor="cli:test")
    S.create_system(c, cso, name="Ward Results Viewer", version="2.3")
    import ohl.app
    importlib.reload(ohl.app)
    return TestClient(ohl.app.app)


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, email="cso@example.org"):
    """No login screen: the app acts as the user it was started as (OHL_USER)."""
    import os
    os.environ["OHL_USER"] = email


@pytest.fixture(autouse=True)
def _reset_user(monkeypatch):
    monkeypatch.delenv("OHL_USER", raising=False)
    yield
    import os
    os.environ.pop("OHL_USER", None)


def test_no_login_screen_acts_as_the_only_cso(client):
    r = client.get("/s/1")
    assert r.status_code == 200 and "Test CSO, CSO" in r.text and "Log out" not in r.text
    assert client.get("/login").status_code == 404


def test_started_as_a_named_user(client):
    login(client, "viewer@example.org")
    assert "Test Viewer, viewer" in client.get("/s/1").text
    login(client, "nobody@example.org")
    r = client.get("/s/1")
    assert r.status_code == 503 and "no such user" in r.text


def test_two_csos_means_say_who_you_are(client):
    import os
    c = db.connect(os.environ["OHL_DB"])
    S.add_user(c, email="second@example.org", name="Second CSO", role="cso", registration="NMC 1", actor="cli:test")
    r = client.get("/s/1")
    assert r.status_code == 503 and "ohl serve --as" in r.text


def test_cso_flow_add_edit_issue_export(client):
    login(client)
    page = client.get("/s/1/h/new")
    r = client.post("/s/1/h/new", data={
        "csrf": csrf(page.text), "title": "Wrong patient shown", "causes": "Race [software]",
        "initial_severity": "major", "initial_likelihood": "medium", "status": "open", "owner": "Test CSO"})
    assert r.status_code == 200 and "H-001" in r.text and "Wrong patient shown" in r.text

    page = client.get("/s/1/c/new?hazard=H-001")
    r = client.post("/s/1/c/new", data={"csrf": csrf(page.text), "hazard_refs": "H-001",
                                         "description": "Patient ID check", "stage": "additional",
                                         "type": "design", "status": "verified", "evidence": "Test | tests/a.ts"})
    assert "Patient ID check" in r.text

    page = client.get("/s/1/h/H-001/edit")
    form = {"csrf": csrf(page.text), "title": "Wrong patient shown", "causes": "Race [software]",
            "initial_severity": "major", "initial_likelihood": "medium", "residual_severity": "major",
            "residual_likelihood": "very_low", "status": "open", "owner": "Test CSO", "note": "control verified"}
    r = client.post("/s/1/h/H-001/edit", data=form)
    assert "control verified" in r.text  # shows in history

    log = client.get("/s/1").text
    assert '<span class="risk r2"' in log and '<span class="faint">3</span>' in log and "Never issued" in log and "intact" in log

    page = client.get("/s/1/issue")
    r = client.post("/s/1/issue", data={"csrf": csrf(page.text), "note": "first", "confirm": "on"})
    assert "v1" in r.text and "GMC 0000000" in r.text

    x = client.get("/s/1/export?format=xlsx&version=1")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    c = client.get("/s/1/export?format=csv").text
    assert "DRAFT" in c and "Wrong patient shown" in c
    a = client.get("/s/1/audit.json").json()
    assert a["verdict"].startswith("OK")


def test_viewer_cannot_edit(client):
    login(client, "viewer@example.org")
    assert client.get("/s/1/h/new").status_code == 403
    page = client.get("/s/1")
    assert "Add hazard" not in page.text and "Issue version" not in page.text
    token = csrf(client.get("/s/1/settings").text)
    r = client.post("/s/1/h/new", data={"csrf": token, "title": "x"})
    assert r.status_code == 403


def test_csrf_required(client):
    login(client)
    r = client.post("/s/1/h/new", data={"csrf": "wrong", "title": "x"})
    assert r.status_code == 403


def test_bad_rating_shows_error_and_keeps_input(client):
    login(client)
    page = client.get("/s/1/h/new")
    r = client.post("/s/1/h/new", data={"csrf": csrf(page.text), "title": "Keep me", "initial_severity": "huge",
                                         "status": "open"})
    assert r.status_code == 400 and "Keep me" in r.text and "severity" in r.text


def test_import_upload(client):
    login(client)
    page = client.get("/s/1/import")
    data = b"No.,Hazard Description / Hazard,Initial Risk Assessment / Severity,Initial Risk Assessment / Likelihood\n" \
           b"7,Imported hazard,Minor,Low\n"
    r = client.post("/s/1/import", data={"csrf": csrf(page.text)}, files={"file": ("old.csv", data, "text/csv")})
    assert r.status_code == 200 and "Imported 1 hazard" in r.text
    assert "H-007" in client.get("/s/1").text


# ---------------------------------------------------------------- M2: scan and review in the browser

def ready_system(path_db, tmp_path):
    from pathlib import Path
    repo = Path(tmp_path) / "product"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "workflow.md").write_text("Nurses review all results for every inpatient on the ward round.\n")
    c = db.connect(path_db)
    cso = S.get_user(c, "cso@example.org")
    S.update_system(c, cso, 1, {"intended_use": "Results for all inpatients.", "no_pid_confirmed": True,
                                "sources": {"repo": str(repo), "docs": ["docs"]}})
    return c


def test_scan_button_and_blind_rating(client, tmp_path, monkeypatch):
    import os
    import ohl.app
    from .fake_model import FakeClient, candidate, output
    ready_system(os.environ["OHL_DB"], tmp_path)
    monkeypatch.setattr(ohl.app, "SCAN_CLIENT", FakeClient(output(candidate(
        "Paediatric ranges missing", [("docs/workflow.md", "review all results for every inpatient")]))))
    login(client)
    page = client.get("/s/1/review")
    client.post("/s/1/scan", data={"csrf": csrf(page.text)})
    ohl.app.SCANS[1]["thread"].join(timeout=10)

    r = client.get("/s/1/review")                      # jumps to the first pending proposal
    assert "Paediatric ranges missing" in r.text and "quote found" in r.text
    assert "AGENT-RATIONALE-MARKER" not in r.text      # agent's rating hidden before the CSO rates
    assert 'disabled title="Enter your rating first"' in r.text
    pid = re.search(r"/s/1/review/(\d+)/rate", r.text).group(1)

    r = client.post(f"/s/1/review/{pid}/rate", data={"csrf": csrf(r.text), "severity": "major",
                                                     "likelihood": "low"})
    assert "AGENT-RATIONALE-MARKER" in r.text          # shown after
    r = client.post(f"/s/1/review/{pid}/approve", data={"csrf": csrf(r.text)})
    assert "No proposals waiting" in r.text and "Approved" in r.text
    assert "Paediatric ranges missing" in client.get("/s/1").text


def test_scan_button_reports_preflight_problems(client):
    login(client)
    page = client.get("/s/1/review")
    r = client.post("/s/1/scan", data={"csrf": csrf(page.text)})
    assert "patient-identifiable" in r.text


def test_page_text_is_escaped(client):
    login(client)
    page = client.get("/s/1/h/new")
    client.post("/s/1/h/new", data={"csrf": csrf(page.text), "title": "<script>alert(1)</script>", "status": "open"})
    html = client.get("/s/1").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
