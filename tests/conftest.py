import pytest

from ohl import db
from ohl import service as S

PASSWORD = "correct horse battery"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def cso(conn):
    return S.add_user(conn, email="cso@example.org", name="Test CSO", role="cso", password=PASSWORD,
                      registration="GMC 0000000", actor="cli:test")


@pytest.fixture
def viewer(conn):
    return S.add_user(conn, email="viewer@example.org", name="Test Viewer", role="viewer", password=PASSWORD,
                      actor="cli:test")


@pytest.fixture
def system(conn, cso):
    return S.create_system(conn, cso, name="Ward Results Viewer", version="2.3")


@pytest.fixture
def populated(conn, cso, system):
    h1 = S.create_hazard(conn, cso, system, {
        "title": "Results shown against the wrong patient", "effects": "Results under wrong banner",
        "harm": "Treatment decided on another patient's results",
        "causes": "Race on patient switch [software] {src/context.ts}\nTwo tabs open [human factors]",
        "initial_severity": "major", "initial_likelihood": "medium",
        "residual_severity": "major", "residual_likelihood": "very_low",
        "deployer_action": "Configure single-patient session mode", "status": "transferred"})
    h2 = S.create_hazard(conn, cso, system, {"title": "Critical potassium not flagged",
                                             "initial_severity": "considerable", "initial_likelihood": "low"})
    S.create_control(conn, cso, system, {"hazard_refs": [h1, h2], "description": "Patient ID on every request",
                                         "type": "design", "status": "verified",
                                         "evidence": "Unit test | tests/a.test.ts\nDesign doc | docs/d.md"})
    S.create_control(conn, cso, system, {"hazard_refs": [h1], "description": "Context switch tests", "type": "test"})
    S.create_control(conn, cso, system, {"hazard_refs": [h1], "description": "Check banner", "type": "training",
                                         "stage": "existing", "status": "in_place"})
    S.create_control(conn, cso, system, {"hazard_refs": [h2], "description": "Units SOP", "type": "process"})
    return system
