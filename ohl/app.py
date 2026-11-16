"""Web app: server-rendered pages, plain HTML forms, no JS build."""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import audit, checks, dashboard, db, export, importer, proposals, risk, scan
from . import service as S

HERE = Path(__file__).resolve().parent
DB_PATH = db.default_path()

_boot = db.connect(DB_PATH)  # creates the schema and the session secret
SECRET = db.meta(_boot, "secret")
_boot.close()

app = FastAPI(title="Open Hazard Log", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site="strict", https_only=False,
                   session_cookie="ohl_session", max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(risk=risk, S=S)
templates.env.filters["causes"] = S.format_causes
templates.env.filters["evidence"] = S.format_evidence
templates.env.filters["day"] = lambda v: (v or "")[:10]


class NoUser(Exception):
    pass


@app.exception_handler(NoUser)
async def _no_user(request: Request, exc):
    return page(request, "message.html", status=503, heading="Who is using this?", text=str(exc))


@app.exception_handler(S.Forbidden)
async def _forbidden(request: Request, exc):
    return page(request, "message.html", status=403, heading="Not allowed", text=str(exc))


@app.exception_handler(LookupError)
async def _missing(request: Request, exc):
    return page(request, "message.html", status=404, heading="Not found", text=str(exc))


# ---------------------------------------------------------------- helpers

def conn():
    return db.connect(DB_PATH, init=False)


def current_user(request: Request, c) -> dict:
    """No login: the app runs on one computer as one named person, so every change is still
    attributed to them. Set by `ohl serve --as`, else the only CSO."""
    email = os.environ.get("OHL_USER", "").strip()
    if email:
        u = S.get_user(c, email)
        if not u:
            raise NoUser(f"The app was started as {email}, but there's no such user. Add one with: ohl user add")
        return u
    csos = [dict(r) for r in c.execute("SELECT email, name, role, registration FROM user WHERE role = 'cso'")]
    if len(csos) == 1:
        return csos[0]
    if not csos:
        raise NoUser("There's no Clinical Safety Officer yet. Add one with:\n"
                     "ohl user add you@example.org --name \"Your Name\" --role cso --registration \"GMC 1234567\"")
    raise NoUser("There's more than one CSO, so say who you are when starting the app:\n"
                 "ohl serve --as you@example.org")


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def form_of(request: Request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        raise S.Forbidden("This form has expired. Go back, reload the page and try again.")
    return form


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", None)
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **ctx}, status_code=status)


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def names(c) -> dict:
    return {r["email"]: r["name"] for r in c.execute("SELECT email, name FROM user")}


def describe(e: dict) -> str:
    if e["action"] in ("scan_run", "check_run"):
        a = json.loads(e["after"])
        n, hidden = len(a.get("proposals", [])), a.get("hidden_no_verified_quote", 0)
        what = "scan found" if e["action"] == "scan_run" else "found"
        return (f"{what} {n} proposal{'s' if n != 1 else ''}"
                + (f", {hidden} hidden (no verified quote)" if hidden else "")
                + (" (replayed saved output)" if str(a.get("model_used", "")).startswith("replay:") else ""))
    if e["action"] == "log_imported":
        a = json.loads(e["after"])
        return f"imported {len(a['hazards'])} hazards and {len(a['controls'])} controls from {a['file']}"
    a = e["action"].replace("_", " ")
    ref = e["object_ref"]
    return f"{a} {ref}".strip() if not ref.startswith("system:") else a


def summarise(c, evs: list[dict]) -> list[dict]:
    """Newest first, with each import shown as one line instead of one per row."""
    who = names(c)
    def name(actor: str) -> str:
        if actor.startswith("agent:"):
            return "Agent"
        if actor.startswith("check:"):
            return "Checks"
        return who.get(actor, actor)
    out = [dict(e, who=name(e["actor"]), what=describe(e)) for e in evs
           if not e["note"].startswith("imported from ")]
    return list(reversed(out))


def log_context(c, sid: int) -> dict:
    s = S.get_system(c, sid)
    issues = S.list_issues(c, sid)
    v = audit.verify(c)
    last_check = checks.last_run(c, sid)
    age = None
    if last_check:
        from datetime import date
        age = (date.fromisoformat(db.today()) - date.fromisoformat(last_check["at"][:10])).days
    return {"system": s, "scheme": s["risk_scheme"], "last_issue": issues[0] if issues else None,
            "pending": S.pending_proposals(c, sid), "verdict": v,
            "changes": len(S.changes_since_issue(c, sid)), "last_scan": proposals.last_run(c, sid),
            "last_check": last_check, "check_age": age,
            "check_stale": age is not None and age > checks.stale_days(s)}


# ---------------------------------------------------------------- systems

@app.get("/")
def home(request: Request):
    c = conn()
    current_user(request, c)
    systems = S.list_systems(c)
    return go(f"/s/{systems[0]['id']}" if len(systems) == 1 else "/systems")


@app.get("/systems", response_class=HTMLResponse)
def systems(request: Request):
    c = conn()
    u = current_user(request, c)
    rows = [dict(s, hazards=len(S.list_hazards(c, s["id"])), issues=S.list_issues(c, s["id"]))
            for s in S.list_systems(c)]
    return page(request, "systems.html", user=u, systems=rows, error="", values={})


@app.post("/systems")
async def systems_create(request: Request):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    values = {k: str(form.get(k, "")) for k in ("name", "version", "intended_use", "clinical_context")}
    try:
        sid = S.create_system(c, u, **values)
    except S.Invalid as e:
        return page(request, "systems.html", status=400, user=u, systems=S.list_systems(c), error=str(e),
                    values=values)
    return go(f"/s/{sid}")


@app.get("/s/{sid}/settings", response_class=HTMLResponse)
def settings(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    return page(request, "settings.html", user=u, **log_context(c, sid), error="", saved=False)


@app.post("/s/{sid}/settings")
async def settings_save(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    s = S.get_system(c, sid)
    sources = dict(s["sources"])
    sources["repo"] = str(form.get("repo", "")).strip()
    sources["docs"] = [d.strip() for d in str(form.get("docs", "")).split(",") if d.strip()]
    sources["code_globs"] = [g.strip() for g in str(form.get("code_globs", "")).split(",") if g.strip()]
    sources["uses_ai"] = form.get("uses_ai") == "on"
    try:
        sources["max_proposals"] = max(1, min(50, int(str(form.get("max_proposals") or 15))))
    except ValueError:
        sources["max_proposals"] = 15
    try:
        S.update_system(c, u, sid, {"name": form.get("name", ""), "version": form.get("version", ""),
                                    "intended_use": form.get("intended_use", ""),
                                    "clinical_context": form.get("clinical_context", ""),
                                    "no_pid_confirmed": form.get("no_pid_confirmed") == "on",
                                    "sources": sources})
    except S.Invalid as e:
        return page(request, "settings.html", status=400, user=u, **log_context(c, sid), error=str(e), saved=False)
    return page(request, "settings.html", user=u, **log_context(c, sid), error="", saved=True)


# ---------------------------------------------------------------- log

@app.get("/s/{sid}", response_class=HTMLResponse)
def log(request: Request, sid: int, f: str = "", stage: str = "", sev: str = "", lik: str = ""):
    c = conn()
    u = current_user(request, c)
    ctx = log_context(c, sid)
    hazards = S.list_hazards(c, sid)
    controls = S.list_controls(c, sid)
    dashboard.annotate(hazards, controls, db.today(), checks.observations(c, sid))
    scheme = ctx["scheme"]
    st = dashboard.stats(scheme, hazards, controls, pending=ctx["pending"], changes=ctx["changes"])
    shown, filter_label = dashboard.apply_filter(hazards, f, stage, sev, lik)
    activity = summarise(c, audit.events(c, sid))[:8]
    return page(request, "dashboard.html", user=u, hazards=shown, all_count=len(hazards), st=st,
                matrices=[dashboard.matrix(scheme, hazards, "initial"), dashboard.matrix(scheme, hazards, "residual")],
                filters=dashboard.FILTERS, f=f, filter_label=filter_label, activity=activity, **ctx)


# ---------------------------------------------------------------- scan (runs in a background thread)

SCAN_CLIENT = None          # tests swap in a fake model client
SCANS: dict[int, dict] = {}  # system id -> state of the latest web-started scan
_scan_lock = threading.Lock()


def _scan_worker(sid: int, user: dict) -> None:
    c = db.connect(DB_PATH, init=False)
    try:
        r = scan.run(c, user, sid, client=SCAN_CLIENT)
        state = {"state": "done", "message": f"Scan finished: {len(r.shown)} proposal{'s' if len(r.shown) != 1 else ''}"
                                               f" for review, {r.hidden_no_quote} hidden (no verified quote)."}
    except (S.Invalid, S.Forbidden, scan.ScanError) as e:
        state = {"state": "error", "message": f"Scan failed, nothing stored: {e}"}
    except Exception as e:  # API errors, network: report, never crash the app
        state = {"state": "error", "message": f"Scan failed, nothing stored: {type(e).__name__}: {e}"}
    finally:
        c.close()
    SCANS[sid] = {**SCANS.get(sid, {}), **state}


@app.post("/s/{sid}/scan")
async def scan_start(request: Request, sid: int):
    await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        scan.preflight(c, u, sid)
    except S.Invalid as e:
        SCANS[sid] = {"state": "error", "message": str(e)}
        return go(f"/s/{sid}/review")
    with _scan_lock:
        if SCANS.get(sid, {}).get("state") == "running":
            return go(f"/s/{sid}/review")
        t = threading.Thread(target=_scan_worker, args=(sid, u), daemon=True)
        SCANS[sid] = {"state": "running", "started": db.now(), "by": u["name"], "thread": t}
    t.start()
    return go(f"/s/{sid}/review")


# ---------------------------------------------------------------- checks (fast, so they run in the request)

CHECKS: dict[int, dict] = {}  # system id -> message from the last web-started check, shown once


@app.post("/s/{sid}/check")
async def check_start(request: Request, sid: int):
    await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        r = checks.run(c, u, sid)
    except S.Forbidden:
        raise
    except Exception as e:
        CHECKS[sid] = {"state": "error", "message": f"Checks failed, nothing stored: {type(e).__name__}: {e}"}
        return go(f"/s/{sid}/review")
    n = len(r.proposals)
    msg = (f"Checks done: {r.evidence_checked} evidence file{'s' if r.evidence_checked != 1 else ''} checked"
           + (f" ({r.baselined} seen for the first time)" if r.baselined else "")
           + f", {n} new proposal{'s' if n != 1 else ''}.")
    if r.notes:
        msg += " " + " ".join(n_.capitalize() + "." for n_ in r.notes)
    CHECKS[sid] = {"state": "done", "message": msg}
    return go(f"/s/{sid}/review")


# ---------------------------------------------------------------- review

def review_context(c, sid: int) -> dict:
    queue = proposals.pending(c, sid)
    return {**log_context(c, sid), "queue": queue, "recent": proposals.decided(c, sid),
            "scan_state": {k: v for k, v in SCANS.get(sid, {}).items() if k != "thread"},
            "check_state": CHECKS.pop(sid, None),
            "bulkable": [q for q in queue if q["kind"] == "evidence_problem" and q["payload"].get("state") == "changed"],
            "reasons": proposals.REJECT_REASONS, "who": names(c)}


@app.get("/s/{sid}/review", response_class=HTMLResponse)
def review(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    queue = proposals.pending(c, sid)
    if queue and SCANS.get(sid, {}).get("state") != "running":
        return go(f"/s/{sid}/review/{queue[0]['id']}")  # the run message waits for the page that shows it
    return page(request, "review.html", user=u, p=None, **review_context(c, sid))


@app.get("/s/{sid}/review/{pid}", response_class=HTMLResponse)
def review_item(request: Request, sid: int, pid: int):
    c = conn()
    u = current_user(request, c)
    p = proposals.get(c, sid, pid)
    proposals.mark_viewed(c, u, p)
    target = S.get_hazard(c, sid, p["target"]) if p["target"] and p["kind"] != "evidence_problem" else None
    return page(request, "review.html", user=u, p=p, target=target, error=request.query_params.get("error", ""),
                **review_context(c, sid))


def _next(c, sid: int) -> RedirectResponse:
    q = proposals.pending(c, sid)
    return go(f"/s/{sid}/review/{q[0]['id']}" if q else f"/s/{sid}/review")


def _back_with(sid: int, pid: int, msg: str) -> RedirectResponse:
    return go(f"/s/{sid}/review/{pid}?error={quote(msg)}")


@app.post("/s/{sid}/review/{pid}/rate")
async def review_rate(request: Request, sid: int, pid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        proposals.rate(c, u, sid, pid, str(form.get("severity", "")), str(form.get("likelihood", "")),
                       str(form.get("rationale", "")))
    except S.Invalid as e:
        return _back_with(sid, pid, str(e))
    return go(f"/s/{sid}/review/{pid}")


@app.post("/s/{sid}/review/{pid}/approve")
async def review_approve(request: Request, sid: int, pid: int):
    await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        proposals.approve(c, u, sid, pid)
    except S.Invalid as e:
        return _back_with(sid, pid, str(e))
    return _next(c, sid)


@app.post("/s/{sid}/review/{pid}/invalidate")
async def review_invalidate(request: Request, sid: int, pid: int):
    await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        proposals.invalidate_control(c, u, sid, pid)
    except S.Invalid as e:
        return _back_with(sid, pid, str(e))
    return _next(c, sid)


@app.post("/s/{sid}/review/bulk")
async def review_bulk(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    try:
        n = proposals.bulk_reconfirm(c, u, sid, ids, form.get("confirm") == "on")
    except S.Invalid as e:
        CHECKS[sid] = {"state": "error", "message": str(e)}
        return go(f"/s/{sid}/review")
    CHECKS[sid] = {"state": "done", "message": f"Re-confirmed {n} evidence item{'s' if n != 1 else ''}."}
    return _next(c, sid)


@app.post("/s/{sid}/review/{pid}/reject")
async def review_reject(request: Request, sid: int, pid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    try:
        proposals.reject(c, u, sid, pid, str(form.get("reason", "")), str(form.get("detail", "")))
    except S.Invalid as e:
        return _back_with(sid, pid, str(e))
    return _next(c, sid)


@app.get("/s/{sid}/review/{pid}/edit", response_class=HTMLResponse)
def review_edit(request: Request, sid: int, pid: int):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    p = proposals.get(c, sid, pid)
    if proposals.needs_rating(p) and not p["cso_rating"]:
        return _back_with(sid, pid, "Enter your own rating before editing.")
    target = S.get_hazard(c, sid, p["target"]) if p["target"] else None
    return page(request, "review_edit.html", user=u, p=p, f=proposals.edit_defaults(p), target=target, error="",
                **log_context(c, sid))


@app.post("/s/{sid}/review/{pid}/edit")
async def review_edit_save(request: Request, sid: int, pid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    p = proposals.get(c, sid, pid)
    data = {k: str(form.get(k, "")) for k in ("title", "description", "effects", "harm", "causes", "controls",
                                              "initial_severity", "initial_likelihood", "initial_justification")}
    try:
        proposals.edit_approve(c, u, sid, pid, data)
    except S.Invalid as e:
        target = S.get_hazard(c, sid, p["target"]) if p["target"] else None
        return page(request, "review_edit.html", status=400, user=u, p=p, f=data, target=target, error=str(e),
                    **log_context(c, sid))
    return _next(c, sid)


# ---------------------------------------------------------------- hazards

HAZARD_FORM = ("title", "description", "effects", "harm", "causes", "initial_severity", "initial_likelihood",
               "initial_justification", "residual_severity", "residual_likelihood", "residual_justification",
               "status", "deployer_action", "action", "owner", "last_reviewed", "review_due")


@app.get("/s/{sid}/h/new", response_class=HTMLResponse)
def hazard_new(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    return page(request, "hazard_form.html", user=u, **log_context(c, sid), h={"owner": u["name"], "status": "open"},
                causes_text="", error="", is_new=True)


@app.post("/s/{sid}/h/new")
async def hazard_create(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    data = {k: str(form.get(k, "")) for k in HAZARD_FORM}
    try:
        ref = S.create_hazard(c, u, sid, data, note=str(form.get("note", "")))
    except S.Invalid as e:
        return page(request, "hazard_form.html", status=400, user=u, **log_context(c, sid), h=data,
                    causes_text=data["causes"], error=str(e), is_new=True)
    return go(f"/s/{sid}/h/{quote(ref)}")


@app.get("/s/{sid}/h/{ref}", response_class=HTMLResponse)
def hazard(request: Request, sid: int, ref: str):
    c = conn()
    u = current_user(request, c)
    h = S.get_hazard(c, sid, ref)
    controls = S.list_controls(c, sid, ref)
    obs = checks.observations(c, sid)
    for ctl in controls:
        for e in ctl["evidence"]:
            e["obs"] = obs.get((ctl["ref"], e["path_or_url"]))
    refs = {ref, *(x["ref"] for x in controls)}
    who = names(c)
    history = [dict(e, who=who.get(e["actor"], e["actor"]), what=describe(e))
               for e in audit.events(c, sid) if e["object_ref"] in refs]
    return page(request, "hazard.html", user=u, **log_context(c, sid), h=h, controls=controls,
                history=list(reversed(history)))


@app.get("/s/{sid}/h/{ref}/edit", response_class=HTMLResponse)
def hazard_edit(request: Request, sid: int, ref: str):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    h = S.get_hazard(c, sid, ref)
    return page(request, "hazard_form.html", user=u, **log_context(c, sid), h=h,
                causes_text=S.format_causes(h["causes"]), error="", is_new=False)


@app.post("/s/{sid}/h/{ref}/edit")
async def hazard_update(request: Request, sid: int, ref: str):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    data = {k: str(form.get(k, "")) for k in HAZARD_FORM}
    try:
        S.update_hazard(c, u, sid, ref, data, note=str(form.get("note", "")))
    except S.Invalid as e:
        return page(request, "hazard_form.html", status=400, user=u, **log_context(c, sid), h=dict(data, ref=ref),
                    causes_text=data["causes"], error=str(e), is_new=False)
    return go(f"/s/{sid}/h/{quote(ref)}")


# ---------------------------------------------------------------- controls

CONTROL_FORM = ("hazard_refs", "description", "stage", "type", "status", "evidence")


@app.get("/s/{sid}/c/new", response_class=HTMLResponse)
def control_new(request: Request, sid: int, hazard: str = ""):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    ctl = {"hazard_refs": hazard, "stage": "additional", "type": "design", "status": "proposed", "evidence": ""}
    return page(request, "control_form.html", user=u, **log_context(c, sid), ctl=ctl, error="", is_new=True,
                back=hazard)


@app.post("/s/{sid}/c/new")
async def control_create(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    data = {k: str(form.get(k, "")) for k in CONTROL_FORM}
    try:
        S.create_control(c, u, sid, data, note=str(form.get("note", "")))
    except (S.Invalid, LookupError) as e:
        return page(request, "control_form.html", status=400, user=u, **log_context(c, sid), ctl=data,
                    error=str(e), is_new=True, back=data["hazard_refs"].split(",")[0].strip())
    first = data["hazard_refs"].split(",")[0].strip()
    return go(f"/s/{sid}/h/{quote(first)}")


@app.get("/s/{sid}/c/{ref}/edit", response_class=HTMLResponse)
def control_edit(request: Request, sid: int, ref: str, back: str = ""):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    ctl = S.get_control(c, sid, ref)
    shown = dict(ctl, hazard_refs=", ".join(ctl["hazard_refs"]), evidence=S.format_evidence(ctl["evidence"]))
    return page(request, "control_form.html", user=u, **log_context(c, sid), ctl=shown, error="", is_new=False,
                back=back or ctl["hazard_refs"][0])


@app.post("/s/{sid}/c/{ref}/edit")
async def control_update(request: Request, sid: int, ref: str):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    data = {k: str(form.get(k, "")) for k in CONTROL_FORM}
    back = str(form.get("back", "")) or data["hazard_refs"].split(",")[0].strip()
    try:
        S.update_control(c, u, sid, ref, data, note=str(form.get("note", "")))
    except (S.Invalid, LookupError) as e:
        return page(request, "control_form.html", status=400, user=u, **log_context(c, sid),
                    ctl=dict(data, ref=ref), error=str(e), is_new=False, back=back)
    return go(f"/s/{sid}/h/{quote(back)}")


# ---------------------------------------------------------------- issue version (DCB0129 3.3.2)

@app.get("/s/{sid}/issue", response_class=HTMLResponse)
def issue_form(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    hazards = S.list_hazards(c, sid)
    unrated = [h["ref"] for h in hazards if not h["residual_risk"] and h["status"] != "closed"]
    return page(request, "issue.html", user=u, **log_context(c, sid), issues=S.list_issues(c, sid),
                change_list=summarise(c, S.changes_since_issue(c, sid))[:50], n_hazards=len(hazards),
                unrated=unrated, error="")


@app.post("/s/{sid}/issue")
async def issue_create(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    if form.get("confirm") != "on":
        return go(f"/s/{sid}/issue")
    try:
        S.issue_version(c, u, sid, str(form.get("note", "")))
    except S.Invalid as e:
        return page(request, "message.html", status=400, user=u, heading="Not issued", text=str(e))
    return go(f"/s/{sid}/issue")


# ---------------------------------------------------------------- import / export

@app.get("/s/{sid}/import", response_class=HTMLResponse)
def import_form(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    S.require_cso(u)
    return page(request, "import.html", user=u, **log_context(c, sid), result=None, error="")


@app.post("/s/{sid}/import")
async def import_upload(request: Request, sid: int):
    form = await form_of(request)
    c = conn()
    u = current_user(request, c)
    up = form.get("file")
    if not up or not getattr(up, "filename", ""):
        return page(request, "import.html", status=400, user=u, **log_context(c, sid), result=None,
                    error="Choose a file first.")
    data = await up.read()
    try:
        result = importer.import_bytes(c, u, sid, data, up.filename)
    except S.Invalid as e:
        return page(request, "import.html", status=400, user=u, **log_context(c, sid), result=None, error=str(e))
    return page(request, "import.html", user=u, **log_context(c, sid), result=result, error="")


EXPORTS = {
    "csv": (export.to_csv, "text/csv", "csv"),
    "xlsx": (export.to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "json": (export.to_json, "application/json", "json"),
    "deployer-csv": (export.deployer_csv, "text/csv", "csv"),
    "deployer-json": (export.deployer_json, "application/json", "json"),
}


@app.get("/s/{sid}/export")
def export_file(request: Request, sid: int, format: str = "xlsx", version: int | None = None):
    c = conn()
    u = current_user(request, c)
    if format not in EXPORTS:
        raise LookupError(f"unknown export format {format}")
    issue = S.get_issue(c, sid, version) if version else None
    snap = issue["snapshot"] if issue else S.snapshot(c, sid)
    fn, mime, ext = EXPORTS[format]
    body = fn(snap, issue, u)
    slug = "".join(ch if ch.isalnum() else "-" for ch in snap["system"]["name"].lower()).strip("-")
    kind = "deployer-actions" if format.startswith("deployer") else "hazard-log"
    name = f"{slug}-{kind}-{'v' + str(version) if version else 'draft'}.{ext}"
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/s/{sid}/audit.json")
def audit_json(request: Request, sid: int):
    c = conn()
    current_user(request, c)
    S.get_system(c, sid)
    body = json.dumps(audit.export(c, sid), indent=2, ensure_ascii=False)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ohl-audit-trail.json"'})
