"""Web app: server-rendered pages, plain HTML forms, no JS build."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import audit, db, export, importer, risk
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


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login(request: Request, exc):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


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
    email = request.session.get("user")
    u = S.get_user(c, email) if email else None
    if not u:
        raise LoginRequired()
    return u


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
    if e["action"] == "log_imported":
        a = json.loads(e["after"])
        return f"imported {len(a['hazards'])} hazards and {len(a['controls'])} controls from {a['file']}"
    a = e["action"].replace("_", " ")
    ref = e["object_ref"]
    return f"{a} {ref}".strip() if not ref.startswith("system:") else a


def summarise(c, evs: list[dict]) -> list[dict]:
    """Newest first, with each import shown as one line instead of one per row."""
    who = names(c)
    out = [dict(e, who=who.get(e["actor"], e["actor"]), what=describe(e)) for e in evs
           if not e["note"].startswith("imported from ")]
    return list(reversed(out))


def log_context(c, sid: int) -> dict:
    s = S.get_system(c, sid)
    issues = S.list_issues(c, sid)
    v = audit.verify(c)
    return {"system": s, "scheme": s["risk_scheme"], "last_issue": issues[0] if issues else None,
            "pending": S.pending_proposals(c, sid), "verdict": v,
            "changes": len(S.changes_since_issue(c, sid))}


# ---------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", next=next, error="")


@app.post("/login")
async def login(request: Request):
    form = await form_of(request)
    c = conn()
    u = S.authenticate(c, str(form.get("email", "")), str(form.get("password", "")))
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not u:
        return page(request, "login.html", status=401, next=nxt, error="Email or password not recognised.")
    request.session.clear()
    request.session["user"] = u["email"]
    return go(nxt)


@app.post("/logout")
async def logout(request: Request):
    await form_of(request)
    request.session.clear()
    return go("/login")


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
def log(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    hazards = S.list_hazards(c, sid)
    controls = S.list_controls(c, sid)
    for h in hazards:
        mine = [x for x in controls if h["ref"] in x["hazard_refs"]]
        h["n_controls"] = len(mine)
        h["n_verified"] = sum(x["status"] == "verified" for x in mine)
        h["n_invalid"] = sum(x["status"] == "invalid" for x in mine)
        h["overdue"] = bool(h["review_due"]) and h["review_due"] < db.today() and h["status"] != "closed"
    return page(request, "log.html", user=u, hazards=hazards, **log_context(c, sid))


@app.get("/s/{sid}/review", response_class=HTMLResponse)
def review(request: Request, sid: int):
    c = conn()
    u = current_user(request, c)
    return page(request, "review.html", user=u, **log_context(c, sid))


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
