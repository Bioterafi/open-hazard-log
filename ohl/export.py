"""Exports in the NHS Digital hazard log template v3.6 column layout, plus OHL extras.

Template control groups vs OHL control types:
  HIT Design                       <- design, and test (prefixed "[test]")
  User Training                    <- training, and warning (prefixed "[warning]")
  Business Process (Change)        <- process
Each control line is "C-001: description"; each evidence line is "C-001: label | path".
"""

from __future__ import annotations

import csv
import io
import json

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from . import risk
from .service import format_causes
from .db import now

GROUPS = {
    "existing": [("HIT Design", ("design", "test")), ("User Training", ("training", "warning")),
                 ("Business Process", ("process",))],
    "additional": [("HIT Design", ("design", "test")), ("User Training", ("training", "warning")),
                   ("Business Process Change", ("process",))],
}
TAGGED = ("test", "warning")  # types that share a template group and need a tag


def columns() -> list[tuple[str, ...]]:
    cols = [("No.",), ("Date Added",)]
    cols += [("Hazard Description", s) for s in ("Effect", "Hazard", "Harm", "Possible Causes")]
    for g, _ in GROUPS["existing"]:
        cols += [("Existing Controls", g, "Description"), ("Existing Controls", g, "Evidence")]
    cols += [("Initial Risk Assessment", s) for s in ("Severity", "Likelihood", "Risk", "Justification")]
    for g, _ in GROUPS["additional"]:
        cols += [("Additional Controls", g, "Description"), ("Additional Controls", g, "Evidence")]
    cols += [("Residual Risk Assessment", s) for s in ("Severity", "Likelihood", "Risk", "Justification")]
    cols += [("Owner",), ("Status",)]
    cols += [("OHL", s) for s in ("Description", "Deployer action", "Action", "Control status",
                                  "Last reviewed", "Review due", "Version")]
    return cols


def flat(col: tuple[str, ...]) -> str:
    return " / ".join(col)


def _control_line(c: dict) -> str:
    tag = f"[{c['type']}] " if c["type"] in TAGGED else ""
    return f"{c['ref']}: {tag}{c['description']}"


def _evidence_lines(c: dict) -> list[str]:
    return [f"{c['ref']}: {e['label']}" + (f" | {e['path_or_url']}" if e["path_or_url"] else "")
            for e in c["evidence"]]


def hazard_row(h: dict, controls: list[dict], scheme: dict) -> dict:
    mine = [c for c in controls if h["ref"] in c["hazard_refs"]]
    row = {
        ("No.",): h["ref"],
        ("Date Added",): h["created_at"],
        ("Hazard Description", "Effect"): h["effects"],
        ("Hazard Description", "Hazard"): h["title"],
        ("Hazard Description", "Harm"): h["harm"],
        ("Hazard Description", "Possible Causes"): format_causes(h["causes"]),
        ("Owner",): h["owner"],
        ("Status",): h["status"].capitalize(),
        ("OHL", "Description"): h["description"],
        ("OHL", "Deployer action"): h["deployer_action"],
        ("OHL", "Action"): h["action"],
        ("OHL", "Control status"): "\n".join(f"{c['ref']}: {c['status']}" for c in mine),
        ("OHL", "Last reviewed"): h["last_reviewed"],
        ("OHL", "Review due"): h["review_due"],
        ("OHL", "Version"): h["version"],
    }
    for stage, group_name in (("existing", "Existing Controls"), ("additional", "Additional Controls")):
        for g, types in GROUPS[stage]:
            cs = [c for c in mine if c["stage"] == stage and c["type"] in types]
            row[(group_name, g, "Description")] = "\n".join(_control_line(c) for c in cs)
            row[(group_name, g, "Evidence")] = "\n".join(line for c in cs for line in _evidence_lines(c))
    for stage, group_name in (("initial", "Initial Risk Assessment"), ("residual", "Residual Risk Assessment")):
        row[(group_name, "Severity")] = risk.label(scheme, h[f"{stage}_severity"])
        row[(group_name, "Likelihood")] = risk.label(scheme, h[f"{stage}_likelihood"])
        row[(group_name, "Risk")] = h[f"{stage}_risk"] or ""
        row[(group_name, "Justification")] = h[f"{stage}_justification"]
    return row


def header_meta(snap: dict, issue: dict | None, exported_by: dict | None) -> list[tuple[str, str]]:
    s = snap["system"]
    if issue:
        signer = issue["issued_by"]
        status = (f"ISSUED v{issue['number']} on {issue['issued_at'][:10]} by {signer['name']}"
                  f" ({signer['registration']}), Clinical Safety Officer")
    else:
        status = "DRAFT, not issued. Not approved by a Clinical Safety Officer."
    meta = [
        ("Hazard log", f"{s['name']} {s['version']}".strip()),
        ("Status", status),
        ("Risk scheme", snap["risk_scheme"].get("name", "")),
        ("Exported", f"{now()}" + (f" by {exported_by['name']}" if exported_by else "")),
        ("Template", "NHS Digital clinical safety hazard log template v3.6 columns, plus OHL columns"),
    ]
    if issue and issue.get("repo_commit"):
        meta.append(("Repo commit", issue["repo_commit"]))
    if issue:
        meta.append(("Audit chain head", issue["chain_head"]))
    return meta


def to_csv(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    buf = io.StringIO()
    for k, v in header_meta(snap, issue, exported_by):
        buf.write(f"# {k}: {v}\n")
    cols = columns()
    w = csv.writer(buf)
    w.writerow([flat(c) for c in cols])
    for h in snap["hazards"]:
        row = hazard_row(h, snap["controls"], snap["risk_scheme"])
        w.writerow([row.get(c, "") for c in cols])
    return buf.getvalue()


def to_xlsx(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Hazard Log"
    bold, thin = Font(bold=True), Side(style="thin")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    centre = Alignment(wrap_text=True, vertical="center", horizontal="center")

    meta = header_meta(snap, issue, exported_by)
    for i, (k, v) in enumerate(meta, start=1):
        ws.cell(i, 1, k).font = bold
        ws.cell(i, 2, v)
    top = len(meta) + 2  # first header row
    cols, depth = columns(), 3

    # Header: write each level, merging runs of the same group across and short paths down.
    for level in range(depth):
        c = 0
        while c < len(cols):
            parts = cols[c]
            if len(parts) <= level:
                c += 1
                continue
            run = 1
            while c + run < len(cols) and cols[c + run][: level + 1] == parts[: level + 1]:
                run += 1
            r1, c1 = top + level, c + 1
            ws.cell(r1, c1, parts[level])
            last_level = len(parts) == level + 1
            r2 = top + depth - 1 if last_level else r1
            if run > 1 or r2 > r1:
                ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c1 + run - 1)
            c += run
    for r in range(top, top + depth):
        for c in range(1, len(cols) + 1):
            cell = ws.cell(r, c)
            cell.font, cell.alignment, cell.border = bold, centre, box

    for i, h in enumerate(snap["hazards"]):
        row = hazard_row(h, snap["controls"], snap["risk_scheme"])
        for c, col in enumerate(cols, start=1):
            cell = ws.cell(top + depth + i, c, row.get(col, ""))
            cell.alignment, cell.border = wrap, box

    widths = {"No.": 10, "Date Added": 12, "Severity": 13, "Likelihood": 12, "Risk": 6, "Owner": 16,
              "Status": 12, "Version": 8}
    for c, col in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(c)].width = widths.get(col[-1], 32)
    ws.freeze_panes = ws.cell(top + depth, 3)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_json(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    return json.dumps({"ohl_export": 1, "meta": dict(header_meta(snap, issue, exported_by)),
                       "issue": {k: v for k, v in issue.items() if k != "snapshot"} if issue else None,
                       **snap}, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- deployer actions (DCB0160 input)

DEPLOYER_COLS = ["No.", "Hazard", "Harm", "Status", "Residual severity", "Residual likelihood", "Residual risk",
                 "Deployer action", "Controls"]


def deployer_rows(snap: dict) -> list[dict]:
    scheme, out = snap["risk_scheme"], []
    for h in snap["hazards"]:
        if not (h["deployer_action"] or h["status"] == "transferred"):
            continue
        mine = [c for c in snap["controls"] if h["ref"] in c["hazard_refs"]]
        out.append({"No.": h["ref"], "Hazard": h["title"], "Harm": h["harm"], "Status": h["status"].capitalize(),
                    "Residual severity": risk.label(scheme, h["residual_severity"]),
                    "Residual likelihood": risk.label(scheme, h["residual_likelihood"]),
                    "Residual risk": h["residual_risk"] or "", "Deployer action": h["deployer_action"],
                    "Controls": "\n".join(f"{c['ref']}: {c['description']} ({c['status']})" for c in mine)})
    return out


def deployer_csv(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    buf = io.StringIO()
    for k, v in header_meta(snap, issue, exported_by):
        buf.write(f"# {k}: {v}\n")
    w = csv.DictWriter(buf, fieldnames=DEPLOYER_COLS)
    w.writeheader()
    w.writerows(deployer_rows(snap))
    return buf.getvalue()


def deployer_json(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    return json.dumps({"ohl_deployer_actions": 1, "meta": dict(header_meta(snap, issue, exported_by)),
                       "hazards": deployer_rows(snap)}, indent=2, ensure_ascii=False)
