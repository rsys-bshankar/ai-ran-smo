"""Tests for RAN NF OAM's three lifecycles (RAN NF OAM LLD section 6).
Run with: pytest smo/ran-nf-oam/tests -q
"""

import pytest

from smo_shared.statemachine import IllegalTransition

from app.statemachine import (
    ENDPOINT_HEALTH_FSM,
    SOFTWARE_MANAGEMENT_FSM,
    WRITE_CONFIG_JOB_FSM,
    EndpointEvent,
    EndpointHealth,
    JobEvent,
    JobState,
    SwmEvent,
    SwmState,
    aggregate_event,
)


# ------------------------------------------------------------ WriteConfigJob

def test_write_config_job_all_applied_completes():
    """Every sub-change applied aggregates to COMPLETED."""
    event = aggregate_event(["APPLIED", "APPLIED", "APPLIED"])
    assert event == JobEvent.AGGREGATE_ALL_APPLIED
    assert WRITE_CONFIG_JOB_FSM.fire(JobState.PROCESSING, event) == JobState.COMPLETED


def test_write_config_job_all_rejected_fails():
    """Every sub-change rejected aggregates to FAILED."""
    event = aggregate_event(["REJECTED", "REJECTED"])
    assert event == JobEvent.AGGREGATE_ALL_REJECTED
    assert WRITE_CONFIG_JOB_FSM.fire(JobState.PROCESSING, event) == JobState.FAILED


def test_write_config_job_mixed_is_partial_success():
    """The core finding of RAN NF OAM LLD section 3.2: TS 28.532's PATCH is
    all-or-nothing, but the framework's own aggregation over N decomposed
    atomic PATCH calls can and does produce PARTIAL_SUCCESS.
    """
    event = aggregate_event(["APPLIED", "REJECTED", "APPLIED"])
    assert event == JobEvent.AGGREGATE_MIXED
    assert WRITE_CONFIG_JOB_FSM.fire(JobState.PROCESSING, event) == JobState.PARTIAL_SUCCESS


def test_write_config_job_precheck_failure_never_reaches_processing():
    """A failed pre-check ends the job FAILED, and a FAILED job takes no further event."""
    new_state = WRITE_CONFIG_JOB_FSM.fire(JobState.PENDING, JobEvent.PRECHECK_FAIL)
    assert new_state == JobState.FAILED
    with pytest.raises(IllegalTransition):
        WRITE_CONFIG_JOB_FSM.fire(new_state, JobEvent.AGGREGATE_ALL_APPLIED)


def test_write_config_job_waits_for_approval_and_its_window():
    """MGT-4.2/4.3: a job asked for approval waits in PENDING_APPROVAL; approval runs it now or schedules it, a scheduled job starts, and either is rejected."""
    waiting = WRITE_CONFIG_JOB_FSM.fire(JobState.PENDING, JobEvent.REQUEST_APPROVAL)
    assert waiting == JobState.PENDING_APPROVAL
    assert WRITE_CONFIG_JOB_FSM.fire(waiting, JobEvent.APPROVE) == JobState.PROCESSING
    scheduled = WRITE_CONFIG_JOB_FSM.fire(waiting, JobEvent.APPROVE_FOR_WINDOW)
    assert scheduled == JobState.SCHEDULED and WRITE_CONFIG_JOB_FSM.fire(scheduled, JobEvent.START) == JobState.PROCESSING
    assert WRITE_CONFIG_JOB_FSM.fire(waiting, JobEvent.REJECT) == JobState.REJECTED == WRITE_CONFIG_JOB_FSM.fire(scheduled, JobEvent.REJECT)


# A rejected job is final, and a waiting one cannot be aggregated or halted: nothing has been sent.
def test_a_job_waiting_for_approval_takes_no_dispatch_event():
    """PENDING_APPROVAL takes no aggregate, halt or start event, and REJECTED takes nothing."""
    for event in (JobEvent.AGGREGATE_ALL_APPLIED, JobEvent.HALT, JobEvent.START, JobEvent.PRECHECK_PASS):
        with pytest.raises(IllegalTransition):
            WRITE_CONFIG_JOB_FSM.fire(JobState.PENDING_APPROVAL, event)
    with pytest.raises(IllegalTransition):
        WRITE_CONFIG_JOB_FSM.fire(JobState.REJECTED, JobEvent.APPROVE)


# ------------------------------------------------------------ SoftwareManagementJob

def test_software_management_full_sequence():
    """A software job goes through download, install and activate to COMPLETED."""
    s = SOFTWARE_MANAGEMENT_FSM.fire(SwmState.PENDING, SwmEvent.START)
    assert s == SwmState.IN_PROGRESS
    s = SOFTWARE_MANAGEMENT_FSM.fire(s, SwmEvent.DOWNLOAD_OK)
    s = SOFTWARE_MANAGEMENT_FSM.fire(s, SwmEvent.INSTALL_OK)
    s = SOFTWARE_MANAGEMENT_FSM.fire(s, SwmEvent.ACTIVATE_OK)
    assert s == SwmState.COMPLETED


def test_software_management_phase_failure():
    """A failure in any phase ends a software job FAILED."""
    s = SOFTWARE_MANAGEMENT_FSM.fire(SwmState.PENDING, SwmEvent.START)
    s = SOFTWARE_MANAGEMENT_FSM.fire(s, SwmEvent.PHASE_FAILED)
    assert s == SwmState.FAILED


# ------------------------------------------------------------ O1AdaptorEndpoint health

def test_endpoint_discovered_to_active_on_first_heartbeat():
    """The first heartbeat moves a DISCOVERED endpoint to ACTIVE."""
    s = ENDPOINT_HEALTH_FSM.fire(EndpointHealth.DISCOVERED, EndpointEvent.HEARTBEAT)
    assert s == EndpointHealth.ACTIVE


def test_endpoint_degrades_then_recovers():
    """Missed heartbeats degrade an ACTIVE endpoint, and a heartbeat brings it back to ACTIVE."""
    s = ENDPOINT_HEALTH_FSM.fire(EndpointHealth.ACTIVE, EndpointEvent.MISSED_HEARTBEATS)
    assert s == EndpointHealth.DEGRADED
    s = ENDPOINT_HEALTH_FSM.fire(s, EndpointEvent.HEARTBEAT)
    assert s == EndpointHealth.ACTIVE


def test_endpoint_degrades_then_deregisters_then_rediscovered():
    """RAN NF OAM LLD section 1.2/6: any managed_entity FK-referencing an
    UNREACHABLE endpoint fails writes/reads until re-registration — this
    test only covers the endpoint's own health lifecycle; the
    ENDPOINT_UNREACHABLE error path is exercised at the route layer.
    """
    s = ENDPOINT_HEALTH_FSM.fire(EndpointHealth.ACTIVE, EndpointEvent.MISSED_HEARTBEATS)
    s = ENDPOINT_HEALTH_FSM.fire(s, EndpointEvent.DEREGISTERED)
    assert s == EndpointHealth.UNREACHABLE
    s = ENDPOINT_HEALTH_FSM.fire(s, EndpointEvent.RE_REGISTERED)
    assert s == EndpointHealth.DISCOVERED


def test_endpoint_cannot_skip_active_straight_to_unreachable():
    """DEREGISTERED is only legal from DEGRADED, not from ACTIVE directly —
    matches the FSM's own documented path (section 6 of the LLD).
    """
    with pytest.raises(IllegalTransition):
        ENDPOINT_HEALTH_FSM.fire(EndpointHealth.ACTIVE, EndpointEvent.DEREGISTERED)
