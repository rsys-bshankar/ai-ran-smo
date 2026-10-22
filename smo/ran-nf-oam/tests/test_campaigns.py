"""PR-MGT-15: software campaigns. A campaign is many software management jobs run in waves (15.1, 15.2), with a health gate between the waves, a rollback (15.3)
and a report (15.4). The jobs are the existing ones, advanced as before; a job that belongs to a campaign tells its campaign what happened."""

import datetime
import json
import uuid

import pytest
from sqlalchemy import select

from smo_shared.scope import SCOPE_HEADER
from smo_shared.testing import concurrent_commit_on

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import ELEMENTS, fleet  # noqa: F401

from app import lifecycle
from app.models import Alarm, ManagedEntity, SoftwareCampaign, SoftwareManagementJob, VendorCapability
from app.statemachine import CAMPAIGN_FSM, CampaignEvent, CampaignState
from smo_shared.statemachine import IllegalTransition


def _create(client, refs=None, **extra):
    body = {"requestedBy": "alice", "name": "r3-upgrade", "softwareVersion": "3.0", **({"managedElementRefs": refs or ELEMENTS} if "selector" not in extra else {}), **extra}
    return client.post("/software-campaigns", json=body)


def _campaign(client, refs=None, **extra):
    resp = _create(client, refs, **extra)
    assert resp.status_code == 202, resp.text
    return resp.json()["campaignId"]


def _view(client, cid):
    return client.get(f"/software-campaigns/{cid}").json()


def _report(client, cid):
    resp = client.get(f"/software-campaigns/{cid}/report")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wave_jobs(client, cid, wave):
    return _report(client, cid)["waves"][wave - 1]["jobs"]


def _step(client, job_id, ok=True):
    resp = client.post(f"/software-management-jobs/{job_id}/advance", params={"succeeded": ok})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _complete(client, job_id):
    for _ in range(3):                                                       # DOWNLOAD, INSTALL, ACTIVATE
        last = _step(client, job_id)
    assert last["status"] == "COMPLETED"


def _run_wave(client, cid, wave, fail=()):
    """Drives every software job of `wave` to its end: each job completes, except those of the elements in `fail`, which fail in their current
    phase.
    """
    for job in _wave_jobs(client, cid, wave):
        if job["managedElementRef"] in fail:
            _step(client, job["jobId"], ok=False)
        else:
            _complete(client, job["jobId"])


def _alarm(fleet, ref, severity="major", hours=1):
    with fleet["db"]() as db:
        db.add(Alarm(source_alarm_id=f"a-{uuid.uuid4()}", managed_element_ref=ref, severity=severity, raised_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)))
        db.commit()


def _events(client, cid):
    return [e["event"] for e in _view(client, cid)["events"]]


# ---- 15.1 the campaign and its jobs

def test_a_campaign_without_waves_starts_every_job_and_completes_when_they_all_have(client, fleet):
    """A campaign without a wave size starts one job per element at once and completes only when every job has ended."""
    cid = _campaign(client)
    view = _view(client, cid)
    assert view["status"] == "RUNNING" and view["wave"] == 1 and view["waveCount"] == 1 and view["elements"] == ELEMENTS and view["softwareVersion"] == "3.0"
    jobs = _wave_jobs(client, cid, 1)
    assert [j["status"] for j in jobs] == ["IN_PROGRESS"] * 4 and {j["phase"] for j in jobs} == {"DOWNLOAD"}
    for job in jobs[:3]:
        _complete(client, job["jobId"])
    assert _view(client, cid)["status"] == "RUNNING"                          # one job still running: the wave has not ended
    _complete(client, jobs[3]["jobId"])
    done = _view(client, cid)
    assert done["status"] == "COMPLETED" and done["finishedAt"] and done["haltedReason"] is None
    assert _events(client, cid) == ["STARTED", "WAVE_STARTED", "GATE_PASSED", "COMPLETED"]


def test_the_software_job_list_shows_the_campaign_of_a_job_and_leaves_other_jobs_as_they_were(client, fleet):
    """The software job list shows the campaign and wave of a campaign's jobs and nothing extra for a job started the old way."""
    plain = client.post("/software-management-jobs", params={"managed_element_ref": "ME-1", "remote_file_path": "sftp://repo/sw-2.0.zip"}).json()
    _complete(client, plain["jobId"])                                          # a job started the old way, advanced the old way
    cid = _campaign(client, ["ME-2"])
    items = {j["managedElementRef"]: j for j in client.get("/software-management-jobs").json()["items"]}
    assert "campaignId" not in items["ME-1"] and items["ME-2"]["campaignId"] == cid and items["ME-2"]["campaignWave"] == 1
    assert "rollbackOf" not in items["ME-2"]


def test_waves_run_one_after_the_other_and_the_next_starts_by_itself(client, fleet):
    """Waves run in order: the next wave starts by itself when the previous one's jobs have all ended, and the campaign completes after the last."""
    cid = _campaign(client, waveSize=2)
    view = _view(client, cid)
    assert view["waveCount"] == 2 and view["wave"] == 1
    report = _report(client, cid)
    assert [w["elements"] for w in report["waves"]] == [["ME-1", "ME-2"], ["ME-3", "ME-4"]] and [w["started"] for w in report["waves"]] == [True, False]
    _run_wave(client, cid, 1)
    assert _view(client, cid)["wave"] == 2 and _view(client, cid)["status"] == "RUNNING"
    assert [j["managedElementRef"] for j in _wave_jobs(client, cid, 2)] == ["ME-3", "ME-4"]
    _run_wave(client, cid, 2)
    assert _view(client, cid)["status"] == "COMPLETED"
    assert _events(client, cid) == ["STARTED", "WAVE_STARTED", "GATE_PASSED", "WAVE_STARTED", "GATE_PASSED", "COMPLETED"]


def test_elements_can_be_selected_by_entity_type_vendor_region_and_tenant(client, fleet):
    """A selector picks the elements matching every key it names (entity type, vendor, region, tenant), in reference order."""
    with fleet["db"]() as db:
        for ref, (region, tenant, vendor) in {"ME-1": ("eu", "acme", "v1"), "ME-2": ("eu", "globex", "v1"), "ME-3": ("us", "acme", "v2"), "ME-4": ("eu", "acme", "v2")}.items():
            row = db.get(ManagedEntity, ref)
            row.region, row.tenant, row.vendor_name = region, tenant, vendor
        db.commit()

    def waves(selector):
        resp = client.post("/software-campaigns", json={"requestedBy": "a", "name": "n", "selector": selector, "waveSize": 1, "dryRun": True})
        assert resp.status_code == 200, resp.text
        return [w[0] for w in resp.json()["waves"]]

    assert waves({"region": "eu"}) == ["ME-1", "ME-2", "ME-4"]                  # in reference order
    assert waves({"region": "eu", "tenant": "acme"}) == ["ME-1", "ME-4"]
    assert waves({"vendorName": "v2"}) == ["ME-3", "ME-4"]
    assert waves({"entityType": "O-DU", "tenant": "globex"}) == ["ME-2"]
    cid = _campaign(client, selector={"region": "us"})
    assert _view(client, cid)["selector"] == {"region": "us"} and _view(client, cid)["elements"] == ["ME-3"]


def test_an_invalid_request_is_refused_and_starts_nothing(client, fleet):
    """A request with neither or both ways of naming elements, an empty selector or list, a bad wave size, unknown or unregistered elements is
    refused and no job is started.
    """
    def refused(**body):
        resp = client.post("/software-campaigns", json={"requestedBy": "a", "name": "n", **body})
        assert resp.status_code in (409, 422), resp.text
        return resp

    assert refused().status_code == 422                                                         # neither elements nor selector
    assert refused(managedElementRefs=["ME-1"], selector={"region": "eu"}).status_code == 422   # both
    assert refused(selector={}).status_code == 422                                              # a selector that selects everything is not a selector
    assert refused(managedElementRefs=[]).status_code == 422
    assert refused(managedElementRefs=["ME-1"], waveSize=0).status_code == 422
    assert refused(managedElementRefs=["ME-1"], onGateFailure="shrug").status_code == 422
    assert refused(managedElementRefs=["ME-1"], bogus=1).status_code == 422
    assert "ME-404" in refused(managedElementRefs=["ME-1", "ME-404"]).json()["detail"]["detail"]
    assert refused(selector={"region": "nowhere"}).json()["detail"]["detail"].startswith("the selector matches no element")
    with fleet["db"]() as db:
        db.add(VendorCapability(vendor_name="nosw", supported_services=["PROV", "FM"], conformance_mode="SPEC", supported_vendor_modes=["O1_NETCONF"]))
        db.get(ManagedEntity, "ME-2").vendor_name = "nosw"
        db.commit()
    no_swm = refused(managedElementRefs=["ME-1", "ME-2"])
    assert no_swm.json()["detail"]["title"] == "O1_SERVICE_NOT_SUPPORTED" and "ME-2" in no_swm.json()["detail"]["detail"]
    assert [j for j in client.get("/software-management-jobs").json()["items"]] == []
    assert client.get("/software-campaigns").json()["items"] == []


def test_selecting_skips_elements_without_a_software_service_and_an_oversized_selection_is_refused(client, fleet, monkeypatch):
    """A selector skips elements whose vendor lacks the SWM service, and a selection above the element limit is refused (422, narrow it)."""
    with fleet["db"]() as db:
        db.add(VendorCapability(vendor_name="nosw", supported_services=["PROV"], conformance_mode="SPEC", supported_vendor_modes=["O1_NETCONF"]))
        db.get(ManagedEntity, "ME-2").vendor_name = "nosw"
        db.commit()
    resp = client.post("/software-campaigns", json={"requestedBy": "a", "name": "n", "selector": {"entityType": "O-DU"}, "dryRun": True})
    assert resp.json()["waves"] == [["ME-1", "ME-3", "ME-4"]]
    monkeypatch.setattr(lifecycle, "MAX_CAMPAIGN_ELEMENTS", 2)
    big = client.post("/software-campaigns", json={"requestedBy": "a", "name": "n", "selector": {"entityType": "O-DU"}})
    assert big.status_code == 422 and "narrow it" in big.json()["detail"]["detail"]


def test_an_element_that_already_has_a_software_job_running_is_refused(client, fleet):
    """An element that already has a software job in flight, from a manual job or another campaign, is 409 and the campaign is not started."""
    client.post("/software-management-jobs", params={"managed_element_ref": "ME-3", "remote_file_path": "sftp://repo/sw-2.0.zip"})
    busy = _create(client)
    assert busy.status_code == 409 and "ME-3" in busy.json()["detail"]["detail"]
    other = _create(client, ["ME-1", "ME-2"])
    assert other.status_code == 202
    assert _create(client, ["ME-2", "ME-4"]).status_code == 409                # one campaign's running job blocks another's


def test_a_dry_run_works_out_the_waves_and_starts_nothing(client, fleet):
    """A dry run answers 200 with the waves it would run and creates no campaign or job."""
    resp = _create(client, waveSize=3, dryRun=True)
    assert resp.status_code == 200 and resp.json() == {"dryRun": True, "status": "VALIDATED", "waveCount": 2, "waves": [["ME-1", "ME-2", "ME-3"], ["ME-4"]]}
    assert client.get("/software-campaigns").json()["items"] == [] and client.get("/software-management-jobs").json()["items"] == []


def test_the_same_request_with_the_same_idempotency_key_is_one_campaign(client, fleet):
    """Repeating a campaign request with the same Idempotency-Key gives the same answer and one campaign."""
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"requestedBy": "a", "name": "n", "managedElementRefs": ["ME-1"]}
    first, second = (client.post("/software-campaigns", json=body, headers=headers) for _ in range(2))
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert len(client.get("/software-campaigns").json()["items"]) == 1


def test_list_get_and_the_unknown_campaign(client, fleet):
    """Campaigns can be listed (and filtered by status; a bad status is 422) and read, and an unknown campaign is 404 on every route."""
    a = _campaign(client, ["ME-1"])
    b = _campaign(client, ["ME-2"])
    _step(client, _wave_jobs(client, b, 1)[0]["jobId"], ok=False)
    client.post(f"/software-campaigns/{b}/abort", json={"requestedBy": "x"})
    assert {c["campaignId"] for c in client.get("/software-campaigns").json()["items"]} == {a, b}
    assert [c["campaignId"] for c in client.get("/software-campaigns", params={"status": "RUNNING"}).json()["items"]] == [a]
    assert client.get("/software-campaigns", params={"status": "BOGUS"}).status_code == 422
    unknown = uuid.uuid4()
    for method, path in (("get", ""), ("get", "/report"), ("post", "/continue"), ("post", "/halt"), ("post", "/abort"), ("post", "/rollback")):
        resp = getattr(client, method)(f"/software-campaigns/{unknown}{path}", **({"json": {"requestedBy": "x"}} if method == "post" else {}))
        assert resp.status_code == 404 and resp.json()["detail"]["title"] == "SOFTWARE_CAMPAIGN_NOT_FOUND"


# ---- 15.2 the health gate

def test_a_failed_job_fails_the_gate_and_halts_the_campaign_before_the_next_wave(client, fleet):
    """A failed job fails the wave's gate: the campaign halts with GATE_FAILED, its detail names the job and the next wave never starts."""
    cid = _campaign(client, waveSize=2)
    _run_wave(client, cid, 1, fail={"ME-2"})
    view = _view(client, cid)
    assert view["status"] == "HALTED" and view["haltedReason"] == "GATE_FAILED" and view["wave"] == 1
    assert "1 software job(s) of wave 1 failed" in view["haltedDetail"] and "ME-2" in view["haltedDetail"]
    assert not _report(client, cid)["waves"][1]["started"]                    # wave 2 never started
    assert _events(client, cid)[-2:] == ["GATE_FAILED", "HALTED"]


def test_new_critical_or_major_alarms_on_the_wave_fail_the_gate_up_to_the_limit(client, fleet):
    """New critical or major alarms on the wave's elements fail the gate; older ones, minor ones and other waves' elements are not counted."""
    cid = _campaign(client, waveSize=2)
    _alarm(fleet, "ME-1", "major", hours=-1)                                    # there before the wave: not new
    _alarm(fleet, "ME-1", "minor")                                              # not severe
    _alarm(fleet, "ME-4", "critical")                                           # another wave's element
    _run_wave(client, cid, 1)
    assert _view(client, cid)["wave"] == 2                                      # the gate passed
    _alarm(fleet, "ME-3", "critical")
    _alarm(fleet, "ME-4", "major")
    _run_wave(client, cid, 2)
    view = _view(client, cid)
    assert view["status"] == "HALTED" and view["haltedReason"] == "GATE_FAILED" and "3 new critical or major alarm(s)" in view["haltedDetail"]


def test_the_alarm_limit_is_the_operators(client, fleet):
    """`gateMaxNewAlarms` sets how many new severe alarms a wave may raise before its gate fails."""
    cid = _campaign(client, waveSize=2, gateMaxNewAlarms=1)
    _alarm(fleet, "ME-1", "major")
    _run_wave(client, cid, 1)
    assert _view(client, cid)["wave"] == 2 and _view(client, cid)["status"] == "RUNNING"
    _alarm(fleet, "ME-3", "major")
    _alarm(fleet, "ME-4", "critical")
    _run_wave(client, cid, 2)
    assert _view(client, cid)["status"] == "HALTED"


def test_continuing_after_a_failed_gate_is_the_operators_decision_to_go_on(client, fleet):
    """After a failed gate an explicit continue goes on with the next wave and is recorded with who did it; a finished campaign cannot be continued
    (409).
    """
    cid = _campaign(client, waveSize=2)
    _run_wave(client, cid, 1, fail={"ME-1"})
    resumed = client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"})
    assert resumed.status_code == 202 and resumed.json()["status"] == "RUNNING" and resumed.json()["wave"] == 2
    events = _view(client, cid)["events"]
    assert any(e["event"] == "CONTINUED" and e["by"] == "bob" for e in events)
    _run_wave(client, cid, 2)
    assert _view(client, cid)["status"] == "COMPLETED"
    assert client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"}).status_code == 409


def test_continuing_after_a_failed_gate_on_the_last_wave_ends_the_campaign(client, fleet):
    """Continuing after a failed gate on the last wave completes the campaign, and the report still lists the failed job as needing attention."""
    cid = _campaign(client)
    _run_wave(client, cid, 1, fail={"ME-1"})
    assert _view(client, cid)["status"] == "HALTED"
    assert client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"}).json()["status"] == "COMPLETED"
    report = _report(client, cid)
    assert report["summary"]["failed"] == 1 and report["attention"] == [{"managedElementRef": "ME-1", "problem": "software job failed in phase DOWNLOAD"}]


def test_a_pause_between_waves_holds_the_campaign_until_it_has_elapsed(client, fleet, db_session_factory):
    """A wave pause halts the campaign as WAVE_PAUSE; continuing early is 409 WAVE_PAUSE_NOT_ELAPSED unless forced, and the sweep does nothing
    before the time.
    """
    cid = _campaign(client, waveSize=2, wavePauseSeconds=3600)
    _run_wave(client, cid, 1)
    view = _view(client, cid)
    assert view["status"] == "HALTED" and view["haltedReason"] == "WAVE_PAUSE" and view["nextWaveAt"]
    early = client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"})
    assert early.status_code == 409 and early.json()["detail"]["title"] == "WAVE_PAUSE_NOT_ELAPSED"
    assert client.post("/software-campaigns/advance-due").json() == {"advanced": []}              # not due yet
    forced = client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob", "force": True})
    assert forced.json()["status"] == "RUNNING" and forced.json()["wave"] == 2


def test_the_sweep_continues_a_campaign_whose_pause_has_elapsed(client, fleet, db_session_factory):
    """The sweep continues a campaign whose pause has elapsed, once."""
    cid = _campaign(client, waveSize=2, wavePauseSeconds=3600)
    _run_wave(client, cid, 1)
    with db_session_factory() as db:
        db.get(SoftwareCampaign, uuid.UUID(cid)).next_wave_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)
        db.commit()
    advanced = client.post("/software-campaigns/advance-due").json()["advanced"]
    assert [(a["campaignId"], a["status"], a["wave"]) for a in advanced] == [(cid, "RUNNING", 2)]
    assert client.post("/software-campaigns/advance-due").json() == {"advanced": []}


def test_the_sweep_catches_up_a_campaign_whose_jobs_ended_while_nobody_was_looking(client, fleet, db_session_factory):
    """The sweep moves on a campaign whose jobs ended without any request advancing the campaign."""
    cid = _campaign(client, waveSize=2)
    with db_session_factory() as db:
        for job in db.scalars(select(SoftwareManagementJob).where(SoftwareManagementJob.campaign_id == uuid.UUID(cid))).all():
            job.status = "COMPLETED"
        db.commit()
    assert _view(client, cid)["wave"] == 1
    advanced = client.post("/software-campaigns/advance-due").json()["advanced"]
    assert [(a["status"], a["wave"]) for a in advanced] == [("RUNNING", 2)]


def test_an_operator_halt_stops_the_next_wave_and_continue_runs_the_gate_then(client, fleet):
    """An operator halt lets the jobs in flight finish but stops the next wave; halting again changes nothing, and continue then runs the gate and
    the next wave.
    """
    cid = _campaign(client, waveSize=2)
    halted = client.post(f"/software-campaigns/{cid}/halt", json={"requestedBy": "bob"})
    assert halted.json()["status"] == "HALTED" and halted.json()["haltedReason"] == "OPERATOR_HALT"
    _run_wave(client, cid, 1)                                                  # the jobs in flight went on
    assert _view(client, cid)["status"] == "HALTED" and _view(client, cid)["wave"] == 1
    assert client.post(f"/software-campaigns/{cid}/halt", json={"requestedBy": "bob"}).json()["haltedReason"] == "OPERATOR_HALT"       # already halted: unchanged
    assert client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"}).json()["wave"] == 2


def test_an_operator_halt_while_the_wave_is_still_running_waits_for_it_on_continue(client, fleet):
    """Continuing an operator-halted campaign whose jobs have not ended leaves it RUNNING until they end."""
    cid = _campaign(client)
    client.post(f"/software-campaigns/{cid}/halt", json={"requestedBy": "bob"})
    assert client.post(f"/software-campaigns/{cid}/continue", json={"requestedBy": "bob"}).json()["status"] == "RUNNING"    # jobs not ended: still running
    _run_wave(client, cid, 1)
    assert _view(client, cid)["status"] == "COMPLETED"


def test_halting_a_pause_makes_it_an_operator_halt_and_halting_an_ended_campaign_is_refused(client, fleet):
    """Halting a campaign held by its pause turns it into an operator halt that the sweep no longer continues."""
    cid = _campaign(client, waveSize=2, wavePauseSeconds=3600)
    _run_wave(client, cid, 1)
    assert client.post(f"/software-campaigns/{cid}/halt", json={"requestedBy": "bob"}).json()["haltedReason"] == "OPERATOR_HALT"
    assert _view(client, cid)["nextWaveAt"] is None                              # the sweep no longer continues it
    assert client.post("/software-campaigns/advance-due").json() == {"advanced": []}


def test_a_campaign_that_ended_cannot_be_halted_continued_or_aborted(client, fleet):
    """Halt, continue and abort on an ended campaign are 409 LIFECYCLE_ILLEGAL_TRANSITION."""
    cid = _campaign(client, ["ME-1"])
    _run_wave(client, cid, 1)
    for action in ("halt", "continue", "abort"):
        resp = client.post(f"/software-campaigns/{cid}/{action}", json={"requestedBy": "bob"})
        assert resp.status_code == 409 and resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION", action


def test_abort_ends_a_halted_campaign_and_the_report_says_which_elements_were_never_reached(client, fleet):
    """Abort ends a halted campaign and the report lists the failed and the never-reached elements as needing attention."""
    cid = _campaign(client, waveSize=2)
    _run_wave(client, cid, 1, fail={"ME-2"})
    aborted = client.post(f"/software-campaigns/{cid}/abort", json={"requestedBy": "bob"})
    assert aborted.json()["status"] == "ABORTED" and _view(client, cid)["finishedAt"]
    report = _report(client, cid)
    assert report["summary"] == {"elements": 4, "started": 2, "notReached": 2, "completed": 1, "failed": 1, "inProgress": 0, "reverted": 0}
    assert {a["managedElementRef"] for a in report["attention"]} == {"ME-2", "ME-3", "ME-4"}
    assert any("never reached" in a["problem"] for a in report["attention"])
    assert client.post("/software-campaigns/advance-due").json() == {"advanced": []}


# ---- 15.3 rollback

def _reverts(client, cid):
    return [j for j in client.get("/software-management-jobs", params={"limit": 100}).json()["items"] if j.get("rollbackOf") and j["campaignId"] == cid]


def test_a_failed_gate_can_undo_what_the_campaign_did_by_itself(client, fleet):
    """With `onGateFailure: rollback` a failed gate starts a rollback of only the jobs that completed, and the campaign ends ROLLED_BACK when the
    revert jobs have.
    """
    cid = _campaign(client, waveSize=2, onGateFailure="rollback")
    _run_wave(client, cid, 1, fail={"ME-2"})
    assert _view(client, cid)["status"] == "ROLLING_BACK"
    reverts = _reverts(client, cid)
    assert [r["managedElementRef"] for r in reverts] == ["ME-1"]                      # only the job that completed is undone
    assert not _report(client, cid)["waves"][1]["started"]
    _complete(client, reverts[0]["jobId"])
    done = _view(client, cid)
    assert done["status"] == "ROLLED_BACK" and done["finishedAt"]
    assert _events(client, cid)[-3:] == ["GATE_FAILED", "ROLLBACK_STARTED", "ROLLBACK_DONE"]
    report = _report(client, cid)
    assert report["summary"]["reverted"] == 1 and report["waves"][0]["jobs"][0]["revert"] == "COMPLETED"
    assert {a["managedElementRef"] for a in report["attention"]} == {"ME-2", "ME-3", "ME-4"}


def test_an_operator_can_roll_back_a_completed_campaign_and_a_failed_revert_can_be_retried(client, fleet):
    """An operator can roll back a completed campaign (one revert job per completed job); if a revert fails the campaign is ROLLBACK_FAILED and
    rolling back again retries only what is not undone.
    """
    cid = _campaign(client, waveSize=2)
    _run_wave(client, cid, 1)
    _run_wave(client, cid, 2)
    assert _view(client, cid)["status"] == "COMPLETED"
    started = client.post(f"/software-campaigns/{cid}/rollback", json={"requestedBy": "bob"})
    assert started.status_code == 202 and started.json()["status"] == "ROLLING_BACK"
    reverts = _reverts(client, cid)
    assert sorted(r["managedElementRef"] for r in reverts) == ELEMENTS
    for r in reverts[:3]:
        _complete(client, r["jobId"])
    assert _view(client, cid)["status"] == "ROLLING_BACK"
    _step(client, reverts[3]["jobId"], ok=False)
    failed = _view(client, cid)
    assert failed["status"] == "ROLLBACK_FAILED"
    assert [a["problem"] for a in _report(client, cid)["attention"]] == ["the revert job failed"]
    retry = client.post(f"/software-campaigns/{cid}/rollback", json={"requestedBy": "bob"})
    assert retry.json()["status"] == "ROLLING_BACK"
    new = [r for r in _reverts(client, cid) if r["status"] == "IN_PROGRESS"]
    assert [r["managedElementRef"] for r in new] == [reverts[3]["managedElementRef"]]       # only the one that failed
    _complete(client, new[0]["jobId"])
    final = _report(client, cid)
    assert final["status"] == "ROLLED_BACK" and final["summary"]["reverted"] == 4 and final["attention"] == []


def test_a_halted_campaign_can_be_rolled_back_and_one_with_nothing_completed_is_rolled_back_at_once(client, fleet):
    """A halted campaign can be rolled back, and one with nothing completed is ROLLED_BACK at once with no revert job."""
    cid = _campaign(client, waveSize=2)
    _run_wave(client, cid, 1, fail={"ME-1", "ME-2"})
    assert _view(client, cid)["status"] == "HALTED"
    resp = client.post(f"/software-campaigns/{cid}/rollback", json={"requestedBy": "bob"})
    assert resp.json()["status"] == "ROLLED_BACK" and _reverts(client, cid) == []
    assert "0 job(s) to undo" in json.dumps(_view(client, cid)["events"])


def test_rollback_waits_for_running_jobs_and_is_refused_in_the_wrong_state(client, fleet):
    """A rollback is 422 ROLLBACK_NOT_POSSIBLE while a job is still running, and 409 from a RUNNING campaign."""
    cid = _campaign(client, waveSize=2)
    halted = client.post(f"/software-campaigns/{cid}/halt", json={"requestedBy": "bob"})
    assert halted.status_code == 200
    too_early = client.post(f"/software-campaigns/{cid}/rollback", json={"requestedBy": "bob"})
    assert too_early.status_code == 422 and too_early.json()["detail"]["title"] == "ROLLBACK_NOT_POSSIBLE"
    running = _campaign(client, ["ME-3"])
    wrong = client.post(f"/software-campaigns/{running}/rollback", json={"requestedBy": "bob"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"     # RUNNING: a rollback starts from a halted or ended campaign


def test_the_campaign_fsm_has_the_documented_edges():
    """The campaign state machine has the documented transitions and refuses the others."""
    fire = CAMPAIGN_FSM.fire
    S, E = CampaignState, CampaignEvent
    assert fire(S.PENDING, E.START) == S.RUNNING and fire(S.RUNNING, E.HALT) == S.HALTED and fire(S.HALTED, E.RESUME) == S.RUNNING
    assert fire(S.RUNNING, E.FINISH) == S.COMPLETED and fire(S.HALTED, E.ABORT) == S.ABORTED
    for state in (S.RUNNING, S.HALTED, S.COMPLETED, S.ABORTED, S.ROLLBACK_FAILED):
        assert fire(state, E.ROLLBACK) == S.ROLLING_BACK
    assert fire(S.ROLLING_BACK, E.ROLLBACK_DONE) == S.ROLLED_BACK and fire(S.ROLLING_BACK, E.ROLLBACK_FAILED) == S.ROLLBACK_FAILED
    for state, event in ((S.COMPLETED, E.HALT), (S.ROLLED_BACK, E.ROLLBACK), (S.PENDING, E.HALT), (S.RUNNING, E.ABORT), (S.ROLLING_BACK, E.ROLLBACK), (S.COMPLETED, E.RESUME)):
        with pytest.raises(IllegalTransition):
            fire(state, event)


# ---- 15.4 the report

def test_the_report_gives_the_outcome_per_wave_and_element(client, fleet):
    """The report gives the totals and, per wave and element, the job, its phase and status and whether a revert undid it."""
    cid = _campaign(client, waveSize=2)
    jobs = _wave_jobs(client, cid, 1)
    _complete(client, jobs[0]["jobId"])
    _step(client, jobs[1]["jobId"])
    report = _report(client, cid)
    assert report["name"] == "r3-upgrade" and report["status"] == "RUNNING" and report["requestedBy"] == "alice"
    assert report["summary"] == {"elements": 4, "started": 2, "notReached": 2, "completed": 1, "failed": 0, "inProgress": 1, "reverted": 0}
    first = report["waves"][0]
    assert first["elements"] == ["ME-1", "ME-2"] and first["jobs"][0]["status"] == "COMPLETED" and first["jobs"][1]["phase"] == "INSTALL"
    assert first["jobs"][0]["revert"] is None
    assert report["attention"] == []                                                       # a campaign that is still going has not "missed" anything yet
    assert report["events"][0]["event"] == "STARTED" and report["events"][0]["by"] == "alice"


# ---- scope (PR-SEC-10)

def _scoped(region):
    return {"X-R1-Invoker-Id": "x", "X-R1-Role": "internal", SCOPE_HEADER: json.dumps({"regions": [region]}, sort_keys=True, separators=(",", ":"))}


def test_a_scoped_caller_starts_sees_and_drives_only_campaigns_inside_its_scope(client, fleet):
    """A scoped caller cannot name elements outside its scope (403), a selector selects only its own, and it sees and drives only campaigns wholly
    inside its scope.
    """
    with fleet["db"]() as db:
        for ref, region in {"ME-1": "eu", "ME-2": "eu", "ME-3": "us", "ME-4": "us"}.items():
            db.get(ManagedEntity, ref).region = region
        db.commit()
    eu = _scoped("eu")
    outside = client.post("/software-campaigns", headers=eu, json={"requestedBy": "x", "name": "n", "managedElementRefs": ["ME-1", "ME-3"]})
    assert outside.status_code == 403 and outside.json()["detail"]["title"] == "SCOPE_DENIED"
    selected = client.post("/software-campaigns", headers=eu, json={"requestedBy": "x", "name": "n", "selector": {"entityType": "O-DU"}})
    assert selected.status_code == 202                                                   # the selector only ever selects what the caller may touch
    mine = selected.json()["campaignId"]
    theirs = _campaign(client, ["ME-3", "ME-4"])
    assert [c["campaignId"] for c in client.get("/software-campaigns", headers=eu).json()["items"]] == [mine]
    assert {c["campaignId"] for c in client.get("/software-campaigns").json()["items"]} == {mine, theirs}
    assert client.get(f"/software-campaigns/{mine}", headers=eu).status_code == 200
    for path, method in ((f"/software-campaigns/{theirs}", "get"), (f"/software-campaigns/{theirs}/report", "get"), (f"/software-campaigns/{theirs}/halt", "post")):
        resp = getattr(client, method)(path, headers=eu, **({"json": {"requestedBy": "x"}} if method == "post" else {}))
        assert resp.status_code == 404, path
    assert _view(client, theirs)["status"] == "RUNNING"


# ---- concurrency

def test_a_campaign_moved_by_another_request_while_a_job_advances_leaves_the_job_advance_standing(client, fleet):
    """If another request moves the campaign while a job advances, the job's advance stands (200) and the sweep brings the campaign up to date."""
    cid = _campaign(client, ["ME-1"])
    job = _wave_jobs(client, cid, 1)[0]["jobId"]
    _step(client, job)
    _step(client, job)
    with concurrent_commit_on("software_campaign") as fired:
        last = client.post(f"/software-management-jobs/{job}/advance", params={"succeeded": True})
    assert fired and last.status_code == 200 and last.json()["status"] == "COMPLETED"        # the job's own advance is not undone by the campaign's conflict
    assert _view(client, cid)["status"] == "RUNNING"                                         # the campaign has not caught up...
    assert [a["status"] for a in client.post("/software-campaigns/advance-due").json()["advanced"]] == ["COMPLETED"]       # ...the sweep does it


def test_a_job_of_a_campaign_that_is_gone_is_advanced_all_the_same(client, fleet, db_session_factory):
    """A software job whose campaign has been deleted is still advanced."""
    cid = _campaign(client, ["ME-1"])
    job = _wave_jobs(client, cid, 1)[0]["jobId"]
    with db_session_factory() as db:
        db.delete(db.get(SoftwareCampaign, uuid.UUID(cid)))
        db.commit()
    assert _step(client, job)["phase"] == "INSTALL"
