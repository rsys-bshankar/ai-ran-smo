"""MGT-2: MSAC beyond CM writes (`RAN_NF_OAM_MSAC_REACH`): config reads, PM and FM subscriptions, alarm acknowledge and clear, software-management jobs and the file
routes are checked against the caller's access rules, when the switch is on and the caller is a registered Identity. Run with: pytest tests/test_msac_reach.py -q
"""

import uuid

import pytest
from types import SimpleNamespace

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.models import Alarm, PMFile, SoftwareManagementJob

ME = "ME-1"
CELL = "NRCellDU=101"



# An R1 answer DME accepted (call flow 11a: the producer registration's answer is read now).
ACCEPTED = SimpleNamespace(status_code=201, json=lambda: {})

def rule(client, selector, operations, action="ALLOW"):
    return client.post("/msac/access-rules", json={"ruleName": f"r-{uuid.uuid4().hex[:6]}", "dataNodeSelector": selector, "operations": operations, "actions": action}).json()["id"]


def identity(client, name, *rules):
    """Creates a role holding the given access rules and a MACHINEUSER identity of that name with that role."""
    role = client.post("/msac/roles", json={"roleName": f"role-{name}", "accessRulesList": list(rules)}).json()["id"]
    assert client.post("/msac/identities", json={"identityType": "MACHINEUSER", "identityName": name, "roleList": [role]}).status_code == 201


def as_(name):
    return {"X-R1-Invoker-Id": name}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("RAN_NF_OAM_MSAC_REACH", "on")


@pytest.fixture
def world(client, db_session_factory):
    """ME-1 with an endpoint, an alarm, a file and a snapshot; `reader` may read ME-1 only, `nobody` has an identity with no rule, `updater` may update it."""
    _make_me(db_session_factory)
    with db_session_factory() as db:
        alarm = Alarm(source_alarm_id="a", managed_element_ref=ME, managed_function_ref=CELL, severity="major")
        file = PMFile(managed_element_ref=ME, counter_type="C", file_data_type="Performance", file_format="json", content="{}", file_size=2)
        other = PMFile(managed_element_ref="ME-2", counter_type="C", file_data_type="Performance", file_format="json", content="{}", file_size=2)
        db.add_all([alarm, file, other])
        db.commit()
        ids = {"alarm": alarm.alarm_id, "file": file.file_id, "other": other.file_id}
    identity(client, "reader", rule(client, "/ManagedElement=ME-1/*", ["read"]), rule(client, "/ManagedElement=ME-1", ["read"]))
    identity(client, "nobody")
    identity(client, "updater", rule(client, "/ManagedElement=ME-1/*", ["update", "exec"]), rule(client, "/ManagedElement=ME-1", ["update", "exec"]))
    identity(client, "denied", rule(client, "/*", ["read", "update", "exec"]), rule(client, "/ManagedElement=ME-1/*", ["read", "update", "exec"], "DENY"),
             rule(client, "/ManagedElement=ME-1", ["read", "update", "exec"], "DENY"))
    return ids


def denied(response):
    """Asserts a 403 MSAC_ACCESS_DENIED response and returns its detail text."""
    assert response.status_code == 403 and response.json()["detail"]["title"] == "MSAC_ACCESS_DENIED", response.text
    return response.json()["detail"]["detail"]


CALLS = [
    ("config", lambda c, h, w: c.get(f"/managed-entities/{ME}/config", headers=h)),
    ("config of a function", lambda c, h, w: c.get(f"/managed-entities/{ME}/config", params={"managed_function_ref": CELL}, headers=h)),
    ("config history", lambda c, h, w: c.get(f"/managed-entities/{ME}/config-history", headers=h)),
    ("config diff", lambda c, h, w: c.get(f"/managed-entities/{ME}/config-history/diff", params={"from_snapshot": str(uuid.uuid4()), "to_snapshot": str(uuid.uuid4())}, headers=h)),
    ("pm subscription", lambda c, h, w: c.post("/pm-subscriptions", params={"managed_element_ref": ME, "counter_type": "C", "delivery_method": "pull"}, headers=h)),
    ("fm subscription", lambda c, h, w: c.post("/fm-subscriptions", params={"managed_element_ref": ME, "delivery_method": "pull"}, headers=h)),
    ("alarm ack", lambda c, h, w: c.patch(f"/alarms/{w['alarm']}/ack", params={"new_state": "ACKNOWLEDGED"}, headers=h)),
    ("alarm clear", lambda c, h, w: c.patch(f"/alarms/{w['alarm']}/clear", headers=h)),
    ("software job", lambda c, h, w: c.post("/software-management-jobs", params={"managed_element_ref": ME}, headers=h)),
    ("file download", lambda c, h, w: c.get(f"/pm-files/{w['file']}/file", headers=h)),
]
NEEDS = {"config": "read", "config of a function": "read", "config history": "read", "config diff": "read", "pm subscription": "read", "fm subscription": "read",
         "alarm ack": "update", "alarm clear": "update", "software job": "exec", "file download": "read"}


@pytest.mark.parametrize("name,call", CALLS, ids=[c[0] for c in CALLS])
def test_a_managed_caller_without_a_rule_is_refused_and_one_with_the_rule_is_not(client, world, on, monkeypatch, name, call):
    """For each guarded route (the CALLS table), a managed caller with no rule is refused, a DENY rule beats an ALLOW, a caller with the right rule gets through, and a rule for a different operation is not enough.
    """
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: ACCEPTED)
    assert denied(call(client, as_("nobody"), world)).startswith("nobody is not permitted: ")
    assert f"{NEEDS[name]} /ManagedElement=ME-1" in denied(call(client, as_("denied"), world))                    # DENY beats ALLOW
    allowed = "reader" if NEEDS[name] == "read" else "updater"
    answer = call(client, as_(allowed), world)
    assert answer.status_code != 403, answer.text
    wrong = "updater" if NEEDS[name] == "read" else "reader"
    denied(call(client, as_(wrong), world))                                                                         # the rule names an operation: another one is not enough


@pytest.mark.parametrize("name,call", CALLS, ids=[c[0] for c in CALLS])
def test_nothing_changes_until_the_switch_is_on(client, world, monkeypatch, name, call):
    """With `RAN_NF_OAM_MSAC_REACH` unset no guarded route asks the access rules, so an upgrade changes nothing by itself."""
    monkeypatch.delenv("RAN_NF_OAM_MSAC_REACH", raising=False)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: ACCEPTED)
    assert call(client, as_("nobody"), world).status_code != 403


@pytest.mark.parametrize("name,call", CALLS, ids=[c[0] for c in CALLS])
def test_a_caller_that_is_not_a_registered_identity_is_not_asked(client, world, on, monkeypatch, name, call):
    """A caller with no registered Identity (an unknown rApp, or a call that did not come through the gateway) is not asked, as for writes."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: ACCEPTED)
    assert call(client, as_("stranger"), world).status_code != 403            # an rApp or tool with no Identity: as for writes, no rule applies
    assert call(client, {}, world).status_code != 403                          # a call that did not come through the gateway


def test_the_target_decides_not_the_caller_alone(client, world, on):
    """A rule for ME-1 does not let the caller read ME-2."""
    assert client.get(f"/managed-entities/{ME}/config", headers=as_("reader")).status_code != 403
    with_other = client.get("/managed-entities/ME-2/config", headers=as_("reader"))
    assert "read /ManagedElement=ME-2" in denied(with_other)


def test_a_rule_on_one_function_does_not_let_the_caller_read_the_element(client, world, on):
    """A rule on the cells of an element lets the caller read a cell but not the element's own configuration."""
    identity(client, "cell-only", rule(client, "/ManagedElement=ME-1/NRCellDU=*", ["read"]))
    assert client.get(f"/managed-entities/{ME}/config", params={"managed_function_ref": CELL}, headers=as_("cell-only")).status_code != 403
    denied(client.get(f"/managed-entities/{ME}/config", headers=as_("cell-only")))


def test_a_malformed_reference_is_a_422_for_a_managed_caller_not_a_crash(client, world, on):
    """A malformed DN from a managed caller is a 422, not an error in the rule evaluation."""
    assert client.get("/managed-entities/Bad=/config", headers=as_("reader")).status_code == 422


def test_an_alarm_that_does_not_exist_is_still_a_404(client, world, on):
    """An unknown alarm is 404 for a managed caller too: existence is checked before the rules."""
    assert client.patch(f"/alarms/{uuid.uuid4()}/clear", headers=as_("nobody")).status_code == 404


def test_a_refused_alarm_change_leaves_the_alarm_as_it_was(client, db_session_factory, world, on):
    """A refused alarm clear changes nothing, and the same call by an identity with the update right clears it."""
    denied(client.patch(f"/alarms/{world['alarm']}/clear", headers=as_("reader")))
    with db_session_factory() as db:
        alarm = db.get(Alarm, world["alarm"])
        assert alarm.severity == "major" and alarm.cleared_at is None and alarm.ack_state == "UNACKNOWLEDGED"
    assert client.patch(f"/alarms/{world['alarm']}/clear", headers=as_("updater")).json()["severity"] == "cleared"


def test_a_refused_software_job_creates_nothing(client, db_session_factory, world, on):
    """A software job refused by the rules creates no job row."""
    denied(client.post("/software-management-jobs", params={"managed_element_ref": ME}, headers=as_("reader")))
    with db_session_factory() as db:
        assert db.query(SoftwareManagementJob).count() == 0


def test_the_file_list_leaves_out_the_files_of_elements_the_caller_may_not_read(client, world, on):
    """The file list is filtered to the elements the caller may read (not refused), and a download of an unreadable file is 403."""
    everything = client.get("/files", params={"fileDataType": "Performance"}).json()["total"]
    assert everything == 2
    seen = client.get("/files", params={"fileDataType": "Performance"}, headers=as_("reader")).json()
    assert seen["total"] == 1 and seen["items"][0]["fileLocation"].endswith(f"{world['file']}/file")
    assert client.get("/files", params={"fileDataType": "Performance"}, headers=as_("nobody")).json()["total"] == 0
    assert client.get("/files", params={"fileDataType": "Performance"}, headers=as_("stranger")).json()["total"] == 2
    denied(client.get(f"/pm-files/{world['other']}/file", headers=as_("reader")))


def test_a_file_subscription_needs_the_right_on_the_whole_network(client, world, on):
    """A file subscription is sent every file's notice, so it needs `read` on the root: a caller allowed on one element is refused, one allowed on everything or not managed is not.
    """
    body = {"consumerReference": "http://consumer.example/notify", "fileDataType": "Performance", "timeTick": 10}
    assert "read /" in denied(client.post("/file-subscriptions", json=body, headers=as_("reader")))              # allowed on ME-1 only
    identity(client, "everything", rule(client, "/*", ["read"]))
    assert client.post("/file-subscriptions", json=body, headers=as_("everything")).status_code == 201
    assert client.post("/file-subscriptions", json=body, headers=as_("stranger")).status_code == 201


def test_a_role_named_by_the_caller_cannot_widen_a_read(client, world, on):
    """Writes take `msacRole` from the request; reads do not, so there is nothing to send to pick a wider role."""
    wide = rule(client, "/*", ["read"])
    client.post("/msac/roles", json={"roleName": "wide", "accessRulesList": [wide]})
    response = client.get("/managed-entities/ME-2/config", params={"msac_role": "wide"}, headers=as_("reader"))
    denied(response)


def test_the_switch_reads_the_usual_spellings(client, world, monkeypatch):
    """The switch is on for 1, true, yes and on in any case, and off for an empty value, 0, off and no."""
    for value, expect in (("1", True), ("true", True), ("YES", True), ("on", True), ("", False), ("0", False), ("off", False), ("no", False)):
        monkeypatch.setenv("RAN_NF_OAM_MSAC_REACH", value)
        assert (client.get(f"/managed-entities/{ME}/config", headers=as_("nobody")).status_code == 403) is expect, value


def test_the_writes_are_unchanged(client, world, on, monkeypatch):
    """The write path keeps its own check (requestedBy, msacRole) whatever the caller's header says."""
    body = {"requestedBy": "nobody", "accessScope": "single-element", "changes": [{"managedElementRef": ME, "managedFunctionRef": CELL, "attributeChanges": {"administrativeState": "LOCKED"}}]}
    assert denied(client.post("/config-jobs", json=body)).startswith("nobody is not permitted")

