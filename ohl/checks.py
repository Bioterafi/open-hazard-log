"""The checks (SPEC.md section 6): no AI, just hashes and git.

- Evidence integrity: re-hash every file a control's evidence points to. The first hash
  seen becomes the baseline; a different hash later is "changed", a vanished file "missing".
- Linked code changed: `git diff` since the last check, matched against each cause's paths.
- Review overdue: shown on the dashboard; no proposal.

Like the scan, the checks never change the log. They keep their own observations
(evidence_obs, check_state) and every finding becomes a proposal for the CSO.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import proposals
from .db import now, tx
from .service import Forbidden, get_system, list_controls, list_hazards  # read-only use

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
MAX_COMMITS = 20
DEFAULT_STALE_DAYS = 7


# ---------------------------------------------------------------- hashing

def sha256_of(path: Path) -> str | None:
    """A file's SHA-256, or for a folder a hash over its files' names and hashes. None if absent."""
    if path.is_file():
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    if path.is_dir():
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                h.update(str(full.relative_to(path)).encode() + b"\0" + (sha256_of(full) or "").encode() + b"\n")
        return h.hexdigest()
    return None


def is_url(location: str) -> bool:
    return location.startswith(("http://", "https://"))


def resolve(location: str, repo: Path | None) -> Path | None:
    p = Path(location).expanduser()
    if p.is_absolute():
        return p
    return repo / p if repo else None


# ---------------------------------------------------------------- git

def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def head(repo: Path) -> str | None:
    out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if out else None


def changed_files(repo: Path, since: str, until: str) -> list[str] | None:
    out = _git(repo, "diff", "--name-only", f"{since}..{until}")
    return None if out is None else [line for line in out.splitlines() if line]


def commits_touching(repo: Path, since: str | None, until: str, files: list[str]) -> list[dict]:
    rng = f"{since}..{until}" if since else until
    args = ["log", f"-{MAX_COMMITS}", "--format=%h%x09%ad%x09%s", "--date=short", rng]
    out = _git(repo, *args, "--", *files) if files else None
    rows = []
    for line in (out or "").splitlines():
        sha, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        rows.append({"sha": sha, "date": date, "subject": subject})
    return rows


def path_matches(cause_path: str, changed: str) -> bool:
    p = cause_path.strip().lstrip("./")
    if not p:
        return False
    return changed == p or changed.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(changed, p)


# ---------------------------------------------------------------- reading observations (used by the pages)

def observations(conn, system_id: int) -> dict[tuple[str, str], dict]:
    rows = conn.execute("SELECT * FROM evidence_obs WHERE system_id = ?", (system_id,))
    return {(r["control_ref"], r["location"]): dict(r) for r in rows}


def last_run(conn, system_id: int) -> dict | None:
    return proposals.last_run(conn, system_id, origin="check")


def stale_days(system: dict) -> int:
    try:
        return int((system.get("sources") or {}).get("check_stale_days") or DEFAULT_STALE_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS


def _pending(conn, system_id: int, kind: str) -> list[dict]:
    return [p for p in proposals.pending(conn, system_id) if p["kind"] == kind]


def fingerprint(state: str, sha: str) -> str:
    return f"{state}:{sha}"


# ---------------------------------------------------------------- the run

@dataclass
class Result:
    run_id: str
    proposals: list[int]
    evidence_checked: int = 0
    baselined: int = 0
    problems: list[str] = field(default_factory=list)
    code_changed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run(conn, user: dict, system_id: int) -> Result:
    """One check run, stored whole or not at all. A run that fails after starting changes
    nothing, so the next run finds the same problems, and leaves a `run_failed` event when
    the database can still be written (the failure note says which)."""
    if user["role"] not in ("cso", "contributor"):
        raise Forbidden("Only a CSO or contributor can run the checks.")
    system = get_system(conn, system_id)
    run_id = uuid.uuid4().hex[:12]
    stamp = now()
    try:
        return _run(conn, user, system, run_id, stamp)
    except Exception as e:
        proposals.record_failure(conn, system_id, {"run_id": run_id, "origin": "check", "started_by": user["email"],
                                                   "started_at": stamp}, e)
        raise


def _run(conn, user: dict, system: dict, run_id: str, stamp: str) -> Result:
    system_id = system["id"]
    src = system["sources"] or {}
    repo = Path(src["repo"]).expanduser() if src.get("repo") else None
    if repo and not repo.is_dir():
        repo = None
    res = Result(run_id=run_id, proposals=[])
    items: list[dict] = []
    hazards = {h["ref"]: h for h in list_hazards(conn, system_id)}
    obs = observations(conn, system_id)
    pending_ev = {(p["payload"].get("control"), p["payload"].get("location"))
                  for p in _pending(conn, system_id, "evidence_problem")}

    # ---- evidence integrity
    updates = []
    for c in list_controls(conn, system_id):
        if c["status"] == "invalid":
            continue  # already known not to hold
        done: set[str] = set()  # a control may name several tests in one file: check each file once
        for e in c["evidence"]:
            loc = (e.get("path_or_url") or "").strip()
            if not loc or loc in done:
                continue
            done.add(loc)
            key = (c["ref"], loc)
            prev = obs.get(key, {})
            baseline, ack = prev.get("baseline_sha256", ""), prev.get("ack", "")
            if is_url(loc):
                updates.append((c["ref"], loc, baseline, "", "manual", ack))
                continue
            path = resolve(loc, repo)
            if path is None:
                updates.append((c["ref"], loc, baseline, "", "unresolved", ack))
                res.notes.append(f"{c['ref']}: {loc} is a relative path but no repository is set")
                continue
            res.evidence_checked += 1
            seen = sha256_of(path)
            if seen is None:
                state = "missing"
            elif not baseline:
                baseline, state = seen, "ok"
                res.baselined += 1
            else:
                state = "ok" if seen == baseline else "changed"
            updates.append((c["ref"], loc, baseline, seen or "", state, ack))
            fp = fingerprint(state, seen or "")
            if state in ("changed", "missing") and fp != ack and key not in pending_ev:
                refs = [r for r in c["hazard_refs"] if r in hazards]
                last = commits_touching(repo, None, "HEAD", [loc])[:3] if repo and not Path(loc).is_absolute() else []
                items.append({
                    "kind": "evidence_problem", "target": c["ref"], "confidence": "high",
                    "payload": {"title": f"Evidence {state}: {loc}", "control": c["ref"],
                                "control_description": c["description"], "control_status": c["status"],
                                "hazards": refs, "location": loc, "state": state, "label": e.get("label", ""),
                                "baseline_sha256": baseline, "seen_sha256": seen or "", "fingerprint": fp,
                                "recent_commits": last},
                    "reasoning": ("The file no longer exists at this location." if state == "missing" else
                                  "The file's SHA-256 no longer matches the version recorded when this evidence "
                                  "was first seen or last re-confirmed."),
                })
                res.problems.append(f"{c['ref']} {loc} {state}")

    # ---- linked code changed
    if repo:
        current = head(repo)
        prev_sha = conn.execute("SELECT value FROM check_state WHERE system_id = ? AND key = 'last_commit'",
                                (system_id,)).fetchone()
        prev_sha = prev_sha["value"] if prev_sha else None
        if current and prev_sha and prev_sha != current:
            files = changed_files(repo, prev_sha, current)
            if files is None:
                res.notes.append(f"previous commit {prev_sha[:10]} not found (history rewritten?); "
                                 "code-change baseline reset to HEAD")
            else:
                pending_rr = {p["target"] for p in _pending(conn, system_id, "re_review")}
                for h in hazards.values():
                    if h["status"] == "closed":
                        continue
                    paths = [p for cause in h["causes"] for p in cause.get("paths", [])]
                    hit = sorted({f for f in files for p in paths if path_matches(p, f)})
                    if not hit:
                        continue
                    res.code_changed.append(h["ref"])
                    if h["ref"] in pending_rr:
                        res.notes.append(f"{h['ref']}: linked code changed again; a re-review is already waiting")
                        continue
                    items.append({
                        "kind": "re_review", "target": h["ref"], "confidence": "high",
                        "payload": {"title": f"Re-review {h['ref']}: linked code changed", "hazard": h["ref"],
                                    "hazard_title": h["title"], "files": hit, "from": prev_sha, "to": current,
                                    "commits": commits_touching(repo, prev_sha, current, hit)},
                        "reasoning": (f"{len(hit)} file{'s' if len(hit) != 1 else ''} linked to this hazard's causes "
                                      f"changed between {prev_sha[:10]} and {current[:10]}. The hazard, its "
                                      "controls and its ratings may need updating."),
                    })
        elif not current:
            res.notes.append("repository has no commits or git is unavailable; code changes not checked")
    else:
        current = None
        res.notes.append("no repository set; code changes not checked")

    # ---- record observations, the run and its proposals in one transaction
    summary = {"run_id": run_id, "origin": "check", "started_by": user["email"], "started_at": stamp,
               "finished_at": now(), "model_used": "", "prompt_version": "", "inputs_sha256": "",
               "evidence_checked": res.evidence_checked, "baselined": res.baselined, "problems": res.problems,
               "code_changed": res.code_changed, "commit": current or "", "notes": res.notes}
    with tx(conn):
        for ref, loc, baseline, seen, state, ack in updates:
            conn.execute(
                "INSERT INTO evidence_obs (system_id, control_ref, location, baseline_sha256, seen_sha256, state, ack,"
                " checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (system_id, control_ref, location) DO UPDATE"
                " SET baseline_sha256 = excluded.baseline_sha256, seen_sha256 = excluded.seen_sha256,"
                " state = excluded.state, checked_at = excluded.checked_at",
                (system_id, ref, loc, baseline, seen, state, ack, stamp))
        if current:
            conn.execute("INSERT INTO check_state (system_id, key, value) VALUES (?, 'last_commit', ?)"
                         " ON CONFLICT (system_id, key) DO UPDATE SET value = excluded.value", (system_id, current))
        res.proposals = proposals.record_run(conn, system_id, summary, items, _in_tx=True)
    return res

