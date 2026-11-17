import sqlite3

import pytest

from ohl import audit, db, export, importer, risk
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
    import csv
    import io
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


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.org"])
def test_serve_refuses_anything_but_this_computer(tmp_path, host):
    """No login, so the app must only listen on loopback (landing page guardrail)."""
    from ohl import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(tmp_path / "x.db"), "serve", "--host", host])
    assert "only listens on this computer" in str(e.value)


# ---------------------------------------------------------------- M4: formula injection (OHL-H9)

NASTY = {"title": "=1+1", "harm": "+cmd|' /C calc'!A0", "effects": "-2+3", "description": "@SUM(A1:A2)",
         "deployer_action": "=HYPERLINK(\"http://evil.example\",\"Click\")", "status": "transferred",
         "causes": "  =2*3 [software]"}


def test_xlsx_cells_are_text_never_formulas(conn, cso, system):
    import io
    import zipfile
    S.update_system(conn, cso, system, {"name": "=EVIL()"})
    S.create_hazard(conn, cso, system, NASTY)
    data = export.to_xlsx(S.snapshot(conn, system))
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet and "<f " not in sheet           # no formula anywhere in the sheet
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data)).active
    values = {c.value for row in ws.iter_rows() for c in row}
    assert {"=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1:A2)"} <= values   # exact words kept, as text
    assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)


@pytest.mark.parametrize("fn", [export.to_csv, export.deployer_csv])
def test_csv_cells_cannot_start_a_formula(conn, cso, system, fn):
    import csv
    import io
    S.update_system(conn, cso, system, {"name": "Viewer,=EVIL()\n=X"})
    S.create_hazard(conn, cso, system, NASTY)
    rows = list(csv.reader(io.StringIO(fn(S.snapshot(conn, system)))))
    cells = [c for r in rows for c in r]
    assert not [c for c in cells if c.lstrip().startswith(export.FORMULA_START)]
    assert "'=1+1" in cells or fn is export.deployer_csv
    assert "'=HYPERLINK(\"http://evil.example\",\"Click\")" in cells
    assert all(r[0].startswith("#") for r in rows[:5])          # the system name stays inside its header cell


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_formula_text_survives_a_round_trip(conn, cso, system, fmt):
    S.create_hazard(conn, cso, system, NASTY)
    snap = S.snapshot(conn, system)
    data = export.to_csv(snap).encode() if fmt == "csv" else export.to_xlsx(snap)
    sid2 = S.create_system(conn, cso, name="Copy")
    importer.import_bytes(conn, cso, sid2, data, f"log.{fmt}")
    h = S.list_hazards(conn, sid2)[0]
    assert (h["title"], h["harm"], h["effects"], h["description"]) == ("=1+1", NASTY["harm"], "-2+3", "@SUM(A1:A2)")


def test_hash_lines_inside_a_cell_survive_csv_import(conn, cso, system):
    S.create_hazard(conn, cso, system, {"title": "Heading in text", "description": "first line\n# not a comment"})
    sid2 = S.create_system(conn, cso, name="Copy")
    importer.import_bytes(conn, cso, sid2, export.to_csv(S.snapshot(conn, system)).encode(), "log.csv")
    assert S.list_hazards(conn, sid2)[0]["description"] == "first line\n# not a comment"


# ---------------------------------------------------------------- M4: backup, verify, issue from the command line

def _cli(*argv):
    from ohl import cli
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    return e.value.code


def test_backup_passes_verify_and_leaves_out_the_secret(tmp_path, conn, populated, capsys):
    from ohl import cli
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    out = tmp_path / "backup.db"
    cli.main(["--db", live, "backup", str(out)])
    assert "OK:" in capsys.readouterr().out
    assert _cli("--db", str(out), "verify") == 0                       # ohl verify passes on the copy
    copy = db.connect_ro(out)
    assert audit.verify(copy).head == audit.head(conn)
    assert copy.execute("SELECT value FROM meta WHERE key = 'secret'").fetchone() is None
    assert db.meta(conn, "secret")                                      # the live database keeps its secret
    assert db.meta(conn, "secret").encode() not in out.read_bytes()    # not even in freed pages
    assert [p.name for p in tmp_path.iterdir() if ".partial" in p.name] == []
    assert "already exists" in str(_cli("--db", live, "backup", str(out)))   # never overwrites


def test_verify_does_not_change_the_file(tmp_path, conn, populated):
    out = tmp_path / "b.db"
    db.backup(conn, out)
    before = out.read_bytes()
    assert _cli("--db", str(out), "verify") == 0
    assert out.read_bytes() == before
    assert "no database" in str(_cli("--db", str(tmp_path / "missing.db"), "verify"))
    assert not (tmp_path / "missing.db").exists()


def test_issue_from_the_command_line_needs_confirm(conn, cso, populated, capsys):
    from ohl import cli
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert "--confirm" in str(_cli("--db", live, "issue", "--system", str(populated), "--as", cso["email"]))
    assert S.list_issues(conn, populated) == []
    cli.main(["--db", live, "issue", "--system", str(populated), "--as", cso["email"], "--confirm", "--note", "R1"])
    assert "issued v1" in capsys.readouterr().out
    assert S.list_issues(conn, populated)[0]["note"] == "R1"


TRICKY = ["'-' sign dropped from negative base excess", "' =x", "'+44 number", "'@ mention", "'plain quote",
          "''two quotes", "=1+1", "-2", "  =padded", "@here", "+44 7700", "'", "it's fine", "\tTabbed"]


def test_csv_round_trip_keeps_every_apostrophe(conn, cso, system):
    for t in TRICKY:
        S.create_hazard(conn, cso, system, {"title": t})
    want = [h["title"] for h in S.list_hazards(conn, system)]
    sid2 = S.create_system(conn, cso, name="Copy")
    importer.import_bytes(conn, cso, sid2, export.to_csv(S.snapshot(conn, system)).encode(), "log.csv")
    assert [h["title"] for h in S.list_hazards(conn, sid2)] == want


def test_text_not_written_by_the_exporter_keeps_its_apostrophe():
    assert importer._csv_cell("'hello") == "'hello"
    assert importer._csv_cell("'=1+1") == "=1+1"
    assert importer._csv_cell("''x") == "'x"


def test_backup_into_a_missing_folder_says_so(tmp_path, conn, populated):
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert "doesn't exist" in str(_cli("--db", live, "backup", str(tmp_path / "nope" / "b.db")))


def test_failed_backup_leaves_no_file_and_no_secret_at_the_destination(tmp_path, conn, populated, monkeypatch):
    import shutil
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    out = tmp_path / "b.db"
    def disk_full(r, w):
        w.write(r.read(100))
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shutil, "copyfileobj", disk_full)
    assert "backup failed" in str(_cli("--db", live, "backup", str(out)))
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if not p.name.startswith("test.db")] == []


EVIL_SEPARATORS = {"name": "Ward viewer;=1+1;", "title": "Wrong patient;=cmd|' /C calc'!A0;x, =1+1",
                   "harm": "Delay\t=1+1\tx", "deployer_action": "Train staff;=2+2;\n@SUM(1)\r\n-3",
                   "description": 'Quote;"=b"; then \'+c'}


@pytest.mark.parametrize("fn", [export.to_csv, export.deployer_csv])
def test_no_cell_can_start_a_formula_whatever_the_spreadsheet_splits_on(conn, cso, system, fn):
    """Excel in ';' locales and LibreOffice by default also split on ';' and tab, and can lose
    track of quotes. No point where any of them could start a cell may begin a formula."""
    import csv
    import io
    import re
    S.update_system(conn, cso, system, {"name": EVIL_SEPARATORS["name"]})
    S.create_hazard(conn, cso, system, {k: v for k, v in EVIL_SEPARATORS.items() if k != "name"} | {"status": "transferred"})
    raw = fn(S.snapshot(conn, system))
    # Commas rely on the quotes (every comma-splitting program honours them); the rest are guarded.
    assert not re.search(r'(?:^|[;\t\r\n])\s*"*\s*[=+\-@]', raw), "a separator is followed by a formula start"
    rows = list(csv.reader(io.StringIO(raw)))
    assert raw.count('"') >= 2 * sum(len(r) for r in rows), "every cell is quoted"
    for delimiter in (",", ";", "\t"):
        cells = [c for r in csv.reader(io.StringIO(raw), delimiter=delimiter) for c in r]
        assert not [c for c in cells if c.lstrip().lstrip('"').lstrip().startswith(export.FORMULA_START)], delimiter


def test_separator_guard_round_trips(conn, cso, system):
    fields = {k: v for k, v in EVIL_SEPARATORS.items() if k != "name"}
    S.create_hazard(conn, cso, system, fields)
    want = S.list_hazards(conn, system)[0]
    sid2 = S.create_system(conn, cso, name="Copy")
    importer.import_bytes(conn, cso, sid2, export.to_csv(S.snapshot(conn, system)).encode(), "log.csv")
    got = S.list_hazards(conn, sid2)[0]
    assert {k: got[k] for k in fields} == {k: want[k] for k in fields}


def test_apostrophes_in_someone_elses_csv_are_kept(conn, cso, system):
    data = ("No.,Hazard Description / Hazard,Hazard Description / Harm\n"
            "1,'-' sign dropped from negative base excess,'=' in a lab comment truncates the rest\n").encode()
    importer.import_bytes(conn, cso, system, data, "trust-log.csv")
    h = S.list_hazards(conn, system)[0]
    assert (h["title"], h["harm"]) == ("'-' sign dropped from negative base excess",
                                       "'=' in a lab comment truncates the rest")


def test_control_characters_never_stop_an_xlsx_export(conn, cso, system):
    """An issued version is frozen, so an unexportable character would block its XLSX for ever."""
    S.create_hazard(conn, cso, system, {"title": "Dose shown\x0bwithout units", "harm": "a\x1cb"})
    n = S.issue_version(conn, cso, system)
    data = export.to_xlsx(S.get_issue(conn, system, n)["snapshot"], S.get_issue(conn, system, n))
    import io

    from openpyxl import load_workbook
    values = {c.value for row in load_workbook(io.BytesIO(data)).active.iter_rows() for c in row}
    assert "Dose shown�without units" in values


def test_no_fragment_of_the_secret_survives_even_without_secure_delete(tmp_path, conn, populated, monkeypatch):
    """SQLite builds differ: with secure_delete off, a deleted row stays in free space. Here
    secure_delete can't be turned on, so only the VACUUM keeps the secret out."""
    import sqlite3
    real = sqlite3.connect

    class Insecure:
        def __init__(self, *a, **kw):
            self.c = real(*a, **kw)
            self.c.execute("PRAGMA secure_delete = OFF")

        def execute(self, sql, *args):
            return self.c.execute("PRAGMA secure_delete = OFF" if "secure_delete" in sql else sql, *args)

        def close(self):
            self.c.close()
    monkeypatch.setattr(db.sqlite3, "connect", Insecure)
    out = tmp_path / "b.db"
    db.backup(conn, out)
    monkeypatch.setattr(db.sqlite3, "connect", real)
    secret, data = db.meta(conn, "secret"), out.read_bytes().decode("latin-1")
    assert not [i for i in range(len(secret) - 11) if secret[i:i + 12] in data]
    assert db.connect_ro(out).execute("PRAGMA freelist_count").fetchone()[0] == 0


@pytest.mark.parametrize("content", [b"", b"hello, not a database\n", b"SQLite format 3\x00" + b"\x00" * 50])
def test_backup_and_verify_explain_a_bad_file_without_a_traceback(tmp_path, content):
    src = tmp_path / "src.db"
    src.write_bytes(content)
    msg = str(_cli("--db", str(src), "backup", str(tmp_path / "b.db")))
    assert "Traceback" not in msg and ("failed" in msg or "doesn't verify" in msg)
    assert src.read_bytes() == content                                   # the source is never changed
    assert _cli("--db", str(src), "verify") == 1


def test_out_to_a_folder_is_a_clear_message(tmp_path, conn, populated):
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert "couldn't write" in str(_cli("--db", live, "audit", "--out", str(tmp_path)))


def test_backup_stopped_by_a_hangup_leaves_nothing_under_its_name(tmp_path, conn, populated, monkeypatch):
    import os
    import shutil
    import signal
    live = conn.execute("PRAGMA database_list").fetchone()["file"]
    out = tmp_path / "b.db"
    real = shutil.copyfileobj

    def hangup_midway(r, w):
        w.write(r.read(4096))
        os.kill(os.getpid(), signal.SIGTERM)   # the terminal closes, or the computer shuts down
        real(r, w)
    monkeypatch.setattr(shutil, "copyfileobj", hangup_midway)
    before = signal.getsignal(signal.SIGTERM)
    assert "stopped by signal" in str(_cli("--db", live, "backup", str(out)))
    assert not out.exists() and not [p for p in tmp_path.iterdir() if ".partial" in p.name]
    assert signal.getsignal(signal.SIGTERM) is before                  # handlers put back


def test_csv_from_ohl_0_1_0_keeps_its_apostrophes(conn, cso, system):
    """0.1.0 exports had no guard; their apostrophes are real and must survive import."""
    data = ('# Hazard log: Ward Results Viewer 2.3\n# Status: DRAFT\n'
            'No.,Hazard Description / Hazard,Hazard Description / Harm\n'
            "H-001,'-' sign dropped from negative base excess,'@ mention\n"
            "H-002,''two quotes,'=' in a lab comment\n").encode()
    importer.import_bytes(conn, cso, system, data, "old-export.csv")
    hs = S.list_hazards(conn, system)
    assert [(h["title"], h["harm"]) for h in hs] == [("'-' sign dropped from negative base excess", "'@ mention"),
                                                      ("''two quotes", "'=' in a lab comment")]


def test_xlsx_says_when_a_long_cell_is_cut(conn, cso, system):
    import io

    from openpyxl import load_workbook
    S.create_hazard(conn, cso, system, {"title": "Long", "effects": "x" * 40000})
    ws = load_workbook(io.BytesIO(export.to_xlsx(S.snapshot(conn, system)))).active
    long = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and len(c.value) > 30000]
    assert len(long) == 1 and len(long[0]) == export.XLSX_CELL_LIMIT and long[0].endswith("full text]")


def test_backup_without_hard_links_never_replaces_a_file(tmp_path, conn, populated, monkeypatch):
    """FAT and exFAT have no hard links; two runs to one name must not both succeed."""
    import os
    monkeypatch.setattr(os, "link", lambda a, b: (_ for _ in ()).throw(OSError(45, "Operation not supported")))
    out = tmp_path / "b.db"
    db.backup(conn, out)
    first = out.read_bytes()
    with pytest.raises(FileExistsError):
        db.backup(conn, out)
    assert out.read_bytes() == first and not [p for p in tmp_path.iterdir() if ".partial" in p.name]


def test_old_partial_files_are_cleared(tmp_path, conn, populated):
    import os
    import time
    stale = tmp_path / ".b.db.partial-deadbeef"
    stale.write_bytes(b"x" * 100)
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = tmp_path / ".b.db.partial-cafef00d"
    fresh.write_bytes(b"y")                                         # another run may be writing it now
    db.backup(conn, tmp_path / "b.db")
    assert not stale.exists() and fresh.exists()


def test_verify_reports_a_tampered_row_instead_of_crashing(tmp_path, conn, populated):
    out = tmp_path / "t.db"
    db.backup(conn, out)
    import sqlite3
    t = sqlite3.connect(out)
    t.execute("DROP TRIGGER event_no_update")
    t.execute("UPDATE event SET note = CAST(note AS BLOB) WHERE seq = 3")
    t.commit()
    t.close()
    assert _cli("--db", str(out), "verify") == 1
    v = audit.verify(db.connect_ro(out))
    assert not v.ok and v.bad_seq == 3


def test_verify_leaves_no_side_files_and_works_read_only(tmp_path, conn, populated):
    import os
    out = tmp_path / "ro" / "b.db"
    out.parent.mkdir()
    db.backup(conn, out)
    os.chmod(out.parent, 0o555)
    try:
        assert _cli("--db", str(out), "verify") == 0
        assert sorted(p.name for p in out.parent.iterdir()) == ["b.db"]
    finally:
        os.chmod(out.parent, 0o755)
