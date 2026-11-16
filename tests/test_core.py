import sqlite3

import pytest

from ohl import audit, export, importer, risk
from ohl import service as S


def test_matrix_matches_dcb0129_table_9():
    scheme = risk.load_default_scheme()
    # Rows of Table 9, likelihood very high -> very low; columns minor -> catastrophic.
    expected = {"very_high": [3, 4, 4, 5, 5], "high": [2, 3, 3, 4, 5], "medium": [2, 2, 3, 3, 4],
                "low": [1, 2, 2, 3, 4], "very_low": [1, 1, 2, 2, 3]}
    for lik, row in expected.items():
        assert [risk.rating(scheme, sev, lik) for sev in scheme["severities"]] == row
    assert risk.rating(scheme, None, "low") is None
    with pytest.raises(ValueError):
        risk.rating(scheme, "terrible", "low")


def test_risk_is_derived_never_typed(conn, cso, system):
    ref = S.create_hazard(conn, cso, system, {"title": "x", "initial_severity": "catastrophic",
                                              "initial_likelihood": "very_high", "initial_risk": 1})
    assert S.get_hazard(conn, system, ref)["initial_risk"] == 5


def test_only_cso_changes_the_log(conn, viewer, populated):
    with pytest.raises(S.Forbidden):
        S.create_hazard(conn, viewer, populated, {"title": "x"})
    with pytest.raises(S.Forbidden):
        S.update_hazard(conn, viewer, populated, "H-001", {"title": "x"})
    with pytest.raises(S.Forbidden):
        S.issue_version(conn, viewer, populated)
    with pytest.raises(S.Forbidden):
        importer.import_bytes(conn, viewer, populated, b"No.,Hazard\n1,x\n", "x.csv")


def test_cso_needs_registration(conn):
    with pytest.raises(S.Invalid):
        S.add_user(conn, email="a@b.c", name="A", role="cso", actor="cli:test")


def test_every_change_is_an_event_and_versioned(conn, cso, populated):
    before = len(audit.events(conn, populated))
    S.update_hazard(conn, cso, populated, "H-002", {"residual_severity": "considerable",
                                                    "residual_likelihood": "very_low"}, note="added SOP")
    evs = audit.events(conn, populated)
    assert len(evs) == before + 1
    assert evs[-1]["action"] == "hazard_updated" and evs[-1]["note"] == "added SOP"
    assert S.get_hazard(conn, populated, "H-002")["version"] == 2
    S.update_hazard(conn, cso, populated, "H-002", {"residual_severity": "considerable"})  # no change
    assert len(audit.events(conn, populated)) == before + 1


def test_failed_write_leaves_no_event(conn, cso, populated):
    before = len(audit.events(conn))
    with pytest.raises(S.Invalid):
        S.update_hazard(conn, cso, populated, "H-001", {"status": "deleted"})
    assert len(audit.events(conn)) == before


def test_chain_verifies_and_catches_tampering(conn, populated):
    assert audit.verify(conn).ok
    # The triggers stop ordinary edits...
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE event SET note = 'x' WHERE seq = 3")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM event WHERE seq = 3")
    # ...and the hash chain catches someone who removes them.
    conn.execute("DROP TRIGGER event_no_update")
    conn.execute("UPDATE event SET after = replace(after, 'major', 'minor') WHERE seq = 3")
    v = audit.verify(conn)
    assert not v.ok and v.bad_seq == 3 and "edited" in v.reason


def test_chain_catches_deleted_row(conn, populated):
    conn.execute("DROP TRIGGER event_no_delete")
    conn.execute("DELETE FROM event WHERE seq = 4")
    v = audit.verify(conn)
    assert not v.ok and v.bad_seq == 5


def test_exported_audit_file_verifies_offline(conn, populated):
    data = audit.export(conn, populated)
    assert audit.verify_rows(data["events"]).ok
    data["events"][2]["actor"] = "someone-else@example.org"
    assert not audit.verify_rows(data["events"]).ok


def test_replay_rebuilds_current_state(conn, cso, populated):
    S.update_hazard(conn, cso, populated, "H-001", {"title": "Renamed"})
    r = audit.replay(conn, populated)
    assert r["hazards"]["H-001"] == S.get_hazard(conn, populated, "H-001")
    assert r["controls"]["C-001"] == S.get_control(conn, populated, "C-001")


def test_issued_version_is_frozen(conn, cso, populated):
    n = S.issue_version(conn, cso, populated, "release 2.3")
    S.update_hazard(conn, cso, populated, "H-001", {"title": "Changed after issue"})
    issue = S.get_issue(conn, populated, n)
    assert issue["snapshot"]["hazards"][0]["title"] == "Results shown against the wrong patient"
    assert issue["issued_by"]["registration"] == "GMC 0000000"
    assert issue["chain_head"] != audit.head(conn)  # chain moved on after issue
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE issue SET note = 'x'")
    csv_text = export.to_csv(issue["snapshot"], issue)
    assert "ISSUED v1" in csv_text and "Changed after issue" not in csv_text
    assert "DRAFT" in export.to_csv(S.snapshot(conn, populated))
    assert S.issue_version(conn, cso, populated) == 2


def test_cannot_issue_empty_log(conn, cso, system):
    with pytest.raises(S.Invalid):
        S.issue_version(conn, cso, system)


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_export_import_round_trip(conn, cso, populated, fmt):
    snap = S.snapshot(conn, populated)
    data = export.to_csv(snap).encode() if fmt == "csv" else export.to_xlsx(snap)
    sid2 = S.create_system(conn, cso, name="Copy")
    r = importer.import_bytes(conn, cso, sid2, data, f"log.{fmt}")
    assert r.warnings == [] and r.ignored_columns == []
    snap2 = S.snapshot(conn, sid2)
    drop = {"system_id", "version", "owner"}
    assert [{k: v for k, v in h.items() if k not in drop} for h in snap2["hazards"]] == \
           [{k: v for k, v in h.items() if k not in drop} for h in snap["hazards"]]
    key = lambda c: c["ref"]
    assert [{k: v for k, v in c.items() if k not in drop | {"created_at"}} for c in sorted(snap2["controls"], key=key)] == \
           [{k: v for k, v in c.items() if k not in drop | {"created_at"}} for c in sorted(snap["controls"], key=key)]
    shared = S.get_control(conn, sid2, "C-001")
    assert shared["hazard_refs"] == ["H-001", "H-002"]


def test_import_template_style_sheet(conn, cso, system):
    """A sheet laid out like the NHS template: no OHL refs on controls, numeric hazard numbers,
    typed risk that disagrees with the matrix, and an unknown column."""
    header = ["No.", "Date Added", "Hazard Description / Effect", "Hazard Description / Hazard",
              "Hazard Description / Harm", "Hazard Description / Possible Causes",
              "Existing Controls / HIT Design / Description", "Existing Controls / HIT Design / Evidence",
              "Initial Risk Assessment / Severity", "Initial Risk Assessment / Likelihood",
              "Initial Risk Assessment / Risk", "Additional Controls / User Training / Description",
              "Residual Risk Assessment / Severity", "Residual Risk Assessment / Likelihood",
              "Residual Risk Assessment / Risk", "Owner", "Status", "Comments"]
    rows = [["1", "2025-01-10", "Wrong result shown", "Result mapped to wrong test code", "Missed diagnosis",
             "Mapping table error", "Validation of code mapping\nAudit job", "TC-22 mapping test",
             "Major", "Medium", "4", "Train lab staff on mapping", "Major", "Low", "3", "J. Smith", "Open",
             "free text"]]
    import csv, io
    buf = io.StringIO()
    csv.writer(buf).writerows([header, *rows])
    r = importer.import_bytes(conn, cso, system, buf.getvalue().encode(), "legacy.csv")
    assert r.hazards == ["H-001"]
    assert len(r.controls) == 3
    assert r.ignored_columns == ["Comments"]
    assert any("matrix gives 3" in w for w in r.warnings)  # Major x Medium is 3, not 4
    h = S.get_hazard(conn, system, "H-001")
    assert h["initial_risk"] == 3 and h["residual_risk"] == 3 and h["owner"] == "J. Smith"
    design = [c for c in S.list_controls(conn, system) if c["type"] == "design"]
    assert design[0]["stage"] == "existing" and design[0]["evidence"][0]["label"] == "TC-22 mapping test"


def test_import_is_all_or_nothing(conn, cso, system):
    data = b"No.,Hazard,Initial Risk Assessment / Severity,Initial Risk Assessment / Likelihood\n" \
           b"1,ok,Major,Low\n2,bad,Enormous,Low\n"
    with pytest.raises(S.Invalid) as e:
        importer.import_bytes(conn, cso, system, data, "bad.csv")
    assert "Enormous" in str(e.value)
    assert S.list_hazards(conn, system) == []


def test_deployer_actions_export(conn, populated):
    rows = export.deployer_rows(S.snapshot(conn, populated))
    assert [r["No."] for r in rows] == ["H-001"]
    assert rows[0]["Deployer action"] == "Configure single-patient session mode"


def test_causes_text_round_trip():
    text = "Race on switch [software] {src/a.ts, src/b.ts}\nTwo tabs [Human Factors]\nPlain cause"
    causes = S.parse_causes(text)
    assert causes[1]["category"] == "human-factors" and causes[0]["paths"] == ["src/a.ts", "src/b.ts"]
    assert S.parse_causes(S.format_causes(causes)) == causes
