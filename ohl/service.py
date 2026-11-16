"""Everything that changes the log. Each write and its audit event share one transaction.

Rule enforced here, not in the UI: only a CSO can change the log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess

from . import audit, risk
from .db import dumps, now, today, tx

CATEGORIES = ("software", "data", "ai-model", "human-factors", "integration", "infrastructure")
STATUSES = ("open", "transferred", "closed")
CONTROL_TYPES = ("design", "test", "process", "training", "warning")  # DCB0129 guidance 6.1 order
CONTROL_STAGES = ("existing", "additional")
CONTROL_STATUSES = ("proposed", "in_place", "verified", "invalid")
ROLES = ("cso", "contributor", "viewer")

HAZARD_FIELDS = (
    "title", "description", "causes", "effects", "harm",
    "initial_severity", "initial_likelihood", "initial_justification",
    "residual_severity", "residual_likelihood", "residual_justification",
    "status", "deployer_action", "action", "owner", "last_reviewed", "review_due",
)
RATING_FIELDS = ("initial_severity", "initial_likelihood", "residual_severity", "residual_likelihood")
CONTROL_FIELDS = ("hazard_refs", "description", "stage", "type", "status", "evidence")
SYSTEM_FIELDS = ("name", "version", "intended_use", "clinical_context", "no_pid_confirmed", "sources")


class Forbidden(Exception):
    pass


class Invalid(ValueError):
    pass


# ---------------------------------------------------------------- users

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(got.hex(), dk)


def add_user(conn, *, email: str, name: str, role: str, password: str, registration: str = "",
             actor: str) -> dict:
    email = email.strip().lower()
    if role not in ROLES:
        raise Invalid(f"role must be one of {', '.join(ROLES)}")
    if not email or not name.strip():
        raise Invalid("email and name are required")
    if len(password) < 10:
        raise Invalid("password must be at least 10 characters")
    if role == "cso" and not registration.strip():
        raise Invalid("a CSO needs a professional registration (DCB0129 2.3.2), e.g. 'GMC 1234567'")
    user = {"email": email, "name": name.strip(), "role": role, "registration": registration.strip()}
    with tx(conn):
        if conn.execute("SELECT 1 FROM user WHERE email = ?", (email,)).fetchone():
            raise Invalid(f"user {email} already exists")
        conn.execute(
            "INSERT INTO user (email, name, role, registration, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, user["name"], role, user["registration"], hash_password(password), now()),
        )
        audit.append(conn, actor=actor, action="user_added", object_ref=email, after=user)
    return user


def get_user(conn, email: str) -> dict | None:
    row = conn.execute("SELECT email, name, role, registration FROM user WHERE email = ?",
                       (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def authenticate(conn, email: str, password: str) -> dict | None:
    row = conn.execute("SELECT * FROM user WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row and check_password(password, row["password_hash"]):
        return {k: row[k] for k in ("email", "name", "role", "registration")}
    return None


def require_cso(user: dict) -> None:
    if not user or user.get("role") != "cso":
        raise Forbidden("Only a Clinical Safety Officer can change the hazard log.")


# ---------------------------------------------------------------- systems

def _system_row(row) -> dict:
    s = dict(row)
    s["sources"] = json.loads(s["sources"])
    s["risk_scheme"] = json.loads(s["risk_scheme"])
    s["no_pid_confirmed"] = bool(s["no_pid_confirmed"])
    return s


def list_systems(conn) -> list[dict]:
    return [_system_row(r) for r in conn.execute("SELECT * FROM system ORDER BY id")]


def get_system(conn, system_id: int) -> dict:
    row = conn.execute("SELECT * FROM system WHERE id = ?", (system_id,)).fetchone()
    if not row:
        raise LookupError(f"no system {system_id}")
    return _system_row(row)


def _public_system(s: dict) -> dict:
    return {k: s[k] for k in ("id", "name", "version", "intended_use", "clinical_context",
                              "no_pid_confirmed", "sources", "hazard_prefix", "control_prefix", "ref_width")}


def create_system(conn, user: dict, *, name: str, version: str = "", intended_use: str = "",
                  clinical_context: str = "", hazard_prefix: str = "H-", control_prefix: str = "C-",
                  ref_width: int = 3, scheme: dict | None = None) -> int:
    require_cso(user)
    if not name.strip():
        raise Invalid("system name is required")
    scheme = scheme or risk.load_default_scheme()
    risk.validate(scheme)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO system (name, version, intended_use, clinical_context, hazard_prefix, control_prefix,"
            " ref_width, risk_scheme, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), version.strip(), intended_use.strip(), clinical_context.strip(),
             hazard_prefix, control_prefix, ref_width, dumps(scheme), now()),
        )
        sid = cur.lastrowid
        s = get_system(conn, sid)
        audit.append(conn, actor=user["email"], action="system_created", system_id=sid,
                     object_ref=f"system:{sid}", after=_public_system(s))
        audit.append(conn, actor=user["email"], action="risk_scheme_set", system_id=sid,
                     object_ref=f"system:{sid}", after=scheme, note=scheme.get("name", ""))
    return sid


def update_system(conn, user: dict, system_id: int, fields: dict) -> None:
    require_cso(user)
    unknown = set(fields) - set(SYSTEM_FIELDS)
    if unknown:
        raise Invalid(f"unknown system fields: {', '.join(sorted(unknown))}")
    if "name" in fields and not str(fields["name"]).strip():
        raise Invalid("system name is required")
    with tx(conn):
        before = _public_system(get_system(conn, system_id))
        for k, v in fields.items():
            if k == "sources":
                v = dumps(v)
            elif k == "no_pid_confirmed":
                v = 1 if v else 0
            else:
                v = str(v).strip()
            conn.execute(f"UPDATE system SET {k} = ? WHERE id = ?", (v, system_id))
        after = _public_system(get_system(conn, system_id))
        audit.append(conn, actor=user["email"], action="system_updated", system_id=system_id,
                     object_ref=f"system:{system_id}", before=before, after=after)


# ---------------------------------------------------------------- refs

def format_ref(prefix: str, n: int, width: int) -> str:
    return f"{prefix}{str(n).zfill(width)}"


def next_ref(conn, system_id: int, table: str) -> str:
    s = get_system(conn, system_id)
    prefix = s["hazard_prefix"] if table == "hazard" else s["control_prefix"]
    highest = 0
    for (ref,) in conn.execute(f"SELECT ref FROM {table} WHERE system_id = ?", (system_id,)):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", ref)
        if m:
            highest = max(highest, int(m.group(1)))
    return format_ref(prefix, highest + 1, s["ref_width"])


# ---------------------------------------------------------------- causes (text form)

_CAUSE = re.compile(r"^(?P<text>.*?)\s*(?:\[(?P<cat>[^\]]+)\])?\s*(?:\{(?P<paths>[^}]*)\})?\s*$")


def normalise_category(cat: str | None) -> str:
    if not cat:
        return ""
    c = cat.strip().lower().replace(" ", "-").replace("_", "-")
    c = {"ai": "ai-model", "ai-ml": "ai-model", "human": "human-factors", "humanfactors": "human-factors"}.get(c, c)
    if c not in CATEGORIES:
        raise Invalid(f"unknown cause category {cat!r}; use one of {', '.join(CATEGORIES)}")
    return c


def parse_causes(text: str) -> list[dict]:
    """One cause per line:  text [category] {path, path}  (category and paths optional)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        m = _CAUSE.match(line)
        paths = [p.strip() for p in (m.group("paths") or "").split(",") if p.strip()]
        out.append({"text": m.group("text").strip(), "category": normalise_category(m.group("cat")),
                    "paths": paths})
    return out


def format_causes(causes: list[dict]) -> str:
    lines = []
    for c in causes:
        line = c["text"]
        if c.get("category"):
            line += f" [{c['category']}]"
        if c.get("paths"):
            line += " {" + ", ".join(c["paths"]) + "}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- evidence (text form)

def parse_evidence(text: str, keep: list[dict] | None = None) -> list[dict]:
    """One item per line:  label | path-or-URL.  Hash state is kept for unchanged items."""
    known = {(e["label"], e["path_or_url"]): e for e in (keep or [])}
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, where = (p.strip() for p in line.partition("|"))
        item = known.get((label, where)) or {"label": label, "path_or_url": where, "sha256": "",
                                             "checked_at": "", "state": "unchecked"}
        out.append(item)
    return out


def format_evidence(evidence: list[dict]) -> str:
    return "\n".join(f"{e['label']} | {e['path_or_url']}" if e["path_or_url"] else e["label"] for e in evidence)


# ---------------------------------------------------------------- hazards

def _hazard_row(row) -> dict:
    h = dict(row)
    h["causes"] = json.loads(h["causes"])
    return h


def list_hazards(conn, system_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM hazard WHERE system_id = ?", (system_id,)).fetchall()
    return sorted((_hazard_row(r) for r in rows), key=lambda h: _ref_key(h["ref"]))


def _ref_key(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def get_hazard(conn, system_id: int, ref: str) -> dict:
    row = conn.execute("SELECT * FROM hazard WHERE system_id = ? AND ref = ?", (system_id, ref)).fetchone()
    if not row:
        raise LookupError(f"no hazard {ref}")
    return _hazard_row(row)


def _clean_hazard(scheme: dict, data: dict) -> dict:
    h = {}
    for k in HAZARD_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "causes":
            v = parse_causes(v) if isinstance(v, str) else [
                {"text": c["text"].strip(), "category": normalise_category(c.get("category")),
                 "paths": list(c.get("paths") or [])} for c in v]
        elif k in RATING_FIELDS:
            v = (v or None) and str(v)
            allowed = scheme["severities"] if k.endswith("severity") else scheme["likelihoods"]
            if v is not None and v not in allowed:
                raise Invalid(f"{k.replace('_', ' ')} must be one of {', '.join(allowed)}")
        elif k == "status":
            v = (v or "open").strip().lower()
            if v not in STATUSES:
                raise Invalid(f"status must be one of {', '.join(STATUSES)}")
        else:
            v = "" if v is None else str(v).strip()
        h[k] = v
    if "title" in h and not h["title"]:
        raise Invalid("hazard title is required")
    return h


def _with_risk(scheme: dict, h: dict) -> dict:
    for stage in ("initial", "residual"):
        h[f"{stage}_risk"] = risk.rating(scheme, h.get(f"{stage}_severity"), h.get(f"{stage}_likelihood"))
    return h


def create_hazard(conn, user: dict, system_id: int, data: dict, *, ref: str | None = None,
                  note: str = "", _in_tx: bool = False) -> str:
    require_cso(user)

    def work():
        s = get_system(conn, system_id)
        h = {"title": "", "description": "", "causes": [], "effects": "", "harm": "",
             "initial_severity": None, "initial_likelihood": None, "initial_justification": "",
             "residual_severity": None, "residual_likelihood": None, "residual_justification": "",
             "status": "open", "deployer_action": "", "action": "", "owner": user["name"],
             "last_reviewed": "", "review_due": ""}
        h.update(_clean_hazard(s["risk_scheme"], data))
        if not h["title"]:
            raise Invalid("hazard title is required")
        _with_risk(s["risk_scheme"], h)
        new_ref = ref or next_ref(conn, system_id, "hazard")
        if conn.execute("SELECT 1 FROM hazard WHERE system_id = ? AND ref = ?", (system_id, new_ref)).fetchone():
            raise Invalid(f"hazard {new_ref} already exists")
        h.update(system_id=system_id, ref=new_ref, created_at=data.get("created_at") or today(), version=1)
        row = dict(h, causes=dumps(h["causes"]))
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO hazard ({cols}) VALUES ({', '.join(':' + c for c in row)})", row)
        audit.append(conn, actor=user["email"], action="hazard_created", system_id=system_id,
                     object_ref=new_ref, after=get_hazard(conn, system_id, new_ref), note=note)
        return new_ref

    if _in_tx:
        return work()
    with tx(conn):
        return work()


def update_hazard(conn, user: dict, system_id: int, ref: str, data: dict, *, note: str = "") -> dict:
    require_cso(user)
    with tx(conn):
        s = get_system(conn, system_id)
        before = get_hazard(conn, system_id, ref)
        changes = _clean_hazard(s["risk_scheme"], data)
        after = _with_risk(s["risk_scheme"], {**before, **changes})
        if {k: after[k] for k in HAZARD_FIELDS} == {k: before[k] for k in HAZARD_FIELDS}:
            return before  # nothing changed, nothing recorded
        after["version"] = before["version"] + 1
        sets = [k for k in (*HAZARD_FIELDS, "initial_risk", "residual_risk", "version")]
        conn.execute(
            f"UPDATE hazard SET {', '.join(f'{k} = :{k}' for k in sets)} WHERE system_id = :system_id AND ref = :ref",
            dict(after, causes=dumps(after["causes"])),
        )
        after = get_hazard(conn, system_id, ref)
        audit.append(conn, actor=user["email"], action="hazard_updated", system_id=system_id,
                     object_ref=ref, before=before, after=after, note=note)
        return after


# ---------------------------------------------------------------- controls

def _control_row(row) -> dict:
    c = dict(row)
    c["hazard_refs"] = json.loads(c["hazard_refs"])
    c["evidence"] = json.loads(c["evidence"])
    return c


def list_controls(conn, system_id: int, hazard_ref: str | None = None) -> list[dict]:
    rows = [_control_row(r) for r in conn.execute("SELECT * FROM control WHERE system_id = ?", (system_id,))]
    if hazard_ref:
        rows = [c for c in rows if hazard_ref in c["hazard_refs"]]
    return sorted(rows, key=lambda c: (CONTROL_STAGES.index(c["stage"]), CONTROL_TYPES.index(c["type"]),
                                       _ref_key(c["ref"])))


def get_control(conn, system_id: int, ref: str) -> dict:
    row = conn.execute("SELECT * FROM control WHERE system_id = ? AND ref = ?", (system_id, ref)).fetchone()
    if not row:
        raise LookupError(f"no control {ref}")
    return _control_row(row)


def _clean_control(conn, system_id: int, data: dict, keep_evidence: list[dict] | None = None) -> dict:
    c = {}
    for k in CONTROL_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "hazard_refs":
            refs = [r.strip() for r in (v.split(",") if isinstance(v, str) else v) if r and r.strip()]
            for r in refs:
                get_hazard(conn, system_id, r)  # raises LookupError if missing
            if not refs:
                raise Invalid("a control must be linked to at least one hazard")
            v = list(dict.fromkeys(refs))
        elif k == "evidence":
            v = parse_evidence(v, keep_evidence) if isinstance(v, str) else list(v)
        else:
            v = str(v or "").strip()
            allowed = {"stage": CONTROL_STAGES, "type": CONTROL_TYPES, "status": CONTROL_STATUSES}.get(k)
            if allowed and v not in allowed:
                raise Invalid(f"control {k} must be one of {', '.join(allowed)}")
        c[k] = v
    if "description" in c and not c["description"]:
        raise Invalid("control description is required")
    return c


def create_control(conn, user: dict, system_id: int, data: dict, *, ref: str | None = None,
                   note: str = "", _in_tx: bool = False) -> str:
    require_cso(user)

    def work():
        c = {"hazard_refs": [], "description": "", "stage": "additional", "type": "design",
             "status": "proposed", "evidence": []}
        c.update(_clean_control(conn, system_id, data))
        if not c["hazard_refs"] or not c["description"]:
            raise Invalid("a control needs a description and at least one hazard")
        new_ref = ref or next_ref(conn, system_id, "control")
        if conn.execute("SELECT 1 FROM control WHERE system_id = ? AND ref = ?", (system_id, new_ref)).fetchone():
            raise Invalid(f"control {new_ref} already exists")
        conn.execute(
            "INSERT INTO control (system_id, ref, hazard_refs, description, stage, type, status, evidence,"
            " created_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (system_id, new_ref, dumps(c["hazard_refs"]), c["description"], c["stage"], c["type"],
             c["status"], dumps(c["evidence"]), today()),
        )
        audit.append(conn, actor=user["email"], action="control_created", system_id=system_id,
                     object_ref=new_ref, after=get_control(conn, system_id, new_ref), note=note)
        return new_ref

    if _in_tx:
        return work()
    with tx(conn):
        return work()


def update_control(conn, user: dict, system_id: int, ref: str, data: dict, *, note: str = "",
                   _in_tx: bool = False) -> dict:
    require_cso(user)

    def work():
        before = get_control(conn, system_id, ref)
        after = {**before, **_clean_control(conn, system_id, data, before["evidence"])}
        if {k: after[k] for k in CONTROL_FIELDS} == {k: before[k] for k in CONTROL_FIELDS}:
            return before
        after["version"] = before["version"] + 1
        conn.execute(
            "UPDATE control SET hazard_refs = ?, description = ?, stage = ?, type = ?, status = ?, evidence = ?,"
            " version = ? WHERE system_id = ? AND ref = ?",
            (dumps(after["hazard_refs"]), after["description"], after["stage"], after["type"], after["status"],
             dumps(after["evidence"]), after["version"], system_id, ref),
        )
        after = get_control(conn, system_id, ref)
        audit.append(conn, actor=user["email"], action="control_updated", system_id=system_id,
                     object_ref=ref, before=before, after=after, note=note)
        return after

    if _in_tx:
        return work()
    with tx(conn):
        return work()


# ---------------------------------------------------------------- issued versions (DCB0129 3.3.2)

def snapshot(conn, system_id: int) -> dict:
    s = get_system(conn, system_id)
    return {"system": _public_system(s), "risk_scheme": s["risk_scheme"],
            "hazards": list_hazards(conn, system_id), "controls": list_controls(conn, system_id)}


def list_issues(conn, system_id: int) -> list[dict]:
    rows = conn.execute("SELECT system_id, number, issued_at, issued_by, note, repo_commit, chain_head"
                        " FROM issue WHERE system_id = ? ORDER BY number DESC", (system_id,))
    return [dict(r, issued_by=json.loads(r["issued_by"])) for r in rows]


def get_issue(conn, system_id: int, number: int) -> dict:
    row = conn.execute("SELECT * FROM issue WHERE system_id = ? AND number = ?", (system_id, number)).fetchone()
    if not row:
        raise LookupError(f"no issued version v{number}")
    return dict(row, issued_by=json.loads(row["issued_by"]), snapshot=json.loads(row["snapshot"]))


def changes_since_issue(conn, system_id: int) -> list[dict]:
    """Log events since the last issued version (or since the start)."""
    last = conn.execute(
        "SELECT seq FROM event WHERE system_id = ? AND action = 'version_issued' ORDER BY seq DESC LIMIT 1",
        (system_id,)).fetchone()
    return [e for e in audit.events(conn, system_id, after_seq=last["seq"] if last else 0)
            if e["action"] != "version_issued"]


def pending_proposals(conn, system_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM proposal WHERE system_id = ? AND status = 'pending'",
                        (system_id,)).fetchone()[0]


def repo_commit(system: dict) -> str:
    repo = (system.get("sources") or {}).get("repo")
    if not repo:
        return ""
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def issue_version(conn, user: dict, system_id: int, note: str = "") -> int:
    """The CSO approves this version of the hazard log. Freezes a numbered snapshot."""
    require_cso(user)
    with tx(conn):
        s = get_system(conn, system_id)
        last = conn.execute("SELECT MAX(number) FROM issue WHERE system_id = ?", (system_id,)).fetchone()[0]
        number = (last or 0) + 1
        snap = snapshot(conn, system_id)
        if not snap["hazards"]:
            raise Invalid("there are no hazards to issue")
        commit = repo_commit(s)
        signer = {"email": user["email"], "name": user["name"], "registration": user["registration"]}
        audit.append(conn, actor=user["email"], action="version_issued", system_id=system_id,
                     object_ref=f"v{number}", after={"number": number, "signer": signer, "repo_commit": commit,
                                                     "snapshot_sha256": hashlib.sha256(dumps(snap).encode()).hexdigest()},
                     note=note.strip())
        conn.execute(
            "INSERT INTO issue (system_id, number, issued_at, issued_by, note, repo_commit, chain_head, snapshot)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (system_id, number, now(), dumps(signer), note.strip(), commit, audit.head(conn), dumps(snap)),
        )
    return number
