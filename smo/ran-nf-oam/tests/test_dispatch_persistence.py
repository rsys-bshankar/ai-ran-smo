"""Tests of call flow 03c (DOC-CF-1.8): a configuration write is saved before it goes to the network. The job and its sub-changes are committed before the
first edit, each sub-change is committed DISPATCHING just before its edit and with its outcome (and before-image) right after, so a crash during the dispatch
leaves the change in flight visible as DISPATCHING, the earlier ones recorded, and a rollback that refuses to guess.

Fixtures come from `test_main.py` (the app on SQLite) and `test_cm_history.py` (a fake NF). Run: cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest
tests/test_dispatch_persistence.py -q
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_cm_history import nf  # noqa: F401  (pytest fixture)
from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.main import app
from app.models import CMSnapshot, WriteConfigJob, WriteConfigSubChange

CELLS = [{"managedElementRef": "ME-1", "managedFunctionRef": f"NRCellDU={i}", "attributeChanges": {"administrativeState": "LOCKED"}} for i in (1, 2, 3)]


def _seen(db_session_factory):
    """What another reader sees now: (the job's status, each sub-change's status in order, how many snapshots are saved)."""
    with db_session_factory() as db:
        job = db.scalars(select(WriteConfigJob)).one()
        subs = db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job.job_id).order_by(WriteConfigSubChange.position)).all()
        return job.status, [s.status for s in subs], len(db.scalars(select(CMSnapshot)).all())


def test_the_job_is_saved_before_the_first_edit_and_each_outcome_right_after_it(client, db_session_factory, nf, monkeypatch):
    """Call flow 03c: during each edit another reader sees the job PROCESSING, the earlier sub-changes APPLIED with their before-images, this one
    DISPATCHING and the later ones PENDING."""
    _make_me(db_session_factory)
    seen = []
    edit = nf["edit_result"]

    def send(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", managed_function_ref=None):
        """Records what a reader sees while this edit is out, then applies it."""
        seen.append(_seen(db_session_factory))
        return edit
    monkeypatch.setattr("app.main.send_edit_config", send)

    job = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": CELLS}).json()

    assert job["status"] == "COMPLETED"
    assert seen == [("PROCESSING", ["DISPATCHING", "PENDING", "PENDING"], 0),
                    ("PROCESSING", ["APPLIED", "DISPATCHING", "PENDING"], 1),
                    ("PROCESSING", ["APPLIED", "APPLIED", "DISPATCHING"], 2)]


def test_a_crash_during_an_edit_leaves_it_dispatching_and_a_rollback_refuses_to_guess(db_session_factory, nf, monkeypatch):
    """Call flow 03c: if RAN NF OAM dies while the second edit is out, the job stays PROCESSING with the first change APPLIED (and its before-image), the
    second DISPATCHING (its outcome unknown) and the third PENDING; a rollback of the job is 422 ROLLBACK_NOT_POSSIBLE naming the change in flight. Before, nothing of the
    job was saved."""
    from smo_shared.db import get_session

    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()
    app.dependency_overrides[get_session] = override_get_session
    try:
        crashing = TestClient(app, raise_server_exceptions=False)
        _make_me(db_session_factory)
        calls = []

        def send(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", managed_function_ref=None):
            """Applies the first edit and dies during the second."""
            calls.append(managed_function_ref)
            if len(calls) == 2:
                raise SystemError("process killed")
            return nf["edit_result"]
        monkeypatch.setattr("app.main.send_edit_config", send)

        assert crashing.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": CELLS}).status_code == 500
        assert _seen(db_session_factory) == ("PROCESSING", ["APPLIED", "DISPATCHING", "PENDING"], 1)

        with db_session_factory() as db:
            job_id = db.scalars(select(WriteConfigJob.job_id)).one()
        refused = crashing.post(f"/config-jobs/{job_id}/rollback", json={"requestedBy": "alice"})
        assert refused.status_code == 422 and "ROLLBACK_NOT_POSSIBLE" in refused.text and "NRCellDU=2" in refused.text and "in flight" in refused.text
        assert crashing.get(f"/config-jobs/{job_id}").json()["subChanges"][1]["status"] == "DISPATCHING"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("blocked_first", [True, False])
def test_a_change_with_no_usable_endpoint_is_recorded_rejected_without_being_sent(client, db_session_factory, nf, blocked_first):
    """A change whose element has no endpoint is REJECTED (ENDPOINT_UNREACHABLE) and committed without an edit, before or after one that is sent."""
    _make_me(db_session_factory)
    other = {"managedElementRef": "ME-UNKNOWN", "attributeChanges": {"administrativeState": "LOCKED"}}
    changes = [other, CELLS[0]] if blocked_first else [CELLS[0], other]
    job = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": changes}).json()
    statuses = {s["managedElementRef"]: (s["status"], s["rejectionReason"]) for s in client.get(f"/config-jobs/{job['jobId']}").json()["subChanges"]}
    assert job["status"] == "PARTIAL_SUCCESS"
    assert statuses == {"ME-1": ("APPLIED", None), "ME-UNKNOWN": ("REJECTED", "ENDPOINT_UNREACHABLE")}


def test_a_repeat_under_the_same_idempotency_key_is_answered_from_the_first_and_not_sent_again(client, db_session_factory, nf, monkeypatch):
    """Call flow 03f: DME asks again about an UNCONFIRMED action with the same `Idempotency-Key`; RAN NF OAM answers from the first attempt and sends nothing.
    The MSAC credential header is not part of the request's fingerprint, so a repeat that carries it (or not) is the same request."""
    _make_me(db_session_factory)
    sent = []
    monkeypatch.setattr("app.main.send_edit_config", lambda *a, **k: sent.append(a) or nf["edit_result"])
    body = {"requestedBy": "operator", "scope": "cell", "changes": CELLS[:1]}
    first = client.post("/config-jobs", json=body, headers={"Idempotency-Key": "dme-action:1", "X-MSAC-Credential": "s3cret"})
    again = client.post("/config-jobs", json=body, headers={"Idempotency-Key": "dme-action:1"})
    assert again.status_code == 202 and again.headers.get("Idempotent-Replayed") == "true" and again.json() == first.json()
    assert len(sent) == 1
