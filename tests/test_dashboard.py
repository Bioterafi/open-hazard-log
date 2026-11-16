from ohl import dashboard, db
from ohl import service as S


def test_stats_matrix_and_filters(conn, cso, populated):
    S.create_hazard(conn, cso, populated, {"title": "Unrated one"})
    S.create_hazard(conn, cso, populated, {"title": "Closed one", "status": "closed",
                                           "residual_severity": "catastrophic", "residual_likelihood": "very_high"})
    scheme = S.get_system(conn, populated)["risk_scheme"]
    hazards, controls = S.list_hazards(conn, populated), S.list_controls(conn, populated)
    dashboard.annotate(hazards, controls, db.today())
    st = dashboard.stats(scheme, hazards, controls, pending=2, changes=0)

    assert st["total"] == 4 and st["by_status"] == {"open": 2, "transferred": 1, "closed": 1}
    assert st["unrated"] == 2                       # H-002 and the new one; closed hazards don't count
    assert st["high"] == 0                          # the closed 5 is excluded
    assert (st["verified"], st["controls"]) == (1, 4)
    assert {d["rating"]: d["count"] for d in st["dist"]} == {5: 0, 4: 0, 3: 0, 2: 1, 1: 0, 0: 2}
    texts = [a["text"] for a in st["attention"]]
    assert texts[0].startswith("proposals waiting") and any("without a residual rating" in t for t in texts)
    assert any("no controls" in t for t in texts)

    m = dashboard.matrix(scheme, hazards, "initial")
    cell = next(c for r in m["rows"] for c in r["cells"] if c["refs"] == ["H-001"])
    assert (cell["sev"], cell["lik"], cell["rating"]) == ("major", "medium", 3)
    assert m["rows"][0]["lik"] == "very_high"       # most likely at the top

    rows, label = dashboard.apply_filter(hazards, "", "initial", "major", "medium")
    assert [h["ref"] for h in rows] == ["H-001"] and "major" in label
    rows, label = dashboard.apply_filter(hazards, "unrated", "", "", "")
    assert label == "Not rated" and len(rows) == 2
