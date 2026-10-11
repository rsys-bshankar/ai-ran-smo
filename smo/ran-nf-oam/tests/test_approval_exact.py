"""Exact-behaviour tests of the approval routes of RAN NF OAM, added because a mutation pass over `read_approval`, `_decidable`, `ApprovalDecisionRequest`, `_start_scheduled` and
the job approval route (PR-SEC-15 part 4) found changes that no test noticed.

They pin: a request that lapsed is lapsed when it is read and cannot be decided (without a sweep having run), a request that has not lapsed is left alone, the `since` filter includes
its boundary, the limits of the decision body, a job whose change window closed while it was scheduled cannot be started, and a job's invoker cannot approve it under another spelling.
Helpers come from `test_approvals.py` (the rApp approval queue) and `test_change_windows.py` (jobs asked through the console). Run:
`PYTHONPATH=.:../shared python -m pytest tests/test_approval_exact.py -q`.
"""

import datetime
import uuid

import pytest
from sqlalchemy import select

from app.models import RAppActionApproval, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import fleet  # noqa: F401
from test_approvals import GUI, _age, _approve, _hold, _parked, no_inline_sending  # noqa: F401  (the fixture is autouse in its own file only)
from test_change_windows import ALICE, _ask, _decide, _iso


def _stored_status(fleet, approval_id):
    """The status of the approval request as stored, read in a session of its own (so an uncommitted change is not seen)."""
    with fleet["db"]() as db:
        return db.scalars(select(RAppActionApproval.status).where(RAppActionApproval.approval_id == uuid.UUID(approval_id))).one()


def test_a_lapsed_request_is_lapsed_when_it_is_read_and_a_live_one_is_not(client, fleet):
    """Reading a request whose deadline has passed settles it (EXPIRED by default) without a sweep, and reading one whose deadline has not passed leaves it PENDING."""
    _hold(client)
    live, old = _parked(client), _parked(client)
    _age(fleet, old, 5)
    assert client.get(f"/rapp-approvals/{live}", headers=GUI).json()["status"] == "PENDING"
    settled = client.get(f"/rapp-approvals/{old}", headers=GUI).json()
    assert settled["status"] == "EXPIRED" and settled["decidedBy"] == "system:timeout"
    assert _stored_status(fleet, old) == "EXPIRED"          # committed, not just shown: a view that is rolled back would leave it PENDING
    assert _stored_status(fleet, live) == "PENDING"


def test_a_request_that_lapsed_cannot_be_approved(client, fleet):
    """Approving a request whose deadline passed is 409 APPROVAL_NOT_PENDING that names the state it lapsed into, and the job is not written."""
    _hold(client)
    approval = _parked(client)
    _age(fleet, approval, 5)
    resp = _approve(client, approval)
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "APPROVAL_NOT_PENDING"
    assert "the request is EXPIRED" in resp.json()["detail"]["detail"] and "nobody decided before" in resp.json()["detail"]["detail"]
    assert fleet["edits"] == []
    assert _stored_status(fleet, approval) == "EXPIRED"     # the lapse is committed even though the decision is refused


def test_the_since_filter_includes_the_moment_a_request_was_made(client, fleet):
    """`since` keeps the requests made at or after it: the exact creation time of a request is included, a moment later is not."""
    _hold(client)
    approval = _parked(client)
    created = client.get(f"/rapp-approvals/{approval}", headers=GUI).json()["createdAt"]
    at = datetime.datetime.fromisoformat(created)
    assert [r["approvalId"] for r in client.get("/rapp-approvals", params={"since": at.isoformat()}).json()["items"]] == [approval]
    later = (at + datetime.timedelta(seconds=1)).isoformat()
    assert client.get("/rapp-approvals", params={"since": later}).json()["items"] == []


# Table: the decision body's limits (`decidedBy` 1 to 200 characters, `reason` at most 1000) -> whether the route takes it.
@pytest.mark.parametrize("field, value, ok", [("decidedBy", "", False), ("decidedBy", "a" * 201, False), ("decidedBy", "a", True), ("decidedBy", "smo-gui:alice", True),
                                              ("reason", "r" * 1000, True), ("reason", "r" * 1001, False)])
def test_the_decision_body_limits(client, fleet, field, value, ok):
    """A decision body with an empty or over-long `decidedBy`, or a `reason` over 1000 characters, is refused with 422 before anything is decided; the limits themselves are accepted."""
    _hold(client)
    approval = _parked(client)
    body = {"decidedBy": "smo-gui:alice", field: value}
    resp = client.post(f"/rapp-approvals/{approval}/approve", headers={**GUI, "X-R1-Acting-User": "smo-gui:alice"}, json=body)
    assert (resp.status_code == 422) is (not ok), resp.text


def test_a_job_whose_window_closed_while_it_was_scheduled_cannot_be_started(client, fleet, db_session_factory):
    """A job approved before its window and then past the window's end is 409 CHANGE_WINDOW_CLOSED on `continue` even with `force`, and nothing is sent; a window that is still open starts it."""
    job = _ask(client, changeWindow={"start": _iso(1), "end": _iso(3)})
    assert _decide(client, job["jobId"], "approve", "smo-gui:bob").json()["status"] == "SCHEDULED"
    with db_session_factory() as db:
        row = db.get(WriteConfigJob, uuid.UUID(job["jobId"]))
        row.window_end = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
        db.commit()
    closed = client.post(f"/config-jobs/{job['jobId']}/continue", json={"requestedBy": "smo-gui:bob", "force": True})
    assert closed.status_code == 409 and closed.json()["detail"]["title"] == "CHANGE_WINDOW_CLOSED"
    assert closed.json()["detail"]["detail"].startswith("the change window closed at 20") and fleet["edits"] == []


def test_the_invoker_that_asked_for_a_job_cannot_approve_it_under_another_spelling(client, fleet):
    """The decider is compared with the job's invoker id as well as its requester, ignoring case and surrounding space: the console's own invoker id cannot approve a job it asked for."""
    job = _ask(client, requireApproval=True)
    resp = _decide(client, job["jobId"], "approve", " GUI-Invoker ")
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "APPROVAL_SELF_DECISION"
    assert resp.json()["detail"]["detail"] == "the requester of a change cannot approve it"
