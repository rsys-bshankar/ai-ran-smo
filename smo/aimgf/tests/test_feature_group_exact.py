"""Exact-behaviour tests of AIMgF's feature-group routes (`POST|GET|DELETE /feature-groups`), added because a mutation pass over `create_feature_group`,
`_create_feature_group_data_job` and `_terminate_feature_group_data_job` found changes no test noticed (PR-SEC-15 part 4).

They pin what the older tests only brushed: every stored field comes back, a duplicate name is refused before DME is called (not after and cleaned up), the lost-race branch cleans up its
DME job, DME's refusal is worded from a problem object, a plain string and a non-JSON answer, and the status boundaries of the data-job calls. Nothing here reads `token`. Fixtures and
helpers (`client`, `FakeResponse`, `_feature_group_body`) come from `test_main.py`. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_feature_group_exact.py -q`.
"""

import uuid

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_main import FakeResponse, _feature_group_body, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)


class CountingDme:
    """A DME double that records every call (`calls`: verb and path) and answers `post_status` / `delete_status` (with `post_payload` for a post); a status of None raises a transport error."""

    def __init__(self, post_status=202, post_payload=None, delete_status=204):
        self.calls = []
        self.post_status, self.delete_status = post_status, delete_status
        self.post_payload = post_payload if post_payload is not None else {"dataJobId": str(uuid.uuid4())}

    def post(self, path, json=None, **kw):
        self.calls.append(("post", path))
        return FakeResponse(self.post_status, self.post_payload)

    def delete(self, path, **kw):
        self.calls.append(("delete", path))
        if self.delete_status is None:
            raise httpx.ConnectError("down")
        return FakeResponse(self.delete_status, None)


@pytest.fixture
def dme(monkeypatch):
    """Replaces the R1 client's `post` and `delete` with a `CountingDme` the test can configure and inspect."""
    fake = CountingDme()
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: fake.post(path, json=json, **kw))
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: fake.delete(path, **kw))
    return fake


def _with_dme(**extra):
    """A feature-group body with `enableDme` and a type id."""
    return _feature_group_body(enableDme=True, dmeTypeId=str(uuid.uuid4()), **extra)


def test_a_group_answers_every_field_it_was_given(client):
    """Every optional and required field of the request is stored and comes back from GET (a renamed or dropped key in the view would lose one)."""
    body = _feature_group_body(measuredObjClass="NRCellDU", dmePort="9000", sourceName="cell-source")
    client.post("/feature-groups", json=body)
    got = client.get("/feature-groups/cellCounters").json()
    for key in ("featureGroupName", "featureList", "datalakeSource", "host", "port", "bucket", "dbOrg", "measurement", "measuredObjClass", "dmePort", "sourceName"):
        assert got[key] == body[key], key
    assert got["enableDme"] is False and got["dmeTypeId"] is None and got["dmeDataJobId"] is None


def test_a_duplicate_name_never_reaches_dme(client, dme):
    """A second group of the same name with `enableDme` is 409 and DME is not called at all: the check comes before the job, so it is not created and cleaned up after."""
    client.post("/feature-groups", json=_feature_group_body())
    resp = client.post("/feature-groups", json=_with_dme())
    assert resp.status_code == 409 and dme.calls == []


def test_losing_the_race_on_the_name_terminates_the_job_that_was_created(client, dme, monkeypatch):
    """When the insert itself fails on the unique name (a second request got in between the check and the commit) the DME job just created is deleted and the answer is the 409."""
    def fail_commit(self):
        raise IntegrityError("insert", {}, Exception("unique"))
    monkeypatch.setattr(Session, "commit", fail_commit)
    resp = client.post("/feature-groups", json=_with_dme())
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "FEATURE_GROUP_ALREADY_REGISTERED"
    assert [c[0] for c in dme.calls] == ["post", "delete"] and dme.calls[1][1].startswith("/dme/data-jobs/")


@pytest.mark.parametrize("status, refused", [(299, False), (300, True), (404, True)])
def test_dme_answering_300_or_more_refuses_the_group(client, dme, status, refused):
    """The boundary of DME's answer for the data job: 299 is a job, 300 is a refusal (422), whatever the body."""
    dme.post_status = status
    resp = client.post("/feature-groups", json=_with_dme())
    assert (resp.status_code == 422) is refused
    assert resp.status_code == (422 if refused else 201)


def _refusal(client, dme, status, payload):
    """Posts an `enableDme` group while DME answers `status` with `payload`; returns the `detail` text of the 422."""
    dme.post_status, dme.post_payload = status, payload
    resp = client.post("/feature-groups", json=_with_dme())
    assert resp.status_code == 422
    return resp.json()["detail"]["detail"]


def test_dme_s_reason_is_worded_from_a_problem_object_a_string_or_nothing(client, dme):
    """A problem-object reason reads `title: detail`, a string reason is used as it is, and an answer with no reason (or not JSON) says so, each after the status."""
    assert _refusal(client, dme, 409, {"detail": {"title": "NOT_OFFERED", "detail": "no offer"}}) == "DME refused the data job (409): NOT_OFFERED: no offer"
    assert _refusal(client, dme, 422, {"detail": "schema rejects it"}) == "DME refused the data job (422): schema rejects it"
    assert _refusal(client, dme, 500, {}) == "DME refused the data job (500): no reason given"

    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("not json")
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: NotJson(502, None))
        resp = client.post("/feature-groups", json=_with_dme())
    finally:
        monkey.undo()
    assert resp.json()["detail"]["detail"] == "DME refused the data job (502): no reason given"


@pytest.mark.parametrize("delete_status, outcome", [(204, "DONE"), (299, "DONE"), (404, "DONE"), (300, "FAILED: HTTP 300"), (500, "FAILED: HTTP 500"), (403, "FAILED: HTTP 403")])
def test_the_teardown_outcome_follows_the_status_of_dme_s_delete(client, dme, delete_status, outcome):
    """Deleting a group with a data job reports DONE for a 2xx and for a 404 (the job was already gone) and `FAILED: HTTP <status>` otherwise, and the group is deleted either way."""
    client.post("/feature-groups", json=_with_dme())
    dme.delete_status = delete_status
    resp = client.delete("/feature-groups/cellCounters")
    assert resp.json()["dmeDataJobTeardown"] == outcome
    assert client.get("/feature-groups/cellCounters").status_code == 404


def test_a_transport_failure_in_the_teardown_is_reported_by_its_class_and_does_not_stop_the_delete(client, dme):
    """DME unreachable when the group is deleted: the outcome names the exception class, and the group is removed anyway."""
    client.post("/feature-groups", json=_with_dme())
    dme.delete_status = None
    assert client.delete("/feature-groups/cellCounters").json()["dmeDataJobTeardown"] == "FAILED: ConnectError"
    assert client.get("/feature-groups/cellCounters").status_code == 404


# Name length boundary: 3 to 63 word characters are accepted, 2 and 64 are not.
@pytest.mark.parametrize("name, ok", [("abc", True), ("a" * 63, True), ("ab", False), ("a" * 64, False)])
def test_the_group_name_length_limits_are_three_and_sixty_three(client, name, ok):
    """A feature-group name of 3 to 63 word characters is accepted and anything shorter or longer is 400 `FEATURE_GROUP_NAME_INVALID`, whose detail says why."""
    resp = client.post("/feature-groups", json=_feature_group_body(name))
    assert resp.status_code == (201 if ok else 400)
    if not ok:
        assert resp.json()["detail"]["detail"] == f"featureGroupName {name!r} must be 3-63 word characters"


def test_enable_dme_without_a_type_says_what_is_missing(client, dme):
    """`enableDme` with no `dmeTypeId` is 422 with a detail naming the missing id, and DME is not called."""
    resp = client.post("/feature-groups", json=_feature_group_body(enableDme=True))
    assert resp.status_code == 422 and "enableDme needs the dmeTypeId the group's data job collects" in resp.json()["detail"]["detail"]
    assert dme.calls == []


def test_the_data_job_is_asked_for_with_empty_delivery_details(client, monkeypatch):
    """The data job body carries `deliveryDetails` as an empty object (DME requires the key) along with the mode, stage and consumer."""
    sent = []

    def fake_post(self, path, json=None, **kw):
        sent.append(json)
        return FakeResponse(202, {"dataJobId": str(uuid.uuid4())})
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    client.post("/feature-groups", json=_with_dme())
    assert sent[0]["deliveryDetails"] == {} and sent[0]["dataDeliveryMode"] == "CONTINUOUS" and sent[0]["lifecycleStage"] == "TRAINING"
