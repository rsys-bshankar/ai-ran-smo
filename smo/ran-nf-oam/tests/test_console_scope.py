"""GUI-5: the operator console's call for a person with a scope claim (`X-R1-Acting-User` and `X-R1-Acting-User-Scope` from an `internal` caller, smo_shared/scope.py
`scope_of`) is narrowed exactly as a scoped rApp's: lists hold only that person's elements, a write outside them is refused, and (SEC-10.12) the approval inbox
for rApp actions holds only the requests inside the claim, another region's request being answered as an unknown id. Run:
`PYTHONPATH=.:../shared python -m pytest tests/test_console_scope.py -q`."""

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_scope import PLACES, claim, places  # noqa: F401  (the fleet in its regions and tenants)
from test_waves import fleet  # noqa: F401

CONSOLE = {"X-R1-Invoker-Id": "gui-client", "X-R1-Role": "internal", "X-R1-Acting-User": "smo-gui:ana"}


def _region_of(region):
    """The fleet elements PLACES puts in `region`."""
    return sorted(ref for ref, (r, _) in PLACES.items() if r == region)


def test_a_person_with_a_claim_sees_only_their_elements_and_one_without_sees_all(client, places):
    """The managed elements of a person limited to one region are that region's; the console acting for an unscoped person sees every element, as before."""
    region = next(r for r, _ in PLACES.values() if r)
    scoped = {**CONSOLE, "X-R1-Acting-User-Scope": claim(regions=[region])}
    seen = sorted(e["managedElementRef"] for e in client.get("/managed-entities", headers=scoped).json()["items"])
    assert seen == _region_of(region)
    assert len(client.get("/managed-entities", headers=CONSOLE).json()["items"]) == len(PLACES)


def test_a_person_cannot_write_outside_their_claim(client, places):
    """A config job the console sends for a person limited to one region, naming an element of another, is refused SCOPE_DENIED and nothing is written."""
    region = next(r for r, _ in PLACES.values() if r)
    outside = next(ref for ref, (r, _) in PLACES.items() if r != region)
    resp = client.post("/config-jobs", headers={**CONSOLE, "X-R1-Acting-User-Scope": claim(regions=[region])},
                       json={"requestedBy": "smo-gui:ana", "scope": "cell", "changes": [{"managedElementRef": outside, "attributeChanges": {"txPower": 20}}]})
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "SCOPE_DENIED" and places["edits"] == []


# ---- SEC-10.12: the approval inbox for rApp actions is narrowed by the person's claim

APPROVER = {"X-R1-Invoker-Id": "gui-client", "X-R1-Role": "internal", "X-R1-Acting-User": "smo-gui:alice"}


def _person(*regions):
    """The console acting for alice, limited to `regions` (no region: unscoped)."""
    return {**APPROVER, "X-R1-Acting-User-Scope": claim(regions=list(regions))} if regions else APPROVER


def _park(client, refs, headers=None):
    """A config job of the rApp es-client (unscoped unless `headers` say otherwise) on `refs`, held for a human decision by its approval policy; returns the
    approval id."""
    assert client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"}).status_code == 200
    resp = client.post("/config-jobs", headers=headers or {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"},
                       json={"requestedBy": "es-rapp", "scope": "cell", "changes": [{"managedElementRef": r, "attributeChanges": {"txPower": 20}} for r in refs]})
    assert resp.status_code == 202 and resp.json()["status"] == "PENDING_APPROVAL", resp.text
    return resp.json()["approvalId"]


def _written(places):
    """What a decision could have written: the approval rows' state, the counts of decision records, outbox events, safeguard refusals, audit entries and jobs, and the
    edits sent to the elements."""
    from sqlalchemy import func, select
    from smo_shared.audit import AuditEntry
    from smo_shared.outbox import NotificationOutbox
    from app.models import RAppActionApproval, RAppDecisionRecord, SafeguardRefusal, WriteConfigJob
    with places["db"]() as db:
        approvals = sorted((str(r.approval_id), r.status, r.decided_by, repr(r.approvals)) for r in db.scalars(select(RAppActionApproval)))
        counts = tuple(db.scalar(select(func.count()).select_from(m)) for m in (RAppDecisionRecord, NotificationOutbox, SafeguardRefusal, AuditEntry, WriteConfigJob))
    return approvals, counts, list(places["edits"])


def test_a_person_limited_to_one_region_sees_only_that_regions_requests(client, places):
    """The inbox of a person limited to eu holds the eu request only and its total counts it alone; a us person sees the us request; an unscoped person sees all."""
    eu, us = _park(client, ["ME-1"]), _park(client, ["ME-3"])
    page = client.get("/rapp-approvals", headers=_person("eu"), params={"status": "PENDING"}).json()
    assert [i["approvalId"] for i in page["items"]] == [eu] and page["total"] == 1
    assert [i["approvalId"] for i in client.get("/rapp-approvals", headers=_person("us")).json()["items"]] == [us]
    unscoped = client.get("/rapp-approvals", headers=_person()).json()
    assert {i["approvalId"] for i in unscoped["items"]} == {eu, us} and unscoped["total"] == 2


def test_another_regions_request_is_a_404_like_an_unknown_one_and_nothing_is_written(client, places):
    """Reading, approving and rejecting a us request as a person limited to eu each answer exactly what an unknown id answers (404, same title, same shape), and no
    approval state, decision record, outbox event, refusal, audit entry or job is written; the request stays PENDING."""
    import uuid
    us = _park(client, ["ME-3"])
    before = _written(places)
    unknown = str(uuid.uuid4())
    for method, suffix, body in (("GET", "", None), ("POST", "/approve", {"reason": "ok"}), ("POST", "/reject", {"reason": "no"})):
        hidden = client.request(method, f"/rapp-approvals/{us}{suffix}", headers=_person("eu"), json=body)
        absent = client.request(method, f"/rapp-approvals/{unknown}{suffix}", headers=_person("eu"), json=body)
        assert hidden.status_code == absent.status_code == 404, (suffix, hidden.text)
        assert hidden.json()["detail"]["title"] == "APPROVAL_NOT_FOUND"
        same = lambda r, i: {**r.json()["detail"], "detail": r.json()["detail"]["detail"].replace(i, "<id>"), "correlationId": None}  # noqa: E731
        assert hidden.json().keys() == absent.json().keys() and same(hidden, us) == same(absent, unknown)
    assert _written(places) == before
    assert client.get(f"/rapp-approvals/{us}", headers=_person()).json()["status"] == "PENDING"


def test_a_request_split_across_two_regions_needs_both(client, places):
    """A request on an eu and a us element is hidden from a person limited to eu (list and decision) and listed and approved by a person with both regions."""
    split = _park(client, ["ME-1", "ME-3"])
    assert client.get("/rapp-approvals", headers=_person("eu")).json()["total"] == 0
    assert client.post(f"/rapp-approvals/{split}/approve", headers=_person("eu"), json={}).status_code == 404
    assert [i["approvalId"] for i in client.get("/rapp-approvals", headers=_person("eu", "us")).json()["items"]] == [split]
    resp = client.post(f"/rapp-approvals/{split}/approve", headers=_person("eu", "us"), json={})
    assert resp.status_code == 200 and resp.json()["status"] == "APPROVED" and sorted(places["edits"]) == ["ME-1", "ME-3"]


def test_an_unscoped_person_decides_every_request(client, places):
    """The console acting for a person with no claim approves a us request and rejects an eu one, as before SEC-10.12."""
    us, eu = _park(client, ["ME-3"]), _park(client, ["ME-1"])
    assert client.post(f"/rapp-approvals/{us}/approve", headers=_person(), json={}).json()["status"] == "APPROVED"
    assert client.post(f"/rapp-approvals/{eu}/reject", headers=_person(), json={"reason": "no"}).json()["status"] == "REJECTED"
    assert places["edits"] == ["ME-3"]


def test_an_element_moved_out_of_the_requesters_scope_still_closes_the_request_refused(client, places):
    """The decider's scope is asked first and the requester's is still checked again at approval (PR-SEC-10.4): an eu rApp's request whose element moved to us while it
    waited is hidden from an eu person (now outside their scope), and an eu-and-us person who approves it gets 403 SCOPE_DENIED and the request is closed REFUSED."""
    approval_id = _park(client, ["ME-2"], {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp", "X-R1-Scope": claim(regions=["eu"])})
    assert client.put("/managed-entities/ME-2/scope", json={"region": "us", "tenant": "globex"}).status_code == 200
    assert client.get(f"/rapp-approvals/{approval_id}", headers=_person("eu")).status_code == 404
    resp = client.post(f"/rapp-approvals/{approval_id}/approve", headers=_person("eu", "us"), json={})
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "SCOPE_DENIED" and places["edits"] == []
    view = client.get(f"/rapp-approvals/{approval_id}", headers=_person()).json()
    assert view["status"] == "REFUSED" and view["refusalCode"] == "SCOPE_DENIED"
