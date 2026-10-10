"""PR-SEC-10.9 and 10.11 (docs/adr/0005-tenant-region-authorization.md): the reads of RAN NF OAM that still showed every element to a caller with a scope claim, and the
ownership of a configuration job. The rule is the one of `test_scope.py`: ME-1 eu/acme, ME-2 eu/globex, ME-3 us/acme, ME-4 no place. A caller with an `eu` claim touches ME-1 and
ME-2. A reference the caller names is a 403; an id the system made, or a DN, is a 404 as if it did not exist; a list is filtered; nothing changes for an unscoped caller.
Run with: pytest tests/test_scope_reads.py -q
"""

import json
import uuid

import pytest

from smo_shared.scope import SCOPE_HEADER

from app import mo_tree
from app.models import Alarm, FileSubscription, KpiSchedule, ManagedEntity, ManagedObject, O1AdaptorEndpoint, O1AdaptorHostKey, PMFile, RAppActionApproval, RAppDecisionRecord, SoftwareManagementJob, WriteConfigJob, WriteConfigSubChange

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_scope import ACME, EU, EU_ACME, GUI, PLACES, UNSCOPED, _title, _write, claim, places  # noqa: F401  (places: a pytest fixture)
from test_waves import ELEMENTS, fleet  # noqa: F401

NOWHERE = {**UNSCOPED, SCOPE_HEADER: claim(regions=["nowhere"])}            # a claim nothing matches
OTHER_RAPP = {"X-R1-Invoker-Id": "ts-client", "X-R1-Role": "rapp", SCOPE_HEADER: claim(regions=["eu"])}
WIDE = {**UNSCOPED, SCOPE_HEADER: claim(regions=["eu", "us"])}
CELL = "NRCellDU=101"


def dn(ref):
    return f"ManagedElement={ref},GNBDUFunction=1,{CELL}"


@pytest.fixture
def tree(places):
    """Syncs the registry and creates a managed-object node (the cell `NRCellDU=101`) for each of ME-1..ME-4 on top of the `places` fixture, and returns that fixture."""
    with places["db"]() as db:
        for ref in ELEMENTS:
            mo_tree.sync_registry(db, db.get(ManagedEntity, ref))
            mo_tree.ensure(db, ref, dn(ref), "registry")
        db.commit()
    return places


# ---- the managed-object tree

def test_a_node_of_another_element_is_a_404_exactly_as_one_that_is_not_there(client, tree):
    """A managed object of an element outside the caller's scope answers 404 with the same title as a missing DN, so a scoped caller cannot learn which DNs exist."""
    assert client.get(f"/managed-objects/{dn('ME-3')}").status_code == 200                        # unscoped: as before
    assert client.get(f"/managed-objects/{dn('ME-1')}", headers=EU).json()["managedElementRef"] == "ME-1"
    hidden, absent = (client.get(f"/managed-objects/{x}", headers=EU) for x in (dn("ME-3"), "ManagedElement=ME-9,GNBDUFunction=1"))
    assert hidden.status_code == absent.status_code == 404
    assert _title(hidden) == _title(absent) == "MANAGED_OBJECT_NOT_FOUND"                        # the answer does not tell a scoped caller which DNs exist
    assert client.get(f"/managed-objects/{dn('ME-4')}", headers=EU).status_code == 404            # no region at all


def test_children_and_subtree_leave_out_the_nodes_of_other_elements(client, tree):
    """Children and subtree filter row by row, so a node of another element hung below a visible node is left out, and the children or subtree of a hidden element are 404."""
    with tree["db"]() as db:       # a node of ME-3 hung below ME-1's function: a row-level filter, not only a check of the node asked for
        db.add(ManagedObject(dn=f"{dn('ME-1')},Stray=1", parent_dn=dn("ME-1"), object_class="Stray", object_id="1", managed_element_ref="ME-3", source="walk"))
        db.add(ManagedObject(dn=f"{dn('ME-1')},Own=1", parent_dn=dn("ME-1"), object_class="Own", object_id="1", managed_element_ref="ME-1", source="walk"))
        db.commit()
    kids = lambda headers: [i["id"] for i in client.get(f"/managed-objects/{dn('ME-1')}/children", headers=headers).json()["items"]]      # noqa: E731
    assert kids({}) == ["1", "1"] and kids(EU) == ["1"] and client.get(f"/managed-objects/{dn('ME-1')}/children", headers=EU).json()["total"] == 1
    walk = lambda headers: client.get(f"/managed-objects/{dn('ME-1')}/subtree", headers=headers).json()["tree"]["children"]      # noqa: E731
    assert [n["class"] for n in walk({})] == ["Own", "Stray"] and [n["class"] for n in walk(EU)] == ["Own"]
    for path in ("children", "subtree"):
        assert client.get(f"/managed-objects/{dn('ME-3')}/{path}", headers=EU).status_code == 404


def test_the_walk_of_an_element_outside_the_scope_is_refused_by_reference(client, tree):
    """A refresh (walk) of an element the caller names but may not touch, or that does not exist, is refused with 403 SCOPE_DENIED, while an unscoped caller is not refused."""
    for ref in ("ME-3", "ME-4", "ME-NOT-THERE"):
        refused = client.post(f"/managed-entities/{ref}/managed-objects/refresh", headers=EU)
        assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED", ref
    assert _title(client.post("/managed-entities/ME-1/managed-objects/refresh", headers=EU)) != "SCOPE_DENIED"     # inside: it goes on to the endpoint check
    assert client.post("/managed-entities/ME-3/managed-objects/refresh").status_code != 403                   # unscoped: as before


def test_the_topology_export_holds_the_nodes_of_the_callers_elements_only(client, tree):
    """The topology export lists only the nodes of the elements inside the caller's claim (nothing for a claim that matches nothing) and is unchanged for an unscoped caller."""
    refs = lambda headers, **q: {a["attributes"]["managedElementRef"] for group in client.get("/topology", headers=headers, params=q).json()["entities"]      # noqa: E731
                                 for a in group["o-ran-smo-teiv-ran:ManagedObject"]}
    assert refs({}) == set(ELEMENTS) and refs(UNSCOPED) == set(ELEMENTS)
    assert refs(EU) == {"ME-1", "ME-2"} and refs(EU_ACME) == {"ME-1"} and refs(NOWHERE) == set()
    assert refs(EU, managed_element_ref="ME-3") == set()                                      # naming another's element exports nothing
    assert client.get("/topology", headers=NOWHERE).json() == {"entities": [], "relationships": []}


def _guards(client, ref, cell, neighbours):
    """Sets the cell guards of one cell with the given neighbour cell ids, and checks that the write was accepted."""
    assert client.put(f"/managed-entities/{ref}/cells/{cell}/guards", json={"cellClass": "NORMAL", "neighbourRefs": neighbours}).status_code == 200


def test_the_links_never_name_an_element_outside_the_scope(client, tree):
    """In the links list a scoped caller sees a cell on an element it may not touch only as EXTERNAL with no element named, and never a link that starts on such an element."""
    _guards(client, "ME-1", "1", ["2", "3", "7"])         # 2 is on ME-2 (eu), 3 on ME-3 (us), 7 on ME-3 and ME-4: ambiguous when both are seen
    _guards(client, "ME-2", "2", ["1"])
    _guards(client, "ME-3", "3", ["1"])
    _guards(client, "ME-3", "7", [])
    _guards(client, "ME-4", "7", [])
    by_cell = lambda headers, **q: {(l["aCell"], l["bCell"]): l for l in client.get("/topology/links", headers=headers, params=q).json()["items"]}      # noqa: E731
    everything = by_cell({})
    assert everything[("1", "2")]["linkType"] == "INTER_ELEMENT" and everything[("1", "3")]["linkType"] == "INTER_ELEMENT" and everything[("1", "7")]["linkType"] == "AMBIGUOUS"
    assert ("3", "1") in everything
    eu = by_cell(EU)
    assert eu[("1", "2")]["linkType"] == "INTER_ELEMENT" and eu[("1", "2")]["reciprocal"] is True
    assert eu[("1", "3")]["linkType"] == "EXTERNAL" and eu[("1", "3")]["bElement"] is None                  # a cell on an element it may not touch is not part of its world
    assert eu[("1", "7")]["linkType"] == "EXTERNAL" and ("3", "1") not in eu and ("7", "7") not in eu
    assert "ME-3" not in json.dumps(client.get("/topology/links", headers=EU).json()) and "ME-4" not in json.dumps(client.get("/topology/links", headers=EU).json())
    assert by_cell(NOWHERE) == {} and by_cell(EU, managed_element_ref="ME-3") == {}
    assert by_cell(UNSCOPED) == everything


def test_the_relation_of_two_nodes_needs_both_in_the_tree_the_caller_sees(client, tree):
    """The relation of two nodes is answered only when both are in the tree the caller sees; if either is not, it is a 404 as for a missing node."""
    one, two, three = dn("ME-1"), dn("ME-2"), dn("ME-3")
    relation = lambda a, b, headers: client.get("/topology/relation", headers=headers, params={"a": a, "b": b})      # noqa: E731
    assert relation(one, three, {}).json()["relation"] == "DIFFERENT_ELEMENT" and relation(one, two, EU).json()["relation"] == "DIFFERENT_ELEMENT"
    for pair in ((one, three), (three, one), (three, three)):
        assert relation(*pair, EU).status_code == 404 and _title(relation(*pair, EU)) == "MANAGED_OBJECT_NOT_FOUND"


# ---- the KPI schedules

@pytest.fixture
def schedules(client, places):
    """Defines the `prb` KPI and three schedules (`eu` on ME-1, `us` on ME-3, `net` on the whole network) on top of the `places` fixture, and returns that fixture."""
    assert client.put("/kpi-definitions/prb", json={"formula": "prb", "unit": "%", "counters": [{"counter": "RRU.PrbTotDl", "variable": "prb", "aggregation": "avg"}]}).status_code == 200
    for name, element in (("eu", "ME-1"), ("us", "ME-3"), ("net", None)):
        body = {"kpi": "prb", "intervalSeconds": 300, **({"managedElementRef": element} if element else {})}
        assert client.put(f"/kpi-schedules/{name}", json=body).status_code == 200
    return places


def test_the_schedules_a_caller_sees_are_those_of_its_own_elements(client, schedules):
    """A scoped caller lists and reads only the KPI schedules of its own elements (the whole-network one is for unscoped callers) and a hidden one is a 404 like a missing one."""
    names = lambda headers: [s["scheduleId"] for s in client.get("/kpi-schedules", headers=headers).json()["items"]]      # noqa: E731
    assert names({}) == names(UNSCOPED) == names(GUI) == ["eu", "net", "us"]
    assert names(EU) == ["eu"] and names(NOWHERE) == []                                          # the schedule of the whole network is for an unscoped caller
    assert client.get("/kpi-schedules/eu", headers=EU).status_code == 200
    for hidden in ("us", "net", "nothing"):
        gone = client.get(f"/kpi-schedules/{hidden}", headers=EU)
        assert gone.status_code == 404 and _title(gone) == "KPI_SCHEDULE_NOT_FOUND"
    assert client.get("/kpi-schedules/us").status_code == 200


def test_a_scoped_caller_cannot_make_or_replace_a_schedule_outside_its_scope(client, schedules):
    """A scoped caller's PUT of a schedule for an element outside its scope, the whole network, or an unknown element, or over a hidden schedule id, is refused with 403 and changes nothing."""
    body = {"kpi": "prb", "intervalSeconds": 600}
    for name, element in (("new-us", "ME-3"), ("new-net", None), ("new-none", "ME-4"), ("new-missing", "ME-NOT-THERE"), ("us", "ME-1"), ("net", "ME-1")):
        refused = client.put(f"/kpi-schedules/{name}", headers=EU, json={**body, **({"managedElementRef": element} if element else {})})
        assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED", name              # an id that exists and is hidden is not replaced either
    with schedules["db"]() as db:
        assert {r.schedule_id: (r.managed_element_ref, r.interval_seconds) for r in db.query(KpiSchedule)} == {"eu": ("ME-1", 300), "us": ("ME-3", 300), "net": (None, 300)}
    assert client.put("/kpi-schedules/new-eu", headers=EU, json={**body, "managedElementRef": "ME-2"}).status_code == 200
    assert client.put("/kpi-schedules/eu", headers=EU, json={**body, "managedElementRef": "ME-1"}).json()["intervalSeconds"] == 600
    assert client.put("/kpi-schedules/anywhere", json=body).status_code == 200                      # unscoped: as before


def test_a_scoped_caller_removes_only_its_own_schedules(client, schedules):
    """A scoped caller deleting a schedule of another element or of the whole network gets 404 and removes nothing, while it can delete its own."""
    for hidden in ("us", "net"):
        assert client.delete(f"/kpi-schedules/{hidden}", headers=EU).status_code == 404
    with schedules["db"]() as db:
        assert db.query(KpiSchedule).count() == 3
    assert client.delete("/kpi-schedules/eu", headers=EU).status_code == 204
    assert client.delete("/kpi-schedules/net").status_code == 204


# ---- the file subscriptions

def test_a_file_subscription_is_the_whole_networks_so_a_scoped_caller_has_none(client, places):
    """Creating a file subscription is refused with 403 for any scoped (or unreadable) claim, and a scoped delete answers 204 and removes nothing, because such a subscription covers the whole network."""
    body = {"consumerReference": "http://consumer.example/notify", "fileDataType": "Performance"}
    for headers in (EU, ACME, NOWHERE, {**UNSCOPED, SCOPE_HEADER: "not json"}):
        refused = client.post("/file-subscriptions", json=body, headers=headers)
        assert refused.status_code == 403 and _title(refused) == "SCOPE_DENIED"
    with places["db"]() as db:
        assert db.query(FileSubscription).count() == 0
    for headers in ({}, UNSCOPED, GUI):
        assert client.post("/file-subscriptions", json=body, headers=headers).status_code == 201       # unscoped: as before
    with places["db"]() as db:
        ids = [row.subscription_id for row in db.query(FileSubscription)]
    assert client.delete(f"/file-subscriptions/{ids[0]}", headers=EU).status_code == 204              # says nothing ...
    with places["db"]() as db:
        assert db.query(FileSubscription).count() == 3                                                 # ... and removes nothing
    assert client.delete(f"/file-subscriptions/{ids[0]}", headers=GUI).status_code == 204
    with places["db"]() as db:
        assert db.query(FileSubscription).count() == 2


# ---- the registries

VENDOR = {"supportedServices": ["PROV", "FM", "PM", "FILE"], "supportedVendorModes": ["O1_NETCONF"]}


@pytest.fixture
def vendors(client, places):
    """Gives ME-1 and ME-3 different vendors, loads a CM schema for each vendor and registers three vendor capabilities (one used by no element), on top of the `places` fixture."""
    with places["db"]() as db:
        db.get(ManagedEntity, "ME-1").vendor_name = "acme-ran"
        db.get(ManagedEntity, "ME-3").vendor_name = "other-ran"
        db.commit()
    for name in ("acme", "other"):
        loaded = client.post("/cm-schemas", json={"schemaName": f"{name}-model", "revision": "1", "location": f"https://models.example/{name}", "descriptor": {"classes": {"NRCellDU": {"x": {"type": "string"}}}}})
        assert loaded.status_code == 201
    client.put("/vendor-capabilities/acme-ran", json={**VENDOR, "conformanceMode": "OWN", "schemaRef": {"schemaName": "acme-model", "revision": "1"}})
    client.put("/vendor-capabilities/other-ran", json={**VENDOR, "conformanceMode": "OWN", "schemaRef": {"schemaName": "other-model", "revision": "1"}, "supportedVendorModes": ["O1_NETCONF", "O1_RESTCONF"]})
    client.put("/vendor-capabilities/unused-ran", json={**VENDOR, "conformanceMode": "SPEC"})
    return places


def test_a_scoped_caller_reads_the_capabilities_of_the_vendors_of_its_own_elements(client, vendors):
    """A scoped caller lists and reads the capabilities of only the vendors of its own elements, and another vendor is a 404 like an unknown one."""
    names = lambda headers: [v["vendorName"] for v in client.get("/vendor-capabilities", headers=headers).json()["items"]]      # noqa: E731
    assert names({}) == names(UNSCOPED) == ["acme-ran", "other-ran", "unused-ran"]
    assert names(EU) == ["acme-ran"] and names(ACME) == ["acme-ran", "other-ran"] and names(NOWHERE) == []
    assert client.get("/vendor-capabilities", headers=EU).json()["total"] == 1
    assert client.get("/vendor-capabilities/acme-ran", headers=EU).status_code == 200
    hidden, absent = (client.get(f"/vendor-capabilities/{v}", headers=EU) for v in ("other-ran", "no-such-vendor"))
    assert hidden.status_code == absent.status_code == 404 and _title(hidden) == _title(absent) == "VENDOR_CAPABILITY_NOT_FOUND"
    assert client.get("/vendor-capabilities/other-ran").status_code == 200


def test_the_summary_of_what_can_be_driven_is_over_the_callers_vendors(client, vendors):
    """The capabilities summary lists only the vendors of the caller's elements and the vendor modes of those vendors, leaving the MnS services unchanged."""
    summary = lambda headers: client.get("/capabilities", headers=headers).json()      # noqa: E731
    assert summary({})["supportedVendorModes"] == ["O1_NETCONF", "O1_RESTCONF"] and len(summary({})["vendors"]) == 3
    assert [v["vendorName"] for v in summary(EU)["vendors"]] == ["acme-ran"] and summary(EU)["supportedVendorModes"] == ["O1_NETCONF"]
    assert summary(NOWHERE)["vendors"] == [] and summary(NOWHERE)["supportedVendorModes"] == [] and summary(NOWHERE)["mnsServices"] == summary({})["mnsServices"]


def test_the_loaded_cm_schemas_shown_are_those_the_callers_vendors_use(client, vendors):
    """A scoped caller sees the loaded CM schemas used by its own vendors (a hidden one is a 404), while the bundled schemas stay visible to everyone."""
    loaded = lambda headers: sorted(s["schemaName"] for s in client.get("/cm-schemas", headers=headers, params={"limit": 500}).json()["items"] if not s["builtin"])      # noqa: E731
    builtin = lambda headers: sorted(s["schemaName"] for s in client.get("/cm-schemas", headers=headers, params={"limit": 500}).json()["items"] if s["builtin"])      # noqa: E731
    assert loaded({}) == ["acme-model", "other-model"] and loaded(EU) == ["acme-model"] and loaded(NOWHERE) == []
    assert builtin(EU) == builtin(NOWHERE) == builtin({}) and "3gpp-ts28541-nrnrm" in builtin(EU)            # the bundled models are the same everywhere and about no element
    assert client.get("/cm-schemas/acme-model", headers=EU, params={"revision": "1"}).status_code == 200
    hidden = client.get("/cm-schemas/other-model", headers=EU, params={"revision": "1"})
    assert hidden.status_code == 404 and _title(hidden) == "CM_SCHEMA_NOT_FOUND"
    assert client.get("/cm-schemas/3gpp-ts28541-nrnrm", headers=NOWHERE, params={"revision": "19.6.0"}).status_code == 200
    assert client.get("/cm-schemas/other-model", params={"revision": "1"}).status_code == 200


def test_a_scoped_caller_cannot_edit_the_guards_of_an_element_outside_its_scope(client, places):
    """Setting or removing the cell guards of an element outside the caller's scope, or of an unknown element, is refused with 403, while inside the scope it works."""
    guard = {"cellClass": "EMERGENCY"}
    for ref in ("ME-3", "ME-4", "ME-NOT-THERE"):
        assert client.put(f"/managed-entities/{ref}/cells/1/guards", headers=EU, json=guard).status_code == 403
        assert client.delete(f"/managed-entities/{ref}/cells/1/guards", headers=EU).status_code == 403
    assert client.put("/managed-entities/ME-1/cells/1/guards", headers=EU, json=guard).status_code == 200
    assert client.delete("/managed-entities/ME-1/cells/1/guards", headers=EU).status_code == 204
    assert client.put("/managed-entities/ME-3/cells/1/guards", json=guard).status_code == 200            # unscoped: as before


def test_the_host_keys_of_an_endpoint_outside_the_scope_are_a_404(client, places):
    """The host keys of an adaptor endpoint of a hidden element are a 404 like those of an unknown endpoint, for both reading and deleting."""
    with places["db"]() as db:
        ids = {e.managed_element_ref: e.endpoint_id for e in db.query(O1AdaptorEndpoint)}
        for endpoint in db.query(O1AdaptorEndpoint):
            endpoint.transport = "ssh"
        db.commit()
        O1AdaptorHostKey.__table__.create(bind=db.get_bind(), checkfirst=True)
    assert client.get(f"/o1-adaptor-endpoints/{ids['ME-1']}/host-keys", headers=EU).json() == {"items": []}
    assert client.get(f"/o1-adaptor-endpoints/{ids['ME-3']}/host-keys").json() == {"items": []}
    for path in (f"{ids['ME-3']}/host-keys", f"{uuid.uuid4()}/host-keys"):
        gone = client.get(f"/o1-adaptor-endpoints/{path}", headers=EU)
        assert gone.status_code == 404 and _title(gone) == "O1_ENDPOINT_NOT_FOUND"
    assert client.delete(f"/o1-adaptor-endpoints/{ids['ME-3']}/host-keys/ssh-ed25519", headers=EU).status_code == 404


# ---- SEC-10.11: whose job it is

def _rapp_job(client, refs, headers):
    """Makes a configuration job on the given elements as the given caller, checks that it was accepted (202) and returns its job id."""
    resp = _write(client, refs, headers)
    assert resp.status_code == 202, resp.text
    return resp.json()["jobId"]


def test_a_scoped_rapp_reads_and_undoes_only_its_own_jobs(client, places):
    """A scoped rApp lists, reads and rolls back only the jobs it made, another rApp's or an operator's job in the same region is a 404, and nothing is written back for them."""
    mine = _rapp_job(client, ["ME-1"], EU)
    theirs = _rapp_job(client, ["ME-2"], OTHER_RAPP)                      # another rApp, the same region: inside the scope, not its job
    operators = _rapp_job(client, ["ME-1"], GUI)
    listed = lambda headers: {j["jobId"] for j in client.get("/config-jobs", headers=headers).json()["items"]}      # noqa: E731
    assert listed(EU) == {mine} and listed(OTHER_RAPP) == {theirs}
    assert listed({}) == listed(GUI) == {mine, theirs, operators}
    assert client.get(f"/config-jobs/{mine}", headers=EU).status_code == 200
    for other in (theirs, operators):
        gone = client.get(f"/config-jobs/{other}", headers=EU)
        assert gone.status_code == 404 and _title(gone) == "CONFIG_JOB_NOT_FOUND"                      # as if it did not exist
        undo = client.post(f"/config-jobs/{other}/rollback", headers=EU, json={"requestedBy": "es-rapp"})
        assert undo.status_code == 404 and _title(undo) == "CONFIG_JOB_NOT_FOUND"
        assert client.post(f"/config-jobs/{other}/rollback", headers=EU, json={"requestedBy": "es-rapp", "dryRun": True}).status_code == 404
    assert places["values"]["ME-2"] == "20"                                                           # nothing was written back
    assert client.get(f"/config-jobs/{theirs}", headers=GUI).status_code == 200                      # an SMO module / the operator reads any job
    assert client.post(f"/config-jobs/{theirs}/rollback", headers=GUI, json={"requestedBy": "alice"}).status_code == 202


def test_the_job_that_undoes_a_job_belongs_to_whom_the_original_belongs_to(client, places):
    """A rollback job (and a rollback of a rollback) belongs to the owner of the original job, including when an operator made the rollback."""
    mine = _rapp_job(client, ["ME-1"], EU)
    undo = client.post(f"/config-jobs/{mine}/rollback", headers=EU, json={"requestedBy": "es-rapp"})
    assert undo.status_code == 202
    again = client.post(f"/config-jobs/{undo.json()['jobId']}/rollback", headers=EU, json={"requestedBy": "es-rapp"})        # an undo of the undo: two deep
    assert again.status_code == 202, again.text
    listed = lambda headers: {j["jobId"] for j in client.get("/config-jobs", headers=headers).json()["items"]}      # noqa: E731
    assert listed(EU) == {mine, undo.json()["jobId"], again.json()["jobId"]} and listed(OTHER_RAPP) == set()
    assert client.get(f"/config-jobs/{undo.json()['jobId']}", headers=EU).status_code == 200
    assert client.get(f"/config-jobs/{again.json()['jobId']}", headers=OTHER_RAPP).status_code == 404
    operators_undo = client.post(f"/config-jobs/{_rapp_job(client, ['ME-2'], EU)}/rollback", headers=GUI, json={"requestedBy": "alice"}).json()["jobId"]
    assert client.get(f"/config-jobs/{operators_undo}", headers=EU).status_code == 200            # an operator's undo of the rApp's job is still visible to the rApp


def test_an_smo_module_acting_for_an_rapp_is_held_to_that_rapps_jobs(client, places):
    """An SMO module acting on behalf of an rApp sees only that rApp's jobs, and a job it creates for the rApp is the rApp's."""
    mine = _rapp_job(client, ["ME-1"], EU)
    operators = _rapp_job(client, ["ME-1"], GUI)
    for_the_rapp = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "dme-client", "X-R1-On-Behalf-Of": "es-client", "X-R1-On-Behalf-Scope": claim(regions=["eu"])}
    assert client.get(f"/config-jobs/{mine}", headers=for_the_rapp).status_code == 200
    assert client.get(f"/config-jobs/{operators}", headers=for_the_rapp).status_code == 404
    mine_through_dme = client.post("/config-jobs", headers=for_the_rapp, json={"requestedBy": "es-rapp", "scope": "cell", "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 5}}]})
    assert client.get(f"/config-jobs/{mine_through_dme.json()['jobId']}", headers=EU).status_code == 200      # a job made for the rApp through DME is the rApp's


def test_nothing_changes_for_a_caller_without_a_claim_or_for_an_smo_module(client, places):
    """Callers with no claim, and SMO modules even when given a claim, still read every job, while an rApp's scoped claim hides jobs that are not its own."""
    job = _rapp_job(client, ["ME-1"], GUI)
    theirs = _rapp_job(client, ["ME-2"], UNSCOPED)
    internal_with_claim = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker", SCOPE_HEADER: claim(regions=["eu"])}      # an SMO module the operator gave a claim: scoped, not an rApp
    for headers in ({}, UNSCOPED, GUI, internal_with_claim):
        assert client.get(f"/config-jobs/{job}", headers=headers).status_code == 200, headers
        assert job in {j["jobId"] for j in client.get("/config-jobs", headers=headers).json()["items"]}
    assert client.get(f"/config-jobs/{theirs}", headers=OTHER_RAPP).status_code == 404
    assert client.post(f"/config-jobs/{theirs}/rollback", headers=UNSCOPED, json={"requestedBy": "x"}).status_code == 202      # an rApp nobody scoped: as before


def test_a_claim_with_no_invoker_owns_nothing(client, places):
    """A claim without an invoker id owns no job: the call neither reads one nor lists any, instead of being read as unscoped."""
    job = _rapp_job(client, ["ME-1"], EU)
    anonymous = {SCOPE_HEADER: claim(regions=["eu"]), "X-R1-Role": "rapp"}
    assert client.get(f"/config-jobs/{job}", headers=anonymous).status_code == 404
    assert client.get("/config-jobs", headers=anonymous).json()["items"] == []


def test_an_approval_request_and_a_decision_record_are_read_by_their_owner_only(client, places):
    """An approval request and a decision record can be read by the rApp that owns them (and by an operator or unscoped caller) but not by another rApp, which gets a 404."""
    assert client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"}).status_code == 200
    parked = _write(client, ["ME-1"], EU)
    assert parked.json()["status"] == "PENDING_APPROVAL"
    approval = parked.json()["approvalId"]
    assert client.get(f"/rapp-approvals/{approval}", headers=EU).status_code == 200
    gone = client.get(f"/rapp-approvals/{approval}", headers=OTHER_RAPP)
    assert gone.status_code == 404 and _title(gone) == "APPROVAL_NOT_FOUND"
    for headers in ({}, GUI, UNSCOPED):
        assert client.get(f"/rapp-approvals/{approval}", headers=headers).status_code == 200
    with places["db"]() as db:
        record = db.query(RAppDecisionRecord).first() or RAppDecisionRecord(
            invoker_id="es-client", requested_by="x", disposition="DIRECT", managed_elements=["ME-1"], change_count=1, content_hash="0" * 64)
        db.add(record)
        db.commit()
        decision = str(record.decision_id)
        owner = record.invoker_id
    other = {**OTHER_RAPP, "X-R1-Invoker-Id": "somebody-else"}
    own = {**EU, "X-R1-Invoker-Id": owner}
    assert client.get(f"/decision-records/{decision}", headers=own).status_code == 200
    assert client.get(f"/decision-records/{decision}", headers=other).status_code == 404
    assert client.get(f"/decision-records/{decision}", headers=GUI).status_code == 200 and client.get(f"/decision-records/{decision}").status_code == 200
    with places["db"]() as db:
        assert db.query(RAppActionApproval).count() == 1


# ---- the walk: every read route of the module is classified, and the ones about elements show a claim that matches nothing nothing

ABOUT_ELEMENTS = {
    "/alarms", "/cell-guards", "/config-jobs", "/config-jobs/{job_id}", "/element-onboarding", "/element-onboarding/{managed_element_ref}", "/files", "/fm-subscriptions",
    "/kpis/{name}", "/managed-entities", "/managed-entities/{managed_element_ref}", "/managed-entities/{managed_element_ref}/config",
    "/managed-entities/{managed_element_ref}/config-history", "/managed-entities/{managed_element_ref}/config-history/diff", "/managed-objects/{dn}",
    "/managed-objects/{dn}/children", "/managed-objects/{dn}/subtree", "/o1-adaptor-endpoints", "/o1-adaptor-endpoints/{endpoint_id}/host-keys", "/pm-files/{file_id}/file",
    "/pm-subscriptions", "/software-campaigns", "/software-campaigns/{campaign_id}", "/software-campaigns/{campaign_id}/report", "/software-management-jobs",
    "/topology", "/topology/links", "/topology/relation", "/kpi-schedules", "/kpi-schedules/{schedule_id}", "/vendor-capabilities", "/vendor-capabilities/{vendor_name}",
    "/capabilities", "/cm-schemas", "/cm-schemas/{schema_name}", "/rapp-approvals/{approval_id}", "/decision-records/{decision_id}",
    # PR-GUI-9.3/9.4/9.8: the console's aggregates count only the caller's elements
    "/alarms/counts", "/alarms/stats", "/alarms/{alarm_id}/correlated", "/managed-entities/health", "/managed-entities/scopes", "/managed-entities/worst",
    "/topology/links/counts",
    # MGT-8.2 / 8.3: an alarm's history and comments, read as the alarm itself is
    "/alarms/{alarm_id}/history", "/alarms/{alarm_id}/comments",
}
NOT_ABOUT_ELEMENTS = {            # route: why a claim has nothing to match on (a new read route must be put in one of the two sets, with its reason)
    "/health": "probe", "/live": "probe", "/ready": "probe", "/version": "build information",
    "/kpi-definitions": "a formula over counter names, no element", "/kpi-definitions/standard": "the seeded set, no element", "/kpi-definitions/{name}": "a formula, no element",
    "/onboarding-templates": "a template of CM values to apply, no element", "/onboarding-templates/{name}": "a template, no element",
    "/msac/access-rules": "access-control administration (internal)", "/msac/access-rules/{rule_id}": "access-control administration (internal)",
    "/msac/identities": "access-control administration (internal)", "/msac/identities/{identity_id}": "access-control administration (internal)",
    "/msac/roles": "access-control administration (internal)", "/msac/roles/{role_id}": "access-control administration (internal)",
    "/rapp-approval-policy/{invoker_id_}": "a safeguard setting named by an invoker id", "/rapp-kill": "internal-only at R1", "/rapp-kill/{invoker_id_}": "a safeguard setting named by an invoker id",
    "/rapp-limits/{invoker_id_}": "a safeguard setting named by an invoker id", "/rapp-approvals": "internal-only at R1", "/decision-records": "internal-only at R1",
    "/safeguard-refusals": "internal-only at R1", "/safeguard-subscriptions": "internal-only at R1", "/approval-subscriptions": "internal-only at R1",
    "/lifecycle-subscriptions": "internal-only at R1", "/decision-records/export.csv": "internal-only at R1 (the list's rows, as CSV)",
}


def _read_routes():
    from app.main import app
    return {path for path, ops in app.openapi()["paths"].items() if "get" in ops}


def test_every_read_route_is_classified():
    """Every GET route of the module is in exactly one of the two sets (about elements, or not), so a new read route cannot be added without deciding whether a claim has to filter it."""
    routes = _read_routes()
    assert routes <= ABOUT_ELEMENTS | NOT_ABOUT_ELEMENTS.keys(), f"a read route nobody decided about: {sorted(routes - ABOUT_ELEMENTS - NOT_ABOUT_ELEMENTS.keys())}"
    assert ABOUT_ELEMENTS <= routes and NOT_ABOUT_ELEMENTS.keys() <= routes, "a route in the lists is not a route any more"
    assert not ABOUT_ELEMENTS & NOT_ABOUT_ELEMENTS.keys()


def _seed_everything(client, tree):
    """One of everything on ME-3 (us, acme), which the caller below may not touch."""
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    with tree["db"]() as db:
        db.add(Alarm(source_alarm_id="a", managed_element_ref="ME-3", severity="major"))
        db.add(SoftwareManagementJob(managed_element_ref="ME-3"))
        db.add(PMFile(managed_element_ref="ME-3", counter_type="c", content="{}", file_size=2, file_ready_time=now))
        db.commit()
    client.put("/managed-entities/ME-3/cells/1/guards", json={"cellClass": "NORMAL"})
    with tree["db"]() as db:
        job = WriteConfigJob(requested_by="x", scope="cell", invoker_id="es-client")
        db.add(job)
        db.flush()
        db.add(WriteConfigSubChange(job_id=job.job_id, managed_element_ref="ME-3", attribute_changes={}, status="APPLIED", position=0, wave=1))
        db.commit()
        return str(job.job_id)


def _nothing(response, name):
    """Tells whether a response to a caller whose claim matches nothing shows nothing: a 403 or 404, an empty list (bundled schemas excepted), an empty topology or vendor
    list, or a console aggregate that counts nothing (no groups, regions or links, no open or acknowledged alarm)."""
    if response.status_code in (403, 404):
        return True
    body = response.json()
    if isinstance(body, list):                                                                    # /managed-entities/worst: a bare list
        return body == []
    if "items" in body:
        return all(item.get("builtin") for item in body["items"]) if name == "/cm-schemas" else body["items"] == []
    return {"/topology": body.get("entities") == [] and body.get("relationships") == [], "/capabilities": body.get("vendors") == [], "/files": False,
            "/alarms/counts": body.get("groups") == [], "/alarms/stats": body.get("open") == 0 and body.get("acked") == 0,
            "/managed-entities/health": body.get("groups") == [], "/managed-entities/scopes": body.get("regions") == [],
            "/topology/links/counts": body.get("total") == 0}.get(name, False)


def test_a_claim_that_matches_nothing_sees_nothing_of_what_is_there(client, vendors, tmp_path):
    """With one of everything seeded on ME-3, a claim that matches nothing gets nothing back from any read route about elements."""
    tree = vendors
    with tree["db"]() as db:
        for ref in ELEMENTS:
            mo_tree.sync_registry(db, db.get(ManagedEntity, ref))
        db.commit()
    job = _seed_everything(client, tree)
    client.put("/kpi-definitions/prb", json={"formula": "prb", "unit": "%", "counters": [{"counter": "RRU.PrbTotDl", "variable": "prb", "aggregation": "avg"}]})
    client.put("/kpi-schedules/s", json={"kpi": "prb", "intervalSeconds": 300, "managedElementRef": "ME-3"})
    with tree["db"]() as db:
        alarm, endpoint = db.query(Alarm).one(), db.query(O1AdaptorEndpoint).filter_by(managed_element_ref="ME-3").one()
        endpoint.transport = "ssh"
        pm_file = db.query(PMFile).one()
        db.commit()
        ids = {"job_id": job, "file_id": pm_file.file_id, "endpoint_id": endpoint.endpoint_id, "alarm_id": alarm.alarm_id}
    params = {"/files": {"fileDataType": "Performance"}, "/alarms/counts": {"group_by": "severity"}, "/kpis/{name}": {"from_time": "2026-01-01T00:00:00Z", "group_by": "element"},
              "/topology/relation": {"a": dn("ME-3"), "b": dn("ME-3")}, "/managed-entities/{managed_element_ref}/config-history/diff": {"from_snapshot": str(uuid.uuid4()), "to_snapshot": str(uuid.uuid4())}}
    values = {"job_id": ids["job_id"], "managed_element_ref": "ME-3", "dn": dn("ME-3"), "file_id": ids["file_id"], "endpoint_id": ids["endpoint_id"], "name": "prb", "schedule_id": "s",
              "vendor_name": "other-ran", "schema_name": "other-model", "campaign_id": uuid.uuid4(), "approval_id": uuid.uuid4(), "decision_id": uuid.uuid4(),
              "alarm_id": ids["alarm_id"]}
    for route in sorted(ABOUT_ELEMENTS):
        path = route.format(**values)
        response = client.get(path, headers=NOWHERE, params={**params.get(route, {}), **({"revision": "1"} if route.startswith("/cm-schemas/") else {})})
        assert _nothing(response, route), f"{route}: {response.status_code} {response.text[:200]}"
    del alarm
