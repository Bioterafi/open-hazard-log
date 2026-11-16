"""Numbers for a system's dashboard. Pure functions over the log, so they're easy to test."""

from __future__ import annotations

from . import risk

FILTERS = {
    "open": ("Open", lambda h: h["status"] == "open"),
    "transferred": ("Transferred", lambda h: h["status"] == "transferred"),
    "closed": ("Closed", lambda h: h["status"] == "closed"),
    "high": ("Residual 3+", lambda h: (h["residual_risk"] or 0) >= 3),
    "unrated": ("Not rated", lambda h: h["status"] != "closed" and not h["residual_risk"]),
}


def annotate(hazards: list[dict], controls: list[dict], today: str) -> None:
    """Per-hazard control counts and flags, used by the table and the attention list."""
    for h in hazards:
        mine = [c for c in controls if h["ref"] in c["hazard_refs"]]
        h["n_controls"] = len(mine)
        h["n_verified"] = sum(c["status"] == "verified" for c in mine)
        h["n_invalid"] = sum(c["status"] == "invalid" for c in mine)
        h["n_evidence_problem"] = sum(e["state"] in ("changed", "missing") for c in mine for e in c["evidence"])
        h["overdue"] = bool(h["review_due"]) and h["review_due"] < today and h["status"] != "closed"


def matrix(scheme: dict, hazards: list[dict], stage: str) -> dict:
    """Rows: likelihood, very high first. Cells: the matrix rating and the hazards that sit there."""
    live = [h for h in hazards if h["status"] != "closed"]
    rows = []
    for lik in reversed(scheme["likelihoods"]):
        cells = []
        for sev in scheme["severities"]:
            refs = [h["ref"] for h in live if h[f"{stage}_severity"] == sev and h[f"{stage}_likelihood"] == lik]
            cells.append({"sev": sev, "lik": lik, "rating": risk.rating(scheme, sev, lik), "refs": refs})
        rows.append({"lik": lik, "cells": cells})
    unrated = sum(1 for h in live if not h[f"{stage}_risk"])
    return {"stage": stage, "rows": rows, "unrated": unrated}


def stats(scheme: dict, hazards: list[dict], controls: list[dict], *, pending: int, changes: int) -> dict:
    live = [h for h in hazards if h["status"] != "closed"]
    by_status = {s: sum(h["status"] == s for h in hazards) for s in ("open", "transferred", "closed")}
    unrated = [h["ref"] for h in live if not h["residual_risk"]]
    high = [h["ref"] for h in live if (h["residual_risk"] or 0) >= 3]
    must = [h["ref"] for h in live if (h["residual_risk"] or 0) >= 4]
    verified = sum(c["status"] == "verified" for c in controls)
    total = len(controls)
    dist = [{"rating": r, "count": sum(h["residual_risk"] == r for h in live), "word": risk.short(scheme, r)}
            for r in (5, 4, 3, 2, 1)]
    dist.append({"rating": 0, "count": len(unrated), "word": "not rated"})
    biggest = max([d["count"] for d in dist] + [1])

    attention = []

    def add(n, text, href, refs=None):
        if n:
            attention.append({"n": n, "text": text, "href": href, "refs": refs or []})

    add(pending, "proposal" + ("s" if pending != 1 else "") + " waiting for your review", "review")
    add(len(must), "with residual risk 4 or 5: must be controlled further", "?f=high", must)
    add(len(unrated), "without a residual rating", "?f=unrated", unrated)
    invalid = [h["ref"] for h in live if h["n_invalid"]]
    add(len(invalid), "with a control marked invalid", "", invalid)
    evidence = [h["ref"] for h in live if h["n_evidence_problem"]]
    add(len(evidence), "with evidence that changed or went missing", "", evidence)
    overdue = [h["ref"] for h in live if h["overdue"]]
    add(len(overdue), "overdue for review", "", overdue)
    bare = [h["ref"] for h in live if not h["n_controls"]]
    add(len(bare), "with no controls yet", "", bare)
    add(changes, "change" + ("s" if changes != 1 else "") + " not yet in an issued version", "issue")

    return {
        "total": len(hazards), "by_status": by_status, "live": len(live),
        "unrated": len(unrated), "high": len(high),
        "verified": verified, "controls": total, "verified_pct": round(100 * verified / total) if total else 0,
        "overdue": len(overdue), "pending": pending,
        "dist": dist, "dist_max": biggest, "attention": attention,
    }


def apply_filter(hazards: list[dict], f: str | None, stage: str | None, sev: str | None, lik: str | None):
    """Filter the table by a named filter or by one matrix cell. Returns (hazards, label)."""
    if sev and lik and stage in ("initial", "residual"):
        rows = [h for h in hazards if h["status"] != "closed" and h[f"{stage}_severity"] == sev
                and h[f"{stage}_likelihood"] == lik]
        return rows, f"{stage.capitalize()} {sev.replace('_', ' ')} × {lik.replace('_', ' ')}"
    if f in FILTERS:
        label, keep = FILTERS[f]
        return [h for h in hazards if keep(h)], label
    return hazards, ""
