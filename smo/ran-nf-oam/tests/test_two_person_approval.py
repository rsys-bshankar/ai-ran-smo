"""Two-person approval of an rApp's action (opt-in: `requiredApprovals: 2` on the approval policy of one rApp; follow-up to AI-11).

Nothing here is on unless a policy asks for it: a policy without the field is the single approval of `test_approvals.py`, which is unchanged."""

import datetime
import uuid

import pytest
from sqlalchemy import select

from smo_shared import audit
from smo_shared.outbox import NotificationOutbox

from app import main
from app.models import RAppActionApproval, RAppDecisionRecord, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import ELEMENTS, fleet  # noqa: F401

ES = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"}
GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}
ALICE, BOB, CAROL = "smo-gui:alice", "smo-gui:bob", "smo-gui:carol"
WATCHER = "http://approvers.example:9000/events"


@pytest.fixture(autouse=True)
def no_inline_sending(monkeypatch):
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")


def _hold(client, required=2, invoker="es-client", **policy):
    """PUT the approval policy of an rApp (requestedBy `admin`) and return the stored policy.
    
    `required` is sent as `requiredApprovals` (default 2); None leaves the field out, which replaces any earlier policy with one that does not say.
    Extra keyword arguments become further policy fields (for example `timeoutSeconds`). Asserts a 200."""
    body = {"requestedBy": "admin", **policy}
    if required is not None:
        body["requiredApprovals"] = required
    resp = client.put(f"/rapp-approval-policy/{invoker}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _park(client, value=25):
    """Make an rApp (`es-client`) write two cells' `txPower` so that the write is parked, and return the new request's approval id.
    
    Asserts a 202 with status PENDING_APPROVAL; an approval policy must already hold the rApp's writes."""
    changes = [{"managedElementRef": ref, "attributeChanges": {"txPower": value}} for ref in ELEMENTS[:2]]
    resp = client.post("/config-jobs", headers=ES, json={"requestedBy": "es-rapp", "scope": "cell", "changes": changes,
                                                          "decision": {"rationale": "low load", "modelVersion": "es 1.0"}})
    assert resp.status_code == 202 and resp.json()["status"] == "PENDING_APPROVAL", resp.text
    return resp.json()["approvalId"]


def _approve(client, approval_id, by=ALICE, **extra):
    """Approves as `by`: the console says so in `X-R1-Acting-User` (the decider, SEC-15.8) and the deprecated body field repeats it."""
    return client.post(f"/rapp-approvals/{approval_id}/approve", headers={**GUI, "X-R1-Acting-User": by}, json={"decidedBy": by, **extra})


def _reject(client, approval_id, by=ALICE, **extra):
    """Rejects as `by`, the same way as `_approve`."""
    return client.post(f"/rapp-approvals/{approval_id}/reject", headers={**GUI, "X-R1-Acting-User": by}, json={"decidedBy": by, **extra})


def _view(client, approval_id):
    return client.get(f"/rapp-approvals/{approval_id}", headers=ES).json()


def _record(client, approval_id):
    [record] = client.get("/decision-records", params={"approval_id": approval_id}).json()["items"]
    return record


def _jobs(fleet):
    with fleet["db"]() as db:
        return db.query(WriteConfigJob).count()


# ---- the policy

def test_a_policy_can_ask_for_two_approvals_and_one_is_the_default_and_reads_as_before(client):
    """A policy stores and returns `requiredApprovals: 2`; a policy that omits it, or says 1, has no such key in what it returns."""
    assert _hold(client, required=2)["requiredApprovals"] == 2
    assert client.get("/rapp-approval-policy/es-client").json()["requiredApprovals"] == 2
    again = _hold(client, required=None)                                                 # replaced by a policy that does not say
    assert "requiredApprovals" not in again and "requiredApprovals" not in client.get("/rapp-approval-policy/es-client").json()
    assert "requiredApprovals" not in _hold(client, required=1)


# Each `number` is not 1 or 2 (0, 3, -1, a string, null, a float): the policy route answers 422 and stores no policy.
@pytest.mark.parametrize("number", [0, 3, -1, "two", None, 1.5])
def test_the_number_of_approvals_is_one_or_two(client, number):
    resp = client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin", "requiredApprovals": number})
    assert resp.status_code == 422
    assert client.get("/rapp-approval-policy/es-client").status_code == 404


def test_an_rapp_cannot_set_its_own_policy_to_one_approval(client):
    """An rApp calling the policy route itself gets 403, so it cannot lower its own two-approval policy, and the stored policy still needs 2."""
    _hold(client)
    resp = client.put("/rapp-approval-policy/es-client", headers=ES, json={"requestedBy": "es-rapp", "requiredApprovals": 1})
    assert resp.status_code == 403
    assert client.get("/rapp-approval-policy/es-client").json()["requiredApprovals"] == 2


# ---- the request keeps what it needs

def test_a_parked_request_says_how_many_approvals_it_needs_and_that_none_are_given(client, fleet):
    """A request parked under a two-approval policy reads PENDING with `requiredApprovals` 2 and an empty `approvals` list."""
    _hold(client)
    view = _view(client, _park(client))
    assert view["status"] == "PENDING" and view["requiredApprovals"] == 2 and view["approvals"] == []


def test_a_request_under_the_default_policy_needs_one_and_reads_as_before(client, fleet):
    """Under a policy with no count, a request needs one approval, one approval writes the job, and the decision record has no approvers list."""
    _hold(client, required=None)
    approval_id = _park(client)
    assert _view(client, approval_id)["requiredApprovals"] == 1 and _view(client, approval_id)["approvals"] == []
    done = _approve(client, approval_id, reason="fine").json()
    assert done["status"] == "APPROVED" and done["jobStatus"] == "COMPLETED" and done["requiredApprovals"] == 1
    assert done["approvals"] == [{"by": ALICE, "at": done["decidedAt"], "reason": "fine"}]       # the one approval, shown the same way
    assert _record(client, approval_id)["approvers"] is None


def test_changing_the_policy_does_not_change_what_a_waiting_request_needs(client, fleet):
    """A waiting request keeps the approval count of the policy it was parked under, whatever the policy is changed to afterwards."""
    _hold(client, required=2)
    needs_two = _park(client)
    _hold(client, required=None)
    needs_one = _park(client)
    _hold(client, required=2)                                                           # and back: the second request keeps the one it was parked under
    assert _approve(client, needs_two, ALICE).json()["status"] == "PENDING"
    assert _approve(client, needs_one, ALICE).json()["status"] == "APPROVED"


# ---- the first approval keeps the request waiting

def test_the_first_approval_is_recorded_and_writes_nothing(client, fleet):
    """The first of two approvals is stored and shown but leaves the request PENDING with no job, no write to the elements and no decision record."""
    _hold(client)
    approval_id = _park(client)
    resp = _approve(client, approval_id, ALICE, reason="looks right")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "PENDING" and body["jobStatus"] is None and body["jobId"] is None
    assert body["decidedBy"] is None and body["decidedAt"] is None
    assert [(a["by"], a["reason"]) for a in body["approvals"]] == [(ALICE, "looks right")] and body["approvals"][0]["at"]
    assert fleet["edits"] == [] and _jobs(fleet) == 0
    assert client.get("/decision-records").json()["total"] == 0                          # a decision is made when the request ends, not at the first vote
    assert [a["approvalId"] for a in client.get("/rapp-approvals", params={"status": "PENDING"}).json()["items"]] == [approval_id]
    assert _view(client, approval_id)["approvals"][0]["by"] == ALICE                     # the rApp that asked sees how far it is


def test_the_same_person_cannot_give_both_approvals(client, fleet):
    """The same person, however the name is cased or padded, gets 409 APPROVAL_ALREADY_GIVEN on a second approval and the request stays PENDING with one approval."""
    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    for again in (ALICE, "SMO-GUI:Alice", f"  {ALICE} "):                              # the same name typed differently is the same person
        resp = _approve(client, approval_id, again)
        assert resp.status_code == 409 and resp.json()["detail"]["title"] == "APPROVAL_ALREADY_GIVEN", again
    view = _view(client, approval_id)
    assert view["status"] == "PENDING" and len(view["approvals"]) == 1 and fleet["edits"] == []


def test_the_requesters_own_approval_never_counts_and_a_refused_try_changes_nothing(client, fleet):
    """The requester's name, however cased or padded, gets 403 APPROVAL_SELF_DECISION as first or second approver, the rApp's own invoker id also gets 403, and a refusal records nothing."""
    _hold(client)
    approval_id = _park(client)
    for who in ("es-rapp", "es-client", "ES-RAPP", " Es-Client "):
        resp = _approve(client, approval_id, who)
        assert resp.status_code == 403 and resp.json()["detail"]["title"] == "APPROVAL_SELF_DECISION", who
    as_the_rapp_itself = client.post(f"/rapp-approvals/{approval_id}/approve", headers={**GUI, "X-R1-Invoker-Id": "es-client", "X-R1-Acting-User": ALICE}, json={"decidedBy": ALICE})
    assert as_the_rapp_itself.status_code == 403
    view = _view(client, approval_id)
    assert view["status"] == "PENDING" and view["approvals"] == []
    assert _approve(client, approval_id, ALICE).json()["status"] == "PENDING"            # and the first real approval still needs a second
    assert _approve(client, approval_id, "es-rapp").status_code == 403                  # the requester cannot be the second either
    assert fleet["edits"] == []


def test_an_rapp_cannot_approve_even_a_first_approval(client, fleet):
    """An rApp-role caller gets 403 when it approves, and no approval is recorded."""
    _hold(client)
    approval_id = _park(client)
    resp = client.post(f"/rapp-approvals/{approval_id}/approve", headers=ES, json={"decidedBy": ALICE})
    assert resp.status_code == 403 and _view(client, approval_id)["approvals"] == []


# ---- the second approval makes the job

def test_a_second_person_approving_makes_the_job_from_the_request(client, fleet):
    """The second, different approver turns the request APPROVED, writes the parked change, makes one job and is shown as the decider; a third approval gets 409."""
    _hold(client)
    approval_id = _park(client, value=31)
    _approve(client, approval_id, ALICE, reason="first")
    resp = _approve(client, approval_id, BOB, reason="second")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "APPROVED" and body["jobStatus"] == "COMPLETED" and body["jobId"]
    assert body["decidedBy"] == BOB and body["decisionReason"] == "second"
    assert [(a["by"], a["reason"]) for a in body["approvals"]] == [(ALICE, "first"), (BOB, "second")]
    assert fleet["values"]["ME-1"] == "31" and _jobs(fleet) == 1
    assert _approve(client, approval_id, CAROL).status_code == 409                       # decided once


def test_the_decision_record_names_both_approvers_and_still_verifies_in_the_chain(client, fleet):
    """The decision record lists both approvers, is in the audit chain, and its hash covers `approvers`, so removing one gives an integrity MISMATCH."""
    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    _approve(client, approval_id, BOB)
    record = _record(client, approval_id)
    assert record["disposition"] == "APPROVED" and record["approvers"] == [ALICE, BOB] and record["approvedBy"] == BOB
    assert record["rationale"] == "low load" and record["auditSeq"]
    full = client.get(f"/decision-records/{record['decisionId']}").json()
    assert full["integrity"]["status"] == "VERIFIED"
    with fleet["db"]() as db:
        assert audit.verify(db) is None
        stored = db.scalars(select(RAppDecisionRecord)).one()
        assert main._decision_hash(stored) == stored.content_hash
        stored.approvers = [ALICE]                                                       # one approver struck out of the record: the hash no longer matches
        db.commit()
    assert client.get(f"/decision-records/{record['decisionId']}").json()["integrity"]["status"] == "MISMATCH"


def test_a_record_that_needed_one_approval_hashes_exactly_as_before_the_field_existed():
    """`approvers` enters the decision hash only when it is not None, so a one-approval record keeps the hash it had before the field existed."""
    rec = RAppDecisionRecord(decision_id=uuid.UUID(int=1), occurred_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC), invoker_id="i", requested_by="r",
                             disposition="DIRECT", managed_elements=["ME-1"], change_count=1, approvers=None)
    with_empty = RAppDecisionRecord(decision_id=rec.decision_id, occurred_at=rec.occurred_at, invoker_id="i", requested_by="r", disposition="DIRECT",
                                    managed_elements=["ME-1"], change_count=1, approvers=[])
    assert main._decision_hash(rec) != main._decision_hash(with_empty)                   # the field is part of the hash only when it is set
    rec.approvers = [ALICE, BOB]
    assert main._decision_hash(rec) != main._decision_hash(with_empty)


# ---- the ways a two-person request ends

def test_one_rejection_ends_the_request_whoever_has_approved(client, fleet):
    """One rejection closes a request that already has an approval as REJECTED, writes nothing, keeps who approved on the record, and a later approval gets 409."""
    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    resp = _reject(client, approval_id, BOB, reason="not now")
    assert resp.status_code == 200 and resp.json()["status"] == "REJECTED" and resp.json()["decidedBy"] == BOB
    assert [a["by"] for a in resp.json()["approvals"]] == [ALICE] and fleet["edits"] == [] and _jobs(fleet) == 0
    record = _record(client, approval_id)
    assert record["disposition"] == "REJECTED" and record["approvers"] == [ALICE]         # who had approved is kept
    assert _approve(client, approval_id, CAROL).status_code == 409


def test_a_person_who_approved_may_reject_instead(client, fleet):
    """Someone who has already approved a two-approval request may still reject it."""
    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    assert _reject(client, approval_id, ALICE, reason="changed my mind").json()["status"] == "REJECTED"


def test_the_requester_cannot_reject_either(client, fleet):
    """The requester gets 403 when it rejects its own request."""
    _hold(client)
    approval_id = _park(client)
    assert _reject(client, approval_id, "ES-RAPP").status_code == 403


def test_a_request_with_one_approval_lapses_and_the_approval_does_not_count(client, fleet):
    """Past its deadline a request with one approval answers a second approval with 409 EXPIRED, writes nothing, and the record is EXPIRED with the one approver kept."""
    _hold(client, timeoutSeconds=60)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    with fleet["db"]() as db:
        row = db.scalars(select(RAppActionApproval)).one()
        row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        db.commit()
    late = _approve(client, approval_id, BOB)
    assert late.status_code == 409 and "EXPIRED" in late.json()["detail"]["detail"]
    view = _view(client, approval_id)
    assert view["status"] == "EXPIRED" and [a["by"] for a in view["approvals"]] == [ALICE] and fleet["edits"] == [] and _jobs(fleet) == 0
    record = _record(client, approval_id)
    assert record["disposition"] == "EXPIRED" and record["approvers"] == [ALICE]


def test_a_request_nobody_touched_lapses_with_an_empty_list_of_approvers(client, fleet):
    """`expire-due` lapses a past-deadline request nobody approved, and its decision record has an empty `approvers` list rather than None."""
    _hold(client, timeoutSeconds=60)
    approval_id = _park(client)
    with fleet["db"]() as db:
        db.scalars(select(RAppActionApproval)).one().expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        db.commit()
    assert client.post("/rapp-approvals/expire-due").json()["lapsed"] == [approval_id]
    assert _record(client, approval_id)["approvers"] == []


def test_a_safeguard_that_refuses_at_the_second_approval_closes_the_request_with_both_approvals_kept(client, fleet):
    """A kill switch set between the approvals makes the second approval answer 403 and closes the request REFUSED (RAPP_KILLED) with both approvals kept and nothing written."""
    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    client.put("/rapp-kill/es-client", json={"requestedBy": "ops", "reason": "stop"})
    refused = _approve(client, approval_id, BOB, reason="go")
    assert refused.status_code == 403
    view = _view(client, approval_id)
    assert view["status"] == "REFUSED" and view["refusalCode"] == "RAPP_KILLED" and view["decidedBy"] == BOB
    assert [a["by"] for a in view["approvals"]] == [ALICE, BOB] and fleet["edits"] == [] and _jobs(fleet) == 0
    assert _record(client, approval_id)["approvers"] == [ALICE, BOB]


def test_a_kill_switch_between_the_approvals_does_not_stop_the_first_one_but_stops_the_job(client, fleet):
    """The first approval checks nothing and writes nothing; every check runs at the approval that would write, as for a single approval."""
    _hold(client)
    approval_id = _park(client)
    client.put("/rapp-kill/es-client", json={"requestedBy": "ops", "reason": "stop"})
    assert _approve(client, approval_id, ALICE).json()["status"] == "PENDING"
    assert _approve(client, approval_id, BOB).status_code == 403


# ---- the notice

def _events(fleet):
    with fleet["db"]() as db:
        return [row.payload for row in db.scalars(select(NotificationOutbox).where(NotificationOutbox.destination == WATCHER)
                                                  .order_by(NotificationOutbox.created_at)).all()]


def test_the_notice_says_how_many_approvals_a_two_person_request_needs_and_is_unchanged_for_one(client, fleet):
    """The RAPP_APPROVAL_REQUESTED notice carries `requiredApprovals` and `approvalsGiven` only for a two-approval request; a one-approval notice has neither key."""
    client.post("/approval-subscriptions", json={"callbackUri": WATCHER})
    _hold(client, required=None)
    _park(client)
    _hold(client, required=2)
    _park(client)
    one, two = _events(fleet)
    assert one["eventType"] == two["eventType"] == "RAPP_APPROVAL_REQUESTED"
    assert "requiredApprovals" not in one and "approvalsGiven" not in one                  # exactly the notice there was
    assert two["requiredApprovals"] == 2 and two["approvalsGiven"] == 0


def test_a_lapse_notice_says_how_many_approvals_were_given(client, fleet):
    """The RAPP_APPROVAL_LAPSED notice reports `requiredApprovals` 2 and `approvalsGiven` 1 for a request that lapsed after one approval."""
    client.post("/approval-subscriptions", json={"callbackUri": WATCHER})
    _hold(client, timeoutSeconds=60)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    with fleet["db"]() as db:
        db.scalars(select(RAppActionApproval)).one().expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        db.commit()
    client.post("/rapp-approvals/expire-due")
    lapsed = _events(fleet)[-1]
    assert lapsed["eventType"] == "RAPP_APPROVAL_LAPSED" and lapsed["requiredApprovals"] == 2 and lapsed["approvalsGiven"] == 1


# ---- the list

def test_the_list_shows_the_approvals_so_far(client, fleet):
    """The approvals list shows each pending request's approvals so far and its `requiredApprovals`."""
    _hold(client)
    first, second = _park(client), _park(client)
    _approve(client, first, ALICE)
    items = {i["approvalId"]: i for i in client.get("/rapp-approvals", params={"status": "PENDING"}).json()["items"]}
    assert [a["by"] for a in items[first]["approvals"]] == [ALICE] and items[second]["approvals"] == []
    assert items[first]["requiredApprovals"] == items[second]["requiredApprovals"] == 2


def test_a_refusal_that_commits_nothing_of_its_own_still_leaves_both_approvals_on_the_closed_request(client, fleet, monkeypatch):
    """MSAC and the schema check refuse without a record of their own: the approval given is rolled back with the attempt and put back when the request is closed."""
    from fastapi import HTTPException

    def refuse(*args, **kwargs):
        raise HTTPException(status_code=422, detail={"title": "SCHEMA_VALIDATION_FAILED", "detail": "no longer valid"})

    _hold(client)
    approval_id = _park(client)
    _approve(client, approval_id, ALICE)
    monkeypatch.setattr(main, "_execute_write", refuse)
    assert _approve(client, approval_id, BOB).status_code == 422
    view = _view(client, approval_id)
    assert view["status"] == "REFUSED" and view["refusalCode"] == "SCHEMA_VALIDATION_FAILED" and [a["by"] for a in view["approvals"]] == [ALICE, BOB]
    assert _record(client, approval_id)["approvers"] == [ALICE, BOB]
