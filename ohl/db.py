"""SQLite schema, connection and write transactions."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user (
  email         TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('cso', 'contributor', 'viewer')),
  registration  TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL,
  version          TEXT NOT NULL DEFAULT '',
  intended_use     TEXT NOT NULL DEFAULT '',
  clinical_context TEXT NOT NULL DEFAULT '',
  no_pid_confirmed INTEGER NOT NULL DEFAULT 0,
  sources          TEXT NOT NULL DEFAULT '{}',
  hazard_prefix    TEXT NOT NULL DEFAULT 'H-',
  control_prefix   TEXT NOT NULL DEFAULT 'C-',
  ref_width        INTEGER NOT NULL DEFAULT 3,
  risk_scheme      TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hazard (
  system_id              INTEGER NOT NULL REFERENCES system(id),
  ref                    TEXT NOT NULL,
  title                  TEXT NOT NULL,
  description            TEXT NOT NULL DEFAULT '',
  causes                 TEXT NOT NULL DEFAULT '[]',
  effects                TEXT NOT NULL DEFAULT '',
  harm                   TEXT NOT NULL DEFAULT '',
  initial_severity       TEXT,
  initial_likelihood     TEXT,
  initial_risk           INTEGER,
  initial_justification  TEXT NOT NULL DEFAULT '',
  residual_severity      TEXT,
  residual_likelihood    TEXT,
  residual_risk          INTEGER,
  residual_justification TEXT NOT NULL DEFAULT '',
  status                 TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'transferred', 'closed')),
  deployer_action        TEXT NOT NULL DEFAULT '',
  action                 TEXT NOT NULL DEFAULT '',
  owner                  TEXT NOT NULL DEFAULT '',
  created_at             TEXT NOT NULL,
  last_reviewed          TEXT NOT NULL DEFAULT '',
  review_due             TEXT NOT NULL DEFAULT '',
  version                INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (system_id, ref)
);

CREATE TABLE IF NOT EXISTS control (
  system_id   INTEGER NOT NULL REFERENCES system(id),
  ref         TEXT NOT NULL,
  hazard_refs TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL,
  stage       TEXT NOT NULL DEFAULT 'additional' CHECK (stage IN ('existing', 'additional')),
  type        TEXT NOT NULL CHECK (type IN ('design', 'test', 'process', 'training', 'warning')),
  status      TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'in_place', 'verified', 'invalid')),
  evidence    TEXT NOT NULL DEFAULT '[]',
  created_at  TEXT NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (system_id, ref)
);

CREATE TABLE IF NOT EXISTS issue (
  system_id   INTEGER NOT NULL REFERENCES system(id),
  number      INTEGER NOT NULL,
  issued_at   TEXT NOT NULL,
  issued_by   TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  repo_commit TEXT NOT NULL DEFAULT '',
  chain_head  TEXT NOT NULL,
  snapshot    TEXT NOT NULL,
  PRIMARY KEY (system_id, number)
);

CREATE TABLE IF NOT EXISTS proposal (
  id               INTEGER PRIMARY KEY,
  system_id        INTEGER NOT NULL REFERENCES system(id),
  origin           TEXT NOT NULL CHECK (origin IN ('scan', 'check')),
  kind             TEXT NOT NULL CHECK (kind IN ('new_hazard', 'change_hazard', 'evidence_problem', 're_review')),
  target           TEXT,
  payload          TEXT NOT NULL DEFAULT '{}',
  reasoning        TEXT NOT NULL DEFAULT '',
  assumptions      TEXT NOT NULL DEFAULT '',
  citations        TEXT NOT NULL DEFAULT '[]',
  confidence       TEXT NOT NULL DEFAULT 'low' CHECK (confidence IN ('low', 'medium', 'high')),
  suggested_rating TEXT NOT NULL DEFAULT '{}',
  run_id           TEXT NOT NULL DEFAULT '',
  model_used       TEXT NOT NULL DEFAULT '',
  prompt_version   TEXT NOT NULL DEFAULT '',
  inputs_sha256    TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'edited', 'rejected')),
  reject_reason    TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
  seq       INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  system_id INTEGER,
  object_ref TEXT NOT NULL DEFAULT '',
  before    TEXT NOT NULL DEFAULT 'null',
  after     TEXT NOT NULL DEFAULT 'null',
  note      TEXT NOT NULL DEFAULT '',
  prev_hash TEXT NOT NULL,
  hash      TEXT NOT NULL
);

-- What the checks last saw. Observations only: not part of the approved log.
CREATE TABLE IF NOT EXISTS evidence_obs (
  system_id       INTEGER NOT NULL REFERENCES system(id),
  control_ref     TEXT NOT NULL,
  location        TEXT NOT NULL,
  baseline_sha256 TEXT NOT NULL DEFAULT '',   -- confirmed version (first seen, or re-confirmed by the CSO)
  seen_sha256     TEXT NOT NULL DEFAULT '',
  state           TEXT NOT NULL,              -- ok | changed | missing | manual | unresolved
  ack             TEXT NOT NULL DEFAULT '',   -- problem the CSO has already decided on (state:sha)
  checked_at      TEXT NOT NULL,
  PRIMARY KEY (system_id, control_ref, location)
);

CREATE TABLE IF NOT EXISTS check_state (
  system_id INTEGER NOT NULL REFERENCES system(id),
  key       TEXT NOT NULL,
  value     TEXT NOT NULL,
  PRIMARY KEY (system_id, key)
);

-- The audit trail and issued versions are append-only. These triggers stop
-- casual edits; the hash chain (ohl verify) catches anyone who removes them.
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
  BEGIN SELECT RAISE(ABORT, 'event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
  BEGIN SELECT RAISE(ABORT, 'event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS issue_no_update BEFORE UPDATE ON issue
  BEGIN SELECT RAISE(ABORT, 'issued versions are final'); END;
CREATE TRIGGER IF NOT EXISTS issue_no_delete BEFORE DELETE ON issue
  BEGIN SELECT RAISE(ABORT, 'issued versions are final'); END;
"""


# Columns added after a table was first released. Applied on connect when missing.
MIGRATIONS = [
    ("proposal", "cso_rating", "TEXT NOT NULL DEFAULT '{}'"),       # the CSO's blind rating
    ("proposal", "first_viewed_at", "TEXT NOT NULL DEFAULT ''"),   # for time-on-proposal
    ("proposal", "decided_at", "TEXT NOT NULL DEFAULT ''"),
    ("proposal", "decided_by", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def default_path() -> Path:
    return Path(os.environ.get("OHL_DB", "ohl.db"))


def connect(path: str | Path | None = None, init: bool = True) -> sqlite3.Connection:
    """Open the database (creating the schema unless init=False). Autocommit mode: writes go through tx()."""
    conn = sqlite3.connect(path or default_path(), isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not init:
        return conn
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    if conn.execute("SELECT 1 FROM meta WHERE key = 'secret'").fetchone() is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('secret', ?)", (secrets.token_hex(32),))
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open an existing database read-only (for verifying a backup without changing it).
    With no write-ahead log beside it, nothing has it open for writing, so it's opened as
    immutable: no side files are made, and it works on read-only media too."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no database at {p}")
    wal = p.with_name(p.name + "-wal")
    flags = "mode=ro" + ("" if wal.exists() and wal.stat().st_size else "&immutable=1")
    conn = sqlite3.connect(f"{p.resolve().as_uri()}?{flags}", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def backup(src: sqlite3.Connection, out: str | Path) -> None:
    """A consistent copy of a live database (VACUUM INTO), without the session secret.

    - The copy is made and scrubbed in a private temporary folder, then rebuilt with VACUUM,
      so no byte of the secret is left even in free space. A file holding the secret never
      appears where the backup goes (a synced folder, a USB stick).
    - It is written next to `out` under a '.partial' name, flushed to disk, then given its
      real name with a hard link, which never replaces an existing file. A run that is
      killed part-way never leaves a file under the backup's name; a '.partial' file left by
      a run killed outright (power cut) is removed by the next backup to that name.
    - Where hard links aren't supported (FAT and exFAT USB sticks), the name is claimed with
      an exclusive create and the copy written into it; a failure removes only that file.
    """
    import shutil
    import tempfile
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; choose a new file name")
    if not out.parent.is_dir():
        raise FileNotFoundError(f"the folder {out.parent} doesn't exist; create it first")
    import time
    for old in out.parent.glob(f".{out.name}.partial-*"):  # left by a run killed outright
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    part = out.with_name(f".{out.name}.partial-{secrets.token_hex(4)}")
    with tempfile.TemporaryDirectory(prefix="ohl-backup-") as work:
        tmp = Path(work) / "copy.db"
        src.execute("VACUUM INTO ?", (str(tmp),))
        copy = sqlite3.connect(tmp, isolation_level=None)
        try:
            copy.execute("PRAGMA secure_delete = ON")  # overwrite the secret's bytes, don't just unlink them
            copy.execute("DELETE FROM meta WHERE key = 'secret'")
            copy.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('backup_at', ?)", (now(),))
            copy.execute("VACUUM")                     # and rebuild, so no free page keeps any of it
        finally:
            copy.close()
        try:
            with open(tmp, "rb") as r, open(part, "xb") as w:
                shutil.copyfileobj(r, w)
                w.flush()
                os.fsync(w.fileno())
            try:
                os.link(part, out)                     # fails if `out` exists: never overwrites
            except FileExistsError:
                raise FileExistsError(f"{out} already exists; choose a new file name") from None
            except OSError:                            # no hard links here (FAT, exFAT, some shares)
                _copy_exclusive(tmp, out)
        finally:
            part.unlink(missing_ok=True)


def _copy_exclusive(src: Path, out: Path) -> None:
    import shutil
    try:
        w = open(out, "xb")                            # claims the name, or fails: never overwrites
    except FileExistsError:
        raise FileExistsError(f"{out} already exists; choose a new file name") from None
    try:
        with w, open(src, "rb") as r:
            shutil.copyfileobj(r, w)
            w.flush()
            os.fsync(w.fileno())
    except BaseException:
        out.unlink(missing_ok=True)                    # only ever the file this run created
        raise


@contextmanager
def tx(conn: sqlite3.Connection):
    """One write transaction. BEGIN IMMEDIATE takes the write lock up front, so two
    writers can never read the same audit-chain head."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # SQLite may already have rolled back (a full disk does that). A failed ROLLBACK
        # must never replace the error that caused it.
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    else:
        conn.execute("COMMIT")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def dumps(obj) -> str:
    """Canonical JSON: sorted keys, no whitespace, UTF-8 kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
