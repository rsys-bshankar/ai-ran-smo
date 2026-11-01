"""The ProvMnS router through HTTP (plan tasks S1.1, S1.2): every operation, both patch formats in all four media types, If-Match and concurrent
writes, containment, unknown classes, schema validation, the lifecycle hooks and their veto, and the notifications on the event bus.

Every test runs on in-memory SQLite and, when SMO_TEST_POSTGRES_URL is set, on real Postgres, against the test-only module of mns_testmodule.py.
"""

import json

import pytest
import yaml
from jsonschema import Draft7Validator
from mns_testmodule import BASE, RecordingHandler, build_tree, make_app, notifications
from mns_testmodule import backend  # noqa: F401 (a pytest fixture)
from referencing import Registry
from referencing.jsonschema import DRAFT7

from smo_shared.mns import PROVMNS_VERSION, MnsError
from smo_shared.mns.registry import BASE_URI, DEFAULT_SPEC_DIR, openapi_to_jsonschema
from smo_shared.testing import concurrent_commit_on

CELL = "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=1"
CELL_DN = CELL.replace("/", ",")
DU = "SubNetwork=A/ManagedElement=1/GNBDUFunction=1"
DU_DN = DU.replace("/", ",")


def _notification_validator(name: str) -> Draft7Validator:
    """A validator for the notification schema `name` of the vendored TS28532_ProvMnS.yaml (its `$ref`s resolved across the vendored files)."""
    def retrieve(uri):
        with (DEFAULT_SPEC_DIR / uri[len(BASE_URI):]).open() as handle:
            return DRAFT7.create_resource(openapi_to_jsonschema(yaml.safe_load(handle)))
    return Draft7Validator({"$ref": f"{BASE_URI}TS28532_ProvMnS.yaml#/components/schemas/{name}"}, registry=Registry(retrieve=retrieve))


def _error_validator(name: str) -> Draft7Validator:
    """A validator for the error schema `name` of the vendored TS28623_ComDefs.yaml."""
    with (DEFAULT_SPEC_DIR / "TS28623_ComDefs.yaml").open() as handle:
        return Draft7Validator(openapi_to_jsonschema(yaml.safe_load(handle))["components"]["schemas"][name])


def test_the_version_is_the_bundles_one():
    """D5: the router's one version constant is the version the bundle's ProvMnS root sets."""
    root = DEFAULT_SPEC_DIR.parent / "smo-schema-bundle" / "openapi" / "common" / "provmns-root.yaml"
    assert yaml.safe_load(root.read_text())["x-smo-provmns"]["version"] == PROVMNS_VERSION


def test_create_get_and_etag(backend):
    """createMOI answers 201 with the Resource and ETag "1"; getMOIAttributes reads it back with the same ETag-free hierarchical body."""
    client, _ = make_app(backend)
    build_tree(client)
    created = client.put(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=7", json={"id": "7", "attributes": {"cellLocalId": 7}})
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json() == {"id": "7", "objectClass": "NRCellDU", "objectInstance": DU_DN + ",NRCellDU=7", "attributes": {"cellLocalId": 7}}
    read = client.get(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=7")
    assert read.status_code == 200
    assert read.headers["content-type"] == "application/json"
    assert read.json() == created.json()


def test_create_at_the_top_and_replace(backend):
    """A root class is created with an empty URI-LDN-first-part; a second PUT replaces the attributes (200, ETag "2", notifyMOIAttributeValueChanges)."""
    client, _ = make_app(backend)
    assert client.put(BASE + "SubNetwork=Z", json={"id": "Z", "attributes": {"userLabel": "one"}}).status_code == 201
    replaced = client.put(BASE + "SubNetwork=Z", json={"id": "Z", "objectClass": "SubNetwork", "objectInstance": "SubNetwork=Z",
                                                       "attributes": {"dnPrefix": "x"}})
    assert replaced.status_code == 200
    assert replaced.headers["etag"] == '"2"'
    assert replaced.json()["attributes"] == {"dnPrefix": "x"}
    change = notifications(backend)[-1]
    assert change["type"] == "org.oran.smo.provmns.notifyMOIAttributeValueChanges"
    assert change["data"]["attributeListValueChanges"] == [{"dnPrefix": "x", "userLabel": None}, {"dnPrefix": None, "userLabel": "one"}]


def test_replacing_with_the_same_attributes_changes_nothing(backend):
    """A PUT with the attributes the instance already has keeps moiVersion 1 and publishes nothing."""
    client, _ = make_app(backend)
    client.put(BASE + "SubNetwork=Z", json={"id": "Z", "attributes": {"userLabel": "one"}})
    again = client.put(BASE + "SubNetwork=Z", json={"id": "Z", "attributes": {"userLabel": "one"}})
    assert (again.status_code, again.headers["etag"]) == (200, '"1"')
    assert [n["type"] for n in notifications(backend)] == ["org.oran.smo.provmns.notifyMOICreation"]


@pytest.mark.parametrize(("body", "bad"), [
    ({"attributes": {}}, ["id"]),
    ({"id": "other"}, ["id"]),
    ({"id": "Z", "objectClass": "ManagedElement"}, ["objectClass"]),
    ({"id": "Z", "objectInstance": "SubNetwork=Y"}, ["objectInstance"]),
    ({"id": "Z", "attributes": []}, ["attributes"]),
])
def test_create_refuses_a_body_for_another_instance(backend, body, bad):
    """The Resource must name the URI's instance: id, objectClass and objectInstance agree with it, attributes is an object (400, badAttributes)."""
    client, _ = make_app(backend)
    response = client.put(BASE + "SubNetwork=Z", json=body)
    assert response.status_code == 400
    assert response.json()["badAttributes"] == bad
    _error_validator("ErrorResponseDefault").validate(response.json())


def test_create_refuses_contained_objects_and_bad_json(backend):
    """createMOI makes one instance (contained objects in the body: 422); a body that is not JSON, or not an object, is a 400."""
    client, _ = make_app(backend)
    nested = client.put(BASE + "SubNetwork=Z", json={"id": "Z", "ManagedElement": [{"id": "1"}]})
    assert (nested.status_code, nested.json()["badAttributes"]) == (422, ["ManagedElement"])
    assert client.put(BASE + "SubNetwork=Z", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.put(BASE + "SubNetwork=Z", json=["Z"]).json()["type"] == "INVALID_BODY"


def test_unknown_class_is_refused_422(backend):
    """A class with no registered schema, and one the module does not serve, are 422 CLASS_NOT_SUPPORTED with the error body of the OpenAPI."""
    client, _ = make_app(backend)
    for path in ("SubNetwork=Z/NoSuchClass=1", "SubNetwork=Z/PerfMetricJob=1"):
        response = client.put(BASE + path, json={"id": "1"})
        assert response.status_code == 422
        assert response.json()["type"] == "CLASS_NOT_SUPPORTED"
        _error_validator("ErrorResponseDefault").validate(response.json())


def test_containment(backend):
    """A child's class must be one its parent's class contains (TS 28.541: NRCellDU under GNBDUFunction, not under SubNetwork or ManagedElement);
    the parent must exist; only SubNetwork and ManagedElement may stand at the top."""
    client, _ = make_app(backend)
    build_tree(client)
    wrong = client.put(BASE + "SubNetwork=A/NRCellDU=9", json={"id": "9"})
    assert (wrong.status_code, wrong.json()["type"], wrong.json()["badObjects"]) == (422, "CONTAINMENT_VIOLATION", ["SubNetwork=A,NRCellDU=9"])
    assert client.put(BASE + "SubNetwork=A/ManagedElement=1/NRCellDU=9", json={"id": "9"}).json()["type"] == "CONTAINMENT_VIOLATION"
    orphan = client.put(BASE + "SubNetwork=B/ManagedElement=1", json={"id": "1"})
    assert (orphan.status_code, orphan.json()["type"], orphan.json()["badObjects"]) == (422, "PARENT_NOT_FOUND", ["SubNetwork=B"])
    top = client.put(BASE + "GNBDUFunction=9", json={"id": "9"})
    assert (top.status_code, top.json()["type"]) == (422, "CONTAINMENT_VIOLATION")
    assert client.put(BASE + "ManagedElement=9", json={"id": "9"}).status_code == 201
    assert client.put(BASE + "SubNetwork=A/SubNetwork=A1", json={"id": "A1"}).status_code == 201


def test_schema_validation(backend):
    """Attributes are checked against the class's TS 28.541 schema: a wrong type, a value outside an enum and an attribute the class does not have
    are 422 naming the attribute; nothing is stored."""
    client, _ = make_app(backend)
    build_tree(client)
    for attributes, bad in (({"cellLocalId": "one"}, ["cellLocalId"]), ({"administrativeState": "OPEN"}, ["administrativeState"]),
                            ({"cellLocalld": 1}, ["cellLocalld"])):
        response = client.put(BASE + DU + "/NRCellDU=8", json={"id": "8", "attributes": attributes})
        assert (response.status_code, response.json()["type"], response.json()["badAttributes"]) == (422, "SCHEMA_VALIDATION_FAILED", bad)
    assert client.get(BASE + DU + "/NRCellDU=8").status_code == 404


@pytest.mark.parametrize("media_type", ["application/merge-patch+json", "application/3gpp-merge-patch+json"])
def test_modify_with_a_merge_patch(backend, media_type):
    """RFC 7396 through both merge-patch media types: a member is set, a null removes one; ETag goes to "2"; the notification carries old and new."""
    client, _ = make_app(backend)
    build_tree(client)
    response = client.patch(BASE + CELL, content=json.dumps({"attributes": {"administrativeState": "UNLOCKED", "cellLocalId": None}}),
                            headers={"content-type": media_type})
    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.json()["attributes"] == {"administrativeState": "UNLOCKED"}
    change = notifications(backend)[-1]
    assert change["subject"] == CELL_DN
    assert change["data"]["attributeListValueChanges"] == [{"administrativeState": "UNLOCKED", "cellLocalId": None},
                                                          {"administrativeState": "LOCKED", "cellLocalId": 1}]


@pytest.mark.parametrize("media_type", ["application/json-patch+json", "application/3gpp-json-patch+json"])
def test_modify_with_a_json_patch(backend, media_type):
    """RFC 6902 through both JSON-patch media types: test, replace, add and remove, applied in order."""
    client, _ = make_app(backend)
    build_tree(client)
    ops = [{"op": "test", "path": "/attributes/administrativeState", "value": "LOCKED"},
           {"op": "replace", "path": "/attributes/administrativeState", "value": "UNLOCKED"},
           {"op": "add", "path": "/attributes/arfcnDL", "value": 7}, {"op": "remove", "path": "/attributes/cellLocalId"}]
    response = client.patch(BASE + CELL, content=json.dumps(ops), headers={"content-type": f"{media_type}; charset=utf-8"})
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"administrativeState": "UNLOCKED", "arfcnDL": 7}


def test_a_failed_json_patch_changes_nothing(backend):
    """A failing operation is a 422 ErrorResponsePatch whose badOp is its index; the earlier operations are not kept; nothing is published."""
    client, _ = make_app(backend)
    build_tree(client)
    before = len(notifications(backend))
    ops = [{"op": "replace", "path": "/attributes/administrativeState", "value": "UNLOCKED"},
           {"op": "test", "path": "/attributes/cellLocalId", "value": 99}]
    response = client.patch(BASE + CELL, content=json.dumps(ops), headers={"content-type": "application/json-patch+json"})
    assert (response.status_code, response.json()["badOp"]) == (422, "1")
    _error_validator("ErrorResponsePatch").validate(response.json())
    assert client.get(BASE + CELL).json()["attributes"]["administrativeState"] == "LOCKED"
    assert len(notifications(backend)) == before


def test_a_patch_is_validated_against_the_schema(backend):
    """The patched attributes are validated like a create's: a value of the wrong type is a 422 and the stored instance is unchanged."""
    client, _ = make_app(backend)
    build_tree(client)
    response = client.patch(BASE + CELL, content=json.dumps({"attributes": {"cellLocalId": "x"}}),
                            headers={"content-type": "application/merge-patch+json"})
    assert (response.status_code, response.json()["badAttributes"]) == (422, ["cellLocalId"])
    assert client.get(BASE + CELL).headers.get("etag") is None


def test_modify_refuses_other_media_types_and_unknown_instances(backend):
    """A patch that is not one of the four media types is a 415; an instance that does not exist is a 404 MOI_NOT_FOUND."""
    client, _ = make_app(backend)
    build_tree(client)
    plain = client.patch(BASE + CELL, json={"attributes": {}})
    assert (plain.status_code, plain.json()["type"]) == (415, "UNSUPPORTED_MEDIA_TYPE")
    missing = client.patch(BASE + DU + "/NRCellDU=99", content="{}", headers={"content-type": "application/merge-patch+json"})
    assert (missing.status_code, missing.json()["type"], missing.json()["badObjects"]) == (404, "MOI_NOT_FOUND", [DU_DN + ",NRCellDU=99"])


def test_stale_version_is_refused(backend):
    """If-Match with an older moiVersion is a 412 STALE_VERSION carrying the current ETag; the current one, a weak one or `*` is accepted."""
    client, _ = make_app(backend)
    build_tree(client)
    patch = {"content-type": "application/merge-patch+json"}
    first = client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 1}}', headers={**patch, "if-match": '"1"'})
    assert first.headers["etag"] == '"2"'
    stale = client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 2}}', headers={**patch, "if-match": '"1"'})
    assert (stale.status_code, stale.json()["type"], stale.headers["etag"]) == (412, "STALE_VERSION", '"2"')
    assert client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 3}}', headers={**patch, "if-match": 'W/"2"'}).headers["etag"] == '"3"'
    assert client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 4}}', headers={**patch, "if-match": '"9", "3"'}).status_code == 200
    assert client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 5}}', headers={**patch, "if-match": "*"}).status_code == 200
    assert client.delete(BASE + CELL, headers={"if-match": '"1"'}).status_code == 412
    assert client.put(BASE + DU + "/NRCellDU=50", json={"id": "50"}, headers={"if-match": "*"}).status_code == 412


def test_a_concurrent_write_is_refused_409(backend):
    """Another writer committing between this request's read and its write (versioning.py's moiVersion check) is a 409 CONCURRENT_MODIFICATION;
    nothing of this request is kept."""
    client, _ = make_app(backend)
    build_tree(client)
    with concurrent_commit_on("moi_store", column="moi_version") as fired:
        response = client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 9}}', headers={"content-type": "application/merge-patch+json"})
    assert fired == [True]
    assert (response.status_code, response.json()["type"], response.json()["badObjects"]) == (409, "CONCURRENT_MODIFICATION", [CELL_DN])
    assert "arfcnDL" not in client.get(BASE + CELL).json()["attributes"]


def test_delete_removes_the_subtree(backend):
    """deleteMOI removes the instance and everything below it, each before its container (reverse DN order), one notifyMOIDeletion each; the rest of
    the tree stays."""
    client, _ = make_app(backend)
    build_tree(client)
    before = len(notifications(backend))
    assert client.delete(BASE + "SubNetwork=A/ManagedElement=1").status_code == 204
    deleted = [(n["subject"], n["data"].get("attributeList")) for n in notifications(backend)[before:]]
    assert [subject for subject, _ in deleted] == [DU_DN + ",NRCellDU=3", DU_DN + ",NRCellDU=2", DU_DN + ",NRCellDU=1", DU_DN,
                                                    "SubNetwork=A,ManagedElement=1"]
    assert deleted[2][1] == {"cellLocalId": 1, "administrativeState": "LOCKED"}
    assert client.get(BASE + "SubNetwork=A/ManagedElement=1").status_code == 404
    assert client.get(BASE + "SubNetwork=A/ManagedElement=2/GNBDUFunction=2/NRCellDU=4").status_code == 200
    assert client.delete(BASE + "SubNetwork=A/ManagedElement=1").json()["type"] == "MOI_NOT_FOUND"


def test_the_hooks_are_called_in_order(backend):
    """The class's handler sees before/after create, modify (also for a replacing PUT) and delete, in that order, with the DN."""
    handler = RecordingHandler()
    client, _ = make_app(backend, handler)
    build_tree(client)
    client.patch(BASE + DU, content='{"attributes": {"gnbDuId": 5}}', headers={"content-type": "application/merge-patch+json"})
    client.put(BASE + DU, json={"id": "1", "attributes": {"gnbDuId": 6}})
    client.delete(BASE + DU)
    other = "SubNetwork=A,ManagedElement=2,GNBDUFunction=2"
    assert [c for c in handler.calls if c[1] == other] == [("before_create", other), ("after_create", other)]
    assert [c for c in handler.calls if c[1] == DU_DN] == [("before_create", DU_DN), ("after_create", DU_DN), ("before_modify", DU_DN),
                                                           ("after_modify", DU_DN), ("before_modify", DU_DN), ("after_modify", DU_DN),
                                                           ("before_delete", DU_DN), ("after_delete", DU_DN)]


@pytest.mark.parametrize("hook", ["before_create", "after_create"])
def test_a_create_veto_rolls_back(backend, hook):
    """A hook that raises MnsError refuses the create with its own TS 28.532 error; nothing is stored and nothing is published."""
    client, _ = make_app(backend, RecordingHandler(veto=hook))
    for path in ("SubNetwork=A", "SubNetwork=A/ManagedElement=1"):
        client.put(BASE + path, json={"id": path.rsplit("=", 1)[1]})
    before = len(notifications(backend))
    response = client.put(BASE + DU, json={"id": "1"})
    assert (response.status_code, response.json()["type"], response.json()["badObjects"]) == (409, "VETOED", [DU_DN])
    assert client.get(BASE + DU).status_code == 404
    assert len(notifications(backend)) == before


@pytest.mark.parametrize("hook", ["before_modify", "after_modify", "before_delete", "after_delete"])
def test_a_modify_or_delete_veto_rolls_back(backend, hook):
    """A veto in a modify or delete hook leaves the instance as it was (same attributes, same ETag) and publishes nothing."""
    client, _ = make_app(backend, RecordingHandler(veto=hook))
    build_tree(client)
    before = len(notifications(backend))
    if hook.endswith("modify"):
        response = client.patch(BASE + DU, content='{"attributes": {"gnbDuId": 5}}', headers={"content-type": "application/merge-patch+json"})
    else:
        response = client.delete(BASE + "SubNetwork=A/ManagedElement=1")
    assert (response.status_code, response.json()["type"]) == (409, "VETOED")
    after = client.get(BASE + DU)
    assert after.json()["attributes"] == {"gnbDuId": 1}
    assert client.get(BASE + CELL).status_code == 200
    assert len(notifications(backend)) == before


def test_notifications_follow_the_ts28532_schemas(backend):
    """Every published notification is a valid NotifyMoiCreation, NotifyMoiAttributeValueChanges or NotifyMoiDeletion of the vendored OpenAPI, with
    the header filled in (href in D5's form, a distinct notificationId, systemDN, sourceIndicator) and the event's subject the instance's DN."""
    client, _ = make_app(backend)
    build_tree(client)
    client.patch(BASE + CELL, content='{"attributes": {"arfcnDL": 3}}', headers={"content-type": "application/merge-patch+json"})
    client.delete(BASE + CELL)
    published = notifications(backend)
    schemas = {"notifyMOICreation": "NotifyMoiCreation", "notifyMOIAttributeValueChanges": "NotifyMoiAttributeValueChanges",
               "notifyMOIDeletion": "NotifyMoiDeletion"}
    for event in published:
        body = event["data"]
        _notification_validator(schemas[body["notificationType"]]).validate(body)
        assert event["type"] == "org.oran.smo.provmns." + body["notificationType"]
        assert event["source"] == "/smo/mns-test"
        assert body["href"] == f"/ProvMnS/{PROVMNS_VERSION}/" + event["subject"].replace(",", "/")
        assert (body["systemDN"], body["sourceIndicator"]) == ("ManagementNode=mns-test", "MANAGEMENT_OPERATION")
    assert len({event["data"]["notificationId"] for event in published}) == len(published)
    assert [e["data"]["notificationType"] for e in published][-2:] == ["notifyMOIAttributeValueChanges", "notifyMOIDeletion"]
    assert published[-2]["data"]["attributeListValueChanges"] == [{"arfcnDL": 3}, {"arfcnDL": None}]


def test_a_creation_without_attributes_has_no_attribute_list(backend):
    """attributeList has at least one member (AttributeNameValuePairSet), so an instance created with none publishes a notification without it."""
    client, _ = make_app(backend)
    client.put(BASE + "SubNetwork=Q", json={"id": "Q"})
    assert "attributeList" not in notifications(backend)[-1]["data"]


def test_unknown_version_and_bad_dns(backend):
    """Another ProvMnS version is a 404 UNKNOWN_VERSION; a malformed RDN in the URI is a 400 INVALID_DN, for every operation."""
    client, _ = make_app(backend)
    assert client.get("/ProvMnS/18.4.0/SubNetwork=A").json()["type"] == "UNKNOWN_VERSION"
    for method in ("get", "delete"):
        assert getattr(client, method)(BASE + "SubNetwork=A/ManagedElement").json()["type"] == "INVALID_DN"
    assert client.put(BASE + "SubNetwork=A//ManagedElement=1", json={"id": "1"}).status_code == 400
    assert client.patch(BASE + "1SubNetwork=A", content="{}", headers={"content-type": "application/merge-patch+json"}).status_code == 400


def test_mns_error_bodies():
    """The three error shapes: the GET one carries badQueryParams only; the PATCH one carries badOp when an operation is to blame; the default one
    carries badAttributes and badObjects; status is the HTTP status as a string."""
    error = MnsError(422, "PATCH_FAILED", "why", bad_attributes=["a"], bad_objects=["o"], bad_query_params=["q"], bad_op="2")
    assert error.body("get") == {"status": "422", "type": "PATCH_FAILED", "reason": "why", "title": "A patch operation could not be applied",
                                 "badQueryParams": ["q"]}
    assert error.body("patch") == {"status": "422", "type": "PATCH_FAILED", "reason": "why", "title": "A patch operation could not be applied",
                                   "badOp": "2"}
    assert error.body("default") == {"status": "422", "type": "PATCH_FAILED", "reason": "why", "title": "A patch operation could not be applied",
                            "badAttributes": ["a"], "badObjects": ["o"]}
    assert MnsError(400, "OWN_CODE", "x").body("patch") == {"status": "400", "type": "OWN_CODE", "reason": "x", "title": "OWN_CODE"}
    assert MnsError(400, "INVALID_QUERY", "x").body("get") == {"status": "400", "type": "INVALID_QUERY", "reason": "x",
                                                                "title": "A query parameter is not valid"}
