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
    assert not re.search(r"(INSERT INTO|UPDATE)\s+(hazard|control|event|issue|proposal)\b", src)
    assert set(re.findall(r"proposals\.(\w+)", src)) <= {"record_run", "pending", "last_run"}


def test_path_matching():
    assert checks.path_matches("src/results/stale.ts", "src/results/stale.ts")
    assert checks.path_matches("src/results/", "src/results/stale.ts")
    assert checks.path_matches("src/results", "src/results/stale.ts")
    assert checks.path_matches("src/*/stale.ts", "src/results/stale.ts")
    assert not checks.path_matches("src/res", "src/results/stale.ts")
