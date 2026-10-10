"""Route-level tests for RAN NF OAM SMOS's WriteConfigurationChanges
(RAN NF OAM LLD section 5.1) — the actual NETCONF dispatch, previously
elided behind a comment that recorded every sub_change as APPLIED without
dispatching anything. Run with: pytest smo/ran-nf-oam/tests -q
"""

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared.db import Base, get_session
from smo_shared.testing import make_test_engine
from smo_shared.testing import concurrent_commit_on
from smo_shared.idempotency import IdempotencyKey
from smo_shared import outbox
from smo_shared.outbox import NotificationOutbox

from app.main import app
from smo_shared.audit import AuditEntry, AuditHead
from app.models import AlarmComment, AlarmHistory, ElementOnboarding, LifecycleSubscription, OnboardingTemplate, SoftwareCampaign, ApprovalSubscription, RAppActionApproval, RAppApprovalPolicy, RAppDecisionRecord, KpiDefinition, KpiSchedule, RAppKill, SafeguardRefusal, SafeguardSubscription, RAppLimit, ManagedObject, Alarm, CMSchemaCache, CMSnapshot, FMSubscription, FileSubscription, ManagedEntity, MsacAccessRule, MsacIdentity, MsacRole, PMFile, O1AdaptorEndpoint, PMSubscription, SoftwareManagementJob, VendorCapability, WriteConfigJob, WriteConfigSubChange


@pytest.fixture
def db_session_factory():
    """Fixture: a SQLite session factory with every table the app's routes touch, created from the ORM models. Other test files import it (and `client`) from here.
    """
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[
        O1AdaptorEndpoint.__table__, ManagedEntity.__table__, Alarm.__table__, AlarmHistory.__table__, AlarmComment.__table__, CMSchemaCache.__table__,
        WriteConfigJob.__table__, WriteConfigSubChange.__table__, CMSnapshot.__table__, PMSubscription.__table__, FMSubscription.__table__, SoftwareManagementJob.__table__,
        VendorCapability.__table__, MsacIdentity.__table__, MsacRole.__table__, MsacAccessRule.__table__, PMFile.__table__,
        FileSubscription.__table__, IdempotencyKey.__table__, NotificationOutbox.__table__, ManagedObject.__table__, OnboardingTemplate.__table__, ElementOnboarding.__table__, LifecycleSubscription.__table__, SoftwareCampaign.__table__, KpiDefinition.__table__, KpiSchedule.__table__, RAppLimit.__table__, RAppKill.__table__, SafeguardRefusal.__table__, SafeguardSubscription.__table__,
        RAppApprovalPolicy.__table__, RAppActionApproval.__table__, ApprovalSubscription.__table__, RAppDecisionRecord.__table__, AuditEntry.__table__, AuditHead.__table__,
    ])
    return sessionmaker(bind=engine)

@pytest.fixture
def elements(db_session_factory):
    """ME-1 and ME-2 as managed elements: a subscription refers to its element (a foreign key in Postgres, which these tests' SQLite does not enforce)."""
    with db_session_factory() as session:
        for ref in ("ME-1", "ME-2"):
            session.add(ManagedEntity(managed_element_ref=ref, entity_type="O-DU", o1_protocol="NETCONF"))
        session.commit()



@pytest.fixture
def client(db_session_factory):
    """Fixture: a TestClient of the app with `get_session` overridden to a session from `db_session_factory`; the override is removed afterwards."""
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=None):
    """Registers element ME-1 with an adaptor endpoint of the given protocol, health and last heartbeat; the tests' standard starting state for a write.
    """
    db = db_session_factory()
    endpoint = O1AdaptorEndpoint(managed_element_ref="ME-1", adaptor_uri="http://adaptor:9000/netconf",
                                  protocol_support=[protocol], health_status=health, last_heartbeat_at=last_heartbeat_at)
    db.add(endpoint)
    db.flush()
    me = ManagedEntity(managed_element_ref="ME-1", entity_type="O-DU", o1_protocol=protocol, o1_adaptor_endpoint_id=endpoint.endpoint_id)
    db.add(me)
    db.commit()
    db.close()


def test_config_change_dispatches_netconf_and_applies(client, db_session_factory, monkeypatch):
    """A config job for a NETCONF element dispatches the edit, ends COMPLETED, and its sub-change is APPLIED with the default merge operation."""
    _make_me(db_session_factory, protocol="NETCONF")
    monkeypatch.setattr("app.main.send_edit_config", lambda adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", **kw: True)

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    assert resp.status_code == 202
    assert resp.json()["status"] == "COMPLETED"

    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["status"] == "APPLIED"
    assert job["subChanges"][0]["operation"] == "merge"


def test_config_change_threads_operation_and_allows_empty_payload_for_delete(client, db_session_factory, monkeypatch):
    """HISTORY.md §7 item 3: RFC 6241 section 7.2's real edit-config
    `operation` attribute, previously not modeled at all — every write
    was implicitly a merge. A delete legitimately carries no
    attributeChanges, which the pre-fix `change["attributeChanges"]`
    lookup would have raised a KeyError on.
    """
    _make_me(db_session_factory, protocol="NETCONF")
    seen = {}

    def fake_send_edit_config(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", **kw):
        seen["attribute_changes"] = attribute_changes
        seen["operation"] = operation
        return True

    monkeypatch.setattr("app.main.send_edit_config", fake_send_edit_config)

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "operation": "delete"}],
    })
    assert resp.status_code == 202
    assert seen["attribute_changes"] == {}
    assert seen["operation"] == "delete"

    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["status"] == "APPLIED"
    assert job["subChanges"][0]["operation"] == "delete"


def test_config_change_rejects_when_netconf_rpc_fails(client, db_session_factory, monkeypatch):
    """A failed edit-config rejects the sub-change with NETCONF_RPC_FAILED and the job ends FAILED."""
    _make_me(db_session_factory, protocol="NETCONF")
    monkeypatch.setattr("app.main.send_edit_config", lambda adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", **kw: False)

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    assert resp.json()["status"] == "FAILED"

    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["status"] == "REJECTED"
    assert job["subChanges"][0]["rejectionReason"] == "NETCONF_RPC_FAILED"


def test_config_change_dispatches_restconf_for_a_restconf_me(client, db_session_factory, monkeypatch):
    """OI-1-cm-sync-restconf: an ME provisioned for RESTCONF is dispatched
    through restconf_client (RFC 8040), never through the NETCONF client."""
    _make_me(db_session_factory, protocol="RESTCONF")
    sent = []
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: pytest.fail("should not use NETCONF for a RESTCONF ME"))
    monkeypatch.setattr("app.main.restconf_client.send_edit",
                        lambda root, ref, changes, message_id, operation="merge", managed_function_ref=None:
                        sent.append((root, ref, changes, operation)) or True)

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}, "operation": "replace"}],
    })
    assert resp.json()["status"] == "COMPLETED"
    assert sent == [("http://adaptor:9000/netconf", "ME-1", {"adminState": "UNLOCKED"}, "replace")]


def test_config_change_rejects_a_protocol_with_no_client(client, db_session_factory, monkeypatch):
    """Anything but NETCONF or RESTCONF is still rejected rather than
    silently treated as applied."""
    _make_me(db_session_factory, protocol="SNMP")
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: pytest.fail("should not dispatch"))
    monkeypatch.setattr("app.main.restconf_client.send_edit", lambda *a, **kw: pytest.fail("should not dispatch"))

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["status"] == "REJECTED"
    assert job["subChanges"][0]["rejectionReason"] == "PROTOCOL_NOT_SUPPORTED"
    assert client.get("/managed-entities/ME-1/config").status_code == 409


def test_config_change_rejects_unreachable_endpoint_without_dispatch(client, db_session_factory, monkeypatch):
    """A write to an UNREACHABLE endpoint is rejected with ENDPOINT_UNREACHABLE and nothing is sent."""
    _make_me(db_session_factory, protocol="NETCONF", health="UNREACHABLE")
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: pytest.fail("should not dispatch to an unreachable endpoint"))

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["rejectionReason"] == "ENDPOINT_UNREACHABLE"


def test_config_change_rejects_a_stale_active_endpoint_live_without_an_explicit_discover_call(client, db_session_factory, monkeypatch):
    """The heartbeat-aging check (HISTORY.md §2) is computed live
    at this gate now, the same "no scheduler exists anywhere in this
    build" pattern already used for DME's producer health — so a stale endpoint is caught here even though
    nothing ever called POST /o1-adaptor-endpoints/discover first.
    """
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=stale)
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: pytest.fail("should not dispatch to a stale endpoint"))

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["rejectionReason"] == "ENDPOINT_UNREACHABLE"


def test_config_change_proceeds_for_a_freshly_heartbeated_active_endpoint(client, db_session_factory, monkeypatch):
    """An ACTIVE endpoint with a recent heartbeat is not aged and the write goes through."""
    fresh = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
    _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=fresh)
    monkeypatch.setattr("app.main.send_edit_config", lambda adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", **kw: True)

    resp = client.post("/config-jobs", json={
        "requestedBy": "operator", "scope": "cell",
        "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    job = client.get(f"/config-jobs/{resp.json()['jobId']}").json()
    assert job["subChanges"][0]["status"] == "APPLIED"


def test_discover_endpoints_ages_a_stale_active_endpoint_to_degraded(client, db_session_factory):
    """The discover sweep degrades an ACTIVE endpoint whose last heartbeat is older than the threshold."""
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=stale)

    resp = client.post("/o1-adaptor-endpoints/discover")
    assert resp.status_code == 200
    assert resp.json() == {"checked": 1}

    db = db_session_factory()
    ep = db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-1").one()
    assert ep.health_status == "DEGRADED"
    db.close()


def test_discover_endpoints_leaves_a_fresh_active_endpoint_alone(client, db_session_factory):
    """The discover sweep leaves an ACTIVE endpoint with a recent heartbeat as it is."""
    fresh = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
    _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=fresh)

    client.post("/o1-adaptor-endpoints/discover")

    db = db_session_factory()
    ep = db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-1").one()
    assert ep.health_status == "ACTIVE"
    db.close()


def test_discover_endpoints_ignores_an_endpoint_that_has_never_heartbeated(client, db_session_factory):
    """last_heartbeat_at stays NULL until the first POST .../heartbeat call
    — must not be treated as "infinitely stale" (None minus now would also
    raise, not just compare wrong).
    """
    _make_me(db_session_factory, protocol="NETCONF", health="ACTIVE", last_heartbeat_at=None)

    resp = client.post("/o1-adaptor-endpoints/discover")
    assert resp.status_code == 200

    db = db_session_factory()
    ep = db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-1").one()
    assert ep.health_status == "ACTIVE"
    db.close()


def test_register_o1_adaptor_endpoint_creates_endpoint_and_managed_entity(client, db_session_factory):
    """RAN NF OAM LLD section 1's own design intent ("per ME's O1 Adaptor
    registers itself into the MnS Registry NRM") previously had no real
    route anywhere in this build — the whole registry could only ever be
    populated by a test fixture reaching directly into the DB.
    """
    resp = client.post("/o1-adaptor-endpoints", json={
        "managedElementRef": "ME-2", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config",
        "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["managedElementRef"] == "ME-2"
    assert body["healthStatus"] == "DISCOVERED"

    db = db_session_factory()
    ep = db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-2").one()
    assert ep.adaptor_uri == "http://mock-o1-adaptor:8000/edit-config"
    assert ep.protocol_support == ["NETCONF"]
    me = db.get(ManagedEntity, "ME-2")
    assert me.entity_type == "O-DU"
    assert me.o1_protocol == "NETCONF"
    assert me.o1_adaptor_endpoint_id == ep.endpoint_id
    db.close()


def test_register_o1_adaptor_endpoint_starts_discovered_not_active(client, db_session_factory):
    """A fresh registration hasn't heartbeated yet — DISCOVERED is the
    FSM's own real starting state, not the model column's own default
    (ACTIVE, kept for other callers' test convenience).
    """
    client.post("/o1-adaptor-endpoints", json={
        "managedElementRef": "ME-3", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config",
        "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-CU",
    })
    db = db_session_factory()
    ep = db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-3").one()
    assert ep.health_status == "DISCOVERED"
    db.close()


def test_registered_endpoint_can_then_heartbeat_to_active(client, db_session_factory):
    """An endpoint registered through the route starts DISCOVERED and a heartbeat makes it ACTIVE."""
    reg = client.post("/o1-adaptor-endpoints", json={
        "managedElementRef": "ME-4", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config",
        "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU",
    }).json()

    resp = client.post(f"/o1-adaptor-endpoints/{reg['endpointId']}/heartbeat")
    assert resp.status_code == 200
    assert resp.json()["healthStatus"] == "ACTIVE"


def test_subscribe_pm_persists_and_returns_granularity_period(client, db_session_factory, monkeypatch, elements):
    """HISTORY.md §7 item 4: TS28550_PerfMeasJobCtrlMnS.yaml's
    granularityPeriod (the sampling interval), previously absent
    entirely from PMSubscription — subscribe_pm's own docstring already
    confirms the rest of that job-control shape (schedule/priority/
    reportingPeriod) is a deliberate scope cut, but this one field is
    needed by any real PM subscription regardless of wrapper shape.
    """
    calls = []
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: calls.append((path, json)))

    resp = client.post("/pm-subscriptions", params={
        "managed_element_ref": "ME-1", "counter_type": "PRB.Usage", "delivery_method": "pull", "granularity_period": 900,
    })
    assert resp.status_code == 200
    assert resp.json()["granularityPeriod"] == 900

    db = db_session_factory()
    sub = db.get(PMSubscription, uuid.UUID(resp.json()["subscriptionId"]))
    assert sub.granularity_period == 900


def test_subscribe_pm_without_granularity_period_defaults_to_null(client, db_session_factory, monkeypatch, elements):
    """A PM subscription without a granularity period answers null for it."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)

    resp = client.post("/pm-subscriptions", params={
        "managed_element_ref": "ME-1", "counter_type": "PRB.Usage", "delivery_method": "pull",
    })
    assert resp.status_code == 200
    assert resp.json()["granularityPeriod"] is None


def test_unsubscribe_pm(client, db_session_factory, monkeypatch, elements):
    """`docs/call-flows/20-alarm-pm-subscription-lifecycle.md`'s own
    gap, closed: PMSubscription previously had no DELETE route at all,
    unlike every other subscription-shaped resource in this build.
    """
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)
    sub_id = client.post("/pm-subscriptions", params={
        "managed_element_ref": "ME-1", "counter_type": "PRB.Usage", "delivery_method": "pull",
    }).json()["subscriptionId"]

    resp = client.delete(f"/pm-subscriptions/{sub_id}")
    assert resp.status_code == 204

    db = db_session_factory()
    assert db.get(PMSubscription, uuid.UUID(sub_id)) is None


def test_unsubscribe_unknown_pm_subscription_is_idempotent(client):
    """Deleting a PM subscription that does not exist is 204, as for every subscription route."""
    resp = client.delete(f"/pm-subscriptions/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_subscribe_fm_registers_ran_nf_oam_as_a_dme_producer(client, db_session_factory, monkeypatch, elements):
    """HISTORY.md OI-6.7, closed: unlike PM (subscribe_pm calls
    RegisterDMEType), FM/alarms had no DME producer registration at all.
    subscribe_fm mirrors subscribe_pm's own shape exactly.
    """
    calls = []
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: calls.append((path, json)))

    resp = client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "push"})
    assert resp.status_code == 200
    assert resp.json()["southboundEngine"] == "FaultMnS"

    assert len(calls) == 1
    path, body = calls[0]
    assert path == "/dme/production-capabilities"
    assert body["namespace"] == "RAN"
    assert body["name"] == "FaultRecords"
    assert body["producerId"] == "ran-nf-oam"
    assert body["producerHealthCallbackUrl"] == "http://ran-nf-oam:8000/health"
    assert body["jobCallbackUrl"] == "http://ran-nf-oam:8000/dme-jobs"

    db = db_session_factory()
    sub = db.get(FMSubscription, uuid.UUID(resp.json()["subscriptionId"]))
    assert sub.managed_element_ref == "ME-1"
    assert sub.delivery_method == "push"


def test_subscribe_fm_unknown_delivery_method_defaults_to_faultmns(client, db_session_factory, monkeypatch, elements):
    """An FM subscription is served by the FaultMnS engine (the test sends `pull`; a method with no mapping also falls back to FaultMnS)."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)

    resp = client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "pull"})
    assert resp.status_code == 200
    assert resp.json()["southboundEngine"] == "FaultMnS"


def test_list_fm_subscriptions_filters_by_managed_element_ref(client, db_session_factory, monkeypatch, elements):
    """The FM subscription list can be narrowed to one element."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)
    client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "push"})
    client.post("/fm-subscriptions", params={"managed_element_ref": "ME-2", "delivery_method": "push"})

    resp = client.get("/fm-subscriptions", params={"managed_element_ref": "ME-1"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["managedElementRef"] == "ME-1"


def test_unsubscribe_fm(client, db_session_factory, monkeypatch, elements):
    """Deleting an FM subscription is 204 and removes the row."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)
    sub_id = client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "push"}).json()["subscriptionId"]

    resp = client.delete(f"/fm-subscriptions/{sub_id}")
    assert resp.status_code == 204

    db = db_session_factory()
    assert db.get(FMSubscription, uuid.UUID(sub_id)) is None


def test_unsubscribe_unknown_fm_subscription_is_idempotent(client):
    """Deleting an FM subscription that does not exist is 204."""
    resp = client.delete(f"/fm-subscriptions/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_health_endpoint_answers_the_callback_url_subscribe_pm_registers(client):
    """HISTORY.md §5: subscribe_pm registers
    http://ran-nf-oam:8000/health as this producer's health-supervision
    callback with DME, but no route ever answered it — a poller hitting
    that URL would 404. Confirms the route now exists and returns 200.
    """
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_dme_jobs_endpoint_answers_the_callback_url_subscribe_pm_registers(client):
    """HISTORY.md §5: subscribe_pm now also registers
    http://ran-nf-oam:8000/dme-jobs as this producer's jobCallbackUrl —
    DME's own create_data_job/terminate_data_job actually push to it
    now, so this closes the same class of dangling-callback bug the
    /health route closed for the health-supervision URL.
    """
    resp = client.post("/dme-jobs", json={"infoJobIdentity": "job-1", "infoTypeIdentity": "type-1", "infoJobData": {}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    resp = client.delete("/dme-jobs/job-1")
    assert resp.status_code == 204


def test_ingest_alarm_persists_standard_fault_fields(client, db_session_factory):
    """HISTORY.md §5: the alarm model was missing the standard
    fault fields the wire format (VES/3GPP alarm IRP, per oam's own
    NotifyNewAlarm template) carries — probableCause, specificProblem,
    rootCauseIndicator, correlatedNotifications, proposedRepairActions.
    alarmType (HISTORY.md §7, TS28111_FaultNrm.yaml's AlarmRecord) was
    the one of these still missing after that pass.
    """
    _make_me(db_session_factory)
    other_alarm_id = str(uuid.uuid4())

    resp = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "critical",
        "probable_cause": "linkFailure", "specific_problem": "Optical link down",
        "root_cause_indicator": True, "correlated_notifications": [other_alarm_id],
        "proposed_repair_actions": "Replace the SFP module.", "alarm_type": "EQUIPMENT_ALARM",
    })
    assert resp.status_code == 200
    alarm_id = resp.json()["alarmId"]

    listing = client.get("/alarms").json()["items"]
    assert len(listing) == 1
    alarm = listing[0]
    assert alarm["alarmId"] == alarm_id
    assert alarm["probableCause"] == "linkFailure"
    assert alarm["specificProblem"] == "Optical link down"
    assert alarm["rootCauseIndicator"] is True
    assert alarm["correlatedNotifications"] == [other_alarm_id]
    assert alarm["proposedRepairActions"] == "Replace the SFP module."
    assert alarm["alarmType"] == "EQUIPMENT_ALARM"


def test_ingest_alarm_defaults_fault_fields_when_not_provided(client, db_session_factory):
    """The reference's NotifyNewAlarm fields are all optional on ingest —
    an alarm raised without them must not crash and must default sanely
    (rootCauseIndicator false, correlatedNotifications empty).
    """
    _make_me(db_session_factory)

    resp = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-2", "managed_element_ref": "ME-1", "severity": "minor",
    })
    assert resp.status_code == 200

    alarm = client.get("/alarms").json()["items"][0]
    assert alarm["probableCause"] is None
    assert alarm["specificProblem"] is None
    assert alarm["rootCauseIndicator"] is False
    assert alarm["correlatedNotifications"] == []
    assert alarm["proposedRepairActions"] is None
    assert alarm["alarmType"] is None


def test_query_alarms_filters_by_managed_element_ref(client, db_session_factory):
    """The alarm list can be narrowed to one element."""
    db = db_session_factory()
    endpoint1 = O1AdaptorEndpoint(managed_element_ref="ME-1", adaptor_uri="http://adaptor-1:9000/netconf", protocol_support=["NETCONF"])
    endpoint2 = O1AdaptorEndpoint(managed_element_ref="ME-2", adaptor_uri="http://adaptor-2:9000/netconf", protocol_support=["NETCONF"])
    db.add(endpoint1)
    db.add(endpoint2)
    db.flush()
    db.add(ManagedEntity(managed_element_ref="ME-1", entity_type="O-DU", o1_protocol="NETCONF", o1_adaptor_endpoint_id=endpoint1.endpoint_id))
    db.add(ManagedEntity(managed_element_ref="ME-2", entity_type="O-DU", o1_protocol="NETCONF", o1_adaptor_endpoint_id=endpoint2.endpoint_id))
    db.commit()
    db.close()

    client.post("/alarms/ingest", params={"source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "critical"})
    client.post("/alarms/ingest", params={"source_alarm_id": "src-2", "managed_element_ref": "ME-2", "severity": "minor"})

    resp = client.get("/alarms", params={"managed_element_ref": "ME-2"})
    alarms = resp.json()["items"]
    assert len(alarms) == 1
    assert alarms[0]["managedElementRef"] == "ME-2"


def test_change_alarm_ack_state(client, db_session_factory):
    """Acknowledging an alarm sets its ack state."""
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "major",
    }).json()["alarmId"]

    resp = client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED"})
    assert resp.status_code == 200
    assert resp.json()["ackState"] == "ACKNOWLEDGED"


def test_ack_and_clear_of_an_unknown_alarm_are_404(client):
    """MGT-8.1: both used to raise AttributeError on None (a 500)."""
    missing = uuid.uuid4()
    for path, params in ((f"/alarms/{missing}/ack", {"new_state": "ACKNOWLEDGED"}), (f"/alarms/{missing}/clear", {})):
        resp = client.patch(path, params=params)
        assert resp.status_code == 404 and resp.json()["detail"]["title"] == "ALARM_NOT_FOUND"


def test_ack_state_must_be_a_known_value_and_nothing_is_stored_otherwise(client, db_session_factory):
    """An unknown ack state is 422 and the stored state is unchanged; both valid states can be set."""
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "major",
    }).json()["alarmId"]
    assert client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "MAYBE"}).status_code == 422
    assert client.get("/alarms").json()["items"][0]["ackState"] == "UNACKNOWLEDGED"
    assert client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED"}).status_code == 200
    assert client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "UNACKNOWLEDGED"}).json()["ackState"] == "UNACKNOWLEDGED"


def test_change_alarm_ack_state_records_ack_user_id_and_changed_at(client, db_session_factory):
    """HISTORY.md §7: TS28111_FaultNrm.yaml's AlarmRecord carries
    ackUserId (who acknowledged it) and alarmChangedTime (its own "last
    mutated" timestamp) — PATCH /alarms/{id}/ack never recorded either.
    """
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "major",
    }).json()["alarmId"]
    assert client.get("/alarms").json()["items"][0]["changedAt"] is None

    resp = client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "operator-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ackUserId"] == "operator-1"
    assert body["changedAt"] is not None


def test_clear_alarm_sets_cleared_severity_and_metadata(client, db_session_factory):
    """HISTORY.md §5: no alarm-cleared lifecycle existed at
    all — an alarm that stopped recurring on the NF had no way to ever
    be marked resolved. Matches the reference's own NotifyClearedAlarm
    shape: perceivedSeverity=CLEARED, not a separate state field.
    """
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "critical",
    }).json()["alarmId"]

    resp = client.patch(f"/alarms/{alarm_id}/clear", params={"clear_user_id": "operator-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["severity"] == "cleared"
    assert body["clearUserId"] == "operator-1"
    assert body["clearedAt"] is not None
    assert body["changedAt"] == body["clearedAt"]


def test_cleared_alarm_still_appears_in_query_alarms(client, db_session_factory):
    """Clearing doesn't delete the alarm — it stays queryable, same as
    the reference's own retained-but-cleared alarm records.
    """
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "critical",
    }).json()["alarmId"]
    client.patch(f"/alarms/{alarm_id}/clear")

    alarms = client.get("/alarms").json()["items"]
    assert len(alarms) == 1
    assert alarms[0]["severity"] == "cleared"


def test_clear_alarm_without_clear_user_id_leaves_it_null(client, db_session_factory):
    """Clearing an alarm without a user id leaves `clearUserId` null and sets the cleared time."""
    _make_me(db_session_factory)
    alarm_id = client.post("/alarms/ingest", params={
        "source_alarm_id": "src-1", "managed_element_ref": "ME-1", "severity": "warning",
    }).json()["alarmId"]

    resp = client.patch(f"/alarms/{alarm_id}/clear")
    assert resp.status_code == 200
    assert resp.json()["clearUserId"] is None
    assert resp.json()["clearedAt"] is not None


# ---------------------------------------------------------------- list reads (GUI pass)

def test_query_alarms_filters_by_severity_and_exposes_raised_at(client, db_session_factory):
    """The alarm list filters by severity (and `cleared` isolates cleared alarms) and shows when each was raised."""
    _make_me(db_session_factory)
    client.post("/alarms/ingest", params={"source_alarm_id": "a1", "managed_element_ref": "ME-1", "severity": "major"})
    minor = client.post("/alarms/ingest", params={"source_alarm_id": "a2", "managed_element_ref": "ME-1", "severity": "minor"}).json()

    only_minor = client.get("/alarms", params={"severity": "minor"}).json()["items"]
    assert [a["alarmId"] for a in only_minor] == [minor["alarmId"]]
    assert only_minor[0]["raisedAt"]

    client.patch(f"/alarms/{minor['alarmId']}/clear")
    assert [a["alarmId"] for a in client.get("/alarms", params={"severity": "cleared"}).json()["items"]] == [minor["alarmId"]]


def test_list_pm_subscriptions(client, db_session_factory, monkeypatch):
    """PM subscriptions are listed with their engine and granularity, and can be filtered by element."""
    _make_me(db_session_factory)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: None)
    sub = client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": "DRB.UEThpDl",
                                                    "delivery_method": "push", "granularity_period": 900}).json()

    listed = client.get("/pm-subscriptions").json()["items"]
    assert [(s["subscriptionId"], s["southboundEngine"], s["granularityPeriod"]) for s in listed] == [(sub["subscriptionId"], "PMJobControl", 900)]
    assert client.get("/pm-subscriptions", params={"managed_element_ref": "ME-2"}).json()["items"] == []


def test_list_o1_adaptor_endpoints_filters_by_health(client, db_session_factory):
    """The endpoint list shows each element's health and can be filtered by it."""
    _make_me(db_session_factory, health="DEGRADED")
    listed = client.get("/o1-adaptor-endpoints").json()["items"]
    assert [(e["managedElementRef"], e["healthStatus"]) for e in listed] == [("ME-1", "DEGRADED")]
    assert client.get("/o1-adaptor-endpoints", params={"health_status": "ACTIVE"}).json()["items"] == []


def test_list_config_and_software_jobs(client, db_session_factory, monkeypatch):
    """Config jobs and software jobs can be listed."""
    _make_me(db_session_factory, last_heartbeat_at=datetime.datetime.now(datetime.UTC))
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: None)
    job = client.post("/config-jobs", json={"requestedBy": "op", "scope": "cell", "changes": []}).json()
    swm = client.post("/software-management-jobs", params={"managed_element_ref": "ME-1"}).json()

    assert [j["jobId"] for j in client.get("/config-jobs").json()["items"]] == [job["jobId"]]
    assert [(j["jobId"], j["phase"]) for j in client.get("/software-management-jobs").json()["items"]] == [(swm["jobId"], swm["phase"])]


def test_an_alarm_can_name_the_cell_it_is_about(client, db_session_factory):
    """W10-alarm-cellref: an alarm may name the managed function it is raised
    on (e.g. a cell), which the list returns and filters by; omitted, it is
    about the element as a whole."""
    _make_me(db_session_factory)
    client.post("/alarms/ingest", params={"source_alarm_id": "cell-101", "managed_element_ref": "ME-1",
                                          "severity": "critical", "managed_function_ref": "NRCellDU=101"})
    client.post("/alarms/ingest", params={"source_alarm_id": "whole", "managed_element_ref": "ME-1", "severity": "critical"})

    by_source = {a["sourceAlarmId"]: a for a in client.get("/alarms").json()["items"]}
    assert by_source["cell-101"]["managedFunctionRef"] == "NRCellDU=101"
    assert by_source["whole"]["managedFunctionRef"] is None
    only = client.get("/alarms", params={"managed_function_ref": "NRCellDU=101"}).json()["items"]
    assert [a["sourceAlarmId"] for a in only] == ["cell-101"]


def test_a_concurrent_writer_turns_a_software_job_advance_into_a_409_and_the_repeat_succeeds(client, db_session_factory):
    """PR-ST-2: SoftwareManagementJob is versioned; a stale write is a 409, not a lost update."""
    _make_me(db_session_factory, last_heartbeat_at=datetime.datetime.now(datetime.UTC))
    job = client.post("/software-management-jobs", params={"managed_element_ref": "ME-1"}).json()

    with concurrent_commit_on("software_management_job") as fired:
        stale = client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True})
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"

    repeat = client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True})
    assert repeat.status_code == 200


def test_a_concurrent_writer_turns_a_config_job_write_into_a_409(client, db_session_factory, monkeypatch):
    """PR-ST-2: WriteConfigJob is versioned: a stale status write is a 409 as well."""
    _make_me(db_session_factory, last_heartbeat_at=datetime.datetime.now(datetime.UTC))
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: None)
    with concurrent_commit_on("write_config_job") as fired:
        stale = client.post("/config-jobs", json={"requestedBy": "op", "scope": "cell", "changes": []})
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"


def test_a_config_job_with_an_idempotency_key_is_written_once(client, db_session_factory, monkeypatch):
    """PR-ST-3: a repeat of WriteConfigurationChanges with the same Idempotency-Key creates no second
    job and sends nothing southbound a second time."""
    _make_me(db_session_factory, protocol="NETCONF")
    sent = []
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **kw: sent.append(a) or True)
    body = {"requestedBy": "operator", "scope": "cell",
            "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}]}

    first = client.post("/config-jobs", json=body, headers={"Idempotency-Key": "cfg-1"})
    again = client.post("/config-jobs", json=body, headers={"Idempotency-Key": "cfg-1"})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    assert len(sent) == 1
    with db_session_factory() as session:
        assert session.query(WriteConfigJob).count() == 1


def test_unknown_ids_are_404_and_malformed_input_is_422_not_500(client):
    """Found by the contract test (tests_integration/test_contract_schemathesis.py): each of these answered 500."""
    unknown = "e3e70682-c209-1cac-a29f-6fbed82c07cd"
    assert client.get(f"/config-jobs/{unknown}").status_code == 404
    assert client.post(f"/software-management-jobs/{unknown}/advance", params={"succeeded": "true"}).status_code == 404
    assert client.post(f"/o1-adaptor-endpoints/{unknown}/heartbeat").status_code == 404
    assert client.post("/config-jobs", json={"requestedBy": "x", "accessScope": "s", "changes": [{}]}).status_code == 422
    body = {"managedElementRef": "ME-DUP", "adaptorUri": "http://a/edit-config", "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU"}
    assert client.post("/o1-adaptor-endpoints", json=body).status_code == 201
    assert client.post("/o1-adaptor-endpoints", json=body).status_code == 409
    assert client.post("/o1-adaptor-endpoints", json={**body, "managedElementRef": "ManagedElement="}).status_code == 422


def test_subscribing_for_an_element_that_does_not_exist_is_a_404_not_a_foreign_key_500(client):
    """Found by the authenticated DAST scan (V-7d): the subscription refers to the element, and Postgres refused the row."""
    for path, params in (("/fm-subscriptions", {"managed_element_ref": "no-such-element", "delivery_method": "push"}),
                         ("/pm-subscriptions", {"managed_element_ref": "no-such-element", "counter_type": "x", "delivery_method": "push"})):
        response = client.post(path, params=params)
        assert response.status_code == 404 and response.json()["detail"]["title"] == "MANAGED_ENTITY_NOT_FOUND", (path, response.text)
