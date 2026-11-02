"""Tests of TE&IV's WG10 TE&IV R1 door and of its app (plan task S3.19, D19; ADR 0013 section 7, 11.7): the 16 operations under their standard
operationIds, what is passed to smo-teiv and what is not, the scoped callers and TE&IV's own decorators refused, the writes audited, smo-teiv's answers
passed through; readiness, the decorator schema loaded at start-up, and the registration of TE&IV's 15 classes under its MnS scope."""

import json

import httpx
import pytest
from sqlalchemy import select

from smo_shared import audit

from app import attrs, model

import tree

WG10 = "/topology-inventory/v1"
OPERATIONS = {"getAllDomains", "getTopologyEntityTypes", "getTopologyByEntityTypeName", "getTopologyById", "getAllRelationshipsForEntityId",
              "getTopologyRelationshipTypes", "getRelationshipsByType", "getRelationshipById", "getSchemas", "getSchemaByName",
              "createUserDefinedSchema", "getUserDefinedSchemas", "getUserDefinedSchemaByName", "deleteUserDefinedSchema", "updateClassifier",
              "updateDecorator"}


def test_the_sixteen_wg10_operations_are_served_under_their_standard_ids(teiv):
    """Every operation of the WG10 TE&IV R1 API is a route of TE&IV's, named by its operationId; smo-teiv's own extras are not."""
    paths = teiv.app.openapi()["paths"]
    ids = {op["operationId"] for p, ops in paths.items() if p.startswith(WG10) for op in ops.values()}
    assert ids == OPERATIONS
    assert not any("/groups" in p or p == WG10 + "/schemas/{schemaName}" for p in paths)


def test_get_topology_by_id_is_smo_teivs_answer_passed_through(api):
    """getTopologyById of a cell: smo-teiv's own body (its module:type key), status and content type."""
    ident = model.teiv_id(tree.du_cell(0, 1))
    r = api.get(f"{WG10}/domains/RAN/entity-types/NRCellDU/entities/{ident}")
    assert r.status_code == 200 and r.json()["o-ran-smo-teiv-ran:NRCellDU"][0]["id"] == ident
    missing = api.get(f"{WG10}/domains/RAN/entity-types/NRCellDU/entities/{ident}9")
    assert missing.status_code == 404 and missing.json()["title"] == "NOT_FOUND"


def test_only_the_standards_query_parameters_are_passed(api, fake):
    """getTopologyByEntityTypeName passes targetFilter, scopeFilter, offset and limit, and drops a parameter the standard does not declare."""
    fake.requests.clear()
    r = api.get(f"{WG10}/domains/RAN/entity-types/NRCellDU/entities",
                params={"targetFilter": "/sourceIds", "scopeFilter": "/attributes[@nRTAC=100]", "limit": 2, "rogue": "x"})
    assert r.status_code == 200 and len(r.json()["items"]) <= 2
    sent = fake.requests[-1].url.params
    assert sent["scopeFilter"] == "/attributes[@nRTAC=100]" and "rogue" not in sent


def test_a_scoped_caller_is_sent_to_provmns(api):
    """A caller with a scope claim: 403 SCOPE_DENIED (the WG10 API names no DN to check the claim against before the answer)."""
    r = api.get(f"{WG10}/domains", headers={"X-R1-Scope": json.dumps({"subNetworks": ["SMO-RAN"]})})
    assert r.status_code == 403 and r.json()["title"] == "SCOPE_DENIED"


def test_teivs_own_schema_and_decorators_cannot_be_changed_through_the_door(api):
    """deleteUserDefinedSchema of smo-teiv-attrs and an updateDecorator naming one of its keys: 403 TEIV_OWNED."""
    assert api.delete(f"{WG10}/user-defined-schemas/{attrs.MODULE}").json()["title"] == "TEIV_OWNED"
    body = {"operation": "merge", "decorators": {f"{attrs.MODULE}:administrativeState": "LOCKED"}, "entityIds": [model.teiv_id(tree.du_cell(0, 1))]}
    r = api.post(f"{WG10}/manage-decorators", json=body)
    assert r.status_code == 403 and r.json()["title"] == "TEIV_OWNED"


def test_a_write_through_the_door_is_audited(api, fake, sessions):
    """updateDecorator of another decorator reaches smo-teiv and is one audit row naming the operation and the decorators."""
    body = {"operation": "merge", "decorators": {"site:label": "north"}, "entityIds": [model.teiv_id(tree.du_cell(0, 1))]}
    r = api.post(f"{WG10}/manage-decorators", json=body)
    assert r.status_code == 204
    with sessions() as db:
        rows = list(db.scalars(select(audit.AuditEntry)))
    assert len(rows) == 1 and rows[0].target == f"{WG10}/manage-decorators" and rows[0].detail["decorators"] == ["site:label"]


def test_smo_teiv_down_is_a_503_problem_on_the_door(api, fake):
    """smo-teiv not answering: 503 TEIV_UNAVAILABLE, RFC 7807, with Retry-After."""
    fake.fail_with = 503
    r = api.get(f"{WG10}/domains")
    assert r.status_code == 503 and r.json()["title"] == "TEIV_UNAVAILABLE" and r.headers["retry-after"] == "5"


def test_ready_loads_the_decorator_schema_and_needs_smo_teiv(api, fake):
    """The readiness probe loads TE&IV's decorator schema into smo-teiv when it is not there (once), and is not ready while smo-teiv is down."""
    assert attrs.MODULE not in fake.schemas
    assert api.get("/ready").status_code == 200
    assert attrs.MODULE in fake.schemas
    uploads = sum(1 for r in fake.requests if r.method == "POST" and r.url.path.endswith("/user-defined-schemas"))
    assert api.get("/ready").status_code == 200
    assert sum(1 for r in fake.requests if r.method == "POST" and r.url.path.endswith("/user-defined-schemas")) == uploads == 1
    fake.fail_with = 500
    assert api.get("/ready").status_code == 503


def test_teiv_registers_its_fifteen_classes_under_its_mns_scope(teiv, monkeypatch):
    """The registration names TE&IV's 15 classes and the scope SubNetwork=SMO-RAN (TEIV_MNS_SCOPE splits on `;`)."""
    assert teiv.REGISTRATION.module == "teiv" and set(teiv.REGISTRATION.classes) == set(model.CLASSES)
    assert teiv.REGISTRATION.options["scope"] == ["SubNetwork=SMO-RAN"]
    monkeypatch.setenv("TEIV_MNS_SCOPE", "SubNetwork=A; SubNetwork=B,SubNetwork=C")
    assert teiv.mns_scope() == ["SubNetwork=A", "SubNetwork=B,SubNetwork=C"]


@pytest.mark.parametrize("raw, expected", [("", 5.0), ("abc", 5.0), ("-1", 5.0), ("0.5", 0.5)])
def test_a_timeout_setting_that_is_not_a_positive_number_falls_back(monkeypatch, raw, expected):
    """TEIV_READ_TIMEOUT_SECONDS: a positive number of seconds, else the default."""
    from app import client
    monkeypatch.setenv("TEIV_READ_TIMEOUT_SECONDS", raw)
    assert client._float("TEIV_READ_TIMEOUT_SECONDS", 5.0) == expected
    assert isinstance(client.SmoTeivClient("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(200))).http, httpx.Client)


@pytest.mark.parametrize("value", ["a'b", 'x"}', "back\\slash", {"nested": ["ok", "it's"]}])
def test_a_decorator_value_smo_teiv_would_paste_into_its_sql_is_refused(api, fake, value):
    """smo-teiv 0.3.0 writes a decorator value into its SQL text unescaped (a `'` ends the literal): the door refuses `'`, `"` and `\\` anywhere in
    the decorators, 400 INVALID_DECORATOR_VALUE, and smo-teiv is not called."""
    fake.requests.clear()
    body = {"operation": "merge", "decorators": {"site:label": value}, "entityIds": [model.teiv_id(tree.du_cell(0, 1))]}
    r = api.post(f"{WG10}/manage-decorators", json=body)
    assert r.status_code == 400 and r.json()["title"] == "INVALID_DECORATOR_VALUE"
    assert not fake.requests
