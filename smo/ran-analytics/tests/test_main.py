"""Tests of RAN Analytics' routes: producer registration (upsert, MDA type handling, SME enrolment) and the producer list.

Run: `cd smo/ran-analytics && PYTHONPATH=.:../shared python -m pytest tests -q`. No Postgres: the table is created on SQLite and
`get_session` is overridden. The SME calls are replaced by patching `app.main.R1Client.post`, so SME itself is not exercised.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared.db import Base, get_session
from smo_shared.testing import make_test_engine

from app.main import app
from app.models import MDAFProducer


class FakeSme:
    """Stands in for SME's answer: a status code and a JSON body (a service id by default)."""
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"serviceId": "11111111-1111-1111-1111-111111111111"}

    def json(self):
        return self._payload


@pytest.fixture
def db_session_factory():
    """Creates the producer table on an in-memory SQLite engine and returns a session factory."""
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[MDAFProducer.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory, monkeypatch):
    """A TestClient with `get_session` overridden and `R1Client.post` / `delete` stubbed to answer as SME does (201 with a service id, 204); undone afterwards."""
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeSme())
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: FakeSme(204, {}))
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_register_analytics_producer_closes_the_v1_3_gap(client):
    """A producer can register with its DME input types and output schema (201)."""
    dme_type = str(uuid.uuid4())
    resp = client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                        json={"dme_input_types": [dme_type], "output_schema": {"type": "object"}})
    assert resp.status_code == 201


def test_reregistering_same_producer_and_type_updates_in_place(client, db_session_factory):
    """Registering the same producer and analytics type again updates the one row (the new inputs and schema) instead of failing on the primary key."""
    first_dme_type = uuid.uuid4()
    resp1 = client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                         json={"dme_input_types": [str(first_dme_type)], "output_schema": {"type": "object"}})
    assert resp1.status_code == 201

    second_dme_type = uuid.uuid4()
    resp2 = client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                         json={"dme_input_types": [str(second_dme_type)], "output_schema": {"type": "string"}})
    assert resp2.status_code == 201

    with db_session_factory() as session:
        rows = session.query(MDAFProducer).filter_by(producer_id="rapp-mdaf-1", analytics_type="coverage-issue-analysis").all()
        assert len(rows) == 1  # updated in place, not a second row or a crash
        assert rows[0].dme_input_types == [str(second_dme_type)]
        assert rows[0].output_schema == {"type": "string"}


def test_different_analytics_type_for_same_producer_is_a_separate_row(client):
    """The key is the pair, so another analytics type for the same producer registers separately."""
    resp1 = client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                         json={"dme_input_types": [], "output_schema": {}})
    resp2 = client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "resource-utilization"},
                         json={"dme_input_types": [], "output_schema": {}})
    assert resp1.status_code == 201
    assert resp2.status_code == 201


def test_register_analytics_producer_publishes_sme_service_registration(client, monkeypatch):
    """A registration enrols the producer in SME first and then publishes the `mdaf.<analyticsType>.<producerId>` service, with the producer id on both calls;
    the producer keeps SME's service id."""
    calls = []
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: calls.append((path, json)) or FakeSme())

    dme_type = str(uuid.uuid4())
    client.post("/producers", params={"producer_id": "rapp-mdaf-2", "analytics_type": "failure-prediction"},
                json={"dme_input_types": [dme_type], "output_schema": {"type": "object"}})

    assert len(calls) == 2
    enroll_path, enroll_payload = calls[0]
    assert enroll_path == "/sme/provider-registrations"
    assert enroll_payload == {"apfId": "rapp-mdaf-2"}
    path, payload = calls[1]
    assert path == "/sme/published-apis/v1/rapp-mdaf-2/service-apis"
    assert payload["serviceName"] == "mdaf.failure-prediction.rapp-mdaf-2"
    assert payload["producerId"] == "rapp-mdaf-2"
    assert payload["moduleScope"] == "ran-analytics"
    assert client.get("/producers").json()["items"][0]["smeServiceId"] == "11111111-1111-1111-1111-111111111111"


def test_list_producers_returns_registered_producer(client):
    """A registered producer is listed with its ids and DME input types as text."""
    dme_type = str(uuid.uuid4())
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                json={"dme_input_types": [dme_type], "output_schema": {"type": "object"}})

    resp = client.get("/producers")
    assert resp.status_code == 200
    producers = resp.json()["items"]
    assert len(producers) == 1
    assert producers[0]["producerId"] == "rapp-mdaf-1"
    assert producers[0]["analyticsType"] == "coverage-issue-analysis"
    assert producers[0]["dmeInputTypes"] == [dme_type]


def test_list_producers_filters_by_analytics_type(client):
    """The producer list can be filtered by analytics type."""
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                json={"dme_input_types": [], "output_schema": {}})
    client.post("/producers", params={"producer_id": "rapp-mdaf-2", "analytics_type": "resource-utilization"},
                json={"dme_input_types": [], "output_schema": {}})

    resp = client.get("/producers", params={"analytics_type": "resource-utilization"})
    ids = [p["producerId"] for p in resp.json()["items"]]
    assert ids == ["rapp-mdaf-2"]


def test_list_producers_filters_by_producer_id(client):
    """The producer list can be filtered by producer id and returns all of that producer's types."""
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                json={"dme_input_types": [], "output_schema": {}})
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "resource-utilization"},
                json={"dme_input_types": [], "output_schema": {}})
    client.post("/producers", params={"producer_id": "rapp-mdaf-2", "analytics_type": "resource-utilization"},
                json={"dme_input_types": [], "output_schema": {}})

    resp = client.get("/producers", params={"producer_id": "rapp-mdaf-1"})
    assert len(resp.json()["items"]) == 2
    assert {p["producerId"] for p in resp.json()["items"]} == {"rapp-mdaf-1"}


def test_list_producers_returns_empty_list_when_none_registered(client):
    """With nothing registered the list is empty, not an error."""
    resp = client.get("/producers")
    assert resp.json()["items"] == []


def test_register_analytics_producer_infers_mda_type_for_a_known_shorthand(client):
    """A known shorthand analytics type gets its TS 28.104 MDA type filled in automatically."""
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis"},
                json={"dme_input_types": [], "output_schema": {}})
    resp = client.get("/producers")
    assert resp.json()["items"][0]["mdaType"] == "COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS"


def test_register_analytics_producer_leaves_mda_type_null_for_an_unmapped_shorthand(client):
    """An analytics type with no unambiguous MDA type is stored with none, not a guess."""
    client.post("/producers", params={"producer_id": "rapp-mdaf-1", "analytics_type": "resource-utilization"},
                json={"dme_input_types": [], "output_schema": {}})
    resp = client.get("/producers")
    assert resp.json()["items"][0]["mdaType"] is None


def test_register_analytics_producer_accepts_an_explicit_real_mda_type(client):
    """A caller can declare a real MDA type for any analytics type, and it is stored."""
    client.post("/producers", params={
        "producer_id": "rapp-mdaf-1", "analytics_type": "some-custom-shorthand", "mda_type": "PREDICTIONS_PM_DATA",
    }, json={"dme_input_types": [], "output_schema": {}})
    resp = client.get("/producers")
    assert resp.json()["items"][0]["mdaType"] == "PREDICTIONS_PM_DATA"


def test_register_analytics_producer_rejects_an_unknown_mda_type(client):
    """An `mda_type` outside the TS 28.104 list is a 422 SCHEMA_VALIDATION_FAILED."""
    resp = client.post("/producers", params={
        "producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis", "mda_type": "NOT_A_REAL_MDA_TYPE",
    }, json={"dme_input_types": [], "output_schema": {}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`, which the GUI BFF's module status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}
