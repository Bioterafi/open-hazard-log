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
import re

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from . import risk
from .db import now
from .service import format_causes

GROUPS = {
    "existing": [("HIT Design", ("design", "test")), ("User Training", ("training", "warning")),
                 ("Business Process", ("process",))],
    "additional": [("HIT Design", ("design", "test")), ("User Training", ("training", "warning")),
                   ("Business Process Change", ("process",))],
}
TAGGED = ("test", "warning")  # types that share a template group and need a tag

# Formula injection (OHL-H9): a hazard log is opened by other organisations, so no exported
# cell may run as a formula. XLSX cells are typed as text. Two XLSX limits remain: a cell
# holds at most 32,767 characters (longer text is cut, and says so) and spreadsheet
# programs turn CR LF line breaks into LF. CSV and JSON keep every character.
#
# CSV has no types. Every cell is quoted, and any stretch of text a spreadsheet could take
# as the start of a cell gets a leading apostrophe (OWASP) if it begins with = + - @, an
# apostrophe or a double quote. A stretch starts at the start of the cell or after ';', a
# tab or a line break: a spreadsheet that splits on those (Excel in many locales, LibreOffice
# by default) can lose track of the quotes and start a cell there. Commas aren't guarded:
# every program that splits on commas honours the quotes around a cell. The file says it is
# guarded (CSV_GUARD_MARK), and the importer then takes the apostrophes off again, so text
# comes back exactly as it went out. Older OHL exports had no guard and are left alone.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
CSV_GUARD_MARK = "# CSV: formula-guarded (OHL 0.1.1)"
XLSX_CELL_LIMIT = 32767
_GUARDED = ("=", "+", "-", "@", "'", '"')
_SPLIT = re.compile(r"([;\t\r\n])")  # separators kept, at the odd indexes of the split


def _needs_guard(stretch: str) -> bool:
    return stretch.lstrip().startswith(_GUARDED)


def csv_safe(value):
    if not isinstance(value, str):
        return value
    parts = _SPLIT.split(value)
    return "".join("'" + p if i % 2 == 0 and _needs_guard(p) else p for i, p in enumerate(parts))


def csv_unguard(text: str) -> str:
    """The inverse of csv_safe: removes exactly the apostrophes it added."""
    parts = _SPLIT.split(text)
    return "".join(p[1:] if i % 2 == 0 and p.startswith("'") and _needs_guard(p[1:]) else p
                   for i, p in enumerate(parts))


def _csv_rows(rows):
    return [[csv_safe(v) for v in row] for row in rows]


def _xlsx_text(text: str) -> str:
    """XLSX can't hold most control characters; one would make the whole export fail,
    for ever for an issued version. Each is shown as U+FFFD instead. Text over the cell
    limit is cut with a visible note rather than silently."""
    text = ILLEGAL_CHARACTERS_RE.sub("\ufffd", text)
    if len(text) > XLSX_CELL_LIMIT:
        note = " [… cut at the spreadsheet cell limit; the CSV and JSON exports hold the full text]"
        text = text[:XLSX_CELL_LIMIT - len(note)] + note
    return text


def _text_cell(ws, row: int, col: int, value):
    """Write a cell; strings are always stored as text, never as a formula."""
    cell = ws.cell(row, col)
    cell.value = _xlsx_text(value) if isinstance(value, str) else value
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


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


def _csv_meta(w, snap, issue, exported_by) -> None:
    """Header lines as one-cell rows starting with '#', quoted like any other cell, so a comma
    or line break in a system name can't start a new cell."""
    for k, v in header_meta(snap, issue, exported_by):
        w.writerow([csv_safe(f"# {k}: {v}")])
    w.writerow([CSV_GUARD_MARK])


def to_csv(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    _csv_meta(w, snap, issue, exported_by)
    cols = columns()
    w.writerow([flat(c) for c in cols])
    rows = [hazard_row(h, snap["controls"], snap["risk_scheme"]) for h in snap["hazards"]]
    w.writerows(_csv_rows([row.get(c, "") for c in cols] for row in rows))
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
        _text_cell(ws, i, 1, k).font = bold
        _text_cell(ws, i, 2, v)
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
            _text_cell(ws, r1, c1, parts[level])
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
            cell = _text_cell(ws, top + depth + i, c, row.get(col, ""))
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
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    _csv_meta(w, snap, issue, exported_by)
    w.writerow(DEPLOYER_COLS)
    w.writerows(_csv_rows([r[c] for c in DEPLOYER_COLS] for r in deployer_rows(snap)))
    return buf.getvalue()


def deployer_json(snap: dict, issue: dict | None = None, exported_by: dict | None = None) -> str:
    return json.dumps({"ohl_deployer_actions": 1, "meta": dict(header_meta(snap, issue, exported_by)),
                       "hazards": deployer_rows(snap)}, indent=2, ensure_ascii=False)
