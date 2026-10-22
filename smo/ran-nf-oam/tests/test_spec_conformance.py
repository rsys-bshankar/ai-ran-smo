"""SA-RANOAM-1 / 2 / 4 / 6-severity / 8: TS 28.319 MSAC, accessScope, DN refs,
PerceivedSeverity and File Data Reporting. Run with: pytest smo/ran-nf-oam/tests -q
"""

import pytest

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.ldn import check_ref, leaf_class, leaf_id, parse_ldn
from app import msac


@pytest.fixture
def applied(monkeypatch):
    """Fixture: replaces the NETCONF edit with a recorder and returns the list of (element, function ref, operation) it was called with; every edit
    reports success.
    """
    calls = []
    monkeypatch.setattr("app.main.send_edit_config",
                        lambda uri, ref, changes, message_id, operation="merge", managed_function_ref=None: calls.append((ref, managed_function_ref, operation)) or True)
    return calls


def _change(**kw):
    return {"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=101", "attributeChanges": {"administrativeState": "LOCKED"}, **kw}


def _post(client, **kw):
    return client.post("/config-jobs", json={"requestedBy": "smo-gui:alice", "accessScope": "cell", "changes": [_change()], **kw})


# ---------------------------------------------------------------- SA-RANOAM-2

def test_access_scope_is_the_field_and_scope_a_deprecated_alias(client, db_session_factory, applied):
    """`accessScope` is the field and `scope` a deprecated alias: either is accepted and the job shows both with the same value."""
    _make_me(db_session_factory)
    assert _post(client).status_code == 202
    legacy = client.post("/config-jobs", json={"requestedBy": "x", "scope": "cell", "changes": [_change()]})
    assert legacy.status_code == 202
    jobs = client.get("/config-jobs").json()["items"]
    assert all(j["accessScope"] == "cell" and j["scope"] == "cell" for j in jobs)


def test_scope_and_access_scope_must_agree_and_one_is_required(client, db_session_factory, applied):
    """A request with disagreeing `scope` and `accessScope`, or with neither, is 422."""
    _make_me(db_session_factory)
    assert client.post("/config-jobs", json={"requestedBy": "x", "scope": "a", "accessScope": "b", "changes": []}).status_code == 422
    assert client.post("/config-jobs", json={"requestedBy": "x", "changes": []}).status_code == 422


# ---------------------------------------------------------------- SA-RANOAM-4

def test_ldn_parsing():
    """DN parsing splits `Class=id` RDNs, reads the class and id of the last RDN, passes a flat id through and refuses malformed DNs."""
    assert parse_ldn("SubNetwork=A,ManagedElement=ME-1,NRCellDU=101") == [("SubNetwork", "A"), ("ManagedElement", "ME-1"), ("NRCellDU", "101")]
    assert leaf_class("SubNetwork=A,NRCellDU=101") == "NRCellDU" and leaf_id("SubNetwork=A,NRCellDU=101") == "101"
    assert leaf_class("ME-1") is None and leaf_id("ME-1") == "ME-1" and check_ref("ME-1") == "ME-1"
    for bad in ("NRCellDU=", "=101", "NRCellDU=1,oops", "9Cell=1"):
        with pytest.raises(ValueError):
            check_ref(bad)


def test_a_malformed_dn_is_refused_and_a_dn_ref_is_accepted(client, db_session_factory, applied):
    """A malformed DN function ref is 422 on a write, and a well-formed DN is accepted and sent to the adaptor as given."""
    _make_me(db_session_factory)
    bad = client.post("/config-jobs", json={"requestedBy": "x", "accessScope": "cell", "changes": [_change(managedFunctionRef="NRCellDU=,x")]})
    assert bad.status_code == 422
    ok = client.post("/config-jobs", json={"requestedBy": "x", "accessScope": "cell",
                                           "changes": [_change(managedFunctionRef="SubNetwork=A,GNBDUFunction=1,NRCellDU=101")]})
    assert ok.status_code == 202 and applied[-1][1] == "SubNetwork=A,GNBDUFunction=1,NRCellDU=101"


def test_alarms_carry_the_dn_class_and_match_an_rdn_suffix(client, db_session_factory):
    """An alarm shows the class and id of its function DN, can be found by the last RDN alone, and a malformed function ref on ingest is 422."""
    _make_me(db_session_factory)
    dn = "SubNetwork=A,GNBDUFunction=1,NRCellDU=101"
    client.post("/alarms/ingest", params={"source_alarm_id": "a", "managed_element_ref": "ME-1", "severity": "major", "managed_function_ref": dn})
    client.post("/alarms/ingest", params={"source_alarm_id": "b", "managed_element_ref": "ME-1", "severity": "major", "managed_function_ref": "NRCellDU=202"})
    by_suffix = client.get("/alarms", params={"managed_function_ref": "NRCellDU=101"}).json()["items"]
    assert [a["sourceAlarmId"] for a in by_suffix] == ["a"]
    assert (by_suffix[0]["managedFunctionClass"], by_suffix[0]["managedFunctionId"]) == ("NRCellDU", "101")
    assert client.post("/alarms/ingest", params={"source_alarm_id": "c", "managed_element_ref": "ME-1", "severity": "major",
                                                 "managed_function_ref": "NRCellDU="}).status_code == 422


# ---------------------------------------------------------------- SA-RANOAM-6-severity

def test_severity_accepts_either_case_and_indeterminate_and_exposes_perceived_severity(client, db_session_factory):
    """Alarm severity is accepted in either case including INDETERMINATE, stored lower-case, and every view also gives the upper-case
    `perceivedSeverity`.
    """
    _make_me(db_session_factory)
    for sev in ("INDETERMINATE", "Critical", "warning"):
        assert client.post("/alarms/ingest", params={"source_alarm_id": sev, "managed_element_ref": "ME-1", "severity": sev}).status_code == 200
    items = {a["sourceAlarmId"]: a for a in client.get("/alarms").json()["items"]}
    assert items["INDETERMINATE"]["perceivedSeverity"] == "INDETERMINATE" and items["INDETERMINATE"]["severity"] == "indeterminate"
    assert items["Critical"]["perceivedSeverity"] == "CRITICAL"
    assert [a["sourceAlarmId"] for a in client.get("/alarms", params={"severity": "INDETERMINATE"}).json()["items"]] == ["INDETERMINATE"]


def test_an_unknown_severity_is_a_422_not_a_database_error(client, db_session_factory):
    """A severity that is not a PerceivedSeverity is 422 on ingest and on the filter, not a database error."""
    _make_me(db_session_factory)
    r = client.post("/alarms/ingest", params={"source_alarm_id": "x", "managed_element_ref": "ME-1", "severity": "SEVERE"})
    assert r.status_code == 422 and "PerceivedSeverity" in r.json()["detail"]["detail"]
    assert client.get("/alarms", params={"severity": "SEVERE"}).status_code == 422


def test_cleared_alarm_reports_cleared(client, db_session_factory):
    """A cleared alarm reports `perceivedSeverity` CLEARED."""
    _make_me(db_session_factory)
    alarm = client.post("/alarms/ingest", params={"source_alarm_id": "x", "managed_element_ref": "ME-1", "severity": "CRITICAL"}).json()
    assert client.patch(f"/alarms/{alarm['alarmId']}/clear").json()["perceivedSeverity"] == "CLEARED"


# ---------------------------------------------------------------- SA-RANOAM-1 (TS 28.319)

def _rule(client, selector, operations, action="ALLOW", name="r"):
    r = client.post("/msac/access-rules", json={"ruleName": name, "dataNodeSelector": selector, "operations": operations, "actions": action})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _role(client, name, rules):
    return client.post("/msac/roles", json={"roleName": name, "accessRulesList": rules}).json()["id"]


def _identity(client, name, roles, credential="s3cret"):
    return client.post("/msac/identities", json={"identityType": "USERNAME", "identityName": name, "credential": credential, "roleList": roles})


def test_msac_resources_use_spec_names_and_never_return_the_credential(client):
    """The MSAC resources use the TS 28.319 attribute names, and the credential is never returned."""
    rule = _rule(client, "/ManagedElement=ME-1/*", ["update", "read"])
    role = _role(client, "cell-operator", [rule])
    created = _identity(client, "smo-gui:alice", [role]).json()
    assert created["attributes"] == {"identityType": "USERNAME", "identityName": "smo-gui:alice", "roleList": [role]}
    assert "credential" not in str(client.get("/msac/identities").json())
    assert client.get(f"/msac/roles/{role}").json()["attributes"] == {"roleName": "cell-operator", "accessRulesList": [rule]}
    assert client.get(f"/msac/access-rules/{rule}").json()["attributes"]["operations"] == ["read", "update"]


def test_credentials_are_hashed(client, db_session_factory):
    """An identity's credential is stored only as a salted hash that verifies the right secret and not a wrong one."""
    from app.models import MsacIdentity
    _identity(client, "bob", [], credential="hunter2")
    row = db_session_factory().query(MsacIdentity).one()
    assert "hunter2" not in row.credential_hash and msac.check_credential("hunter2", row.credential_hash)
    assert not msac.check_credential("wrong", row.credential_hash)


def test_msac_refuses_dangling_refs_duplicates_and_unsupported_selectors(client):
    """MSAC refuses a role or identity that lists an object that does not exist, a duplicate role name, a selector it cannot evaluate and an
    unknown operation (422); an unknown role is 404.
    """
    ghost = "00000000-0000-0000-0000-000000000000"
    assert client.post("/msac/roles", json={"roleName": "r", "accessRulesList": [ghost]}).status_code == 422
    assert _identity(client, "x", [ghost]).status_code == 422
    role = _role(client, "dup", [])
    assert client.post("/msac/roles", json={"roleName": "dup"}).status_code == 422
    for bad in ("//ManagedElement[@id='1']", "ManagedElement=ME-1", "/notaselector"):
        assert client.post("/msac/access-rules", json={"ruleName": "r", "dataNodeSelector": bad, "operations": ["read"], "actions": "ALLOW"}).status_code == 422
    assert client.post("/msac/access-rules", json={"ruleName": "r", "dataNodeSelector": "/*", "operations": ["fly"], "actions": "ALLOW"}).status_code == 422
    assert client.get(f"/msac/roles/{ghost}").status_code == 404 and role


def test_an_identity_may_write_only_what_its_roles_allow(client, db_session_factory, applied):
    """A registered identity may write only what its roles' rules allow: another class or another operation (delete when only update is allowed) is
    403.
    """
    _make_me(db_session_factory)
    allow = _rule(client, "/ManagedElement=ME-1/NRCellDU=*", ["update"])
    _identity(client, "smo-gui:alice", [_role(client, "cell-operator", [allow])])
    assert _post(client).status_code == 202 and applied
    other = _change(managedFunctionRef="GNBDUFunction=1")  # not a cell
    denied = client.post("/config-jobs", json={"requestedBy": "smo-gui:alice", "accessScope": "cell", "changes": [other]})
    assert denied.status_code == 403 and "GNBDUFunction=1" in denied.json()["detail"]["detail"]
    delete = client.post("/config-jobs", json={"requestedBy": "smo-gui:alice", "accessScope": "cell", "changes": [_change(operation="delete")]})
    assert delete.status_code == 403  # update is allowed, delete is not


def test_one_denied_sub_change_dispatches_nothing(client, db_session_factory, applied):
    """One denied sub-change refuses the whole job: nothing is dispatched and no job is made."""
    _make_me(db_session_factory)
    allow = _rule(client, "/ManagedElement=ME-1/NRCellDU=101", ["update"])
    _identity(client, "alice", [_role(client, "one-cell", [allow])])
    r = client.post("/config-jobs", json={"requestedBy": "alice", "accessScope": "cell",
                                          "changes": [_change(), _change(managedFunctionRef="NRCellDU=202")]})
    assert r.status_code == 403 and applied == []
    assert client.get("/config-jobs").json()["items"] == []


def test_deny_beats_allow_and_no_matching_rule_is_a_refusal(client, db_session_factory, applied):
    """A matching DENY beats an ALLOW, and an identity with no matching rule is refused."""
    _make_me(db_session_factory)
    allow = _rule(client, "/ManagedElement=ME-1/*", ["update"])
    deny = _rule(client, "/ManagedElement=ME-1/NRCellDU=101", ["update"], action="DENY", name="freeze-101")
    _identity(client, "alice", [_role(client, "r", [allow, deny])])
    assert client.post("/config-jobs", json={"requestedBy": "alice", "accessScope": "cell", "changes": [_change()]}).status_code == 403
    _identity(client, "nobody", [])
    assert client.post("/config-jobs", json={"requestedBy": "nobody", "accessScope": "cell", "changes": [_change()]}).status_code == 403


def test_msac_role_alone_selects_a_defined_role(client, db_session_factory, applied):
    """A requester with no identity can name a defined role in `msacRole` to be evaluated against it."""
    _make_me(db_session_factory)
    allow = _rule(client, "/*", ["update", "create", "delete"])
    _role(client, "admin", [allow])
    r = client.post("/config-jobs", json={"requestedBy": "unregistered-service", "accessScope": "entire-RAN", "msacRole": "admin", "changes": [_change()]})
    assert r.status_code == 202


def test_a_requester_with_no_identity_or_defined_role_keeps_the_legacy_gate(client, db_session_factory, applied):
    """A requester with neither an identity nor a defined role keeps the legacy gate: entire-RAN scope needs a named `msacRole`, whose existence is
    not checked.
    """
    _make_me(db_session_factory)
    entire = {"requestedBy": "svc", "accessScope": "entire-RAN", "changes": [_change()]}
    assert client.post("/config-jobs", json=entire).status_code == 403
    assert client.post("/config-jobs", json={**entire, "msacRole": "admin"}).status_code == 202  # no Role "admin" defined: presence only


def test_dn_targets_match_dn_selectors(client, db_session_factory, applied):
    """A DN-keyed element and function match a DN selector, and the target path is built from the DNs."""
    _make_me(db_session_factory)
    allow = _rule(client, "/SubNetwork=A/*/NRCellDU=*", ["update"])
    _identity(client, "alice", [_role(client, "r", [allow])])
    ok = client.post("/config-jobs", json={"requestedBy": "alice", "accessScope": "cell", "changes": [
        {"managedElementRef": "SubNetwork=A,ManagedElement=ME-1", "managedFunctionRef": "NRCellDU=101", "attributeChanges": {}}]})
    assert ok.status_code in (202, 422)  # authorised; the unknown ME is not a MSAC matter
    assert msac.target_path("SubNetwork=A,ManagedElement=ME-1", "NRCellDU=101") == "/SubNetwork=A/ManagedElement=ME-1/NRCellDU=101"


def test_deleting_a_rule_or_role_unlists_it(client):
    """Deleting a rule removes it from the roles that listed it and deleting a role removes it from identities; deletes are idempotent (204)."""
    rule = _rule(client, "/*", ["read"])
    role = _role(client, "r", [rule])
    identity = _identity(client, "alice", [role]).json()["id"]
    assert client.delete(f"/msac/access-rules/{rule}").status_code == 204
    assert client.get(f"/msac/roles/{role}").json()["attributes"]["accessRulesList"] == []
    assert client.delete(f"/msac/roles/{role}").status_code == 204
    assert client.get(f"/msac/identities/{identity}").json()["attributes"]["roleList"] == []
    assert client.delete(f"/msac/identities/{identity}").status_code == 204
    assert client.delete(f"/msac/identities/{identity}").status_code == 204  # idempotent


def test_replacing_a_role_and_an_identity(client):
    """A PUT replaces a role's name and rules and an identity's type and roles."""
    r1, r2 = _rule(client, "/*", ["read"]), _rule(client, "/*", ["update"], name="w")
    role = _role(client, "r", [r1])
    assert client.put(f"/msac/roles/{role}", json={"roleName": "r2", "accessRulesList": [r2]}).json()["attributes"]["accessRulesList"] == [r2]
    ident = _identity(client, "alice", []).json()["id"]
    out = client.put(f"/msac/identities/{ident}", json={"identityType": "MACHINEUSER", "identityName": "alice", "roleList": [role]}).json()
    assert out["attributes"]["identityType"] == "MACHINEUSER" and out["attributes"]["roleList"] == [role]


# ---------------------------------------------------------------- SA-RANOAM-8 (file data reporting)

class _Resp:
    status_code = 200            # an accepted call (call flow 11a reads the producer registration's answer)
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture
def dme(monkeypatch):
    """Fixture: simulates DME through the R1 client (one type and one open data job for `RAN.PMCounters.PRB_UTILIZATION`); returns the list of
    posts made as (path, json).
    """
    posted = []
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: _Resp(
        [{"dmeTypeId": "t-1", "typeName": "RAN.PMCounters.PRB_UTILIZATION"}] if path == "/dme/dme-types" else {"items": [{"dataJobId": "j-1"}]}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posted.append((path, json)) or _Resp({}))
    return posted


FILE = {"managedElementRef": "ME-1", "counterType": "PRB_UTILIZATION",
        "measurements": [{"cellId": "101", "value": 3.2, "timestamp": "2026-01-01T00:00:00Z"}]}


def _subscribe_pm(client):
    client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": "PRB_UTILIZATION", "delivery_method": "pull"})


def test_a_pm_file_is_stored_listed_downloaded_and_fed_to_dme(client, db_session_factory, dme):
    """A PM file needs a PM subscription (422 without one), is then stored, listed in `/files`, downloadable, and its measurements are delivered to
    DME.
    """
    _make_me(db_session_factory)
    assert client.post("/pm-files", json=FILE).status_code == 422  # no PM subscription yet
    _subscribe_pm(client)
    dme.clear()
    out = client.post("/pm-files", json={**FILE, "jobId": "job-7"}).json()
    assert out["fileLocation"] == f"/ran-nf-oam/pm-files/{out['fileId']}/file" and out["fileDataType"] == "Performance"
    assert out["recordsDelivered"] == 1 and dme[0][0] == "/dme/data-jobs/j-1/records"
    listing = client.get("/files", params={"fileDataType": "Performance"}).json()["items"]
    assert [f["jobId"] for f in listing] == ["job-7"] and listing[0]["fileSize"] == out["fileSize"]
    assert client.get("/files", params={"fileDataType": "Trace"}).json()["items"] == []
    assert client.get("/files", params={"fileDataType": "Performance", "beginTime": "2999-01-01T00:00:00Z"}).json()["items"] == []
    body = client.get(f"/pm-files/{out['fileId']}/file").json()
    assert body["measurements"][0]["cellId"] == "101" and body["counterType"] == "PRB_UTILIZATION"


def test_file_ready_is_notified_to_matching_subscriptions(client, db_session_factory, dme, monkeypatch):
    """A new PM file queues `notifyFileReady` for each matching file subscription (all types, or the file's type), with increasing sequence
    numbers.
    """
    _make_me(db_session_factory)
    _subscribe_pm(client)
    sent = []
    import httpx
    # PM-file notifications are outbox rows (PR-MSG-1.9), delivered through smo_shared.webhook right after the commit
    monkeypatch.setattr("smo_shared.webhook.post_webhook", lambda dest, json, timeout=5.0: sent.append((dest, json)) or httpx.Response(200))
    all_types = client.post("/file-subscriptions", json={"consumerReference": "http://consumer/a", "timeTick": 5}).json()
    client.post("/file-subscriptions", json={"consumerReference": "http://consumer/trace", "fileDataType": "Trace"})
    out = client.post("/pm-files", json=FILE).json()
    assert out["notified"] == 1 and [d for d, _ in sent] == ["http://consumer/a"]
    note = sent[0][1]
    assert note["notificationType"] == "notifyFileReady" and note["subscriptionId"] == all_types["subscriptionId"]
    assert note["sequenceNo"] == 1 and note["fileInfoList"][0]["fileLocation"] == out["fileLocation"]
    client.post("/pm-files", json=FILE)
    assert sent[-1][1]["sequenceNo"] == 2
    assert client.delete(f"/file-subscriptions/{all_types['subscriptionId']}").status_code == 204
    sent.clear()
    assert client.post("/pm-files", json=FILE).json()["notified"] == 0 and sent == []


def test_file_subscription_refuses_an_unsupported_filter(client):
    """A file subscription with a `filter` (not supported) is 422."""
    r = client.post("/file-subscriptions", json={"consumerReference": "http://c", "filter": "//x"})
    assert r.status_code == 422


def test_an_expired_or_unknown_file_is_404(client, db_session_factory, dme):
    """Downloading an expired or unknown file is 404."""
    _make_me(db_session_factory)
    _subscribe_pm(client)
    out = client.post("/pm-files", json={**FILE, "fileExpirationTime": "2000-01-01T00:00:00Z"}).json()
    assert client.get(f"/pm-files/{out['fileId']}/file").status_code == 404
    assert client.get("/pm-files/00000000-0000-0000-0000-000000000000/file").status_code == 404


def test_pm_files_need_the_file_service(client, db_session_factory, dme):
    """A PM file for an element whose vendor does not offer the FILE service is 409 O1_SERVICE_NOT_SUPPORTED."""
    _make_me(db_session_factory)
    client.put("/vendor-capabilities/acme", json={"supportedServices": ["PROV", "PM"], "conformanceMode": "SPEC", "supportedVendorModes": ["O1_NETCONF"]})
    from app.models import ManagedEntity
    db = db_session_factory()
    db.get(ManagedEntity, "ME-1").vendor_name = "acme"
    db.commit()
    _subscribe_pm(client)
    assert client.post("/pm-files", json=FILE).status_code == 409  # O1_SERVICE_NOT_SUPPORTED


def test_a_file_ready_notification_survives_a_crash_between_commit_and_send(client, db_session_factory, dme, monkeypatch):
    """The crash test of MSG-1.9 for RAN NF OAM: the file, the subscription's new sequence number and the notification row are one
    committed transaction; with the inline send off (the process died after the commit) nothing went out, and a later drain delivers it."""
    import httpx
    from smo_shared import outbox
    from smo_shared.outbox import NotificationOutbox
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("MODULE", "ran-nf-oam")
    _make_me(db_session_factory)
    _subscribe_pm(client)
    sent = []
    monkeypatch.setattr("smo_shared.webhook.post_webhook", lambda dest, json, timeout=5.0: sent.append((dest, json)) or httpx.Response(200))
    sub = client.post("/file-subscriptions", json={"consumerReference": "http://consumer/a", "timeTick": 5}).json()

    out = client.post("/pm-files", json=FILE).json()

    assert sent == []
    with db_session_factory() as db:
        rows = db.query(NotificationOutbox).all()
    assert [(r.module, r.status, r.destination) for r in rows] == [("ran-nf-oam", "PENDING", "http://consumer/a")]
    assert rows[0].payload["subscriptionId"] == sub["subscriptionId"] and rows[0].payload["sequenceNo"] == 1
    assert rows[0].payload["fileInfoList"][0]["fileLocation"] == out["fileLocation"]

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")
    assert outbox.drain(db_session_factory().get_bind())["sent"] == 1
    assert [d for d, _ in sent] == ["http://consumer/a"]
