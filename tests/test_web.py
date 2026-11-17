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
    return TestClient(ohl.app.app, base_url=LOCAL, client=("127.0.0.1", 50000))


LOCAL = "http://127.0.0.1:8000"


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


# ---------------------------------------------------------------- local only (OHL-H9)

@pytest.mark.parametrize("host", ["evil.example:8000", "127.0.0.1.evil.example", "localhost.:8000", "0.0.0.0:8000",
                                  "192.168.1.20:8000", "[::2]:8000", ""])
def test_foreign_host_header_gets_400(client, host):
    """DNS rebinding: a web page reaches 127.0.0.1 under its own name, so the Host header gives it away."""
    r = client.get("/s/1", headers={"host": host})
    assert r.status_code == 400 and "Test CSO" not in r.text
    r = client.post("/s/1/h/new", headers={"host": host}, data={"title": "planted"})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "LOCALHOST", "[::1]:8000", "127.0.0.2"])
def test_local_host_headers_are_served(client, host):
    assert client.get("/s/1", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("addr", ["192.168.1.20", "10.0.0.5", "::ffff:192.168.1.20", "testclient"])
def test_non_loopback_client_gets_403(client, addr):
    """Started with raw `uvicorn --host 0.0.0.0`, someone else on the network still can't act as the CSO."""
    import ohl.app
    other = TestClient(ohl.app.app, base_url=LOCAL, client=(addr, 50000))
    r = other.get("/s/1")
    assert r.status_code == 403 and "Test CSO" not in r.text
    assert other.get("/static/ohl.css").status_code == 403


def test_loopback_clients_are_served(client):
    import ohl.app
    for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert TestClient(ohl.app.app, base_url=LOCAL, client=(addr, 1)).get("/s/1").status_code == 200


def test_cross_site_form_without_a_session_is_refused(client):
    """A page on another site submits a form to 127.0.0.1: the browser leaves out the SameSite
    session cookie, so there's no session and no token. That must not count as a match."""
    import ohl.app
    fresh = TestClient(ohl.app.app, base_url=LOCAL, client=("127.0.0.1", 50000))   # no cookies at all
    r = fresh.post("/s/1/h/new", data={"title": "planted by evil.example"})
    assert r.status_code == 403
    r = fresh.post("/s/1/issue", data={"confirm": "on", "note": "attacker note"})
    assert r.status_code == 403
    r = fresh.post("/s/1/h/new", data={"title": "planted", "csrf": ""})
    assert r.status_code == 403
    assert "planted" not in client.get("/s/1").text and "Never issued" in client.get("/s/1").text


@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"origin": "null"},
                                     {"origin": "http://localhost:3000"}, {"sec-fetch-site": "cross-site"},
                                     {"sec-fetch-site": "same-site"},
                                     {"origin": LOCAL, "sec-fetch-site": "same-origin", "sec-fetch-dest": "iframe"}])
def test_changes_from_another_site_are_refused_even_with_a_token(client, headers):
    page = client.get("/s/1/h/new")
    r = client.post("/s/1/h/new", headers=headers, data={"csrf": csrf(page.text), "title": "cross-site"})
    assert r.status_code == 403 and "its own pages" in r.text
    assert "cross-site" not in client.get("/s/1").text


@pytest.mark.parametrize("headers", [{"origin": LOCAL}, {"sec-fetch-site": "same-origin"},
                                     {"origin": LOCAL, "sec-fetch-site": "same-origin"}, {}])
def test_changes_from_the_apps_own_pages_are_accepted(client, headers):
    page = client.get("/s/1/h/new")
    r = client.post("/s/1/h/new", headers=headers, data={"csrf": csrf(page.text), "title": "Own page"},
                    follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("header", ["x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host", "via",
                                    "x-forwarded-proto", "x-forwarded-port", "true-client-ip", "cf-connecting-ip"])
def test_proxy_headers_are_refused(client, header):
    assert client.get("/s/1", headers={header: "127.0.0.1"}).status_code == 403


def test_a_server_that_trusts_proxy_headers_still_refuses_the_network(client):
    """uvicorn --forwarded-allow-ips '*' would rewrite the client to 127.0.0.1; the guard still refuses."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    import ohl.app
    trusting = ProxyHeadersMiddleware(ohl.app.app, trusted_hosts="*")
    lan = TestClient(trusting, base_url=LOCAL, client=("192.168.1.20", 50000))
    assert lan.get("/s/1", headers={"x-forwarded-for": "127.0.0.1"}).status_code == 403
    assert lan.get("/s/1").status_code == 403


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
    suggested = "Major × Medium"                       # the fake agent's severity × likelihood
    for leak in ("AGENT-RATIONALE-MARKER", "data-agent-rating", suggested, "differs from yours"):
        assert leak not in r.text, f"agent's rating shown before the CSO rates: {leak}"
    assert 'disabled title="Enter your rating first"' in r.text
    pid = re.search(r"/s/1/review/(\d+)/rate", r.text).group(1)

    r = client.post(f"/s/1/review/{pid}/rate", data={"csrf": csrf(r.text), "severity": "considerable",
                                                     "likelihood": "low"})
    for shown in ("AGENT-RATIONALE-MARKER", "data-agent-rating", suggested, "differs from yours"):
        assert shown in r.text                         # shown after
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


# ---------------------------------------------------------------- M3: checks in the browser

def test_run_checks_and_decide(client, tmp_path):
    import os

    from .test_checks import git
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "tests" / "a.test.ts").write_text("v1\n")
    (repo / "src" / "a.ts").write_text("v1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    c = db.connect(os.environ["OHL_DB"])
    cso = S.get_user(c, "cso@example.org")
    S.update_system(c, cso, 1, {"sources": {"repo": str(repo)}})
    h = S.create_hazard(c, cso, 1, {"title": "Linked hazard", "causes": "Bug [software] {src/a.ts}"})
    S.create_control(c, cso, 1, {"hazard_refs": [h], "description": "Unit test", "type": "test",
                                 "status": "verified", "evidence": "A test | tests/a.test.ts"})
    login(client)

    r = client.post("/s/1/check", data={"csrf": csrf(client.get("/s/1/review").text)})
    assert "1 evidence file checked (1 seen for the first time), 0 new proposals" in r.text
    assert "unchanged" in client.get(f"/s/1/h/{h}").text

    (repo / "tests" / "a.test.ts").write_text("v2\n")
    (repo / "src" / "a.ts").write_text("v2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two")
    r = client.post("/s/1/check", data={"csrf": csrf(r.text)})
    assert "2 new proposals" in r.text
    r = client.get("/s/1/review")                                  # first pending: the evidence problem
    assert "Evidence changed" in r.text and "Re-confirm evidence" in r.text and "check · no AI" in r.text
    pid = re.search(r"/s/1/review/(\d+)/approve", r.text).group(1)
    r = client.post(f"/s/1/review/{pid}/approve", data={"csrf": csrf(r.text)})
    assert "Re-review H-001" in r.text and "Reviewed, still accurate" in r.text and "two" in r.text
    pid = re.search(r"/s/1/review/(\d+)/approve", r.text).group(1)
    r = client.post(f"/s/1/review/{pid}/approve", data={"csrf": csrf(r.text)})
    assert "No proposals waiting" in r.text
    assert db.today() in client.get(f"/s/1/h/{h}").text            # last reviewed today
    dash = client.get("/s/1").text
    assert "Last checks <b>" in dash and "never</b> · <b>" not in dash


def test_checks_flagged_after_seven_days(client, monkeypatch):
    """The freshness line warns once the last check run is more than 7 days old (OHL-H4)."""
    from datetime import date, timedelta

    import ohl.app
    login(client)
    client.post("/s/1/check", data={"csrf": csrf(client.get("/s/1/review").text)})
    today = date.fromisoformat(db.today())
    monkeypatch.setattr(ohl.app.db, "today", lambda: (today + timedelta(days=7)).isoformat())
    assert "days ago</span>" not in client.get("/s/1").text
    monkeypatch.setattr(ohl.app.db, "today", lambda: (today + timedelta(days=8)).isoformat())
    assert '<span class="tag warn">8 days ago</span>' in client.get("/s/1").text


def test_another_site_loading_a_review_page_does_not_start_the_clock(client):
    """Time on a proposal is recorded in the audit trail; only the CSO opening it starts it."""
    import os

    import ohl.app
    from ohl import proposals
    c = db.connect(os.environ["OHL_DB"])
    proposals.record_run(c, 1, {"run_id": "r1", "origin": "scan", "started_by": "cso@example.org",
                                "model_used": "replay:test.json", "prompt_version": "scan-v1"},
                         [{"kind": "new_hazard", "payload": {"title": "Seen?", "description": "", "causes": [],
                                                             "effects": "", "harm": "", "controls": [],
                                                             "lens": "workflow"}}])
    pid = proposals.pending(c, 1)[0]["id"]
    seen = lambda: proposals.get(c, 1, pid)["first_viewed_at"]
    fresh = TestClient(ohl.app.app, base_url=LOCAL, client=("127.0.0.1", 50000))
    assert fresh.get(f"/s/1/review/{pid}", headers={"sec-fetch-site": "cross-site", "sec-fetch-dest": "image"}
                     ).status_code == 200 and seen() == ""
    client.get("/s/1")                                              # the CSO's own browser, with a session
    client.get(f"/s/1/review/{pid}", headers={"sec-fetch-site": "same-origin", "sec-fetch-dest": "image"})
    assert seen() == ""
    client.get(f"/s/1/review/{pid}", headers={"sec-fetch-site": "same-origin", "sec-fetch-dest": "document"})
    assert seen() != ""


@pytest.mark.parametrize("path", ["/s/1", "/s/1/h/new", "/s/1/issue", "/static/ohl.css", "/nope"])
def test_no_page_can_be_shown_in_a_frame(client, path):
    """Clickjacking: a page on another port could frame the app and get the CSO to click Issue."""
    r = client.get(path)
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    refused = client.get("/s/1", headers={"host": "evil.example"})
    assert refused.headers["x-frame-options"] == "DENY"


def test_the_csos_first_visit_in_a_new_browser_starts_the_clock(client):
    """A bookmark or link to the review queue, with no session yet, is still the CSO's own visit."""
    import os

    import ohl.app
    from ohl import proposals
    c = db.connect(os.environ["OHL_DB"])
    proposals.record_run(c, 1, {"run_id": "r2", "origin": "scan", "started_by": "cso@example.org",
                                "model_used": "replay:test.json", "prompt_version": "scan-v1"},
                         [{"kind": "new_hazard", "payload": {"title": "First visit", "description": "", "causes": [],
                                                             "effects": "", "harm": "", "controls": [],
                                                             "lens": "workflow"}}])
    pid = proposals.pending(c, 1)[0]["id"]
    fresh = TestClient(ohl.app.app, base_url=LOCAL, client=("127.0.0.1", 50000))
    r = fresh.get("/s/1/review", headers={"sec-fetch-site": "none", "sec-fetch-dest": "document"})
    assert r.url.path == f"/s/1/review/{pid}"
    assert proposals.get(c, 1, pid)["first_viewed_at"] != ""


RECORDED = "the failure is recorded in the audit trail."
NOT_RECORDED = "could not be recorded in the audit trail either"


@pytest.mark.parametrize("db_writable", [True, False])
def test_web_scan_failure_says_whether_it_was_recorded(client, tmp_path, monkeypatch, db_writable):
    import os
    import sqlite3

    import ohl.app
    from ohl import proposals, scan
    ready_system(os.environ["OHL_DB"], tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    monkeypatch.setattr(ohl.app, "SCAN_CLIENT", scan.ReplayClient(bad))
    if not db_writable:
        monkeypatch.setattr(proposals, "tx", lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    login(client)
    client.post("/s/1/scan", data={"csrf": csrf(client.get("/s/1/review").text)})
    ohl.app.SCANS[1]["thread"].join(timeout=10)
    msg = ohl.app.SCANS[1]["message"]
    assert msg.startswith("Scan failed: ScanError: output was not valid JSON") and ".. " not in msg
    assert (RECORDED in msg) is db_writable and (NOT_RECORDED in msg) is not db_writable


@pytest.mark.parametrize("db_writable", [True, False])
def test_web_check_failure_says_whether_it_was_recorded(client, monkeypatch, db_writable):
    import sqlite3

    from ohl import checks, proposals
    monkeypatch.setattr(checks, "_run", lambda *a, **kw: (_ for _ in ()).throw(KeyError("baseline")))
    if not db_writable:
        monkeypatch.setattr(proposals, "tx", lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    login(client)
    r = client.post("/s/1/check", data={"csrf": csrf(client.get("/s/1/review").text)})
    assert r.status_code == 200 and "Checks failed: KeyError: &#39;baseline&#39;" in r.text
    assert (RECORDED in r.text) is db_writable and (NOT_RECORDED in r.text) is not db_writable
