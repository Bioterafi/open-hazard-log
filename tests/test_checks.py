import json
import re
import subprocess
from pathlib import Path

import pytest

from ohl import audit, checks, db, proposals
from ohl import service as S


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.org", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "product"
    for rel, text in {"src/results/stale.ts": "export const MAX_AGE = 120;\n",
                      "tests/stale.test.ts": "test('stale banner', () => {});\n",
                      "docs/design.md": "# Stale results\nShow a banner after two hours.\n",
                      "src/other.ts": "export {};\n"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    git(r, "init", "-q")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def setup(conn, cso, system, repo):
    S.update_system(conn, cso, system, {"sources": {"repo": str(repo)}})
    h = S.create_hazard(conn, cso, system, {"title": "Delayed results look current",
                                            "causes": "Stale check wrong [software] {src/results/stale.ts}"})
    c = S.create_control(conn, cso, system, {
        "hazard_refs": [h], "description": "Stale banner after two hours", "type": "design", "status": "verified",
        "evidence": "Stale test | tests/stale.test.ts\nDesign note | docs/design.md\nTraining | https://example.org/t"})
    return system, h, c


def kinds(conn, sid):
    return sorted((p["kind"], p["payload"].get("state", "")) for p in proposals.pending(conn, sid))


def test_first_run_sets_baselines_and_raises_nothing(conn, cso, setup, repo):
    sid, _, c = setup
    r = checks.run(conn, cso, sid)
    assert (r.evidence_checked, r.baselined, r.proposals) == (2, 2, [])
    obs = checks.observations(conn, sid)
    assert obs[(c, "tests/stale.test.ts")]["state"] == "ok"
    assert obs[(c, "https://example.org/t")]["state"] == "manual"
    assert checks.last_run(conn, sid)["commit"] == checks.head(repo)
    assert audit.events(conn)[-1]["action"] == "check_run"
    assert audit.events(conn)[-1]["actor"].startswith("check:")      # not recorded as the AI agent


def test_done_when_deleted_test_and_changed_source(conn, cso, setup, repo):
    """SPEC M3: deleting a linked test file and changing a linked source file each produce the right proposal."""
    sid, h, c = setup
    checks.run(conn, cso, sid)
    (repo / "tests/stale.test.ts").unlink()
    (repo / "src/results/stale.ts").write_text("export const MAX_AGE = 240;\n")
    (repo / "docs/design.md").write_text("# Stale results\nShow a banner after four hours.\n")
    (repo / "src/other.ts").write_text("export const x = 1;\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Relax stale threshold")

    r = checks.run(conn, cso, sid)
    assert len(r.proposals) == 3
    assert kinds(conn, sid) == [("evidence_problem", "changed"), ("evidence_problem", "missing"), ("re_review", "")]
    rr = next(p for p in proposals.pending(conn, sid) if p["kind"] == "re_review")
    assert rr["target"] == h and rr["payload"]["files"] == ["src/results/stale.ts"]   # other.ts isn't linked
    assert rr["payload"]["commits"][0]["subject"] == "Relax stale threshold"
    missing = next(p for p in proposals.pending(conn, sid) if p["payload"].get("state") == "missing")
    assert missing["target"] == c and missing["origin"] == "check" and missing["payload"]["hazards"] == [h]

    assert checks.run(conn, cso, sid).proposals == []          # nothing new: the same problems are already waiting
    assert S.get_control(conn, sid, c)["status"] == "verified"  # the checks never touched the log


def test_decisions(conn, cso, setup, repo):
    sid, h, c = setup
    checks.run(conn, cso, sid)
    (repo / "tests/stale.test.ts").unlink()
    (repo / "src/results/stale.ts").write_text("changed\n")
    (repo / "docs/design.md").write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    checks.run(conn, cso, sid)
    ps = {(p["kind"], p["payload"].get("state", "")): p["id"] for p in proposals.pending(conn, sid)}

    with pytest.raises(S.Invalid, match="can't be re-confirmed"):
        proposals.reconfirm_evidence(conn, cso, sid, ps[("evidence_problem", "missing")])
    proposals.reconfirm_evidence(conn, cso, sid, ps[("evidence_problem", "changed")])
    assert checks.observations(conn, sid)[(c, "docs/design.md")]["state"] == "ok"

    proposals.mark_reviewed(conn, cso, sid, ps[("re_review", "")])
    assert S.get_hazard(conn, sid, h)["last_reviewed"] == db.today()

    proposals.invalidate_control(conn, cso, sid, ps[("evidence_problem", "missing")])
    assert S.get_control(conn, sid, c)["status"] == "invalid"
    e = audit.events(conn)[-1]
    assert e["action"] == "proposal_approved" and json.loads(e["after"])["control_status"] == "invalid"

    assert checks.run(conn, cso, sid).proposals == []          # invalid controls aren't re-checked
    assert proposals.pending(conn, sid) == []
    assert audit.verify(conn).ok


def test_dismissed_problem_returns_only_if_it_changes_again(conn, cso, setup, repo):
    sid, _, c = setup
    checks.run(conn, cso, sid)
    (repo / "docs/design.md").write_text("v2\n")
    checks.run(conn, cso, sid)
    p = proposals.pending(conn, sid)[0]
    proposals.reject(conn, cso, sid, p["id"], "false_alarm", "typo fix only")
    assert checks.run(conn, cso, sid).proposals == []          # same change, already dismissed
    (repo / "docs/design.md").write_text("v3\n")
    assert len(checks.run(conn, cso, sid).proposals) == 1      # changed again: raised again


def test_bulk_reconfirm(conn, cso, setup, repo):
    sid, _, c = setup
    S.update_control(conn, cso, sid, c, {"evidence": "A | docs/design.md\nB | src/other.ts"})
    checks.run(conn, cso, sid)
    (repo / "docs/design.md").write_text("v2\n")
    (repo / "src/other.ts").write_text("v2\n")
    checks.run(conn, cso, sid)
    ids = [p["id"] for p in proposals.pending(conn, sid)]
    assert len(ids) == 2
    with pytest.raises(S.Invalid, match="Tick the box"):
        proposals.bulk_reconfirm(conn, cso, sid, ids, confirmed=False)
    assert proposals.bulk_reconfirm(conn, cso, sid, ids, confirmed=True) == 2
    assert proposals.pending(conn, sid) == []


def test_only_cso_or_contributor_runs_checks(conn, viewer, setup):
    with pytest.raises(S.Forbidden):
        checks.run(conn, viewer, setup[0])


def test_no_repository_still_checks_absolute_evidence(conn, cso, system, tmp_path):
    f = tmp_path / "sop.md"
    f.write_text("SOP v1\n")
    h = S.create_hazard(conn, cso, system, {"title": "x"})
    S.create_control(conn, cso, system, {"hazard_refs": [h], "description": "SOP", "type": "process",
                                         "evidence": f"SOP | {f}\nRelative | docs/x.md"})
    r = checks.run(conn, cso, system)
    assert r.evidence_checked == 1 and any("no repository" in n for n in r.notes)
    f.write_text("SOP v2\n")
    assert len(checks.run(conn, cso, system).proposals) == 1


def test_checks_module_has_no_write_path_to_the_log():
    src = Path(checks.__file__).read_text()
    for forbidden in ("create_hazard", "update_hazard", "create_control", "update_control", "issue_version",
                      "audit.append"):
        assert forbidden not in src, forbidden
    log_tables = r"(hazard|control|event|issue|proposal|system|user)\b"
    assert not re.search(rf"\b(insert|replace)\s+(or\s+\w+\s+)?into\s+{log_tables}|\bupdate\s+{log_tables}"
                         rf"|\bdelete\s+from\s+{log_tables}", src, re.I)
    assert set(re.findall(r"proposals\.(\w+)", src)) <= {"record_run", "record_failure", "pending", "last_run"}


def test_path_matching():
    assert checks.path_matches("src/results/stale.ts", "src/results/stale.ts")
    assert checks.path_matches("src/results/", "src/results/stale.ts")
    assert checks.path_matches("src/results", "src/results/stale.ts")
    assert checks.path_matches("src/*/stale.ts", "src/results/stale.ts")
    assert not checks.path_matches("src/res", "src/results/stale.ts")


# ---------------------------------------------------------------- M4: a run is stored whole or not at all

def test_failed_run_stores_nothing_and_the_next_run_still_finds_the_change(conn, cso, setup, repo, monkeypatch):
    """A failure while storing the run leaves the commit cursor and observations as they were (OHL-H4)."""
    sid, h, _ = setup
    checks.run(conn, cso, sid)
    cursor = checks.last_run(conn, sid)["commit"]
    obs_before = checks.observations(conn, sid)
    (repo / "src/results/stale.ts").write_text("export const MAX_AGE = 240;\n")
    (repo / "tests/stale.test.ts").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "relax")

    real = proposals.record_run

    def broken(*a, **kw):
        real(*a, **kw)                      # writes inside the run's transaction...
        raise RuntimeError("disk full")     # ...then fails before commit

    monkeypatch.setattr(proposals, "record_run", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        checks.run(conn, cso, sid)
    row = conn.execute("SELECT value FROM check_state WHERE system_id = ? AND key = 'last_commit'", (sid,)).fetchone()
    assert row["value"] == cursor
    assert checks.observations(conn, sid) == obs_before
    assert proposals.pending(conn, sid) == []
    failed = audit.events(conn)[-1]
    assert failed["action"] == "run_failed" and failed["actor"].startswith("check:")
    assert "RuntimeError: disk full" in json.loads(failed["after"])["error"]
    assert audit.verify(conn).ok

    monkeypatch.setattr(proposals, "record_run", real)
    checks.run(conn, cso, sid)
    assert kinds(conn, sid) == [("evidence_problem", "missing"), ("re_review", "")]
    assert checks.last_run(conn, sid)["commit"] == checks.head(repo)


def test_failed_run_does_not_count_as_a_fresh_check(conn, cso, setup, monkeypatch):
    sid, _, _ = setup
    monkeypatch.setattr(checks, "sha256_of", lambda p: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError):
        checks.run(conn, cso, sid)
    assert checks.last_run(conn, sid) is None


def test_message_only_claims_the_audit_record_when_it_was_written(conn, cso, setup, monkeypatch, capsys):
    """A locked or full database stops the run and the run_failed write alike; say so (OHL-H4)."""
    import sqlite3

    from ohl import cli
    sid, _, _ = setup
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(checks, "sha256_of", lambda p: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", db_path, "check", "--system", str(sid), "--as", cso["email"]])
    assert str(e.value).endswith("No observations or proposals were stored; the failure is recorded in the audit trail.")

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(proposals, "tx", locked)
    before = len(audit.events(conn))
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", db_path, "check", "--system", str(sid), "--as", cso["email"]])
    msg = str(e.value)
    assert "could not be recorded in the audit trail either (OperationalError: database is locked)" in msg
    assert "is recorded in the audit trail" not in msg
    assert len(audit.events(conn)) == before


def test_an_error_inside_the_run_is_reported_not_shown_as_not_found(conn, cso, setup, monkeypatch):
    """A KeyError from inside the run is a failed run, not a missing system (no 404)."""
    from ohl import cli
    sid, _, _ = setup
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(checks, "sha256_of", lambda p: (_ for _ in ()).throw(KeyError("baseline")))
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", db_path, "check", "--system", str(sid), "--as", cso["email"]])
    assert "Checks failed: KeyError" in str(e.value) and str(e.value).endswith("is recorded in the audit trail.")
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", db_path, "check", "--system", "99", "--as", cso["email"]])
    assert "no system" in str(e.value).lower()


def test_record_run_refuses_in_tx_without_a_transaction(conn, system, cso):
    with pytest.raises(RuntimeError, match="inside the caller's write transaction"):
        proposals.record_run(conn, system, {"run_id": "x", "origin": "check"},
                             [{"kind": "re_review", "target": "H-001", "payload": {}}], _in_tx=True)
    assert proposals.pending(conn, system) == []


def test_a_full_database_reports_the_real_cause(conn, cso, setup, monkeypatch):
    """On a full disk SQLite rolls the transaction back by itself; the ROLLBACK that follows
    must not replace 'database or disk is full' with 'cannot rollback'."""
    import sqlite3

    from ohl import cli
    sid, _, _ = setup
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def full(c, *a, **kw):
        c.execute("ROLLBACK")                       # what SQLite does on SQLITE_FULL
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(proposals, "record_run", full)
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", db_path, "check", "--system", str(sid), "--as", cso["email"]])
    assert "OperationalError: database or disk is full" in str(e.value) and "cannot rollback" not in str(e.value)
    failed = json.loads(audit.events(conn)[-1]["after"])
    assert failed["error"] == "OperationalError: database or disk is full"


def test_a_file_named_twice_by_one_control_is_checked_once(conn, cso, setup, repo):
    sid, _, c = setup
    S.update_control(conn, cso, sid, c, {"evidence": "test_a | tests/stale.test.ts\ntest_b | tests/stale.test.ts"})
    assert checks.run(conn, cso, sid).evidence_checked == 1
    (repo / "tests/stale.test.ts").write_text("changed\n")
    r = checks.run(conn, cso, sid)
    assert r.evidence_checked == 1 and len(r.proposals) == 1


def test_agents_and_checks_cannot_write_to_the_log_at_all(conn, cso, setup, repo):
    """Behavioural, not a text search: with the log's tables made unwritable, a scan that
    proposes changes and a check run that finds problems both still complete (OHL-H7)."""
    from ohl import scan

    from .fake_model import FakeClient, candidate, output
    sid, h, _ = setup
    S.update_system(conn, cso, sid, {"intended_use": "Results viewer.", "no_pid_confirmed": True,
                                     "sources": {"repo": str(repo), "docs": ["docs"]}})
    checks.run(conn, cso, sid)
    (repo / "src/results/stale.ts").write_text("export const MAX_AGE = 999;\n")
    (repo / "docs/design.md").write_text("# Stale results\nChanged.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    for table in ("hazard", "control", "issue", "system", "user"):
        for op in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"CREATE TEMP TRIGGER no_{op}_{table} BEFORE {op} ON main.{table} "
                         f"BEGIN SELECT RAISE(ABORT, 'agent or check wrote to {table}'); END")
    quote = ("docs/design.md", "Stale results")
    scan.run(conn, cso, sid, client=FakeClient(output(candidate("New stale hazard", [quote]),
                                                      candidate("Changed hazard", [quote], modifies=h))))
    r = checks.run(conn, cso, sid)
    assert len(r.proposals) == 2 and len(proposals.pending(conn, sid)) == 4
