"""AI-13: why an rApp acted. A record per rApp config job (inputs reference, model version, rationale, job, who approved), written with the job, hash-chained
through `smo_shared.audit`, and queryable."""

import datetime
import uuid

import pytest
from sqlalchemy import select

from smo_shared import audit

from app import tasks
from app.models import RAppDecisionRecord, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import ELEMENTS, fleet  # noqa: F401

ES = {"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"}
TS = {"X-R1-Invoker-Id": "ts-client", "X-R1-Role": "rapp"}
GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}
CONTEXT = {"inputsRef": "dme://data-jobs/42", "modelVersion": "energy-saving 1.4.2", "rationale": "PRB use under 5 percent for an hour", "actionId": "act-1"}


@pytest.fixture(autouse=True)
def no_inline_sending(monkeypatch):
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")


def _write(client, headers=ES, refs=ELEMENTS[:2], value=20, decision=CONTEXT, **extra):
    """Posts a config job as the given caller (default: the es-client rApp) setting txPower on `refs`, with the decision context unless `decision`
    is None.
    """
    changes = [{"managedElementRef": ref, "attributeChanges": {"txPower": value}} for ref in refs]
    body = {"requestedBy": "es-rapp", "scope": "cell", "changes": changes, **extra}
    if decision is not None:
        body["decision"] = decision
    return client.post("/config-jobs", headers=headers, json=body)


def _records(client, **params):
    resp = client.get("/decision-records", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 13.1 / 13.2: a record per rApp config job

def test_a_job_made_for_an_rapp_has_a_record_of_why(client, fleet):
    """A job made for an rApp has a DIRECT decision record with who, what, the context the rApp gave (inputs, model version, rationale, action id),
    the elements and a 64-character content hash.
    """
    job_id = _write(client).json()["jobId"]
    [record] = _records(client)["items"]
    assert record["jobId"] == job_id and record["disposition"] == "DIRECT" and record["invokerId"] == "es-client" and record["requestedBy"] == "es-rapp"
    assert (record["inputsRef"], record["modelVersion"], record["rationale"], record["actionId"]) == (
        CONTEXT["inputsRef"], CONTEXT["modelVersion"], CONTEXT["rationale"], "act-1")
    assert record["managedElements"] == ELEMENTS[:2] and record["changeCount"] == 2 and record["approvedBy"] is None and record["decidedBy"] is None
    assert record["occurredAt"].endswith("Z") and record["contentHash"] and len(record["contentHash"]) == 64


def test_a_job_with_no_context_still_has_a_record_with_those_fields_empty(client, fleet):
    """A job sent without a decision context still gets a record, with the context fields empty."""
    job_id = _write(client, decision=None).json()["jobId"]
    [record] = _records(client, job_id=job_id)["items"]
    assert record["inputsRef"] is None and record["modelVersion"] is None and record["rationale"] is None


def test_an_smo_module_on_its_own_account_has_no_record_but_one_acting_for_an_rapp_does(client, fleet):
    """A write by an SMO module for itself (the GUI) leaves no decision record, while one carried on behalf of an rApp does, under the rApp's id
    and not the module's.
    """
    assert _write(client, headers=GUI, decision=None).status_code == 202                  # the GUI's own write: not an rApp's action
    assert _records(client)["total"] == 0
    through_dme = {"X-R1-Invoker-Id": "dme-module", "X-R1-Role": "internal", "X-R1-On-Behalf-Of": "es-client"}
    _write(client, headers=through_dme, refs=ELEMENTS[2:3])
    [record] = _records(client)["items"]
    assert record["invokerId"] == "es-client"                                             # the rApp, not the module that carried it


def test_a_rollback_by_an_rapp_has_a_record_marked_as_one(client, fleet):
    """A rollback made by an rApp has its own record with disposition ROLLBACK."""
    job_id = _write(client).json()["jobId"]
    undo = client.post(f"/config-jobs/{job_id}/rollback", headers=ES, json={"requestedBy": "es-rapp", "accessScope": "cell"})
    assert undo.status_code == 202, undo.text
    assert [r["disposition"] for r in _records(client)["items"]] == ["ROLLBACK", "DIRECT"]


def test_the_context_is_validated_and_a_long_rationale_is_refused(client, fleet):
    """A rationale over 4000 characters or a non-string field in the context is 422 and no record is made."""
    assert _write(client, decision={"rationale": "x" * 4001}).status_code == 422
    assert _write(client, decision={"modelVersion": 5}).status_code == 422
    assert _records(client)["total"] == 0


def test_a_record_is_written_in_the_transaction_of_the_job(client, fleet, monkeypatch):
    """The record is written in the job's transaction: if the dispatch raises, neither the job nor its record is kept."""
    def boom(*a, **kw):
        raise RuntimeError("dispatch failed")
    monkeypatch.setattr("app.main._advance", boom)
    with pytest.raises(RuntimeError):
        _write(client)
    with fleet["db"]() as db:                                                              # neither the job nor its record survived
        assert db.query(WriteConfigJob).count() == 0 and db.query(RAppDecisionRecord).count() == 0


# ---- the approval path

def test_an_approved_job_names_the_approver_and_the_approval(client, fleet):
    """A job made from an approved request has an APPROVED record that names the approver, the approval and the original context."""
    client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"})
    approval_id = _write(client).json()["approvalId"]
    approved = client.post(f"/rapp-approvals/{approval_id}/approve", headers={**GUI, "X-R1-Acting-User": "smo-gui:alice"}, json={}).json()
    [record] = _records(client, job_id=approved["jobId"])["items"]
    assert record["disposition"] == "APPROVED" and record["approvedBy"] == "smo-gui:alice" and record["approvalId"] == approval_id
    assert record["decidedAt"] and record["modelVersion"] == CONTEXT["modelVersion"] and record["rationale"] == CONTEXT["rationale"]


def test_a_rejected_request_leaves_a_record_with_no_job(client, fleet):
    """A request that is only waiting leaves no record; once rejected it leaves a REJECTED record with no job."""
    client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"})
    approval_id = _write(client).json()["approvalId"]
    assert _records(client)["total"] == 0                                                  # waiting is not yet a decision
    client.post(f"/rapp-approvals/{approval_id}/reject", headers={**GUI, "X-R1-Acting-User": "smo-gui:alice"}, json={"reason": "no"})
    [record] = _records(client, approval_id=approval_id)["items"]
    assert record["disposition"] == "REJECTED" and record["jobId"] is None and record["approvedBy"] is None and record["decidedBy"] == "smo-gui:alice"


# ---- 13.3: the query

def test_the_query_filters_by_rapp_disposition_model_job_and_time(client, fleet):
    """The list can be filtered by rApp, disposition, model version, job and time (`since` inclusive, `until` exclusive), and a bad disposition or
    job id is 422.
    """
    first = _write(client).json()["jobId"]
    _write(client, headers=TS, refs=ELEMENTS[2:3], decision={**CONTEXT, "modelVersion": "ts 2.0"})
    cut = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=1)
    assert _records(client, invoker_id="ts-client")["total"] == 1
    assert _records(client, model_version="ts 2.0")["items"][0]["invokerId"] == "ts-client"
    assert [r["jobId"] for r in _records(client, job_id=first)["items"]] == [first]
    assert _records(client, disposition="APPROVED")["total"] == 0 and _records(client, disposition="DIRECT")["total"] == 2
    assert _records(client, since=cut.isoformat())["total"] == 0 and _records(client, until=cut.isoformat())["total"] == 2
    assert _records(client, until="2000-01-01T00:00:00Z")["total"] == 0
    assert client.get("/decision-records", params={"disposition": "MAYBE"}).status_code == 422
    assert client.get("/decision-records", params={"job_id": "not-a-uuid"}).status_code == 422


def test_the_query_pages_newest_first_and_can_skip_the_count(client, fleet):
    """Records are listed newest first with paging, `total=false` replaces the count with `hasMore`, and a limit of 0 is 422."""
    for value in (11, 12, 13):
        _write(client, value=value, refs=ELEMENTS[:1])
    everything = _records(client)
    assert everything["total"] == 3 and [r["occurredAt"] for r in everything["items"]] == sorted((r["occurredAt"] for r in everything["items"]), reverse=True)
    page = _records(client, limit=2, offset=1, total="false")
    assert "total" not in page and page["hasMore"] is False and [r["decisionId"] for r in page["items"]] == [r["decisionId"] for r in everything["items"][1:]]
    assert _records(client, limit=1, total="false")["hasMore"] is True
    assert client.get("/decision-records", params={"limit": 0}).status_code == 422


def test_one_record_by_id_and_a_404_for_an_unknown_one(client, fleet):
    """One record can be read by id with its integrity VERIFIED, and an unknown id is 404 DECISION_RECORD_NOT_FOUND."""
    _write(client)
    [listed] = _records(client)["items"]
    one = client.get(f"/decision-records/{listed['decisionId']}").json()
    assert one["decisionId"] == listed["decisionId"] and one["integrity"]["status"] == "VERIFIED"
    missing = client.get(f"/decision-records/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["detail"]["title"] == "DECISION_RECORD_NOT_FOUND"


# ---- the hash chain

def test_every_record_is_written_to_the_audit_chain_and_the_chain_verifies(client, fleet):
    """Each record is written to the shared audit chain with its content hash and the record's id, the record points back at its audit row, and the
    chain verifies.
    """
    _write(client)
    _write(client, headers=TS, refs=ELEMENTS[2:3])
    with fleet["db"]() as db:
        entries = db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq)).all()
        assert [(e.action, e.actor, e.result) for e in entries] == [("DECISION", "es-client", "DIRECT"), ("DECISION", "ts-client", "DIRECT")]
        records = {str(r.decision_id): r for r in db.scalars(select(RAppDecisionRecord)).all()}
        for entry in entries:
            record = records[entry.detail["decisionId"]]
            assert entry.detail["contentHash"] == record.content_hash and record.audit_seq == entry.seq
            assert entry.target == f"/ran-nf-oam/decision-records/{record.decision_id}" and entry.actor_role == "rapp"
        assert audit.verify(db) is None


def test_a_record_changed_afterwards_no_longer_matches_its_hash(client, fleet):
    """Editing a record's field after the fact makes its integrity MISMATCH because it no longer hashes to its stored hash."""
    _write(client)
    [listed] = _records(client)["items"]
    with fleet["db"]() as db:
        record = db.scalars(select(RAppDecisionRecord)).one()
        record.rationale = "an edited rationale"
        db.commit()
    again = client.get(f"/decision-records/{listed['decisionId']}").json()
    assert again["integrity"]["status"] == "MISMATCH" and "no longer hashes" in again["integrity"]["reason"]


def test_a_record_with_its_hash_recomputed_still_does_not_match_the_chain(client, fleet):
    """Even if the editor also recomputes the record's own hash, the integrity is MISMATCH because the audit row still carries the original."""
    _write(client)
    [listed] = _records(client)["items"]
    from app.main import _decision_hash
    with fleet["db"]() as db:
        record = db.scalars(select(RAppDecisionRecord)).one()
        record.rationale = "an edited rationale"
        record.content_hash = _decision_hash(record)                                       # whoever edits the table also recomputes the record's own hash
        db.commit()
    again = client.get(f"/decision-records/{listed['decisionId']}").json()
    assert again["integrity"]["status"] == "MISMATCH" and "audit row" in again["integrity"]["reason"]


def test_an_audit_row_that_does_not_carry_the_record_is_a_mismatch(client, fleet):
    """If the audit row's copy of the hash is altered, the record reads MISMATCH and the chain itself no longer verifies."""
    _write(client)
    [listed] = _records(client)["items"]
    with fleet["db"]() as db:
        entry = db.scalars(select(audit.AuditEntry)).one()
        entry.detail = {**entry.detail, "contentHash": "0" * 64}
        db.commit()
        assert audit.verify(db) is not None                                                # and the chain itself breaks, as it does for any edited row
    assert client.get(f"/decision-records/{listed['decisionId']}").json()["integrity"]["status"] == "MISMATCH"


def test_a_record_whose_chain_write_failed_is_unchained_until_the_worker_chains_it(client, fleet, monkeypatch):
    """If the audit write fails the job is still made and answered, the record is UNCHAINED, and the worker's `chain-decisions` task chains it
    later.
    """
    real = audit.record

    def failing(*a, **kw):
        raise RuntimeError("the audit table is not reachable")
    monkeypatch.setattr("app.main.audit.record", failing)
    job_id = _write(client).json()["jobId"]                                                # the job is made and answered; the chain write is the only thing that failed
    [listed] = _records(client, job_id=job_id)["items"]
    assert listed["auditSeq"] is None
    assert client.get(f"/decision-records/{listed['decisionId']}").json()["integrity"] == {"status": "UNCHAINED", "reason": "not yet written to the audit chain"}
    monkeypatch.setattr("app.main.audit.record", real)
    monkeypatch.setattr(tasks, "SessionLocal", fleet["db"])
    tasks.chain_decisions()
    one = client.get(f"/decision-records/{listed['decisionId']}").json()
    assert one["integrity"]["status"] == "VERIFIED" and one["auditSeq"] == 1
    tasks.chain_decisions()                                                                # nothing left to chain, nothing duplicated
    with fleet["db"]() as db:
        assert db.query(audit.AuditEntry).count() == 1


def test_a_lapsed_request_is_recorded_and_chained(client, fleet):
    """An approval request that times out is recorded as EXPIRED, decided by `system:timeout`, and chained."""
    client.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin", "timeoutSeconds": 60})
    approval_id = _write(client).json()["approvalId"]
    from app.models import RAppActionApproval
    with fleet["db"]() as db:
        row = db.scalars(select(RAppActionApproval)).one()
        row.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
        db.commit()
    client.post("/rapp-approvals/expire-due")
    [record] = _records(client, approval_id=approval_id)["items"]
    assert record["disposition"] == "EXPIRED" and record["decidedBy"] == "system:timeout" and record["auditSeq"] == 1
