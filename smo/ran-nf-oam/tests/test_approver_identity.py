"""SEC-15.8: who decides an approval is the person the gateway vouches for, not what the body says.

The operator's console sends `X-R1-Acting-User`; R1 Termination forwards it from an `internal` caller. `POST /rapp-approvals/{id}/approve|reject` records that person: a body
`decidedBy` (deprecated, optional) must equal it, a call from an SMO module that names no person is refused, and one person cannot be two approvers by sending two names. The
fixtures (`client`, `fleet`) and the helpers (`_hold`, `_park`, `_view`, the headers and names) come from `test_two_person_approval.py`. Run:
`PYTHONPATH=.:../shared python -m pytest tests/test_approver_identity.py -q`.
"""

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_two_person_approval import ALICE, BOB, ES, GUI, _hold, _park, _view, no_inline_sending  # noqa: F401  (no_inline_sending: an autouse fixture)
from test_waves import fleet  # noqa: F401


def _decide(client, approval_id, action, acting=None, body=None, headers=GUI):
    """Sends an approve or reject with `X-R1-Acting-User: acting` (none when None) and the JSON `body`."""
    sent = {**headers, **({"X-R1-Acting-User": acting} if acting is not None else {})}
    return client.post(f"/rapp-approvals/{approval_id}/{action}", headers=sent, json=body if body is not None else {})


def _title(resp):
    return resp.json()["detail"]["title"]


def test_the_decider_is_the_acting_user_when_the_body_names_nobody(client, fleet):
    """With no `decidedBy` in the body, the person in `X-R1-Acting-User` approves, and the request, the job and the decision record name that person."""
    _hold(client, required=None)
    approval_id = _park(client)
    resp = _decide(client, approval_id, "approve", acting=ALICE)
    assert resp.status_code == 200 and resp.json()["status"] == "APPROVED" and resp.json()["decidedBy"] == ALICE
    record = client.get("/decision-records", params={"approval_id": approval_id}, headers=GUI).json()["items"][0]
    assert record["approvedBy"] == ALICE


def test_a_body_decider_that_is_not_the_acting_user_is_refused_and_records_nothing(client, fleet):
    """A `decidedBy` naming someone else than the gateway's person is 403 APPROVER_IDENTITY_MISMATCH on approve and on reject; the request stays PENDING with no approval."""
    _hold(client, required=None)
    approval_id = _park(client)
    for action in ("approve", "reject"):
        resp = _decide(client, approval_id, action, acting=ALICE, body={"decidedBy": BOB})
        assert resp.status_code == 403 and _title(resp) == "APPROVER_IDENTITY_MISMATCH"
    view = _view(client, approval_id)
    assert view["status"] == "PENDING" and view["approvals"] == [] and view["decidedBy"] is None


def test_a_body_decider_equal_to_the_acting_user_is_accepted_for_now(client, fleet):
    """The deprecated field still works when it repeats the verified person, in any case or padding, and the person is recorded as the console wrote it."""
    _hold(client, required=None)
    approval_id = _park(client)
    resp = _decide(client, approval_id, "approve", acting=ALICE, body={"decidedBy": "  SMO-GUI:Alice "})
    assert resp.status_code == 200 and resp.json()["decidedBy"] == ALICE


def test_an_smo_module_that_names_no_person_cannot_decide(client, fleet):
    """An `internal` caller without `X-R1-Acting-User` is 403 APPROVER_IDENTITY_REQUIRED even with a `decidedBy` in the body: a body is not an identity. Nothing is recorded."""
    _hold(client, required=None)
    approval_id = _park(client)
    for action in ("approve", "reject"):
        resp = _decide(client, approval_id, action, body={"decidedBy": ALICE})
        assert resp.status_code == 403 and _title(resp) == "APPROVER_IDENTITY_REQUIRED"
    assert _view(client, approval_id)["status"] == "PENDING"


def test_one_person_cannot_give_both_approvals_by_sending_two_names(client, fleet):
    """Under requiredApprovals 2 the verified person counts once: a second approval by the same acting user is 409 whatever the body says, a body naming a second person is 403,
    and a different acting user completes the request."""
    _hold(client)
    approval_id = _park(client)
    assert _decide(client, approval_id, "approve", acting=ALICE).json()["status"] == "PENDING"
    mismatch = _decide(client, approval_id, "approve", acting=ALICE, body={"decidedBy": BOB})              # pretending to be the second person
    assert mismatch.status_code == 403 and _title(mismatch) == "APPROVER_IDENTITY_MISMATCH"
    again = _decide(client, approval_id, "approve", acting="  SMO-GUI:ALICE")                              # the same person spelled differently
    assert again.status_code == 409 and _title(again) == "APPROVAL_ALREADY_GIVEN"
    assert [v["by"] for v in _view(client, approval_id)["approvals"]] == [ALICE]
    done = _decide(client, approval_id, "approve", acting=BOB)
    assert done.status_code == 200 and done.json()["status"] == "APPROVED" and [v["by"] for v in done.json()["approvals"]] == [ALICE, BOB]


def test_the_requester_is_judged_by_the_acting_user_not_the_body(client, fleet):
    """An acting user who is the requester is 403 APPROVAL_SELF_DECISION with no body decider; naming another person in the body to disguise it is a mismatch, so the requester cannot hide behind the body."""
    _hold(client, required=None)
    approval_id = _park(client)
    own = _decide(client, approval_id, "approve", acting="es-rapp")
    assert own.status_code == 403 and _title(own) == "APPROVAL_SELF_DECISION"
    disguised = _decide(client, approval_id, "approve", acting="es-rapp", body={"decidedBy": ALICE})
    assert disguised.status_code == 403 and _title(disguised) == "APPROVER_IDENTITY_MISMATCH"
    assert _view(client, approval_id)["status"] == "PENDING"


def test_a_call_that_did_not_come_through_the_gateway_is_taken_at_its_word(client, fleet):
    """With no role at all (a test, an in-process call) there is nothing to verify: `decidedBy` is used, and it is required (422)."""
    _hold(client, required=None)
    approval_id = _park(client)
    assert client.post(f"/rapp-approvals/{approval_id}/approve", json={}).status_code == 422
    resp = client.post(f"/rapp-approvals/{approval_id}/approve", json={"decidedBy": ALICE})
    assert resp.status_code == 200 and resp.json()["decidedBy"] == ALICE


def test_an_rapp_cannot_decide_whoever_it_claims_to_be(client, fleet):
    """An rApp sending `X-R1-Acting-User` (the gateway drops it, and the module would not believe it without the `internal` role) is still 403."""
    _hold(client, required=None)
    approval_id = _park(client)
    resp = _decide(client, approval_id, "approve", acting=ALICE, body={"decidedBy": ALICE}, headers=ES)
    assert resp.status_code == 403 and _view(client, approval_id)["status"] == "PENDING"
