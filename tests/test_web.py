import importlib
import re

import pytest
from fastapi.testclient import TestClient

from ohl import db
from ohl import service as S

from .conftest import PASSWORD


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    monkeypatch.setenv("OHL_DB", str(path))
    c = db.connect(path)
    cso = S.add_user(c, email="cso@example.org", name="Test CSO", role="cso", password=PASSWORD,
                     registration="GMC 0000000", actor="cli:test")
    S.add_user(c, email="viewer@example.org", name="Test Viewer", role="viewer", password=PASSWORD, actor="cli:test")
    S.create_system(c, cso, name="Ward Results Viewer", version="2.3")
    import ohl.app
    importlib.reload(ohl.app)
    return TestClient(ohl.app.app)


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, email="cso@example.org"):
    token = csrf(client.get("/login").text)
    r = client.post("/login", data={"email": email, "password": PASSWORD, "csrf": token, "next": "/"})
    assert r.status_code == 200, r.text


def test_requires_login(client):
    r = client.get("/s/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_wrong_password(client):
    token = csrf(client.get("/login").text)
    r = client.post("/login", data={"email": "cso@example.org", "password": "nope", "csrf": token})
    assert r.status_code == 401


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
    assert "3 → <b>2</b>" in log and "Never issued" in log and "intact" in log

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
    r = client.post("/s/1/h/new", data={"csrf": csrf(page.text), "title": "x"})
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
