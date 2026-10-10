"""AI-11: a human approves an rApp's action before it is written. The policy, the queue, the decision, the timeout and the notice to the approvers.

An rApp without a policy is not touched by any of this (`test_a_rapp_without_a_policy_writes_at_once`)."""

import datetime
import uuid

import pytest
from sqlalchemy import select

from smo_shared.outbox import NotificationOutbox

from app import tasks
from app.models import RAppActionApproval, RAppDecisionRecord, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import ELEMENTS, fleet  # noqa: F401

ES = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"}
OTHER = {"X-R1-Invoker-Id": "ts-client", "X-R1-Role": "rapp"}
GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}
APPROVER = "smo-gui:alice"
WATCHER = "http://approvers.example:9000/events"


@pytest.fixture(autouse=True)
def no_inline_sending(monkeypatch):
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")          # the outbox rows are what is under test, not the network


def _hold(client, invoker="es-client", **policy):
    resp = client.put(f"/rapp-approval-policy/{invoker}", json={"requestedBy": "admin", **policy})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _write(client, headers=ES, refs=ELEMENTS[:2], value=20, **extra):
    changes = [{"managedElementRef": ref, "attributeChanges": {"txPower": value}} for ref in refs]
    return client.post("/config-jobs", headers=headers, json={"requestedBy": "es-rapp", "scope": "cell", "changes": changes, **extra})


def _parked(client, **kw):
    resp = _write(client, **kw)
    assert resp.status_code == 202 and resp.json()["status"] == "PENDING_APPROVAL", resp.text
    return resp.json()["approvalId"]


def _approve(client, approval_id, by=APPROVER, headers=GUI, **extra):
    """Approves as `by`: the console says so in `X-R1-Acting-User` (the decider, SEC-15.8) and the deprecated body field repeats it."""
    return client.post(f"/rapp-approvals/{approval_id}/approve", headers={**headers, "X-R1-Acting-User": by}, json={"decidedBy": by, **extra})


def _reject(client, approval_id, by=APPROVER, headers=GUI, **extra):
    """Rejects as `by`, the same way as `_approve`."""
    return client.post(f"/rapp-approvals/{approval_id}/reject", headers={**headers, "X-R1-Acting-User": by}, json={"decidedBy": by, **extra})


def _age(fleet, approval_id, seconds):
    """Move the request's deadline `seconds` into the past (the policy's own minimum is a minute)."""
    with fleet["db"]() as db:
        row = db.scalars(select(RAppActionApproval).where(RAppActionApproval.approval_id == uuid.UUID(approval_id))).one()
        row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds)
        db.commit()


def _events(fleet):
    with fleet["db"]() as db:
        return [row.payload for row in db.scalars(select(NotificationOutbox).where(NotificationOutbox.destination == WATCHER)
                                                  .order_by(NotificationOutbox.created_at)).all()]


# ---- 11.4: the hook

def test_a_rapp_without_a_policy_writes_at_once(client, fleet):
    """An rApp with no approval policy writes at once, as before, and no approval request exists."""
    resp = _write(client)
    assert resp.status_code == 202 and resp.json()["status"] == "COMPLETED" and resp.json()["jobId"]
    assert fleet["values"]["ME-1"] == "20"
    assert client.get("/rapp-approvals").json()["items"] == []


def test_a_rapp_with_a_policy_is_parked_and_nothing_is_written(client, fleet):
    """An rApp with an approval policy gets 202 PENDING_APPROVAL with no job; nothing is written or created, and the rApp can read its own request."""
    _hold(client)
    resp = _write(client)
    body = resp.json()
    assert resp.status_code == 202 and body["status"] == "PENDING_APPROVAL" and body["jobId"] is None and body["approvalId"] and body["expiresAt"]
    assert fleet["edits"] == [] and fleet["values"]["ME-1"] == "10"
    with fleet["db"]() as db:
        assert db.query(WriteConfigJob).count() == 0
    view = client.get(f"/rapp-approvals/{body['approvalId']}", headers=ES).json()      # the rApp reads its own request
    assert view["status"] == "PENDING" and view["invokerId"] == "es-client" and view["requestedBy"] == "es-rapp"
    assert view["managedElements"] == ELEMENTS[:2] and view["changeCount"] == 2 and view["onTimeout"] == "EXPIRE"
    assert [c["attributeChanges"] for c in view["changes"]] == [{"txPower": 20}, {"txPower": 20}] and view["jobId"] is None


def test_only_the_rapp_with_the_policy_is_held(client, fleet):
    """Only the rApp the policy names is held; other rApps and an SMO module on its own account write at once."""
    _hold(client)
    assert _write(client, headers=OTHER).json()["status"] == "COMPLETED"
    assert _write(client, headers=GUI, refs=ELEMENTS[2:3]).json()["status"] == "COMPLETED"      # the GUI's own write, an SMO module on its own account
    assert client.get("/rapp-approvals").json()["total"] == 0


def test_a_policy_on_the_rapp_holds_a_write_made_for_it_by_an_smo_module(client, fleet):
    """A write an SMO module makes on behalf of the held rApp is held too."""
    _hold(client)
    through_dme = {"X-R1-Invoker-Id": "dme-module", "X-R1-Role": "internal", "X-R1-On-Behalf-Of": "es-client"}
    assert _write(client, headers=through_dme).json()["status"] == "PENDING_APPROVAL"
    assert fleet["edits"] == []


def test_a_dry_run_is_not_held_and_checks_still_run_before_a_request_is_parked(client, fleet):
    """A dry run is never held, and the normal checks (here the access gate) refuse a write before it is parked."""
    _hold(client)
    dry = _write(client, dryRun=True)
    assert dry.status_code == 200 and dry.json()["dryRun"] is True
    wide = client.post("/config-jobs", headers=ES, json={"requestedBy": "es-rapp", "scope": "entire-RAN", "changes": [
        {"managedElementRef": "ME-1", "attributeChanges": {"txPower": 1}}]})
    assert wide.status_code == 403 and wide.json()["detail"]["title"] == "MSAC_ACCESS_DENIED"        # refused as for any write, not parked
    assert fleet["edits"] == [] and client.get("/rapp-approvals").json()["total"] == 0


def test_the_replay_of_a_parked_request_is_the_same_request(client, fleet):
    """Repeating a request with the same Idempotency-Key gives the same approval and parks one request."""
    _hold(client)
    key = {"Idempotency-Key": "park-1"}
    first = _write(client, headers={**ES, **key}).json()
    second = _write(client, headers={**ES, **key}).json()
    assert first["approvalId"] == second["approvalId"] and client.get("/rapp-approvals").json()["total"] == 1


def test_the_safeguards_still_apply_before_a_request_is_parked(client, fleet):
    """A kill switch refuses a write before it can be parked."""
    _hold(client)
    client.put("/rapp-kill/es-client", json={"requestedBy": "alice", "reason": "stop"})
    assert _write(client).status_code == 403
    assert client.get("/rapp-approvals").json()["total"] == 0


# ---- 11.2: the queue

def test_approving_makes_the_job_from_the_request_and_closes_it(client, fleet):
    """Approving makes the job from the request as the rApp sent it, writes it, and closes the request APPROVED with the approver, reason and job
    id.
    """
    _hold(client)
    approval_id = _parked(client, value=25)
    resp = _approve(client, approval_id, reason="looks right")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "APPROVED" and body["decidedBy"] == APPROVER and body["decisionReason"] == "looks right"
    assert body["jobId"] and body["jobStatus"] == "COMPLETED"
    assert fleet["values"]["ME-1"] == "25" and fleet["values"]["ME-2"] == "25"
    job = client.get(f"/config-jobs/{body['jobId']}").json()
    assert job["status"] == "COMPLETED" and job["requestedBy"] == "es-rapp"
    with fleet["db"]() as db:
        assert db.scalar(select(WriteConfigJob.invoker_id).where(WriteConfigJob.job_id == uuid.UUID(body["jobId"]))) == "es-client"      # counted against the rApp's limits


def test_rejecting_writes_nothing_and_says_why(client, fleet):
    """Rejecting closes the request REJECTED with the reason and writes nothing."""
    _hold(client)
    approval_id = _parked(client)
    resp = _reject(client, approval_id, reason="not in this window")
    assert resp.status_code == 200 and resp.json()["status"] == "REJECTED" and resp.json()["decisionReason"] == "not in this window"
    assert fleet["edits"] == [] and resp.json()["jobId"] is None
    assert client.get(f"/rapp-approvals/{approval_id}", headers=ES).json()["status"] == "REJECTED"


def test_a_request_is_decided_once(client, fleet):
    """A decided request cannot be decided again (409 APPROVAL_NOT_PENDING), and the write happened once."""
    _hold(client)
    approval_id = _parked(client)
    assert _approve(client, approval_id).status_code == 200
    again = _approve(client, approval_id, by="smo-gui:bob")
    assert again.status_code == 409 and again.json()["detail"]["title"] == "APPROVAL_NOT_PENDING" and "APPROVED" in again.json()["detail"]["detail"]
    assert _reject(client, approval_id, by="smo-gui:bob").status_code == 409
    assert fleet["edits"].count("ME-1") == 1                                                    # written once


def test_an_unknown_request_is_404(client, fleet):
    """An unknown approval id is 404 on read, approve and reject."""
    missing = "00000000-0000-0000-0000-000000000001"
    assert client.get(f"/rapp-approvals/{missing}").status_code == 404
    assert _approve(client, missing).json()["detail"]["title"] == "APPROVAL_NOT_FOUND"
    assert _reject(client, missing).status_code == 404


def test_an_rapp_can_never_decide_an_approval_not_even_for_another_rapp(client, fleet):
    """An rApp cannot approve or reject any request, whoever it names as decider: 403 ROLE_NOT_PERMITTED and the request stays pending."""
    _hold(client)
    approval_id = _parked(client)
    for headers in (ES, OTHER):
        for call in (_approve, _reject):
            resp = call(client, approval_id, headers=headers, by="smo-gui:alice")
            assert resp.status_code == 403 and resp.json()["detail"]["title"] == "ROLE_NOT_PERMITTED"
    assert client.get(f"/rapp-approvals/{approval_id}").json()["status"] == "PENDING" and fleet["edits"] == []


def test_the_requester_cannot_decide_its_own_action(client, fleet):
    """The requester cannot decide its own action under its invoker id or the name it gave as requester, nor through a call carrying its own id
    (403 APPROVAL_SELF_DECISION).
    """
    _hold(client)
    approval_id = _parked(client)
    for who in ("es-client", "es-rapp"):                                       # the invoker id, and the name the rApp gave as requestedBy
        resp = _approve(client, approval_id, by=who)
        assert resp.status_code == 403 and resp.json()["detail"]["title"] == "APPROVAL_SELF_DECISION"
    assert _reject(client, approval_id, headers={"X-R1-Invoker-Id": "es-client"}, by="someone").status_code == 403      # a call that carries the requester's own id
    assert client.get(f"/rapp-approvals/{approval_id}").json()["status"] == "PENDING"


def test_the_queue_lists_newest_first_filters_and_pages(client, fleet):
    """The approval queue is newest first and can be filtered by status, rApp and time and paged."""
    _hold(client)
    _hold(client, invoker="ts-client")
    first = _parked(client)
    second = _parked(client, headers=OTHER)
    third = _parked(client, refs=ELEMENTS[2:3])
    _reject(client, first)
    listing = client.get("/rapp-approvals").json()
    assert listing["total"] == 3 and [i["approvalId"] for i in listing["items"]] == [third, second, first]
    assert [i["approvalId"] for i in client.get("/rapp-approvals", params={"status": "PENDING"}).json()["items"]] == [third, second]
    assert [i["approvalId"] for i in client.get("/rapp-approvals", params={"invoker_id": "ts-client"}).json()["items"]] == [second]
    page = client.get("/rapp-approvals", params={"limit": 1, "offset": 1, "total": "false"}).json()
    assert "total" not in page and page["hasMore"] is True and [i["approvalId"] for i in page["items"]] == [second]
    assert client.get("/rapp-approvals", params={"status": "MAYBE"}).status_code == 422
    assert "changes" not in listing["items"][0] and listing["items"][0]["changeCount"] == 1


def test_approve_checks_the_safeguards_again_and_closes_a_refused_request(client, fleet):
    """Approval checks the safeguards again: a kill thrown while the request waited refuses it (403 RAPP_KILLED), closes it REFUSED with the code,
    and it does not wait for a decision that cannot succeed.
    """
    _hold(client)
    approval_id = _parked(client)
    client.put("/rapp-kill/es-client", json={"requestedBy": "alice", "reason": "oscillating"})        # stopped while it waited
    resp = _approve(client, approval_id)
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "RAPP_KILLED"
    assert fleet["edits"] == []
    view = client.get(f"/rapp-approvals/{approval_id}").json()
    assert view["status"] == "REFUSED" and view["refusalCode"] == "RAPP_KILLED" and view["decidedBy"] == APPROVER and view["jobId"] is None
    assert [r["refusal"] for r in client.get("/safeguard-refusals").json()["items"]] == ["RAPP_KILLED"]
    assert _approve(client, approval_id).status_code == 409                                            # it does not wait for a decision that cannot succeed


def test_a_request_the_checks_refuse_at_approval_is_closed_with_the_reason(client, fleet):
    """A request that the access checks refuse when it is run is closed REFUSED with that code."""
    _hold(client)
    approval_id = _parked(client)
    with fleet["db"]() as db:                                                                          # the element's endpoint went away while it waited
        row = db.scalars(select(RAppActionApproval)).one()
        row.request = {**row.request, "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}, "operation": "merge"}], "accessScope": "entire-RAN", "scope": "entire-RAN"}
        db.commit()
    resp = _approve(client, approval_id)
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "MSAC_ACCESS_DENIED"
    assert client.get(f"/rapp-approvals/{approval_id}").json()["status"] == "REFUSED"


# ---- 11.3: the timeout

def test_a_request_nobody_decided_expires_by_default_and_cannot_then_be_approved(client, fleet):
    """By default a request nobody decides expires after an hour, decided by `system:timeout`, and cannot then be approved (409)."""
    policy = _hold(client)
    assert policy["timeoutSeconds"] == 3600 and policy["onTimeout"] == "EXPIRE"                        # the conservative default
    approval_id = _parked(client)
    view = client.get(f"/rapp-approvals/{approval_id}").json()
    assert 3590 < (datetime.datetime.fromisoformat(view["expiresAt"]) - datetime.datetime.fromisoformat(view["createdAt"])).total_seconds() <= 3600
    _age(fleet, approval_id, 5)
    expired = client.get(f"/rapp-approvals/{approval_id}").json()
    assert expired["status"] == "EXPIRED" and expired["decidedBy"] == "system:timeout" and expired["jobId"] is None
    resp = _approve(client, approval_id)
    assert resp.status_code == 409 and "EXPIRED" in resp.json()["detail"]["detail"]
    assert fleet["edits"] == []


def test_with_on_timeout_reject_the_platform_rejects_it(client, fleet):
    """With `onTimeout: REJECT` a request that timed out is REJECTED by the platform, and the deadline is enforced when someone tries to decide it."""
    _hold(client, timeoutSeconds=120, onTimeout="REJECT")
    approval_id = _parked(client)
    _age(fleet, approval_id, 1)
    assert _approve(client, approval_id).status_code == 409                                            # the deadline is enforced when it is decided
    view = client.get(f"/rapp-approvals/{approval_id}").json()
    assert view["status"] == "REJECTED" and view["decidedBy"] == "system:timeout" and "nobody decided" in view["decisionReason"]


def test_a_timeout_holds_without_a_scheduler_the_list_lapses_what_is_due(client, fleet):
    """Reading the queue lapses what is due, so a timeout holds even when no scheduler runs."""
    _hold(client)
    late, on_time = _parked(client), _parked(client, refs=ELEMENTS[2:3])
    _age(fleet, late, 10)
    pending = client.get("/rapp-approvals", params={"status": "PENDING"}).json()["items"]
    assert [i["approvalId"] for i in pending] == [on_time]
    assert client.get(f"/rapp-approvals/{late}").json()["status"] == "EXPIRED"


def test_the_sweep_and_the_workers_task_lapse_what_is_due(client, fleet, monkeypatch):
    """The expire-due sweep and the worker's `expire-approvals` task lapse each due request once."""
    _hold(client)
    due, waiting = _parked(client), _parked(client, refs=ELEMENTS[2:3])
    _age(fleet, due, 10)
    assert client.post("/rapp-approvals/expire-due").json() == {"lapsed": [due]}
    assert client.post("/rapp-approvals/expire-due").json() == {"lapsed": []}                           # once
    _age(fleet, waiting, 10)
    monkeypatch.setattr(tasks, "SessionLocal", fleet["db"])
    tasks.expire_approvals()
    assert [client.get(f"/rapp-approvals/{i}").json()["status"] for i in (due, waiting)] == ["EXPIRED", "EXPIRED"]


def test_the_policy_bounds_the_timeout(client, fleet):
    """A policy timeout must be between a minute and a week, and there is no option that approves by itself."""
    assert client.put("/rapp-approval-policy/x", json={"requestedBy": "a", "timeoutSeconds": 59}).status_code == 422
    assert client.put("/rapp-approval-policy/x", json={"requestedBy": "a", "timeoutSeconds": 604_801}).status_code == 422
    assert client.put("/rapp-approval-policy/x", json={"requestedBy": "a", "onTimeout": "APPROVE"}).status_code == 422      # no option approves by itself
    assert client.put("/rapp-approval-policy/x", json={"requestedBy": "a", "timeoutSeconds": 60}).status_code == 200


def test_a_policy_is_set_read_replaced_and_removed(client, fleet):
    """A policy can be set, read, replaced whole (not merged) and removed; after removal the rApp writes at once."""
    assert client.get("/rapp-approval-policy/es-client").status_code == 404
    _hold(client, timeoutSeconds=600, onTimeout="REJECT")
    assert client.get("/rapp-approval-policy/es-client").json() | {"updatedAt": None} == {
        "invokerId": "es-client", "timeoutSeconds": 600, "onTimeout": "REJECT", "setBy": "admin", "updatedAt": None}
    assert _hold(client, timeoutSeconds=900)["onTimeout"] == "EXPIRE"                                  # replaced, not merged
    assert client.delete("/rapp-approval-policy/es-client").status_code == 204
    assert client.delete("/rapp-approval-policy/es-client").status_code == 404
    assert _write(client).json()["status"] == "COMPLETED"                                              # written at once again


def test_an_rapp_cannot_set_or_remove_its_own_policy(client, fleet):
    """An rApp cannot set or remove its own approval policy (403 RAPP_LIMIT_SELF_CHANGE)."""
    _hold(client)
    resp = client.put("/rapp-approval-policy/es-client", headers=ES, json={"requestedBy": "es-rapp"})
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "RAPP_LIMIT_SELF_CHANGE" and "approval policy" in resp.json()["detail"]["detail"]
    assert client.delete("/rapp-approval-policy/es-client", headers=ES).status_code == 403


def test_a_request_parked_before_the_policy_was_removed_still_waits(client, fleet):
    """Removing a policy leaves requests already parked waiting, and they can still be approved."""
    _hold(client)
    approval_id = _parked(client)
    client.delete("/rapp-approval-policy/es-client")
    assert client.get(f"/rapp-approvals/{approval_id}").json()["status"] == "PENDING"
    assert _approve(client, approval_id).json()["status"] == "APPROVED"


# ---- 11.5: the notice to the approvers

def _subscribe(client):
    resp = client.post("/approval-subscriptions", json={"callbackUri": WATCHER})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_the_approvers_are_told_when_a_request_waits_and_when_it_lapses(client, fleet):
    """Subscribers get one notice when a request is parked and one when it lapses, naming the request and elements but not carrying the changes."""
    sub = _subscribe(client)
    _hold(client)
    approval_id = _parked(client)
    [event] = _events(fleet)
    assert event["eventType"] == "RAPP_APPROVAL_REQUESTED" and event["approvalId"] == approval_id and event["invokerId"] == "es-client"
    assert event["managedElements"] == ELEMENTS[:2] and event["changeCount"] == 2 and event["status"] == "PENDING" and event["expiresAt"]
    assert event["href"] == "/ran-nf-oam/rapp-approvals" and "changes" not in event                    # what to open, not the data itself
    _age(fleet, approval_id, 5)
    client.post("/rapp-approvals/expire-due")
    assert [e["eventType"] for e in _events(fleet)] == ["RAPP_APPROVAL_REQUESTED", "RAPP_APPROVAL_LAPSED"]
    assert _events(fleet)[1]["status"] == "EXPIRED"
    assert client.delete(f"/approval-subscriptions/{sub['subscriptionId']}").status_code == 204


def test_a_decision_made_by_a_human_sends_no_second_notice_and_nobody_subscribed_means_no_rows(client, fleet):
    """A human decision sends no second notice, and with no subscriber nothing is queued."""
    _hold(client)
    _parked(client)
    assert _events(fleet) == []                                                                        # nobody subscribed: the request still waits for the GUI inbox
    _subscribe(client)
    approval_id = _parked(client, refs=ELEMENTS[2:3])
    _reject(client, approval_id)
    assert [e["eventType"] for e in _events(fleet)] == ["RAPP_APPROVAL_REQUESTED"]


def test_a_notice_is_not_sent_for_a_request_that_was_not_kept(client, fleet):
    """A write refused before it is parked sends no approval notice."""
    _subscribe(client)
    _hold(client)
    client.put("/rapp-kill/es-client", json={"requestedBy": "alice"})
    _write(client)
    assert _events(fleet) == []


def test_a_destination_the_guard_refuses_is_a_422_and_an_unknown_subscription_a_404(client, fleet):
    """A callback the SSRF guard refuses is 422, an unknown subscription is 404, and subscriptions can be listed and removed."""
    assert client.post("/approval-subscriptions", json={"callbackUri": "http://127.0.0.1:9/x"}).status_code == 422
    assert client.delete("/approval-subscriptions/00000000-0000-0000-0000-000000000001").json()["detail"]["title"] == "APPROVAL_SUBSCRIPTION_NOT_FOUND"
    sub = _subscribe(client)
    assert [s["callbackUri"] for s in client.get("/approval-subscriptions").json()["items"]] == [WATCHER]
    assert client.delete(f"/approval-subscriptions/{sub['subscriptionId']}").status_code == 204
    assert client.get("/approval-subscriptions").json()["items"] == []


def test_the_decision_records_of_an_approval_name_the_approver(client, fleet):
    """Approving and rejecting each leave a decision record that names the decider."""
    _hold(client)
    approved, rejected = _parked(client), _parked(client, refs=ELEMENTS[2:3])
    _approve(client, approved)
    _reject(client, rejected, by="smo-gui:bob")
    with fleet["db"]() as db:
        rows = {r.approval_id.hex: r for r in db.scalars(select(RAppDecisionRecord)).all()}
    assert {r.disposition for r in rows.values()} == {"APPROVED", "REJECTED"}
    assert {(r.disposition, r.decided_by) for r in rows.values()} == {("APPROVED", APPROVER), ("REJECTED", "smo-gui:bob")}
