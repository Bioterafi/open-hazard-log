"""Risk scheme: severity x likelihood -> rating 1-5, from config, never hard-coded."""

from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path

DEFAULT_SCHEME = files("ohl") / "config" / "risk_matrix.toml"  # shipped inside the package


def load_default_scheme(path: Path | None = None) -> dict:
    scheme = tomllib.loads((Path(path) if path else DEFAULT_SCHEME).read_text(encoding="utf-8"))
    validate(scheme)
    return scheme


def validate(scheme: dict) -> None:
    sev, lik, matrix = scheme["severities"], scheme["likelihoods"], scheme["matrix"]
    if set(matrix) != set(lik):
        raise ValueError("risk matrix rows must match likelihoods")
    for row in matrix.values():
        if len(row) != len(sev) or not all(isinstance(v, int) and 1 <= v <= 5 for v in row):
            raise ValueError("each matrix row needs one rating 1-5 per severity")


def rating(scheme: dict, severity: str | None, likelihood: str | None) -> int | None:
    """Derived risk rating, or None if either input is missing."""
    if not severity or not likelihood:
        return None
    if severity not in scheme["severities"]:
        raise ValueError(f"unknown severity {severity!r}")
    if likelihood not in scheme["likelihoods"]:
        raise ValueError(f"unknown likelihood {likelihood!r}")
    return scheme["matrix"][likelihood][scheme["severities"].index(severity)]


def label(scheme: dict, key: str | None) -> str:
    return scheme["labels"].get(key, "") if key else ""


def from_label(scheme: dict, text: str | None) -> str | None:
    """Map 'Very low' / 'very_low' / 'VERY LOW' back to a key. Blank -> None."""
    if not text or not str(text).strip():
        return None
    norm = str(text).strip().lower().replace("-", " ").replace("_", " ")
    for key, lab in scheme["labels"].items():
        if norm in (key.replace("_", " "), lab.lower()):
            return key
    raise ValueError(f"unrecognised rating value {text!r}")


def short(scheme: dict, r: int | None) -> str:
    return scheme["short"].get(str(r), "") if r else ""


def acceptability(scheme: dict, r: int | None) -> str:
    return scheme["acceptability"].get(str(r), "") if r else ""


def blocks(r: int | None) -> str:
    return ("■" * r + "□" * (5 - r)) if r else "·····"
