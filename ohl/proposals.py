"""Proposals: what agents and checks suggest, and the CSO's decisions on them.

`record_run` is the only write the scan can make. Decisions (approve, edit, reject)
are CSO-only and change the log through service.py, in the same transaction as
the decision's audit event.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import audit, risk
from . import service as S
from .db import dumps, now, today, tx

KINDS = ("new_hazard", "change_hazard", "evidence_problem", "re_review")
REJECT_REASONS = {
    "duplicate": "Duplicate",
    "not_a_hazard": "Not a hazard",
    "out_of_scope": "Out of scope",
    "wrong_context": "Wrong clinical context",
    "already_controlled": "Already controlled",
    "false_alarm": "False alarm",
    "other": "Other",
}


def pid(n: int) -> str:
    return f"P-{n:03d}"


def _row(r) -> dict:
    p = dict(r)
    for k in ("payload", "citations", "suggested_rating", "cso_rating"):
        p[k] = json.loads(p[k])
    p["label"] = pid(p["id"])
    return p


def get(conn, system_id: int, proposal_id: int) -> dict:
    r = conn.execute("SELECT * FROM proposal WHERE id = ? AND system_id = ?", (proposal_id, system_id)).fetchone()
    if not r:
        raise LookupError(f"no proposal {pid(proposal_id)}")
    return _row(r)


def pending(conn, system_id: int) -> list[dict]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM proposal WHERE system_id = ? AND status = 'pending' ORDER BY id", (system_id,))]


def decided(conn, system_id: int, limit: int = 10) -> list[dict]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM proposal WHERE system_id = ? AND status != 'pending' ORDER BY decided_at DESC, id DESC LIMIT ?",
        (system_id, limit))]


def recent_rejections(conn, system_id: int, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT payload, reject_reason FROM proposal WHERE system_id = ? AND status = 'rejected'"
                        " ORDER BY decided_at DESC LIMIT ?", (system_id, limit))
    return [{"title": json.loads(r["payload"]).get("title", ""), "reason": r["reject_reason"]} for r in rows]


def last_run(conn, system_id: int, origin: str = "scan") -> dict | None:
    r = conn.execute("SELECT at, after FROM event WHERE system_id = ? AND action = ? ORDER BY seq DESC LIMIT 1",
                     (system_id, f"{origin}_run")).fetchone()
    return dict(json.loads(r["after"]), at=r["at"]) if r else None


# ---------------------------------------------------------------- the scan's only write

def record_run(conn, system_id: int, run: dict, items: list[dict]) -> list[int]:
    """Store one agent run's proposals plus a single run event, atomically.

    `run` holds run_id, model_used, prompt_version, inputs_sha256, started_by and the
    summary; each item holds kind, target, payload, reasoning, assumptions, citations,
    confidence and suggested_rating.
    """
    ids = []
    with tx(conn):
        for it in items:
            if it["kind"] not in KINDS:
                raise ValueError(f"unknown proposal kind {it['kind']}")
            cur = conn.execute(
                "INSERT INTO proposal (system_id, origin, kind, target, payload, reasoning, assumptions, citations,"
                " confidence, suggested_rating, run_id, model_used, prompt_version, inputs_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (system_id, run.get("origin", "scan"), it["kind"], it.get("target"), dumps(it["payload"]),
                 it.get("reasoning", ""), it.get("assumptions", ""), dumps(it.get("citations", [])),
                 it.get("confidence", "low"), dumps(it.get("suggested_rating", {})), run["run_id"],
                 run.get("model_used", ""), run.get("prompt_version", ""), run.get("inputs_sha256", ""), now()))
            ids.append(cur.lastrowid)
        origin = run.get("origin", "scan")
        actor = f"agent:{run['run_id']}" if origin == "scan" else f"check:{run['run_id']}"  # checks aren't AI
        audit.append(conn, actor=actor, action=f"{origin}_run",
                     system_id=system_id, object_ref=f"run:{run['run_id']}",
                     after={**{k: v for k, v in run.items() if k != "origin"}, "proposals": [pid(i) for i in ids]},
                     note=f"started by {run.get('started_by', '')}")
    return ids


# ---------------------------------------------------------------- review

def mark_viewed(conn, user: dict, p: dict) -> None:
    if user["role"] == "cso" and not p["first_viewed_at"] and p["status"] == "pending":
        conn.execute("UPDATE proposal SET first_viewed_at = ? WHERE id = ? AND first_viewed_at = ''",
                     (now(), p["id"]))


def needs_rating(p: dict) -> bool:
    """New hazards are rated blind by the CSO before approval (OHL-H3)."""
    return p["kind"] == "new_hazard"


def rate(conn, user: dict, system_id: int, proposal_id: int, severity: str, likelihood: str,
         rationale: str = "") -> dict:
    """The CSO's own rating, entered before the agent's suggestion is shown. Fixed once saved."""
    S.require_cso(user)
    p = get(conn, system_id, proposal_id)
    scheme = S.get_system(conn, system_id)["risk_scheme"]
    if p["status"] != "pending":
        raise S.Invalid(f"{p['label']} has already been decided")
    if p["cso_rating"]:
        raise S.Invalid("Your blind rating is already recorded. Change the final rating with Edit.")
    if severity not in scheme["severities"] or likelihood not in scheme["likelihoods"]:
        raise S.Invalid("Choose both a severity and a likelihood.")
    rating = {"severity": severity, "likelihood": likelihood, "risk": risk.rating(scheme, severity, likelihood),
              "rationale": rationale.strip(), "by": user["email"], "at": now()}
    conn.execute("UPDATE proposal SET cso_rating = ? WHERE id = ? AND cso_rating = '{}'", (dumps(rating), p["id"]))
    return rating


def _seconds_on(p: dict) -> int | None:
    if not p["first_viewed_at"]:
        return None
    t0 = datetime.strptime(p["first_viewed_at"], "%Y-%m-%dT%H:%M:%SZ")
    t1 = datetime.strptime(now(), "%Y-%m-%dT%H:%M:%SZ")
    return int((t1 - t0).total_seconds())


def _decide(conn, user, p, status: str, *, outcome: dict, reject_reason: str = "", before=None) -> None:
    conn.execute("UPDATE proposal SET status = ?, reject_reason = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                 (status, reject_reason, now(), user["email"], p["id"]))
    audit.append(conn, actor=user["email"], action=f"proposal_{status}", system_id=p["system_id"],
                 object_ref=p["label"], before=before, note=reject_reason,
                 after={"kind": p["kind"], "target": p["target"], "run_id": p["run_id"],
                        "model_used": p["model_used"], "prompt_version": p["prompt_version"],
                        "blind_rating": p["cso_rating"] or None, "suggested_rating": p["suggested_rating"] or None,
                        "seconds_on_proposal": _seconds_on(p), **outcome})


def _check_pending(user, p):
    S.require_cso(user)
    if p["status"] != "pending":
        raise S.Invalid(f"{p['label']} has already been decided ({p['status']})")


def _control_lines(controls: list[dict]) -> str:
    return "\n".join(f"{c['type']}: {c['description']}" for c in controls)


def parse_control_lines(text: str) -> list[dict]:
    """One per line: 'type: description'. A line without a known type counts as design."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and head.strip().lower() in S.CONTROL_TYPES:
            out.append({"type": head.strip().lower(), "description": rest.strip()})
        else:
            out.append({"type": "design", "description": line})
    return [c for c in out if c["description"]]


def _apply(conn, user, p, fields: dict, note: str) -> dict:
    """Write an approved (possibly edited) proposal into the log. Returns what was created."""
    sid = p["system_id"]
    controls = fields.pop("controls")
    if p["kind"] == "new_hazard":
        ref = S.create_hazard(conn, user, sid, fields, note=note, _in_tx=True)
    elif p["kind"] == "change_hazard":
        ref = p["target"]
        current = S.get_hazard(conn, sid, ref)
        known = {c["text"].strip().lower() for c in current["causes"]}
        new_causes = [c for c in fields.get("causes", []) if c["text"].strip().lower() not in known]
        changes = {"causes": current["causes"] + new_causes}
        for k in ("description", "effects", "harm"):
            if not current[k] and fields.get(k):
                changes[k] = fields[k]
        S.update_hazard(conn, user, sid, ref, changes, note=note, _in_tx=True)
    else:
        raise S.Invalid(f"{p['kind']} proposals are decided in M3")
    made = [S.create_control(conn, user, sid, {"hazard_refs": [ref], "description": c["description"],
                                               "type": c["type"], "stage": "additional", "status": "proposed"},
                             note=note, _in_tx=True) for c in controls]
    return {"hazard": ref, "controls": made}


def as_fields(p: dict) -> dict:
    """The proposal as hazard fields, with the CSO's blind rating (never the agent's)."""
    pl = p["payload"]
    f = {k: pl.get(k, "") for k in ("title", "description", "effects", "harm")}
    f["causes"] = pl.get("causes", [])
    f["controls"] = pl.get("controls", [])
    if p["kind"] == "new_hazard":
        r = p["cso_rating"]
        f.update(initial_severity=r.get("severity"), initial_likelihood=r.get("likelihood"),
                 initial_justification=r.get("rationale", ""))
    return f


# ---------------------------------------------------------------- decisions on check proposals (M3)

def _evidence_obs(conn, p: dict, *, rebaseline: bool = False) -> None:
    """Record the CSO's decision against what the checks saw, so the same problem isn't raised twice."""
    pl = p["payload"]
    if rebaseline:
        conn.execute("UPDATE evidence_obs SET baseline_sha256 = seen_sha256, state = 'ok', ack = ''"
                     " WHERE system_id = ? AND control_ref = ? AND location = ? AND seen_sha256 != ''",
                     (p["system_id"], pl["control"], pl["location"]))
    else:
        conn.execute("UPDATE evidence_obs SET ack = ? WHERE system_id = ? AND control_ref = ? AND location = ?",
                     (pl["fingerprint"], p["system_id"], pl["control"], pl["location"]))


def reconfirm_evidence(conn, user: dict, system_id: int, proposal_id: int) -> dict:
    """The CSO has looked: the changed file still supports the control. Its new version becomes the baseline."""
    p = get(conn, system_id, proposal_id)
    _check_pending(user, p)
    if p["kind"] != "evidence_problem":
        raise S.Invalid("Only evidence problems can be re-confirmed.")
    if p["payload"]["state"] != "changed":
        raise S.Invalid("Missing evidence can't be re-confirmed. Mark the control invalid, or fix the evidence "
                        "link on the control and dismiss this.")
    with tx(conn):
        _evidence_obs(conn, p, rebaseline=True)
        _decide(conn, user, p, "approved", outcome={"evidence": "re-confirmed", "control": p["payload"]["control"],
                                                    "location": p["payload"]["location"],
                                                    "new_sha256": p["payload"]["seen_sha256"]})
    return {"control": p["payload"]["control"]}


def invalidate_control(conn, user: dict, system_id: int, proposal_id: int) -> dict:
    """The evidence no longer supports the control: mark the control invalid in the log."""
    p = get(conn, system_id, proposal_id)
    _check_pending(user, p)
    if p["kind"] != "evidence_problem":
        raise S.Invalid("Only evidence problems can invalidate a control.")
    ref = p["payload"]["control"]
    with tx(conn):
        S.update_control(conn, user, system_id, ref, {"status": "invalid"}, note=f"from {p['label']}", _in_tx=True)
        _evidence_obs(conn, p)
        _decide(conn, user, p, "approved", outcome={"control": ref, "control_status": "invalid"})
    return {"control": ref}


def mark_reviewed(conn, user: dict, system_id: int, proposal_id: int) -> dict:
    """Linked code changed; the CSO has re-reviewed the hazard and it still stands. Sets last reviewed to today."""
    p = get(conn, system_id, proposal_id)
    _check_pending(user, p)
    if p["kind"] != "re_review":
        raise S.Invalid("Only re-review proposals can be marked reviewed.")
    ref = p["target"]
    with tx(conn):
        S.update_hazard(conn, user, system_id, ref, {"last_reviewed": today()}, note=f"re-reviewed for {p['label']}",
                        _in_tx=True)
        _decide(conn, user, p, "approved", outcome={"hazard": ref, "last_reviewed": today(),
                                                    "commits": [c["sha"] for c in p["payload"].get("commits", [])]})
    return {"hazard": ref}


def bulk_reconfirm(conn, user: dict, system_id: int, proposal_ids: list[int], confirmed: bool) -> int:
    """Re-confirm several changed-evidence proposals at once (SPEC section 7). Never for hazards or ratings."""
    S.require_cso(user)
    if not confirmed:
        raise S.Invalid("Tick the box to confirm you've re-checked each file.")
    ps = [get(conn, system_id, i) for i in proposal_ids]
    if not ps:
        raise S.Invalid("Choose at least one item.")
    for p in ps:
        _check_pending(user, p)
        if p["kind"] != "evidence_problem" or p["payload"]["state"] != "changed":
            raise S.Invalid(f"{p['label']} can't be bulk re-confirmed; only changed evidence can.")
    with tx(conn):
        for p in ps:
            _evidence_obs(conn, p, rebaseline=True)
            _decide(conn, user, p, "approved", outcome={"evidence": "re-confirmed", "bulk": True,
                                                        "control": p["payload"]["control"],
                                                        "location": p["payload"]["location"]})
    return len(ps)


def approve(conn, user: dict, system_id: int, proposal_id: int) -> dict:
    p = get(conn, system_id, proposal_id)
    if p["kind"] == "evidence_problem":
        return reconfirm_evidence(conn, user, system_id, proposal_id)
    if p["kind"] == "re_review":
        return mark_reviewed(conn, user, system_id, proposal_id)
    _check_pending(user, p)
    if needs_rating(p) and not p["cso_rating"]:
        raise S.Invalid("Enter your own rating before approving.")
    with tx(conn):
        made = _apply(conn, user, p, as_fields(p), note=f"from {p['label']}")
        _decide(conn, user, p, "approved", outcome=made)
    return made


def edit_approve(conn, user: dict, system_id: int, proposal_id: int, form: dict) -> dict:
    """The CSO's edited version goes into the log; the event keeps both versions."""
    p = get(conn, system_id, proposal_id)
    _check_pending(user, p)
    if needs_rating(p) and not p["cso_rating"]:
        raise S.Invalid("Enter your own rating before approving.")
    fields = {k: form.get(k, "") for k in ("title", "description", "effects", "harm")}
    fields["causes"] = S.parse_causes(form.get("causes", ""))
    fields["controls"] = parse_control_lines(form.get("controls", ""))
    if p["kind"] == "new_hazard":
        for k in ("initial_severity", "initial_likelihood", "initial_justification"):
            fields[k] = form.get(k, "")
        if not fields["title"].strip():
            raise S.Invalid("hazard title is required")
    final = dict(fields, causes=list(fields["causes"]), controls=list(fields["controls"]))
    with tx(conn):
        made = _apply(conn, user, p, fields, note=f"from {p['label']}, edited")
        _decide(conn, user, p, "edited", before=as_fields(p), outcome={**made, "final": final})
    return made


def reject(conn, user: dict, system_id: int, proposal_id: int, reason: str, detail: str = "") -> None:
    p = get(conn, system_id, proposal_id)
    _check_pending(user, p)
    if reason not in REJECT_REASONS:
        raise S.Invalid("Choose a reason for rejecting.")
    text = REJECT_REASONS[reason] + (f": {detail.strip()}" if detail.strip() else "")
    if reason == "other" and not detail.strip():
        raise S.Invalid("Say why, when the reason is Other.")
    with tx(conn):
        if p["kind"] == "evidence_problem":
            _evidence_obs(conn, p)  # dismissed: don't raise this exact problem again
        _decide(conn, user, p, "rejected", outcome={}, reject_reason=text)


def edit_defaults(p: dict) -> dict:
    """Form values for the Edit page."""
    f = as_fields(p)
    return dict(f, causes=S.format_causes(f["causes"]), controls=_control_lines(f["controls"]))
