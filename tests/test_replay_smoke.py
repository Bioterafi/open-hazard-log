"""Replay smoke set (evals/hazard.scan/smoke.json): ten saved model outputs through the real scan
pipeline, no model call. A determinism control for OHL-H6: the same output and sources must always
give the same proposals. It doesn't measure how well a real model finds hazards."""

import json
from pathlib import Path

import pytest

from ohl import audit, db, proposals, scan
from ohl import service as S

SMOKE = json.loads((Path(__file__).resolve().parent.parent / "evals" / "hazard.scan" / "smoke.json").read_text())
CASES = SMOKE["cases"]


def test_the_set_has_ten_cases_with_unique_names():
    assert len(CASES) == 10 and len({c["name"] for c in CASES}) == 10


def replay(tmp_path: Path, case: dict, n: int):
    root = tmp_path / f"run{n}"
    repo = root / "product"
    for rel, text in SMOKE["files"].items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    conn = db.connect(root / "smoke.db")
    cso = S.add_user(conn, email="cso@example.org", name="CSO", role="cso", registration="GMC 0000000",
                     actor="cli:test")
    s = SMOKE["system"]
    sid = S.create_system(conn, cso, name=s["name"], version=s["version"], intended_use=s["intended_use"])
    S.update_system(conn, cso, sid, {"no_pid_confirmed": True, "sources": {
        "repo": str(repo), "docs": ["docs"], "code_globs": ["src/*.ts"], "uses_ai": False}})
    for title in SMOKE["existing_hazards"]:
        S.create_hazard(conn, cso, sid, {"title": title})
    saved = root / f"{case['name']}.json"
    saved.write_text(json.dumps(case["output"]))
    try:
        r = scan.run(conn, cso, sid, client=scan.ReplayClient(saved), max_proposals=case.get("max"))
    except scan.ScanError as e:
        return {"error": str(e), "events": [e_["action"] for e_ in audit.events(conn, sid)][-1:],
                "pending": proposals.pending(conn, sid)}
    ps = proposals.pending(conn, sid)
    run = proposals.last_run(conn, sid)
    return {
        "result": (r.hidden_no_quote, r.over_limit, r.lenses_run, r.not_proposed_summary),
        "proposals": [{k: p[k] for k in ("kind", "target", "payload", "reasoning", "assumptions", "citations",
                                         "confidence", "suggested_rating", "prompt_version")} for p in ps],
        "run": {k: v for k, v in run.items() if k not in ("run_id", "started_at", "finished_at", "at", "started_by")},
    }


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_replay_case(tmp_path, case):
    first, second = replay(tmp_path, case, 1), replay(tmp_path, case, 2)
    assert first == second, "the same saved output gave different results"
    if "expect_error" in case:
        assert case["expect_error"] in first["error"]
        assert first["pending"] == [] and first["events"] == ["run_failed"]
        return
    ex, ps = case["expect"], first["proposals"]
    assert (len(ps), first["result"][0], first["result"][1]) == (ex["shown"], ex["hidden"], ex["over_limit"])
    assert [p["kind"] for p in ps] == ex["kinds"]
    assert [p["target"] for p in ps] == ex["targets"]
    assert [p["confidence"] for p in ps] == ex["confidence"]
    assert [[c["found"] for c in p["citations"]] for p in ps] == ex["found"]
    assert [[c["lines"] for c in p["citations"]] for p in ps] == ex["lines"]
    assert first["run"]["model_used"] == f"replay:{case['name']}.json"
