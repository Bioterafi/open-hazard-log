import json

import pytest

from ohl import audit, proposals, scan
from ohl import service as S

from .fake_model import FakeClient, candidate, output
from .test_scan import GOOD_DOC, ready, repo  # noqa: F401  (fixtures)


@pytest.fixture
def queued(conn, cso, ready):
    scan.run(conn, cso, ready, client=FakeClient(output(
        candidate("Paediatric ranges missing", [GOOD_DOC], severity="major", likelihood="high"),
        candidate("New cause for wrong patient", [GOOD_DOC], modifies="H-001",
                  causes=[{"text": "Race on switch", "category": "software", "paths": []},
                          {"text": "Slow network delays context event", "category": "infrastructure",
                           "paths": ["src/context.ts"]}],
                          controls=[{"type": "test", "description": "Slow-network context tests"}]),
    )))
    new, change = proposals.pending(conn, ready)
    return ready, new["id"], change["id"]


def last_event(conn):
    e = audit.events(conn)[-1]
    return dict(e, after=json.loads(e["after"]), before=json.loads(e["before"]))


def test_approve_needs_blind_rating_first(conn, cso, queued):
    sid, new, _ = queued
    with pytest.raises(S.Invalid, match="own rating"):
        proposals.approve(conn, cso, sid, new)
    with pytest.raises(S.Invalid, match="own rating"):
        proposals.edit_approve(conn, cso, sid, new, {"title": "x"})


def test_approve_uses_the_cso_rating_not_the_agents(conn, cso, queued):
    sid, new, _ = queued
    proposals.rate(conn, cso, sid, new, "considerable", "low", "seen rarely")
    made = proposals.approve(conn, cso, sid, new)
    h = S.get_hazard(conn, sid, made["hazard"])
    assert (h["initial_severity"], h["initial_likelihood"], h["initial_risk"]) == ("considerable", "low", 2)
    assert h["initial_justification"] == "seen rarely" and h["causes"][0]["paths"] == ["src/ranges.ts"]
    ctls = S.list_controls(conn, sid, made["hazard"])
    assert [(c["type"], c["status"], c["stage"]) for c in ctls] == [("design", "proposed", "additional"),
                                                                    ("test", "proposed", "additional")]
    e = last_event(conn)
    assert e["action"] == "proposal_approved" and e["actor"] == cso["email"]
    assert e["after"]["blind_rating"]["severity"] == "considerable"
    assert e["after"]["suggested_rating"]["severity"] == "major"
    assert "seconds_on_proposal" in e["after"]
    assert proposals.get(conn, sid, new)["status"] == "approved"
    assert audit.verify(conn).ok


def test_blind_rating_is_fixed_once_saved(conn, cso, queued):
    sid, new, _ = queued
    proposals.rate(conn, cso, sid, new, "major", "low")
    with pytest.raises(S.Invalid, match="already recorded"):
        proposals.rate(conn, cso, sid, new, "minor", "low")


def test_edit_and_approve_keeps_both_versions(conn, cso, queued):
    sid, new, _ = queued
    proposals.rate(conn, cso, sid, new, "major", "low")
    made = proposals.edit_approve(conn, cso, sid, new, {
        "title": "Children's results compared with adult ranges", "description": "", "effects": "x", "harm": "y",
        "causes": "Adult-only range table [software] {src/ranges.ts}", "controls": "design: Age bands\nprocess: Hide ranges",
        "initial_severity": "major", "initial_likelihood": "medium", "initial_justification": "after discussion"})
    h = S.get_hazard(conn, sid, made["hazard"])
    assert h["title"] == "Children's results compared with adult ranges" and h["initial_risk"] == 3
    e = last_event(conn)
    assert e["action"] == "proposal_edited"
    assert e["before"]["title"] == "Paediatric ranges missing"
    assert e["after"]["final"]["title"] == "Children's results compared with adult ranges"
    assert e["after"]["blind_rating"]["likelihood"] == "low"           # blind rating kept beside the final one
    assert [c["type"] for c in S.list_controls(conn, sid, made["hazard"])] == ["design", "process"]


def test_change_proposal_adds_only_new_causes_and_controls(conn, cso, queued):
    sid, _, change = queued
    before = S.get_hazard(conn, sid, "H-001")
    proposals.approve(conn, cso, sid, change)          # no rating needed: ratings don't change
    h = S.get_hazard(conn, sid, "H-001")
    assert [c["text"] for c in h["causes"]] == ["Race on switch", "Slow network delays context event"]
    assert h["initial_risk"] == before["initial_risk"] and h["version"] == before["version"] + 1
    assert [c["description"] for c in S.list_controls(conn, sid, "H-001")] == ["Slow-network context tests"]


def test_reject_needs_a_reason(conn, cso, queued):
    sid, new, _ = queued
    with pytest.raises(S.Invalid):
        proposals.reject(conn, cso, sid, new, "")
    with pytest.raises(S.Invalid):
        proposals.reject(conn, cso, sid, new, "other", "")
    proposals.reject(conn, cso, sid, new, "duplicate", "same as H-001")
    p = proposals.get(conn, sid, new)
    assert p["status"] == "rejected" and p["reject_reason"] == "Duplicate: same as H-001"
    assert last_event(conn)["action"] == "proposal_rejected"
    assert len(S.list_hazards(conn, sid)) == 1


def test_only_cso_decides_and_only_once(conn, cso, viewer, queued):
    sid, new, change = queued
    for fn in (lambda u: proposals.approve(conn, u, sid, change),
               lambda u: proposals.reject(conn, u, sid, change, "duplicate"),
               lambda u: proposals.rate(conn, u, sid, new, "major", "low")):
        with pytest.raises(S.Forbidden):
            fn(viewer)
    proposals.approve(conn, cso, sid, change)
    with pytest.raises(S.Invalid, match="already been decided"):
        proposals.reject(conn, cso, sid, change, "duplicate")


def test_hazard_proposals_can_never_be_bulk_approved(conn, cso, queued):
    """Bulk re-confirm is for changed evidence only; every hazard is decided one at a time (OHL-H2)."""
    sid, new, change = queued
    for ids in ([new], [change], [new, change]):
        with pytest.raises(S.Invalid, match="can't be bulk re-confirmed"):
            proposals.bulk_reconfirm(conn, cso, sid, ids, confirmed=True)
    assert len(proposals.pending(conn, sid)) == 2
