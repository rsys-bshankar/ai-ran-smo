"""PR-SEC-10 (docs/adr/0005-tenant-region-authorization.md): a caller with a scope claim touches only the managed elements inside it.

The gateway vouches for the claim (`X-R1-Scope`, or `X-R1-On-Behalf-Scope` from an SMO module acting for an rApp); this module owns the targets, so it decides. The rules
under test: an unscoped caller is unchanged; a scoped one may touch an element only when every restricted axis matches; an element with no region (tenant) or not registered
is outside a claim that restricts that axis; a write that names any element outside is refused whole with 403 `SCOPE_DENIED`; a list is filtered; an item addressed by an id
is a 404."""

import datetime
import uuid

import pytest
from sqlalchemy import select

from smo_shared.scope import ON_BEHALF_SCOPE_HEADER, SCOPE_HEADER

from app.models import Alarm, CMSnapshot, ManagedEntity, PMFile, PMSubscription, FMSubscription, RAppActionApproval, SafeguardRefusal, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import ELEMENTS, fleet  # noqa: F401

# ME-1 eu/acme, ME-2 eu/globex, ME-3 us/acme, ME-4 no region and no tenant
PLACES = {"ME-1": ("eu", "acme"), "ME-2": ("eu", "globex"), "ME-3": ("us", "acme"), "ME-4": (None, None)}


def claim(**axes) -> str:
    """The JSON text of a scope claim with the given axes (`regions=[...]`, `tenants=[...]`), as the gateway sends it in the scope header."""
    import json
    return json.dumps(axes, sort_keys=True, separators=(",", ":"))


EU = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp", SCOPE_HEADER: claim(regions=["eu"])}
ACME = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp", SCOPE_HEADER: claim(tenants=["acme"])}
EU_ACME = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp", SCOPE_HEADER: claim(regions=["eu"], tenants=["acme"])}
UNSCOPED = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"}
GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}


@pytest.fixture(autouse=True)
def no_inline_sending(monkeypatch):
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")


@pytest.fixture
def places(fleet):
    """Fixture: puts the four fleet elements in the regions and tenants of `PLACES` and returns the fleet state."""
    with fleet["db"]() as db:
        for ref, (region, tenant) in PLACES.items():
            row = db.get(ManagedEntity, ref)
            row.region, row.tenant = region, tenant
        db.commit()
    return fleet


def _write(client, refs, headers, value=20, **extra):
    changes = [{"managedElementRef": ref, "attributeChanges": {"txPower": value}} for ref in refs]
    return client.post("/config-jobs", headers=headers, json={"requestedBy": "es-rapp", "scope": "cell", "changes": changes, **extra})


def _title(resp):
    return resp.json()["detail"]["title"]


def _jobs(fleet):
    with fleet["db"]() as db:
        return db.query(WriteConfigJob).count()


# ---- SEC-10.2: region and tenant on the element

def test_an_element_is_registered_with_a_region_and_a_tenant_and_they_can_be_edited(client, db_session_factory):
    """A region and tenant given at registration are kept and shown, and `PUT .../scope` replaces both (a key left out clears it)."""
    resp = client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-9", "adaptorUri": "http://a:9/netconf", "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF",
                                                      "entityType": "O-DU", "region": "eu-west", "tenant": "acme"})
    assert resp.status_code == 201 and (resp.json()["region"], resp.json()["tenant"]) == ("eu-west", "acme")
    view = client.get("/managed-entities/ME-9").json()
    assert (view["region"], view["tenant"]) == ("eu-west", "acme")
    moved = client.put("/managed-entities/ME-9/scope", json={"region": "us-east", "tenant": None})
    assert moved.status_code == 200 and moved.json() == {"managedElementRef": "ME-9", "region": "us-east", "tenant": None}
    assert client.get("/managed-entities/ME-9").json()["tenant"] is None
    assert client.put("/managed-entities/ME-9/scope", json={}).json()["region"] is None                      # a key left out clears it
    assert client.put("/managed-entities/nope/scope", json={"region": "x1"}).status_code == 404


def test_an_element_without_a_region_or_tenant_registers_as_before(client):
    """An element registered without a region or tenant has none, as before."""
    resp = client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-8", "adaptorUri": "http://a:9/netconf", "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF",
                                                      "entityType": "O-DU"})
    assert resp.status_code == 201 and resp.json()["region"] is None and resp.json()["tenant"] is None


@pytest.mark.parametrize("value", ["", " eu", "eu west", "-eu", "a" * 101, "eu\n", "é"])
def test_a_region_or_tenant_that_is_not_valid_is_refused(client, db_session_factory, value):
    """A region or tenant that is empty, has spaces or a leading dash, is too long or non-ASCII is 422 at registration and on the scope route, and
    so is an unknown field.
    """
    body = {"managedElementRef": "ME-7", "adaptorUri": "http://a:9/netconf", "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU"}
    assert client.post("/o1-adaptor-endpoints", json={**body, "region": value}).status_code == 422
    assert client.post("/o1-adaptor-endpoints", json={**body, "tenant": value}).status_code == 422
    _ = db_session_factory
    client.post("/o1-adaptor-endpoints", json=body)
    assert client.put("/managed-entities/ME-7/scope", json={"region": value}).status_code == 422
    assert client.put("/managed-entities/ME-7/scope", json={"tenant": value}).status_code == 422
    assert client.put("/managed-entities/ME-7/scope", json={"zone": "x"}).status_code == 422


def test_the_managed_entity_list_can_be_narrowed_by_region_and_tenant(client, places):
    """The element list can be narrowed by region and tenant, separately or together."""
    refs = lambda **q: [i["managedElementRef"] for i in client.get("/managed-entities", params=q).json()["items"]]      # noqa: E731
    assert refs() == ELEMENTS and refs(region="eu") == ["ME-1", "ME-2"] and refs(tenant="acme") == ["ME-1", "ME-3"] and refs(region="eu", tenant="acme") == ["ME-1"]


# ---- SEC-10.4: POST /config-jobs (the pilot)

def test_an_unscoped_caller_is_unchanged_whatever_the_elements_are(client, places):
    """The upgrade promise: nothing changes until a scope is set."""
    for headers in ({}, UNSCOPED, GUI):
        resp = _write(client, ELEMENTS, headers)
        assert resp.status_code == 202 and resp.json()["status"] == "COMPLETED", resp.text


def test_a_scoped_caller_writes_inside_its_scope(client, places):
    """A scoped caller may write to elements inside its claim, whether the claim restricts the region, the tenant or both."""
    assert _write(client, ["ME-1", "ME-2"], EU).status_code == 202
    assert _write(client, ["ME-1", "ME-3"], ACME).status_code == 202
    assert _write(client, ["ME-1"], EU_ACME).status_code == 202
    assert places["edits"] == ["ME-1", "ME-2", "ME-1", "ME-3", "ME-1"]


def test_one_element_outside_the_scope_refuses_the_whole_job_and_nothing_is_sent(client, places):
    """One element outside the claim refuses the whole job with 403 SCOPE_DENIED, naming only the element outside, and nothing is recorded or sent."""
    refused = _write(client, ["ME-1", "ME-3"], EU)
    assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED"
    assert "ME-3" in refused.json()["detail"]["detail"] and "ME-1" not in refused.json()["detail"]["detail"]
    assert places["edits"] == [] and _jobs(places) == 0


@pytest.mark.parametrize("headers, refs, denied", [
    (EU, ["ME-3"], "ME-3"),                       # another region
    (ACME, ["ME-2"], "ME-2"),                     # another tenant
    (EU_ACME, ["ME-2"], "ME-2"),                  # the right region, the wrong tenant
    (EU_ACME, ["ME-3"], "ME-3"),                  # the right tenant, the wrong region
    (EU, ["ME-4"], "ME-4"),                       # no region at all
    (ACME, ["ME-4"], "ME-4"),                     # no tenant at all
    (EU, ["ME-NOT-THERE"], "ME-NOT-THERE"),      # not registered: has no region either, and the answer is the same
])
def test_the_refusals(client, places, headers, refs, denied):
    """Each way of being outside a claim gives the same 403 for the write: another region, another tenant, a mismatch on one axis, an element with
    no region or tenant, and an element that is not registered.
    """
    resp = _write(client, refs, headers)
    assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED" and denied in resp.json()["detail"]["detail"]
    assert places["edits"] == [] and _jobs(places) == 0


def test_an_unregistered_element_and_an_out_of_scope_one_are_answered_alike(client, places):
    """No leak of which references exist: same status, same title, same shape of detail."""
    a, b = _write(client, ["ME-3"], EU), _write(client, ["ME-NOT-THERE"], EU)
    assert (a.status_code, _title(a)) == (b.status_code, _title(b)) == (403, "SCOPE_DENIED")
    assert a.json()["detail"]["detail"].replace("ME-3", "X") == b.json()["detail"]["detail"].replace("ME-NOT-THERE", "X")


def test_a_dry_run_is_refused_the_same_way(client, places):
    """A dry run is refused for an element outside the scope as a real write is, and is 200 inside it."""
    resp = _write(client, ["ME-3"], EU, dryRun=True)
    assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED"
    assert _write(client, ["ME-1"], EU, dryRun=True).status_code == 200


def test_a_refusal_comes_before_every_other_check(client, places):
    """The scope is checked before the schema and the dispatch: a scoped caller learns nothing about an element it may not touch (no 422 on its attributes)."""
    resp = client.post("/config-jobs", headers=EU, json={"requestedBy": "r", "scope": "cell", "changes": [{"managedElementRef": "ME-3", "attributeChanges": {"nonsense": 1}}]})
    assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED"


def test_the_refusal_is_recorded_and_announced_like_the_other_safeguards(client, places):
    """A scope refusal is recorded as a safeguard refusal with code SCOPE_DENIED, and a safeguard subscription can ask for that code."""
    _write(client, ["ME-3"], EU)
    listed = client.get("/safeguard-refusals").json()["items"]
    assert [(r["invokerId"], r["refusal"]) for r in listed] == [("es-client", "SCOPE_DENIED")] and "ME-3" in listed[0]["detail"]
    sub = client.post("/safeguard-subscriptions", json={"callbackUri": "http://watch.example:9/e", "refusals": ["SCOPE_DENIED"]})
    assert sub.status_code == 201 and sub.json()["refusals"] == ["SCOPE_DENIED"]


def test_a_module_acting_for_a_scoped_rapp_is_held_to_the_rapps_scope(client, places):
    """DME's call for an rApp: the claim travels beside the id (X-R1-On-Behalf-Of / -Scope), so writing through a module is no way round."""
    module_for_rapp = {"X-R1-Invoker-Id": "dme-client", "X-R1-Role": "internal", "X-R1-On-Behalf-Of": "es-client", ON_BEHALF_SCOPE_HEADER: claim(regions=["eu"])}
    refused = _write(client, ["ME-3"], module_for_rapp)
    assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED"
    assert client.get("/safeguard-refusals").json()["items"][0]["invokerId"] == "es-client"                  # recorded against the rApp
    assert _write(client, ["ME-1"], module_for_rapp).status_code == 202
    # the module's own claim header is not the rApp's, and a module acting for an unscoped rApp is unscoped
    assert _write(client, ["ME-3"], {**module_for_rapp, ON_BEHALF_SCOPE_HEADER: ""}).status_code == 202
    assert _write(client, ["ME-3"], {"X-R1-Invoker-Id": "dme-client", "X-R1-Role": "internal", SCOPE_HEADER: claim(regions=["eu"])}).status_code == 403


def test_a_damaged_claim_permits_nothing(client, places):
    """A claim that is not valid JSON or has no usable axis permits nothing: the write is 403 and nothing is sent."""
    for broken in ("not json", "[]", '{"regions":[]}', '{"zones":["x"]}'):
        resp = _write(client, ["ME-1"], {**UNSCOPED, SCOPE_HEADER: broken})
        assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED", broken
    assert places["edits"] == []


def test_the_scope_is_checked_before_the_rate_limit_counts_the_job(client, places):
    """A job refused by scope does not use the rApp's hourly job budget."""
    client.put("/rapp-limits/es-client", json={"maxConfigJobsPerHour": 1})
    assert _write(client, ["ME-3"], EU).status_code == 403
    assert _write(client, ["ME-1"], EU).status_code == 202                         # the refused job did not use the budget


# ---- SEC-10.4: rollback

def test_a_rollback_needs_every_element_the_job_wrote_to(client, places):
    """A rollback (and its dry run) is refused when the job wrote to an element outside the caller's scope; the refusal names none of them, is
    recorded, and nothing is written.
    """
    allowed = {**UNSCOPED, SCOPE_HEADER: claim(regions=["eu", "us"])}
    original = _write(client, ["ME-1", "ME-3"], allowed).json()["jobId"]         # the same rApp, with a wider claim, wrote to an eu and a us element (PR-SEC-10.11: its own job)
    before = _jobs(places)
    refused = client.post(f"/config-jobs/{original}/rollback", headers=EU, json={"requestedBy": "es-rapp"})
    assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED"
    assert "ME-3" not in refused.json()["detail"]["detail"] and "ME-1" not in refused.json()["detail"]["detail"]       # it names none: the caller did not send them
    assert client.post(f"/config-jobs/{original}/rollback", headers=EU, json={"requestedBy": "es-rapp", "dryRun": True}).status_code == 403
    assert _jobs(places) == before and places["values"]["ME-1"] == "20"
    assert client.get("/safeguard-refusals").json()["items"][0]["refusal"] == "SCOPE_DENIED"
    assert client.post(f"/config-jobs/{original}/rollback", headers=allowed, json={"requestedBy": "es-rapp"}).status_code == 202
    assert places["values"]["ME-1"] == "10" and places["values"]["ME-3"] == "10"


def test_a_rollback_of_an_unknown_job_is_still_a_404(client, places):
    """A rollback of an unknown job is 404 for a scoped caller too."""
    assert client.post(f"/config-jobs/{uuid.uuid4()}/rollback", headers=EU, json={"requestedBy": "x"}).status_code == 404


# ---- SEC-10.4: the approval path

def _hold(client):
    assert client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"}).status_code == 200


def _approve(client, approval_id):
    return client.post(f"/rapp-approvals/{approval_id}/approve", headers={**GUI, "X-R1-Acting-User": "smo-gui:alice"}, json={"decidedBy": "smo-gui:alice"})


def test_a_request_is_checked_when_it_is_made_and_the_claim_is_kept_with_it(client, places):
    """With an approval policy the scope is checked when the request is made (nothing is parked on refusal), and the requester's claim is kept with
    the parked request.
    """
    _hold(client)
    refused = _write(client, ["ME-3"], EU)
    assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED"
    with places["db"]() as db:
        assert db.query(RAppActionApproval).count() == 0                                         # nothing was parked
    parked = _write(client, ["ME-1", "ME-2"], EU)
    assert parked.status_code == 202 and parked.json()["status"] == "PENDING_APPROVAL"
    with places["db"]() as db:
        row = db.scalars(select(RAppActionApproval)).one()
        assert row.requester_scope == {"regions": ["eu"]}
    assert _approve(client, parked.json()["approvalId"]).status_code == 200
    assert places["values"]["ME-1"] == "20"


def test_an_element_that_moved_while_the_request_waited_refuses_the_approval(client, places):
    """The requester's claim at the time, the target as it is now: the approver (an operator, unscoped) does not widen what the rApp may touch."""
    _hold(client)
    approval_id = _write(client, ["ME-1", "ME-2"], EU).json()["approvalId"]
    assert client.put("/managed-entities/ME-2/scope", json={"region": "us", "tenant": "globex"}).status_code == 200
    resp = _approve(client, approval_id)
    assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED"
    assert places["edits"] == []
    view = client.get(f"/rapp-approvals/{approval_id}", headers=GUI).json()
    assert view["status"] == "REFUSED" and view["refusalCode"] == "SCOPE_DENIED" and view["jobId"] is None
    assert _jobs(places) == 0


def test_an_unscoped_requester_is_not_held_to_a_scope_at_approval(client, places):
    """A request parked by an unscoped requester is approved and written with no scope check."""
    _hold(client)
    approval_id = _write(client, ["ME-3", "ME-4"], UNSCOPED).json()["approvalId"]
    assert _approve(client, approval_id).status_code == 200 and places["edits"] == ["ME-3", "ME-4"]
    with places["db"]() as db:
        assert db.scalars(select(RAppActionApproval)).one().requester_scope is None


def test_a_requester_scoped_after_the_request_was_parked_is_not_held_to_the_later_claim(client, places):
    """Decided in the ADR: the claim is the one the request was made under (a snapshot); narrowing a claim does not reach a request already parked, rejecting it or
    stopping the rApp (kill switch, checked again at approval) does."""
    _hold(client)
    approval_id = _write(client, ["ME-3"], UNSCOPED).json()["approvalId"]
    assert _approve(client, approval_id).status_code == 200


# ---- SEC-10.5: reads of the configuration

def test_the_configuration_is_read_only_inside_the_scope(client, places):
    """Reading an element's configuration is 403 SCOPE_DENIED outside the scope, including for an unregistered element, and unchanged without a
    claim.
    """
    assert client.get("/managed-entities/ME-3/config").status_code == 200                                 # unscoped: as before
    assert client.get("/managed-entities/ME-1/config", headers=EU).status_code == 200
    for ref in ("ME-3", "ME-4", "ME-NOT-THERE"):
        resp = client.get(f"/managed-entities/{ref}/config", headers=EU)
        assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED", ref


def test_the_history_and_the_diff_of_an_element_are_scoped_too(client, places):
    """The config history and diff of an element are 403 for a caller whose scope does not cover it."""
    _write(client, ["ME-3"], GUI)
    assert client.get("/managed-entities/ME-3/config-history", headers=EU).status_code == 403
    assert client.get("/managed-entities/ME-3/config-history", headers=ACME).status_code == 200
    assert client.get("/managed-entities/ME-3/config-history").json()["items"]
    one = uuid.uuid4()
    assert client.get("/managed-entities/ME-3/config-history/diff", headers=EU, params={"from_snapshot": str(one), "to_snapshot": str(one)}).status_code == 403


def test_the_element_views_are_scoped(client, places):
    """The element list and cell guards show only the elements inside the claim, and reading one outside is 403."""
    assert [i["managedElementRef"] for i in client.get("/managed-entities", headers=EU).json()["items"]] == ["ME-1", "ME-2"]
    assert [i["managedElementRef"] for i in client.get("/managed-entities", headers=EU_ACME).json()["items"]] == ["ME-1"]
    assert client.get("/managed-entities", headers={**UNSCOPED, SCOPE_HEADER: claim(regions=["nowhere"])}).json()["items"] == []
    assert client.get("/managed-entities/ME-3", headers=EU).status_code == 403
    assert client.get("/managed-entities/ME-1", headers=EU).json()["region"] == "eu"
    guards = {"cellClass": "EMERGENCY"}
    for ref in ("ME-1", "ME-3"):
        assert client.put(f"/managed-entities/{ref}/cells/1/guards", json=guards).status_code == 200
    assert [g["managedElementRef"] for g in client.get("/cell-guards", headers=EU).json()["items"]] == ["ME-1"]
    assert len(client.get("/cell-guards").json()["items"]) == 2


def test_the_jobs_a_caller_sees_are_the_ones_inside_its_scope(client, places):
    """A caller sees the jobs all of whose elements are inside its scope, in the list and by id; a job touching an outside element, and an unknown
    id, are the same 404.
    """
    wide = {**UNSCOPED, SCOPE_HEADER: claim(regions=["eu", "us"])}                  # the same rApp, with a wider claim: the jobs are its own (PR-SEC-10.11), only the scope hides them
    inside = _write(client, ["ME-1", "ME-2"], wide).json()["jobId"]
    mixed = _write(client, ["ME-1", "ME-3"], wide).json()["jobId"]
    outside = _write(client, ["ME-3"], wide).json()["jobId"]
    listed = lambda headers: {j["jobId"] for j in client.get("/config-jobs", headers=headers).json()["items"]}      # noqa: E731
    assert listed({}) == {inside, mixed, outside} and listed(EU) == {inside}
    assert client.get(f"/config-jobs/{inside}", headers=EU).status_code == 200
    for hidden in (mixed, outside, str(uuid.uuid4())):
        resp = client.get(f"/config-jobs/{hidden}", headers=EU)
        assert resp.status_code == 404 and _title(resp) == "CONFIG_JOB_NOT_FOUND"                         # as if it did not exist
    assert client.get(f"/config-jobs/{mixed}").status_code == 200


# ---- SEC-10.6: alarms and PM

def _alarm(db_factory, ref):
    """Adds a major alarm on `ref` and returns its id."""
    with db_factory() as db:
        alarm = Alarm(source_alarm_id=f"a-{uuid.uuid4()}", managed_element_ref=ref, severity="major")
        db.add(alarm)
        db.commit()
        return str(alarm.alarm_id)


def test_the_alarm_list_is_filtered_to_the_scope_never_refused(client, places):
    """The alarm list shows only the alarms of elements inside the claim; asking for an outside element gives an empty page, not an error."""
    ids = {ref: _alarm(places["db"], ref) for ref in ELEMENTS}
    seen = lambda headers, **q: {a["managedElementRef"] for a in client.get("/alarms", headers=headers, params=q).json()["items"]}      # noqa: E731
    assert seen({}) == set(ELEMENTS) and seen(UNSCOPED) == set(ELEMENTS)
    assert seen(EU) == {"ME-1", "ME-2"} and seen(ACME) == {"ME-1", "ME-3"} and seen(EU_ACME) == {"ME-1"}
    outside = client.get("/alarms", headers=EU, params={"managed_element_ref": "ME-3"})
    assert outside.status_code == 200 and outside.json()["items"] == []            # an empty page, like an element with no alarms
    assert len(ids) == 4


def test_the_total_of_a_filtered_page_does_not_count_what_is_hidden(client, places):
    """The total of a filtered page counts only what the caller may see."""
    for ref in ELEMENTS:
        _alarm(places["db"], ref)
    page = client.get("/alarms", headers=EU).json()
    assert len(page["items"]) == 2 and page["total"] == 2
    assert client.get("/alarms").json()["total"] == 4


def test_an_alarm_outside_the_scope_cannot_be_acknowledged_or_cleared_and_looks_absent(client, places):
    """An alarm of an element outside the scope is 404 on acknowledge and clear and is left unchanged, while an unscoped caller can still clear it."""
    hidden, shown = _alarm(places["db"], "ME-3"), _alarm(places["db"], "ME-1")
    for action in (f"/alarms/{hidden}/ack?new_state=ACKNOWLEDGED", f"/alarms/{hidden}/clear"):
        resp = client.patch(action, headers=EU)
        assert resp.status_code == 404 and _title(resp) == "ALARM_NOT_FOUND"
    assert client.patch(f"/alarms/{uuid.uuid4()}/clear", headers=EU).status_code == 404
    with places["db"]() as db:
        assert db.get(Alarm, uuid.UUID(hidden)).severity == "major" and db.get(Alarm, uuid.UUID(hidden)).ack_state == "UNACKNOWLEDGED"
    assert client.patch(f"/alarms/{shown}/ack?new_state=ACKNOWLEDGED", headers=EU).status_code == 200
    assert client.patch(f"/alarms/{hidden}/clear").status_code == 200                                 # unscoped: as before


def test_pm_subscriptions_are_listed_created_and_removed_inside_the_scope_only(client, places, monkeypatch):
    """PM and FM subscriptions are listed, created and removed only for elements inside the scope; creating for an outside element is 403 and
    removing one is a silent no-op.
    """
    monkeypatch.setattr("app.main.R1Client", lambda *a, **k: type("R", (), {"post": lambda self, *a, **k: None})())
    with places["db"]() as db:
        for ref in ("ME-1", "ME-3"):
            db.add(PMSubscription(managed_element_ref=ref, counter_type="c", delivery_method="pull", southbound_engine="ProvMnS"))
            db.add(FMSubscription(managed_element_ref=ref, delivery_method="pull", southbound_engine="FaultMnS"))
        db.commit()
    for path in ("/pm-subscriptions", "/fm-subscriptions"):
        assert [s["managedElementRef"] for s in client.get(path, headers=EU).json()["items"]] == ["ME-1"]
        assert len(client.get(path).json()["items"]) == 2
    assert client.post("/pm-subscriptions", headers=EU, params={"managed_element_ref": "ME-3", "counter_type": "c", "delivery_method": "pull"}).status_code == 403
    assert client.post("/fm-subscriptions", headers=EU, params={"managed_element_ref": "ME-3", "delivery_method": "pull"}).status_code == 403
    with places["db"]() as db:
        outside = {"pm": db.scalars(select(PMSubscription).where(PMSubscription.managed_element_ref == "ME-3")).one().subscription_id,
                   "fm": db.scalars(select(FMSubscription).where(FMSubscription.managed_element_ref == "ME-3")).one().subscription_id}
    assert client.delete(f"/pm-subscriptions/{outside['pm']}", headers=EU).status_code == 204              # says nothing ...
    assert client.delete(f"/fm-subscriptions/{outside['fm']}", headers=EU).status_code == 204
    with places["db"]() as db:
        assert db.query(PMSubscription).count() == 2 and db.query(FMSubscription).count() == 2          # ... and removes nothing
    assert client.delete(f"/pm-subscriptions/{outside['pm']}").status_code == 204
    with places["db"]() as db:
        assert db.query(PMSubscription).count() == 1


def test_performance_files_are_listed_and_downloaded_inside_the_scope_only(client, places):
    """Performance files of elements outside the scope are left out of the list and are 404 on download."""
    ids = {}
    with places["db"]() as db:
        for ref in ("ME-1", "ME-3"):
            f = PMFile(managed_element_ref=ref, counter_type="c", content="{}", file_size=2, file_ready_time=datetime.datetime.now(datetime.UTC))
            db.add(f)
            db.flush()
            ids[ref] = str(f.file_id)
        db.commit()
    names = lambda headers: len(client.get("/files", headers=headers, params={"fileDataType": "Performance"}).json()["items"])      # noqa: E731
    assert names({}) == 2 and names(EU) == 1
    assert client.get(f"/pm-files/{ids['ME-1']}/file", headers=EU).status_code == 200
    assert client.get(f"/pm-files/{ids['ME-3']}/file", headers=EU).status_code == 404
    assert client.get(f"/pm-files/{ids['ME-3']}/file").status_code == 200


def test_the_scope_columns_are_read_fresh_not_from_the_identity_map(client, places):
    """A move of an element is seen by the next request in the same process (column selects, not a cached entity)."""
    assert _write(client, ["ME-1"], EU).status_code == 202
    client.put("/managed-entities/ME-1/scope", json={"region": "us", "tenant": "acme"})
    assert _write(client, ["ME-1"], EU).status_code == 403
    assert _write(client, ["ME-1"], {**UNSCOPED, SCOPE_HEADER: claim(regions=["us"])}).status_code == 202


def test_a_kpi_is_computed_over_the_elements_inside_the_scope_only(client, places):
    """Aggregating over `all` must not be a way to read the network's totals, or another tenant's cell."""
    import json
    t0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
    with places["db"]() as db:
        for ref, value in (("ME-1", 10.0), ("ME-3", 90.0)):
            content = json.dumps({"measurements": [{"cellId": "1", "timestamp": t0.isoformat(), "value": value}]})
            db.add(PMFile(managed_element_ref=ref, counter_type="RRU.PrbTotDl", content=content, file_size=len(content)))
        db.commit()
    assert client.put("/kpi-definitions/prb", json={"formula": "prb", "unit": "%", "counters": [{"counter": "RRU.PrbTotDl", "variable": "prb", "aggregation": "avg"}]}).status_code == 200
    params = {"from_time": t0.isoformat(), "to_time": (t0 + datetime.timedelta(hours=1)).isoformat()}
    value = lambda headers, **q: [(i["group"], i["value"]) for i in client.get("/kpis/prb", headers=headers, params={**params, **q}).json()["items"]]      # noqa: E731
    assert value({}, group_by="all") == [({}, 50.0)] and value(UNSCOPED, group_by="all") == [({}, 50.0)]
    assert value(EU, group_by="all") == [({}, 10.0)]                                                    # the average of its elements, not the network's
    assert [g["managedElementRef"] for g, _ in value(EU, group_by="element")] == ["ME-1"]
    assert value(EU, group_by="element", managed_element_ref="ME-3") == []                               # naming another's element reads nothing
    assert client.get("/kpis/prb", headers=EU, params={**params, "group_by": "all", "managed_element_ref": "ME-3"}).json()["items"][0]["reason"] == "NO_DATA"


def test_the_endpoint_list_shows_where_each_element_is_and_hides_the_rest_from_a_scoped_caller(client, places):
    """The endpoint list shows each element's region and tenant and, for a scoped caller, only the endpoints inside its claim."""
    listed = client.get("/o1-adaptor-endpoints").json()["items"]
    assert {e["managedElementRef"]: (e["region"], e["tenant"]) for e in listed} == PLACES
    assert sorted(e["managedElementRef"] for e in client.get("/o1-adaptor-endpoints", headers=EU).json()["items"]) == ["ME-1", "ME-2"]
    assert [e["managedElementRef"] for e in client.get("/o1-adaptor-endpoints", headers=EU_ACME).json()["items"]] == ["ME-1"]


def test_refusals_by_scope_are_recorded_once_per_attempt(client, places):
    """Each refused attempt is recorded once, and refused attempts write no snapshot."""
    for _ in range(3):
        _write(client, ["ME-3"], EU)
    with places["db"]() as db:
        assert db.query(SafeguardRefusal).filter(SafeguardRefusal.code == "SCOPE_DENIED").count() == 3
        assert db.query(CMSnapshot).count() == 0
