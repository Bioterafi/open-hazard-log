"""Append-only audit trail with a SHA-256 hash chain.

hash(n) = sha256(prev_hash + canonical_json(row n without its hash))

Every row includes its sequence number, so deleting, reordering or editing any
row breaks the chain from that point on. `verify` reports the first broken row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .db import dumps, now

GENESIS = "0" * 64
FIELDS = ("seq", "at", "actor", "action", "system_id", "object_ref", "before", "after", "note", "prev_hash")


def _digest(row: dict) -> str:
    body = dumps({k: row[k] for k in FIELDS})
    return hashlib.sha256((row["prev_hash"] + body).encode("utf-8")).hexdigest()


def head(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM event ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS


def append(conn: sqlite3.Connection, *, actor: str, action: str, system_id: int | None = None,
           object_ref: str = "", before=None, after=None, note: str = "") -> str:
    """Write one event. Call inside db.tx() together with the change it records."""
    if not conn.in_transaction:
        raise RuntimeError("audit.append must run inside a write transaction")
    last = conn.execute("SELECT seq, hash FROM event ORDER BY seq DESC LIMIT 1").fetchone()
    row = {
        "seq": (last["seq"] + 1) if last else 1,
        "at": now(),
        "actor": actor,
        "action": action,
        "system_id": system_id,
        "object_ref": object_ref,
        "before": dumps(before),
        "after": dumps(after),
        "note": note,
        "prev_hash": last["hash"] if last else GENESIS,
    }
    row["hash"] = _digest(row)
    conn.execute(
        "INSERT INTO event (seq, at, actor, action, system_id, object_ref, before, after, note, prev_hash, hash)"
        " VALUES (:seq, :at, :actor, :action, :system_id, :object_ref, :before, :after, :note, :prev_hash, :hash)",
        row,
    )
    return row["hash"]


@dataclass
class Verdict:
    ok: bool
    count: int
    head: str
    bad_seq: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"OK: {self.count} events, chain head {self.head[:16]}…"
        return f"BROKEN at event {self.bad_seq}: {self.reason}"


def verify_rows(rows) -> Verdict:
    prev, expected_seq, count = GENESIS, 1, 0
    for r in rows:
        r = dict(r)
        if r["seq"] != expected_seq:
            return Verdict(False, count, prev, r["seq"], f"expected event {expected_seq} (missing or reordered rows)")
        if r["prev_hash"] != prev:
            return Verdict(False, count, prev, r["seq"], "link to previous event does not match")
        if _digest(r) != r["hash"]:
            return Verdict(False, count, prev, r["seq"], "contents do not match their hash (row was edited)")
        prev, expected_seq, count = r["hash"], expected_seq + 1, count + 1
    return Verdict(True, count, prev)


def verify(conn: sqlite3.Connection) -> Verdict:
    return verify_rows(conn.execute("SELECT * FROM event ORDER BY seq"))


def events(conn: sqlite3.Connection, system_id: int | None = None, object_ref: str | None = None,
           after_seq: int = 0) -> list[dict]:
    sql, args = "SELECT * FROM event WHERE seq > ?", [after_seq]
    if system_id is not None:
        sql += " AND system_id = ?"
        args.append(system_id)
    if object_ref is not None:
        sql += " AND object_ref = ?"
        args.append(object_ref)
    return [dict(r) for r in conn.execute(sql + " ORDER BY seq", args)]


def export(conn: sqlite3.Connection, system_id: int | None = None) -> dict:
    """Whole chain (it can only be verified whole), optionally flagged for one system."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM event ORDER BY seq")]
    return {"ohl_audit": 1, "exported_at": now(), "system_id": system_id,
            "verdict": str(verify_rows(rows)), "head": rows[-1]["hash"] if rows else GENESIS, "events": rows}


def replay(conn: sqlite3.Connection, system_id: int, until: str | None = None) -> dict:
    """Rebuild a system's hazards and controls from events alone, up to an ISO time."""
    hazards, controls = {}, {}
    for e in events(conn, system_id):
        if until and e["at"] > until:
            break
        after = json.loads(e["after"])
        if e["action"] in ("hazard_created", "hazard_updated"):
            hazards[e["object_ref"]] = after
        elif e["action"] in ("control_created", "control_updated"):
            controls[e["object_ref"]] = after
    return {"hazards": hazards, "controls": controls}
