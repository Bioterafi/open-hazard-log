"""M4: the prompt, risk matrix and OHL's own hazard log ship inside the package, and prompts
are never edited in place (OHL-H6)."""

import hashlib
import re
from pathlib import Path

import pytest

import ohl
from ohl import audit, proposals, scan
from ohl import service as S

from .fake_model import FakeClient, output

PKG = Path(ohl.__file__).resolve().parent


# Released prompt files and their SHA-256. Lines are only ever added here, never changed:
# a released prompt is frozen. A new prompt is a new file, version and line (here and in MANIFEST).
RELEASED_PROMPTS = {
    "scan_v1.md": "9e2ec57d73449398add1652182261a541766da6d14b621950b5c885e335b26a8",
}


def test_released_prompts_are_frozen():
    """Editing scan_v1.md and its MANIFEST line together still fails here (OHL-H6). CI also
    refuses a pull request that changes an existing prompt file or MANIFEST line."""
    manifest = scan.prompt_manifest()
    for name, digest in RELEASED_PROMPTS.items():
        assert manifest.get(name) == digest, f"{name} changed after release; add a new version instead"
    assert set(manifest) >= set(RELEASED_PROMPTS)


def test_every_prompt_file_matches_the_manifest():
    """Edit a prompt in place and this fails: a change needs a new file, version and MANIFEST line."""
    manifest = scan.prompt_manifest()
    on_disk = {p.name for p in (PKG / "prompts").iterdir() if p.name != "MANIFEST"}
    assert on_disk == set(manifest), "every prompt file needs exactly one MANIFEST line"
    for name, digest in manifest.items():
        raw = (PKG / "prompts" / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == digest, f"{name} was edited in place"
    assert f"{scan.PROMPT_VERSION.replace('-', '_')}.md" in manifest


def test_scan_refuses_a_prompt_edited_in_place(conn, cso, system, tmp_path, monkeypatch):
    for name in ("MANIFEST", "scan_v1.md"):
        (tmp_path / name).write_bytes((PKG / "prompts" / name).read_bytes())
    (tmp_path / "scan_v1.md").write_text((tmp_path / "scan_v1.md").read_text() + "\nAlso propose fewer hazards.\n")
    monkeypatch.setattr(scan, "PROMPT_DIR", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("Results for every inpatient.\n")
    S.update_system(conn, cso, system, {"intended_use": "Results viewer.", "no_pid_confirmed": True,
                                        "sources": {"docs": [str(docs)]}})
    fake = FakeClient(output())
    with pytest.raises(scan.ScanError, match="edited in place"):
        scan.run(conn, cso, system, client=fake)
    assert fake.calls == [] and proposals.pending(conn, system) == []
    assert audit.events(conn)[-1]["action"] == "run_failed"


def test_resources_load_from_the_package_not_the_checkout():
    """The built wheel holds only the ohl/ folder, so nothing may reach outside it."""
    for src in PKG.glob("*.py"):
        text = src.read_text()
        assert not re.search(r"parent\.parent", text), f"{src.name} reads a file outside the package"
    for rel in ("prompts/scan_v1.md", "prompts/MANIFEST", "config/risk_matrix.toml", "seed/ohl_self.toml"):
        assert (PKG / rel).is_file(), rel


def test_own_log_controls_link_to_tests_that_exist():
    """Each in-place control in OHL's own hazard log names a test, and that test exists."""
    import tomllib
    root = PKG.parent
    seed = tomllib.loads((PKG / "seed" / "ohl_self.toml").read_text())
    linked = 0
    for h in seed["hazard"]:
        for c in h.get("controls", []):
            lines = [ln for ln in c.get("evidence", "").splitlines() if ln.strip()]
            if c.get("status") == "in_place":
                assert lines, f"in-place control has no evidence: {c['description']}"
            for line in lines:
                name, path = (x.strip() for x in line.split("|"))
                assert (root / path).is_file(), path
                if name.startswith("test_"):
                    assert re.search(rf"^def {name}\(", (root / path).read_text(), re.M), f"{name} not in {path}"
                    linked += 1
    assert linked >= 20


def test_seed_self_loads_and_checks_run_against_the_checkout(conn, cso, tmp_path):
    from ohl import checks, cli
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    cli.main(["--db", db_path, "seed-self", "--repo", str(PKG.parent), "--as", cso["email"]])
    sid = max(s["id"] for s in S.list_systems(conn))
    assert len(S.list_hazards(conn, sid)) == 10
    assert all(not h["residual_risk"] and not h["initial_risk"] for h in S.list_hazards(conn, sid))   # unrated
    r = checks.run(conn, cso, sid)
    assert r.evidence_checked >= 20 and r.baselined == r.evidence_checked and r.proposals == []
