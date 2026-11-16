"""Import an existing hazard log from CSV or XLSX.

Reads OHL's own exports and spreadsheets laid out like the NHS Digital hazard log
template (grouped, merged header rows). Columns are matched by their header text,
so column order doesn't matter and unknown columns are reported, not guessed.
All-or-nothing: any error and nothing is written.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from . import audit, risk
from . import service as S
from .db import tx

REF_PREFIX = re.compile(r"^([A-Za-z][\w-]*\d+):\s*(.*)$")
TAG = re.compile(r"^\[(test|warning)\]\s*", re.I)
GROUP_TYPES = {"design": ("design", "test"), "training": ("training", "warning"), "process": ("process", None)}


@dataclass
class Result:
    hazards: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- reading files

def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines(keepends=True) if not ln.startswith("#")]
    rows = list(csv.reader(io.StringIO("".join(lines))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise S.Invalid("the CSV file has no header row")
    return [c.strip() for c in rows[0]], [[c.strip() for c in r] for r in rows[1:]]


def _is_ref_header(text: str) -> bool:
    return _norm(text) in ("no", "hazard number", "hazard no", "id", "ref")


def read_xlsx(data: bytes) -> tuple[list[str], list[list[str]]]:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb["Hazard Log"] if "Hazard Log" in wb.sheetnames else wb.worksheets[0]
    merged = {}
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged[(r, c)] = (rng.min_row, rng.min_col, rng.max_row)

    def value(r, c):
        r0, c0, _ = merged.get((r, c), (r, c, r))
        return _cell_text(ws.cell(r0, c0).value)

    # Find the header row: the first row (within 40) with a "No." style cell.
    top = ref_col = None
    for r in range(1, min(ws.max_row, 40) + 1):
        for c in range(1, ws.max_column + 1):
            if _is_ref_header(_cell_text(ws.cell(r, c).value)):
                top, ref_col = r, c
                break
        if top:
            break
    if not top:
        raise S.Invalid("couldn't find the header row (a column headed 'No.' or 'Hazard number')")

    # Header depth: rows covered by the merged "No." cell, else rows until data starts (max 4).
    if (top, ref_col) in merged:
        bottom = merged[(top, ref_col)][2]
    else:
        bottom = top
        while bottom - top < 3 and not _cell_text(ws.cell(bottom + 1, ref_col).value) and any(
                _cell_text(ws.cell(bottom + 1, c).value) for c in range(1, ws.max_column + 1)):
            bottom += 1

    headers = []
    for c in range(1, ws.max_column + 1):
        parts = []
        for r in range(top, bottom + 1):
            v = value(r, c)
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        headers.append(" / ".join(parts))

    rows = []
    for r in range(bottom + 1, ws.max_row + 1):
        row = [_cell_text(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)]
        if any(row):
            rows.append(row)
    return headers, rows


# ---------------------------------------------------------------- matching headers

def _norm(s: str) -> str:
    s = s.lower().replace("\n", " ")
    s = re.sub(r"[^a-z0-9/ ]+", " ", s)
    return " / ".join(" ".join(p.split()) for p in s.split("/")).strip(" /")


def classify(header: str):
    n = _norm(header)
    if not n:
        return None
    parts = [p.strip() for p in n.split("/")]
    first, last = parts[0], parts[-1]
    if ("existing" in first or "additional" in first) and "control" in first:
        stage = "existing" if "existing" in first else "additional"
        if "design" in n or "hit" in n or re.search(r"\btest", n):
            grp = "design"
        elif "training" in n:
            grp = "training"
        elif "process" in n:
            grp = "process"
        else:
            return None
        return ("control", stage, grp, "evidence" if "evidence" in last else "description")
    for stage in ("initial", "residual"):
        if stage in first and len(parts) > 1:
            for f in ("severity", "likelihood", "justification", "risk"):
                if f in last:
                    return ("rating", stage, f)
    if _is_ref_header(first) and len(parts) == 1:
        return ("ref",)
    table = {
        "date added": "created_at", "effect": "effects", "potential clinical impact": "effects", "harm": "harm",
        "possible causes": "causes", "causes": "causes", "hazard": "title", "hazard name": "title",
        "deployer action": "deployer_action", "control status": "control_status", "action": "action",
        "summary": "action", "actions summary": "action", "owner": "owner", "status": "status",
        "hazard status": "status", "last reviewed": "last_reviewed", "review due": "review_due", "version": "ignore",
    }
    if first == "ohl" and last == "description" or n == "hazard description":
        return ("description",)
    if n in table:
        return (table[n],)
    if last in table and first in ("hazard description", "ohl", "actions"):
        return (table[last],)
    return None


# ---------------------------------------------------------------- import

def import_file(conn, user: dict, system_id: int, path: str | Path) -> Result:
    p = Path(path)
    return import_bytes(conn, user, system_id, p.read_bytes(), p.name)


def import_bytes(conn, user: dict, system_id: int, data: bytes, filename: str) -> Result:
    S.require_cso(user)  # imported entries carry ratings, so only a CSO can import
    if filename.lower().endswith((".xlsx", ".xlsm")):
        headers, rows = read_xlsx(data)
    elif filename.lower().endswith(".csv"):
        headers, rows = read_csv(data)
    else:
        raise S.Invalid("import needs a .csv or .xlsx file")
    digest = hashlib.sha256(data).hexdigest()
    note = f"imported from {filename} (sha256 {digest[:16]}…)"

    res = Result()
    keys = [classify(h) for h in headers]
    res.ignored_columns = [h for h, k in zip(headers, keys) if h and k is None]
    if ("title",) not in keys:
        raise S.Invalid("no hazard column found (expected 'Hazard', 'Hazard name' or 'Hazard Description / Hazard')")

    system = S.get_system(conn, system_id)
    scheme = system["risk_scheme"]
    errors: list[str] = []
    hazards: list[dict] = []
    controls: dict[str, dict] = {}       # imported control ref -> control
    unnamed: list[dict] = []             # controls with no ref in the file

    for i, row in enumerate(rows, start=1):
        cells = {}
        for k, v in zip(keys, row):
            if k and v:
                cells.setdefault(k, v)
        if not cells.get(("title",)):
            if any(cells.values()):
                errors.append(f"row {i}: no hazard text")
            continue
        h = {"title": cells[("title",)]}
        raw_ref = cells.get(("ref",), "")
        h["ref"] = (S.format_ref(system["hazard_prefix"], int(raw_ref), system["ref_width"])
                    if raw_ref.isdigit() else raw_ref or None)
        label = h["ref"] or f"row {i}"
        h["_key"] = label
        for k in ("created_at", "effects", "harm", "description", "deployer_action", "action", "owner",
                  "last_reviewed", "review_due"):
            if (k,) in cells:
                h[k] = cells[(k,)]
        h["status"] = cells.get(("status",), "open").lower()
        if h["status"] not in S.STATUSES:
            errors.append(f"{label}: unknown status {cells[('status',)]!r}")
        causes = []
        for line in cells.get(("causes",), "").splitlines():
            try:
                causes += S.parse_causes(line)
            except S.Invalid:
                causes.append({"text": line.strip(), "category": "", "paths": []})
        h["causes"] = causes
        for stage in ("initial", "residual"):
            try:
                h[f"{stage}_severity"] = risk.from_label(scheme, cells.get(("rating", stage, "severity")))
                h[f"{stage}_likelihood"] = risk.from_label(scheme, cells.get(("rating", stage, "likelihood")))
            except ValueError as e:
                errors.append(f"{label}: {stage} rating: {e}")
                continue
            h[f"{stage}_justification"] = cells.get(("rating", stage, "justification"), "")
            typed = cells.get(("rating", stage, "risk"))
            derived = risk.rating(scheme, h[f"{stage}_severity"], h[f"{stage}_likelihood"])
            if typed and str(derived or "") != typed:
                res.warnings.append(f"{label}: file says {stage} risk {typed}, matrix gives {derived}; "
                                    f"OHL keeps the matrix value")
        hazards.append(h)

        statuses = {}
        for line in cells.get(("control_status",), "").splitlines():
            m = REF_PREFIX.match(line.strip())
            if m:
                statuses[m.group(1)] = m.group(2).strip().lower().replace(" ", "_")

        for stage in ("existing", "additional"):
            for grp, (plain, tagged) in GROUP_TYPES.items():
                in_group = []
                for line in cells.get(("control", stage, grp, "description"), "").splitlines():
                    line = line.strip().lstrip("-•* ").strip()
                    if not line:
                        continue
                    m = REF_PREFIX.match(line)
                    cref, text = (m.group(1), m.group(2)) if m else (None, line)
                    ctype = plain
                    t = TAG.match(text)
                    if t and tagged and t.group(1).lower() == tagged:
                        ctype, text = tagged, text[t.end():]
                    if cref and cref in controls:
                        c = controls[cref]
                        if label not in c["hazard_refs"]:
                            c["hazard_refs"].append(label)
                    else:
                        c = {"ref": cref, "hazard_refs": [label], "description": text, "stage": stage,
                             "type": ctype, "status": statuses.get(cref, "in_place" if stage == "existing" else
                                                                    "proposed"),
                             "evidence": []}
                        if c["status"] not in S.CONTROL_STATUSES:
                            errors.append(f"{label}: control {cref}: unknown status {c['status']!r}")
                        if cref:
                            controls[cref] = c
                        else:
                            unnamed.append(c)
                    in_group.append(c)
                ev_lines = [ln.strip() for ln in cells.get(("control", stage, grp, "evidence"), "").splitlines()
                            if ln.strip()]
                for j, line in enumerate(ev_lines):
                    m = REF_PREFIX.match(line)
                    target = controls.get(m.group(1)) if m else None
                    text = m.group(2) if m and target else line
                    if target is None:
                        if not in_group:
                            res.warnings.append(f"{label}: evidence {line!r} has no control in its column; skipped")
                            continue
                        target = in_group[min(j, len(in_group) - 1)]
                    lab, _, where = (x.strip() for x in text.partition("|"))
                    item = {"label": lab, "path_or_url": where, "sha256": "", "checked_at": "", "state": "unchecked"}
                    if item not in target["evidence"]:
                        target["evidence"].append(item)

    seen = [h["ref"] for h in hazards if h["ref"]]
    dupes = {r for r in seen if seen.count(r) > 1}
    if dupes:
        errors.append(f"duplicate hazard numbers in file: {', '.join(sorted(dupes))}")
    if errors:
        raise S.Invalid("Nothing imported. Fix these and try again:\n" + "\n".join(errors))

    with tx(conn):
        label_to_ref = {}
        for h in hazards:
            key, ref = h.pop("_key"), h.pop("ref")
            new_ref = S.create_hazard(conn, user, system_id, h, ref=ref, note=note, _in_tx=True)
            label_to_ref[key] = new_ref
            res.hazards.append(new_ref)
        for c in [*controls.values(), *unnamed]:
            ref = c.pop("ref")
            c["hazard_refs"] = [label_to_ref.get(r, r) for r in c["hazard_refs"]]
            if ref and conn.execute("SELECT 1 FROM control WHERE system_id = ? AND ref = ?",
                                    (system_id, ref)).fetchone():
                raise S.Invalid(f"control {ref} already exists in this system; nothing imported")
            res.controls.append(S.create_control(conn, user, system_id, c, ref=ref, note=note, _in_tx=True))
        audit.append(conn, actor=user["email"], action="log_imported", system_id=system_id,
                     object_ref=f"system:{system_id}",
                     after={"file": filename, "sha256": digest, "hazards": res.hazards, "controls": res.controls,
                            "warnings": res.warnings, "ignored_columns": res.ignored_columns})
    return res
