"""ohl command line."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sqlite3
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

from . import audit, db, export, importer, proposals
from . import service as S

SEED = files("ohl") / "seed" / "ohl_self.toml"  # OHL's own hazard log, shipped inside the package


def _conn(args):
    return db.connect(args.db)


def _user(conn, email: str) -> dict:
    u = S.get_user(conn, email)
    if not u:
        sys.exit(f"no user {email}; add one with: ohl user add")
    return u


def _out(path: str | None, data: str | bytes) -> None:
    if path:
        if not Path(path).parent.is_dir():
            sys.exit(f"the folder {Path(path).parent} doesn't exist; create it first")
        try:
            Path(path).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        except OSError as e:
            sys.exit(f"couldn't write {path}: {e.strerror or e}")
        print(f"wrote {path}")
    elif isinstance(data, bytes):
        sys.exit("binary output needs --out")
    else:
        sys.stdout.write(data)


def cmd_init(args):
    _conn(args)
    print(f"database ready: {args.db}")


def cmd_user_add(args):
    conn = _conn(args)
    u = S.add_user(conn, email=args.email, name=args.name, role=args.role,
                   registration=args.registration or "", actor=f"cli:{getpass.getuser()}")
    print(f"added {u['role']} {u['name']} <{u['email']}>")


def cmd_system_add(args):
    conn = _conn(args)
    sid = S.create_system(conn, _user(conn, args.as_), name=args.name, version=args.version or "",
                          intended_use=args.intended_use or "", clinical_context=args.clinical_context or "")
    print(f"created system {sid}: {args.name}")


def cmd_system_configure(args):
    conn = _conn(args)
    user = _user(conn, args.as_)
    s = S.get_system(conn, args.system)
    sources = dict(s["sources"])
    if args.repo is not None:
        sources["repo"] = args.repo
    if args.docs is not None:
        sources["docs"] = [d.strip() for d in args.docs.split(",") if d.strip()]
    if args.code_globs is not None:
        sources["code_globs"] = [g.strip() for g in args.code_globs.split(",") if g.strip()]
    if args.uses_ai is not None:
        sources["uses_ai"] = args.uses_ai
    fields = {"sources": sources}
    if args.confirm_no_pid:
        fields["no_pid_confirmed"] = True
    for k in ("intended_use", "clinical_context", "version"):
        if getattr(args, k) is not None:
            fields[k] = getattr(args, k)
    S.update_system(conn, user, args.system, fields)
    print(f"updated system {args.system}")


def cmd_seed_self(args):
    conn = _conn(args)
    user = _user(conn, args.as_)
    seed = tomllib.loads(SEED.read_text(encoding="utf-8"))
    s = seed["system"]
    sid = S.create_system(conn, user, name=s["name"], version=s["version"], intended_use=s["intended_use"],
                          clinical_context=s["clinical_context"], hazard_prefix=s["hazard_prefix"],
                          control_prefix=s["control_prefix"], ref_width=s["ref_width"])
    with db.tx(conn):
        for h in seed["hazard"]:
            ref = S.create_hazard(conn, user, sid, {"title": h["title"], "harm": h.get("harm", ""),
                                                    "causes": "\n".join(h.get("causes", [])),
                                                    "deployer_action": h.get("deployer_action", "")},
                                  note="seeded from ohl/seed/ohl_self.toml", _in_tx=True)
            for c in h.get("controls", []):
                S.create_control(conn, user, sid, {"hazard_refs": [ref], **c}, note="seeded", _in_tx=True)
    if args.repo:
        S.update_system(conn, user, sid, {"sources": {"repo": str(Path(args.repo).expanduser().resolve())}})
    print(f"created system {sid}: {s['name']} with {len(seed['hazard'])} unrated hazards"
          + (f"; evidence is checked against {args.repo}" if args.repo else ""))


def cmd_import(args):
    conn = _conn(args)
    try:
        r = importer.import_file(conn, _user(conn, args.as_), args.system, args.file)
    except S.Invalid as e:
        sys.exit(str(e))
    print(f"imported {len(r.hazards)} hazards, {len(r.controls)} controls")
    for w in r.warnings:
        print(f"warning: {w}")
    for c in r.ignored_columns:
        print(f"ignored column: {c}")


def cmd_scan(args):
    from . import scan
    conn = _conn(args)
    user = _user(conn, args.as_)
    system, inputs = scan.preflight(conn, user, args.system)
    size = sum(len(t) for t in inputs.sources.values())
    client = scan.ReplayClient(args.replay) if args.replay else None
    how = (f"replaying saved output {args.replay}, no model call" if client else
           f"model {args.model or scan.DEFAULT_MODEL}; this can take a few minutes")
    print(f"Scanning {system['name']}: {len(inputs.sources)} sources, {size:,} characters, "
          f"{len(inputs.tree)} files in tree; {how}.")
    try:
        r = scan.run(conn, user, args.system, client=client, model=args.model, max_proposals=args.max)
    except (S.Invalid, S.Forbidden):
        raise
    except Exception as e:  # bad output, API, network, auth: say what happened and whether it's recorded
        sys.exit(proposals.failure_message("Scan", e))
    print(f"Run {r.run_id} ({r.model_used}): {len(r.shown)} proposals for review, "
          f"{r.hidden_no_quote} hidden (no verified quote), {r.over_limit} over the limit.")
    print(f"Lenses: {', '.join(r.lenses_run)}")
    if r.not_proposed_summary:
        print(f"Not proposed: {r.not_proposed_summary}")
    for s in r.skipped:
        print(f"skipped source: {s}")


def _user_or_only_cso(conn, email: str | None) -> dict:
    if email:
        return _user(conn, email)
    csos = conn.execute("SELECT email FROM user WHERE role = 'cso'").fetchall()
    if len(csos) != 1:
        sys.exit("say who is running this with --as you@example.org")
    return _user(conn, csos[0]["email"])


def cmd_check(args):
    from . import checks
    conn = _conn(args)
    user = _user_or_only_cso(conn, args.as_)
    S.get_system(conn, args.system)  # an unknown system stops here, before any run starts
    try:
        r = checks.run(conn, user, args.system)
    except S.Forbidden:
        raise
    except Exception as e:  # anything once the run has started, KeyError included
        sys.exit(proposals.failure_message("Checks", e, "observations or proposals"))
    print(f"Checks {r.run_id}: {r.evidence_checked} evidence files checked ({r.baselined} seen for the first time), "
          f"{len(r.proposals)} new proposal{'s' if len(r.proposals) != 1 else ''} for review.")
    for line in r.problems:
        print(f"evidence: {line}")
    for ref in r.code_changed:
        print(f"linked code changed: {ref}")
    for note in r.notes:
        print(f"note: {note}")


def cmd_export(args):
    conn = _conn(args)
    issue = S.get_issue(conn, args.system, args.version) if args.version else None
    snap = issue["snapshot"] if issue else S.snapshot(conn, args.system)
    fn = {"csv": export.to_csv, "xlsx": export.to_xlsx, "json": export.to_json,
          "deployer-csv": export.deployer_csv, "deployer-json": export.deployer_json}[args.format]
    _out(args.out, fn(snap, issue))


def cmd_audit(args):
    conn = _conn(args)
    _out(args.out, json.dumps(audit.export(conn, args.system), indent=2, ensure_ascii=False))


def cmd_verify(args):
    if args.file:
        rows = json.loads(Path(args.file).read_text())["events"]
        verdict = audit.verify_rows(rows)
    else:
        try:
            conn = db.connect_ro(args.db)  # read-only: verifying a backup never changes it
            verdict = audit.verify(conn)
        except FileNotFoundError as e:
            sys.exit(str(e))
        except sqlite3.Error as e:
            print(f"FAILED: {args.db} can't be read as an Open Hazard Log database ({type(e).__name__}: {e})")
            sys.exit(1)
        except OSError as e:
            print(f"FAILED: can't read {args.db}: {e.strerror or e}")
            sys.exit(1)
    print(verdict)
    sys.exit(0 if verdict.ok else 1)


def _stop_on_hangup():
    """Closing the terminal or shutting down raises SystemExit, so clean-up still runs."""
    import signal

    def stop(signum, frame):
        raise SystemExit(f"backup stopped by signal {signum}, nothing written to the backup's name")
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM))}
    return lambda: [signal.signal(s, h) for s, h in old.items()]


def cmd_backup(args):
    restore = _stop_on_hangup()
    try:
        if not Path(args.db).is_file():
            sys.exit(f"no database at {args.db}")
        conn = db.connect(args.db, init=False)  # never writes to the database it copies
        db.backup(conn, args.out)
    except (FileExistsError, FileNotFoundError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("backup stopped, nothing written to the backup's name")
    except (OSError, sqlite3.Error) as e:
        sys.exit(f"backup failed: {type(e).__name__}: {e}")
    finally:
        restore()  # the copy is in place now; from here a stop leaves a complete backup
    try:
        copy = audit.verify(db.connect_ro(args.out))
        # The app may have written since the copy was taken, so the copy's head must be in the live chain.
        in_live = copy.count == 0 or conn.execute("SELECT 1 FROM event WHERE hash = ?", (copy.head,)).fetchone()
    except (OSError, sqlite3.Error) as e:
        sys.exit(f"backup written to {args.out} but it couldn't be checked ({type(e).__name__}: {e}); "
                 f"run: ohl --db {args.out} verify")
    if not copy.ok or not in_live:
        sys.exit(f"backup written to {args.out} but it doesn't verify against the database: {copy}")
    digest = hashlib.sha256(Path(args.out).read_bytes()).hexdigest()
    print(f"backed up {args.db} to {args.out}: {copy}")
    print(f"file SHA-256 {digest}. The session secret is left out; a new one is made when the backup is opened.")


def cmd_issue(args):
    conn = _conn(args)
    user = _user(conn, args.as_)
    if not args.confirm:
        sys.exit("Issuing signs this version of the log in your name. Review it first, then add --confirm.")
    n = S.issue_version(conn, user, args.system, args.note or "")
    issue = S.get_issue(conn, args.system, n)
    print(f"issued v{n} of system {args.system}, signed by {user['name']}"
          + (f", repo commit {issue['repo_commit'][:12]}" if issue.get("repo_commit") else ", no repo commit recorded"))


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def cmd_serve(args):
    import os

    import uvicorn
    if args.host not in LOOPBACK:
        sys.exit("There's no login, so the app only listens on this computer (127.0.0.1). "
                 "Anyone who could reach it would act as you.")
    conn = _conn(args)
    if args.as_:
        _user(conn, args.as_)
        os.environ["OHL_USER"] = args.as_
    os.environ["OHL_DB"] = str(args.db)
    uvicorn.run("ohl.app:app", host=args.host, port=args.port, reload=args.reload)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ohl", description="Open Hazard Log")
    p.add_argument("--db", default=str(db.default_path()), help="database file (default: $OHL_DB or ./ohl.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database").set_defaults(fn=cmd_init)

    u = sub.add_parser("user", help="manage users").add_subparsers(dest="ucmd", required=True)
    ua = u.add_parser("add", help="add a user")
    ua.add_argument("email")
    ua.add_argument("--name", required=True)
    ua.add_argument("--role", choices=S.ROLES, required=True)
    ua.add_argument("--registration", help="CSO professional registration, e.g. 'GMC 1234567'")
    ua.set_defaults(fn=cmd_user_add)

    sa = sub.add_parser("system", help="manage systems").add_subparsers(dest="scmd", required=True)
    sadd = sa.add_parser("add", help="add a system")
    sadd.add_argument("name")
    sadd.add_argument("--version")
    sadd.add_argument("--intended-use")
    sadd.add_argument("--clinical-context")
    sadd.add_argument("--as", dest="as_", required=True, help="CSO email")
    sadd.set_defaults(fn=cmd_system_add)

    sconf = sa.add_parser("configure", help="set a system's sources and scan settings")
    sconf.add_argument("--system", type=int, required=True)
    sconf.add_argument("--repo")
    sconf.add_argument("--docs", help="comma-separated doc folders")
    sconf.add_argument("--code-globs", help="comma-separated patterns of code files the scan reads")
    sconf.add_argument("--uses-ai", action=argparse.BooleanOptionalAction, default=None)
    sconf.add_argument("--confirm-no-pid", action="store_true",
                       help="confirm the sources contain no patient-identifiable data")
    sconf.add_argument("--intended-use")
    sconf.add_argument("--clinical-context")
    sconf.add_argument("--version", help="the system's version, e.g. the release a log is being issued for")
    sconf.add_argument("--as", dest="as_", required=True, help="CSO email")
    sconf.set_defaults(fn=cmd_system_configure)

    ss = sub.add_parser("seed-self", help="load OHL's own hazard log (unrated)")
    ss.add_argument("--repo", help="an Open Hazard Log checkout, so `ohl check` can watch the linked tests")
    ss.add_argument("--as", dest="as_", required=True, help="CSO email")
    ss.set_defaults(fn=cmd_seed_self)

    im = sub.add_parser("import", help="import a hazard log from CSV or XLSX")
    im.add_argument("file")
    im.add_argument("--system", type=int, required=True)
    im.add_argument("--as", dest="as_", required=True, help="CSO email")
    im.set_defaults(fn=cmd_import)

    sc = sub.add_parser("scan", help="run the agent scan; proposals go to the review queue")
    sc.add_argument("--system", type=int, required=True)
    sc.add_argument("--as", dest="as_", required=True, help="CSO or contributor email")
    sc.add_argument("--model", help="model ID (default: $OHL_SCAN_MODEL or claude-opus-5-5)")
    sc.add_argument("--max", type=int, help="maximum proposals (default 15)")
    sc.add_argument("--replay", help="use a saved model output (JSON) instead of calling the API")
    sc.set_defaults(fn=cmd_scan)

    ck = sub.add_parser("check", help="run the evidence and code-change checks (no AI); findings go to review")
    ck.add_argument("--system", type=int, required=True)
    ck.add_argument("--as", dest="as_", help="CSO or contributor email (default: the only CSO)")
    ck.set_defaults(fn=cmd_check)

    ex = sub.add_parser("export", help="export the hazard log")
    ex.add_argument("--system", type=int, required=True)
    ex.add_argument("--format", choices=["csv", "xlsx", "json", "deployer-csv", "deployer-json"], default="csv")
    ex.add_argument("--version", type=int, help="issued version number (default: working draft)")
    ex.add_argument("--out")
    ex.set_defaults(fn=cmd_export)

    au = sub.add_parser("audit", help="export the audit trail as JSON")
    au.add_argument("--system", type=int)
    au.add_argument("--out")
    au.set_defaults(fn=cmd_audit)

    v = sub.add_parser("verify", help="check the audit hash chain")
    v.add_argument("--file", help="verify an exported audit JSON file instead of the database")
    v.set_defaults(fn=cmd_verify)

    bk = sub.add_parser("backup", help="copy the database safely, while it's in use; the copy is verified")
    bk.add_argument("out", help="new file to write, e.g. ohl-2026-11-17.db")
    bk.set_defaults(fn=cmd_backup)

    iv = sub.add_parser("issue", help="issue (sign) a numbered version of a system's hazard log")
    iv.add_argument("--system", type=int, required=True)
    iv.add_argument("--note", help="what this version is for, e.g. 'Release 2.4'")
    iv.add_argument("--confirm", action="store_true", help="confirm you have reviewed the log and approve it")
    iv.add_argument("--as", dest="as_", required=True, help="CSO email")
    iv.set_defaults(fn=cmd_issue)

    sv = sub.add_parser("serve", help="run the web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.add_argument("--as", dest="as_", help="who changes are recorded as (default: the only CSO)")
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (S.Invalid, S.Forbidden, LookupError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
