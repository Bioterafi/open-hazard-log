"""Agentic scan: read a system's docs and code, propose candidate hazards.

Guardrails (SPEC.md section 5), all enforced here in code:
- No write path to the log: the only writes are proposals.record_run() (proposals plus one
  run event) and proposals.record_failure() (one run_failed event).
- Quote check: every citation must appear verbatim in the cited source.
- Schema check: the model's output must match SCHEMA or the run stores no proposals.
- Sources go in as delimited data; the prompt says never to follow them.
- No run until the CSO has confirmed the sources contain no patient data.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import proposals
from .db import now
from .service import Forbidden, Invalid, get_system, list_hazards  # read-only use

PROMPT_DIR = resources.files("ohl") / "prompts"  # shipped inside the package; MANIFEST holds each file's SHA-256
PROMPT_VERSION = "scan-v1"
DEFAULT_MODEL = os.environ.get("OHL_SCAN_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = "high"
MAX_TOKENS = 64000
MAX_FILE_BYTES = 200_000
MAX_INPUT_CHARS = 600_000      # about 150k tokens; raise it with OHL_SCAN_MAX_CHARS
MAX_TREE_LINES = 2000
DOC_SUFFIXES = (".md", ".markdown", ".txt")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".pytest_cache"}

LENSES = ["workflow", "failure_modes", "ai_specific", "human_factors"]
CATEGORIES = ["software", "data", "ai-model", "human-factors", "integration", "infrastructure"]
CONTROL_TYPES = ["design", "test", "process", "training", "warning"]


def _str():
    return {"type": "string"}


def build_schema(scheme: dict) -> dict:
    candidate = {
        "type": "object", "additionalProperties": False,
        "required": ["lens", "modifies", "title", "description", "causes", "effects", "harm", "controls",
                     "suggested_severity", "suggested_likelihood", "rating_rationale", "reasoning", "assumptions",
                     "citations", "confidence"],
        "properties": {
            "lens": {"type": "string", "enum": LENSES},
            "modifies": _str(),
            "title": _str(), "description": _str(),
            "causes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "category", "paths"],
                "properties": {"text": _str(), "category": {"type": "string", "enum": CATEGORIES},
                               "paths": {"type": "array", "items": _str()}}}},
            "effects": _str(), "harm": _str(),
            "controls": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["type", "description"],
                "properties": {"type": {"type": "string", "enum": CONTROL_TYPES}, "description": _str()}}},
            "suggested_severity": {"type": "string", "enum": list(scheme["severities"])},
            "suggested_likelihood": {"type": "string", "enum": list(scheme["likelihoods"])},
            "rating_rationale": _str(), "reasoning": _str(), "assumptions": _str(),
            "citations": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["path", "quote"],
                "properties": {"path": _str(), "quote": _str()}}},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
    return {"type": "object", "additionalProperties": False,
            "required": ["lenses_run", "candidates", "not_proposed_summary"],
            "properties": {"lenses_run": {"type": "array", "items": {"type": "string", "enum": LENSES}},
                           "candidates": {"type": "array", "items": candidate},
                           "not_proposed_summary": _str()}}


# ---------------------------------------------------------------- inputs

@dataclass
class Inputs:
    sources: dict[str, str]                 # display path -> exact text (what quotes are checked against)
    tree: list[str]
    tree_truncated: int = 0
    skipped: list[str] = field(default_factory=list)


def _git_files(repo: Path) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def _walk(root: Path) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            files.append(str((Path(dirpath) / f).relative_to(root)))
    return files


def _read(path: Path, label: str, skipped: list[str]) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            skipped.append(f"{label} (over {MAX_FILE_BYTES // 1000} KB)")
            return None
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        skipped.append(f"{label} (not UTF-8 text)")
    except OSError as e:
        skipped.append(f"{label} ({e.strerror})")
    return None


def gather(system: dict) -> Inputs:
    src = system["sources"] or {}
    repo = Path(src["repo"]).expanduser() if src.get("repo") else None
    if repo and not repo.is_dir():
        raise Invalid(f"repository path not found: {repo}")
    sources: dict[str, str] = {"system:intended_use": system["intended_use"],
                               "system:clinical_context": system["clinical_context"]}
    skipped: list[str] = []

    for folder in src.get("docs") or []:
        root = Path(folder).expanduser()
        if not root.is_absolute() and repo:
            root = repo / root
        if not root.is_dir():
            raise Invalid(f"doc folder not found: {root}")
        for rel in _walk(root):
            if not rel.lower().endswith(DOC_SUFFIXES):
                continue
            full = root / rel
            label = str(full.relative_to(repo)) if repo and full.is_relative_to(repo) else f"{root.name}/{rel}"
            text = _read(full, label, skipped)
            if text is not None:
                sources[label] = text

    tree: list[str] = []
    if repo:
        tree = _git_files(repo) or _walk(repo)
        for pattern in src.get("code_globs") or []:
            for rel in tree:
                if rel not in sources and fnmatch.fnmatch(rel, pattern):
                    text = _read(repo / rel, rel, skipped)
                    if text is not None:
                        sources[rel] = text
    truncated = max(0, len(tree) - MAX_TREE_LINES)
    return Inputs(sources=sources, tree=tree[:MAX_TREE_LINES], tree_truncated=truncated, skipped=skipped)


def _escape(text: str) -> str:
    """Stop a source from closing its own <document> block."""
    return re.sub(r"</(document|documents)", r"<\\/\1", text, flags=re.I)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()  # same on a Windows checkout


def prompt_manifest() -> dict[str, str]:
    """prompts/MANIFEST: one 'sha256  file' line per prompt file (sha256sum format)."""
    out = {}
    for line in (PROMPT_DIR / "MANIFEST").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, name = line.split(maxsplit=1)
            out[name.strip()] = digest
    return out


def prompt_template(version: str = PROMPT_VERSION) -> str:
    """The prompt file for a version, refused if it was edited in place (OHL-H6).
    A prompt change means a new file, a new version string and a new MANIFEST line."""
    name = f"{version.replace('-', '_')}.md"
    raw = (PROMPT_DIR / name).read_bytes()
    if prompt_manifest().get(name) != _sha(raw):
        raise ScanError(f"prompt file {name} doesn't match prompts/MANIFEST; it was edited in place. "
                        "A prompt change needs a new file and version")
    return raw.decode("utf-8")


def build_prompts(system: dict, inputs: Inputs, hazards: list[dict], rejections: list[dict],
                  max_candidates: int) -> tuple[str, str]:
    scheme = system["risk_scheme"]
    uses_ai = bool((system["sources"] or {}).get("uses_ai"))
    template = prompt_template()
    sev = "\n".join(f"- {s}: {scheme['labels'][s]}. {scheme['severity_help'][s]}" for s in scheme["severities"])
    lik = "\n".join(f"- {k}: {scheme['labels'][k]}. {scheme['likelihood_help'][k]}" for k in scheme["likelihoods"])
    ai = ("fabrication, omission, automation bias, deskilling, distribution shift, subgroup under-performance, "
          "prompt injection, model version change, silent degradation." if uses_ai else
          "skip this lens: the CSO has recorded that this system has no AI or machine-learning components.")
    system_prompt = (template.replace("{{AI_LENS}}", ai).replace("{{SEVERITY}}", sev)
                     .replace("{{LIKELIHOOD}}", lik).replace("{{MAX}}", str(max_candidates)))

    existing = "\n".join(
        f"{h['ref']}: {h['title']}" + (f" | causes: {'; '.join(c['text'] for c in h['causes'])}" if h["causes"] else "")
        for h in hazards) or "(none yet)"
    rejected = "\n".join(f"- {r['title']} | reason: {r['reason']}" for r in rejections) or "(none)"
    more = f"\n… {inputs.tree_truncated} more files not listed" if inputs.tree_truncated else ""
    tree = "\n".join(inputs.tree) + more
    docs = "\n".join(f'<document path="{p}">\n{_escape(t)}\n</document>' for p, t in inputs.sources.items())
    user = (f"<system>\nName: {system['name']} {system['version']}\n"
            f"Contains AI or machine-learning components: {'yes' if uses_ai else 'no'}\n</system>\n\n"
            f"<existing_hazards>\n{existing}\n</existing_hazards>\n\n"
            f"<rejected_proposals>\n{rejected}\n</rejected_proposals>\n\n"
            f"<file_tree>\n{tree or '(no repository configured)'}\n</file_tree>\n\n"
            f"<documents>\n{docs}\n</documents>\n\n"
            f"Propose up to {max_candidates} candidate hazards for this system, following your instructions.")
    return system_prompt, user


# ---------------------------------------------------------------- quote check

def _norm(s: str) -> str:
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def check_quote(sources: dict[str, str], path: str, quote: str) -> dict:
    """Verbatim match after collapsing whitespace and straightening quote marks. Adds the real line range."""
    text = sources.get(path)
    q = _norm(quote).strip(" .…\"'")
    result = {"path": path, "quote": quote, "found": False, "lines": ""}
    if text is None or len(q) < 8:
        return result
    flat = _norm(text)
    i = flat.find(q)
    if i < 0:
        return result
    # Map the match back to line numbers in the original text.
    words_before = len(flat[:i].split())
    words_in = len(q.split())
    count, start_line, end_line = 0, None, None
    for n, line in enumerate(text.splitlines(), start=1):
        w = len(_norm(line).split())
        if start_line is None and count + w > words_before:
            start_line = n
        if start_line is not None and count + w >= words_before + words_in:
            end_line = n
            break
        count += w
    result.update(found=True, lines=f"{start_line}-{end_line}" if start_line and end_line and end_line != start_line
                  else str(start_line or ""))
    return result


# ---------------------------------------------------------------- model call

class ScanError(Exception):
    pass


class ReplayClient:
    """Stands in for the API with a saved model output (JSON file). For demos and offline regression
    runs: the whole pipeline runs except the model call, and the run is recorded as a replay."""

    def __init__(self, path: str | Path):
        from types import SimpleNamespace as NS
        self.path = Path(path)
        self.replay_label = f"replay:{self.path.name}"
        text = self.path.read_text()
        self._msg = NS(content=[NS(type="text", text=text)], model=self.replay_label,
                       stop_reason="end_turn", stop_details=None, usage=None)
        self.beta = NS(messages=NS(stream=lambda **kw: self))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._msg


def call_model(client, model: str, effort: str, system_prompt: str, user: str, schema: dict):
    with client.beta.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=system_prompt,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        raise ScanError(f"the model declined this request ({getattr(details, 'category', None) or 'no category'})")
    if msg.stop_reason == "max_tokens":
        raise ScanError("the model ran out of output space; lower the proposal limit or narrow the sources")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise ScanError("the model returned no text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScanError(f"output was not valid JSON ({e})") from e
    return data, msg


def _validate(data: dict, schema: dict) -> None:
    """Belt and braces on top of structured outputs: required keys and enums."""
    def check(value, sch, where):
        t = sch.get("type")
        if t == "object":
            if not isinstance(value, dict):
                raise ScanError(f"{where}: expected an object")
            for k in sch.get("required", []):
                if k not in value:
                    raise ScanError(f"{where}: missing {k}")
            extra = set(value) - set(sch.get("properties", {}))
            if extra:
                raise ScanError(f"{where}: unexpected {', '.join(sorted(extra))}")
            for k, v in value.items():
                check(v, sch["properties"][k], f"{where}.{k}")
        elif t == "array":
            if not isinstance(value, list):
                raise ScanError(f"{where}: expected a list")
            for i, v in enumerate(value):
                check(v, sch["items"], f"{where}[{i}]")
        elif t == "string":
            if not isinstance(value, str):
                raise ScanError(f"{where}: expected text")
            if "enum" in sch and value not in sch["enum"]:
                raise ScanError(f"{where}: {value!r} is not allowed")
    check(data, schema, "output")


# ---------------------------------------------------------------- run

@dataclass
class Result:
    run_id: str
    shown: list[int]
    hidden_no_quote: int
    over_limit: int
    lenses_run: list[str]
    not_proposed_summary: str
    model_used: str
    skipped: list[str]


def preflight(conn, user: dict, system_id: int) -> tuple[dict, Inputs]:
    """Everything that can stop a scan before any model call. Raises with a plain reason."""
    if user["role"] not in ("cso", "contributor"):
        raise Forbidden("Only a CSO or contributor can run a scan.")
    system = get_system(conn, system_id)
    if not system["no_pid_confirmed"]:
        raise Invalid("Confirm in System details that the sources contain no patient-identifiable data first.")
    if not system["intended_use"].strip():
        raise Invalid("Add the system's intended use in System details first (DCB0129 4.2).")
    inputs = gather(system)
    if len(inputs.sources) <= 2 and not inputs.tree:
        raise Invalid("No sources to scan. Add a repository path or doc folders in System details.")
    size = sum(len(t) for t in inputs.sources.values())
    cap = int(os.environ.get("OHL_SCAN_MAX_CHARS", MAX_INPUT_CHARS))
    if size > cap:
        biggest = sorted(inputs.sources.items(), key=lambda kv: -len(kv[1]))[:5]
        raise Invalid(f"Sources total {size:,} characters, over the {cap:,} limit. Narrow the doc folders or code "
                      f"globs. Largest: " + ", ".join(f"{p} ({len(t):,})" for p, t in biggest))
    return system, inputs


def run(conn, user: dict, system_id: int, *, client=None, model: str | None = None, effort: str = DEFAULT_EFFORT,
        max_proposals: int | None = None) -> Result:
    """Preflight problems stop the scan before it starts and record nothing. A scan that fails
    after starting stores no proposals and leaves one `run_failed` event when the database
    can still be written (OHL-H4)."""
    system, inputs = preflight(conn, user, system_id)
    model = model or DEFAULT_MODEL
    run_id = uuid.uuid4().hex[:12]
    started = now()
    replayed = getattr(client, "replay_label", "")  # a failed replay must not look like a model call
    try:
        return _run(conn, user, system, inputs, client=client, model=model, effort=effort,
                    max_proposals=max_proposals, run_id=run_id, started=started)
    except Exception as e:
        proposals.record_failure(conn, system_id, {"run_id": run_id, "origin": "scan", "started_by": user["email"],
                                                   "started_at": started, "model_requested": model, "effort": effort,
                                                   "prompt_version": PROMPT_VERSION,
                                                   **({"model_used": replayed} if replayed else {})}, e)
        raise


def _run(conn, user: dict, system: dict, inputs: Inputs, *, client, model: str, effort: str,
         max_proposals: int | None, run_id: str, started: str) -> Result:
    system_id = system["id"]
    limit = max_proposals or int((system["sources"] or {}).get("max_proposals") or 15)

    hazards = list_hazards(conn, system_id)
    system_prompt, user_msg = build_prompts(system, inputs, hazards, proposals.recent_rejections(conn, system_id),
                                            limit)
    schema = build_schema(system["risk_scheme"])
    inputs_sha = hashlib.sha256(json.dumps([model, effort, system_prompt, user_msg]).encode()).hexdigest()

    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    data, msg = call_model(client, model, effort, system_prompt, user_msg, schema)
    _validate(data, schema)

    refs = {h["ref"] for h in hazards}
    items, hidden = [], 0
    candidates = data["candidates"]
    over = max(0, len(candidates) - limit)
    for c in candidates[:limit]:
        cites = [check_quote(inputs.sources, x["path"], x["quote"]) for x in c["citations"]]
        if not any(x["found"] for x in cites):
            hidden += 1
            continue
        confidence = c["confidence"] if all(x["found"] for x in cites) else "low"
        target = c["modifies"].strip() if c["modifies"].strip() in refs else None
        items.append({
            "kind": "change_hazard" if target else "new_hazard",
            "target": target,
            "payload": {k: c[k] for k in ("title", "description", "causes", "effects", "harm", "controls", "lens")},
            "reasoning": c["reasoning"], "assumptions": c["assumptions"], "citations": cites,
            "confidence": confidence,
            "suggested_rating": {"severity": c["suggested_severity"], "likelihood": c["suggested_likelihood"],
                                 "rationale": c["rating_rationale"]},
        })

    usage = getattr(msg, "usage", None)
    summary = {
        "run_id": run_id, "origin": "scan", "started_by": user["email"], "started_at": started, "finished_at": now(),
        "model_requested": model, "model_used": getattr(msg, "model", model), "effort": effort,
        "prompt_version": PROMPT_VERSION,
        "prompt_file_sha256": prompt_manifest().get(f"{PROMPT_VERSION.replace('-', '_')}.md", ""),
        "prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(), "inputs_sha256": inputs_sha,
        "sources": sorted(inputs.sources), "skipped": inputs.skipped, "lenses_run": data["lenses_run"],
        "candidates": len(candidates), "hidden_no_verified_quote": hidden, "over_limit": over,
        "not_proposed_summary": data["not_proposed_summary"],
        "input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None),
    }
    ids = proposals.record_run(conn, system_id, summary, items)
    return Result(run_id=run_id, shown=ids, hidden_no_quote=hidden, over_limit=over, lenses_run=data["lenses_run"],
                  not_proposed_summary=data["not_proposed_summary"], model_used=summary["model_used"],
                  skipped=inputs.skipped)
