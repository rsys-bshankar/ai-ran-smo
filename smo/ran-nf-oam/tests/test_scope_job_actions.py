"""SEC-15.4 and SEC-15.7: the ownership and scope rules of the config-job actions, the software-update routes and the deletion of a vendor capability.

`POST /config-jobs/{id}/kpi-check|continue|halt|abort` follow the rollback: 404 `CONFIG_JOB_NOT_FOUND` for another scoped rApp's job, 403 `SCOPE_DENIED` for a job that wrote
outside the caller's scope. `POST /software-management-jobs` is a 403 for an element outside the scope and `.../{id}/advance` a 404 for a job on one. `DELETE /vendor-capabilities/{v}`
is filtered by scope as its get is. The fixtures (`places`, `vendors`) and the headers come from `test_scope.py` and `test_scope_reads.py` (ME-1 eu/acme, ME-2 eu/globex, ME-3 us/acme).
Run with: `PYTHONPATH=.:../shared python -m pytest tests/test_scope_job_actions.py -q`.
"""

import pytest

from smo_shared.scope import SCOPE_HEADER

from app.models import SoftwareManagementJob, WriteConfigJob

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_scope import EU, GUI, UNSCOPED, _title, _write, claim, places  # noqa: F401  (places: a pytest fixture)
from test_scope_reads import OTHER_RAPP, vendors  # noqa: F401  (vendors: a pytest fixture)
from test_waves import fleet  # noqa: F401

WIDE = {**UNSCOPED, SCOPE_HEADER: claim(regions=["eu", "us"])}
ACTIONS = ("continue", "halt", "abort", "kpi-check")
BODY = {"requestedBy": "es-rapp", "kpi": "no-such-kpi"}               # the extra fields are for kpi-check; the others ignore them


def _halted(client, refs, headers) -> str:
    """Makes a job on `refs` as `headers`, in waves of one with an hour between them, so that it stops HALTED after its first wave; returns its id."""
    resp = _write(client, refs, headers, waveSize=1, wavePauseSeconds=3600)
    assert resp.status_code == 202 and resp.json()["status"] == "HALTED", resp.text
    return resp.json()["jobId"]


def _status(places, job_id) -> str:
    """The job's status as stored, read fresh."""
    import uuid
    with places["db"]() as db:
        return db.get(WriteConfigJob, uuid.UUID(job_id)).status


@pytest.mark.parametrize("action", ACTIONS)
def test_a_scoped_rapp_cannot_act_on_another_rapps_job(client, places, action):
    """Another scoped rApp in the same region gets 404 CONFIG_JOB_NOT_FOUND on continue, halt, abort and kpi-check of a job that is not its own, and the job is not touched."""
    job = _halted(client, ["ME-1", "ME-2"], EU)
    edits = list(places["edits"])
    resp = client.post(f"/config-jobs/{job}/{action}", headers=OTHER_RAPP, json=BODY)
    assert resp.status_code == 404 and _title(resp) == "CONFIG_JOB_NOT_FOUND"
    assert _status(places, job) == "HALTED" and places["edits"] == edits           # not continued, not aborted, nothing written


@pytest.mark.parametrize("action", ACTIONS)
def test_a_job_that_wrote_outside_the_callers_scope_is_refused(client, places, action):
    """The same rApp with a narrower claim gets 403 SCOPE_DENIED (recorded, naming no element) on the actions of a job that wrote to an element outside it, and the job is not touched."""
    job = _halted(client, ["ME-1", "ME-3"], WIDE)                       # an eu element and a us one
    edits = list(places["edits"])
    resp = client.post(f"/config-jobs/{job}/{action}", headers=EU, json=BODY)
    assert resp.status_code == 403 and _title(resp) == "SCOPE_DENIED"
    assert "ME-3" not in resp.json()["detail"]["detail"] and "ME-1" not in resp.json()["detail"]["detail"]
    assert _status(places, job) == "HALTED" and places["edits"] == edits
    assert client.get("/safeguard-refusals").json()["items"][0]["refusal"] == "SCOPE_DENIED"


def test_the_owner_and_the_operator_still_act_on_a_halted_job(client, places):
    """The rApp that made a job, inside its scope, continues it; an SMO module on its own account halts and aborts any job; an unknown job is still 404 for them."""
    mine = _halted(client, ["ME-1", "ME-2"], EU)
    assert client.post(f"/config-jobs/{mine}/continue", headers=EU, json={**BODY, "force": True}).status_code == 202
    other = _halted(client, ["ME-1", "ME-2"], EU)
    assert client.post(f"/config-jobs/{other}/halt", headers=GUI, json=BODY).json()["haltedReason"] == "OPERATOR_HALT"
    assert client.post(f"/config-jobs/{other}/abort", headers=GUI, json=BODY).status_code == 200
    unscoped = _halted(client, ["ME-1", "ME-2"], UNSCOPED)               # an rApp nobody scoped: its job is not a scoped rApp's to touch, but it acts on its own as before
    assert client.post(f"/config-jobs/{unscoped}/abort", headers=OTHER_RAPP, json=BODY).status_code == 404
    assert client.post(f"/config-jobs/{unscoped}/abort", headers=UNSCOPED, json=BODY).status_code == 200


def test_a_software_update_needs_an_element_inside_the_scope(client, places):
    """Starting a software job on an element outside the caller's scope (or not registered) is 403 SCOPE_DENIED and starts nothing; inside the scope and without a claim it is accepted."""
    denied = client.post("/software-management-jobs", headers=EU, params={"managed_element_ref": "ME-3"})
    unknown = client.post("/software-management-jobs", headers=EU, params={"managed_element_ref": "ME-99"})
    assert denied.status_code == unknown.status_code == 403 and _title(denied) == _title(unknown) == "SCOPE_DENIED"
    with places["db"]() as db:
        assert db.query(SoftwareManagementJob).count() == 0
    assert client.post("/software-management-jobs", headers=EU, params={"managed_element_ref": "ME-1"}).status_code == 202
    assert client.post("/software-management-jobs", params={"managed_element_ref": "ME-3"}).status_code == 202            # no claim: as before


def test_a_software_job_on_an_element_outside_the_scope_cannot_be_advanced(client, places):
    """Reporting a phase of a software job on an element outside the caller's scope is a 404 like an unknown job, and the job does not move; an unscoped caller advances it."""
    job = client.post("/software-management-jobs", params={"managed_element_ref": "ME-3"}).json()["jobId"]
    hidden = client.post(f"/software-management-jobs/{job}/advance", headers=EU, params={"succeeded": True})
    assert hidden.status_code == 404 and _title(hidden) == "SOFTWARE_JOB_NOT_FOUND"
    with places["db"]() as db:
        assert db.query(SoftwareManagementJob).one().phase == "DOWNLOAD"
    assert client.post(f"/software-management-jobs/{job}/advance", params={"succeeded": True}).json()["phase"] == "INSTALL"


def test_a_scoped_caller_deletes_only_the_capability_of_a_vendor_of_its_own_elements(client, vendors):
    """A caller with a claim that none of its elements matches deletes nothing (204, as for an unknown vendor); the entry of a vendor one of its elements uses is deleted; an unscoped caller deletes any."""
    assert client.delete("/vendor-capabilities/other-ran", headers=EU).status_code == 204               # ME-3 is us: not an eu vendor
    assert client.delete("/vendor-capabilities/unused-ran", headers=EU).status_code == 204
    assert client.get("/vendor-capabilities/other-ran").status_code == 200 and client.get("/vendor-capabilities/unused-ran").status_code == 200
    assert client.delete("/vendor-capabilities/acme-ran", headers=EU).status_code == 204
    assert client.get("/vendor-capabilities/acme-ran").status_code == 404
    assert client.delete("/vendor-capabilities/other-ran").status_code == 204
    assert client.get("/vendor-capabilities/other-ran").status_code == 404
