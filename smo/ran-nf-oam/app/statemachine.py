"""Separate lifecycles hosted by RAN NF OAM SMOS, the first three per RAN NF OAM LLD
section 6:
  - WriteConfigJob      (section 3.2's decomposed-PATCH aggregation)
  - SoftwareManagementJob
  - O1AdaptorEndpoint health (section 1.2's new endpoint registry)
  - the onboarding of a newly registered element (MGT-14.5)
  - a software campaign over many elements (MGT-15)

Kept as independent StateMachine instances rather than one shared
FSM — the entities don't share transitions or a common lifecycle shape,
so merging them would just be false economy.
"""

from __future__ import annotations

from enum import StrEnum

from smo_shared.statemachine import StateMachine

# ---------------------------------------------------------------- WriteConfigJob

class JobState(StrEnum):
    """States of a WriteConfigJob. PENDING and PROCESSING lead to a terminal COMPLETED, PARTIAL_SUCCESS or FAILED; HALTED is the pause of a staged job between waves.
    A job asked with a change window or for approval (MGT-4) waits in PENDING_APPROVAL for a person other than its requester; approved before its window it waits
    in SCHEDULED; refused (or withdrawn) it ends REJECTED with nothing sent.
    """
    PENDING = "PENDING"
    PENDING_APPROVAL = "PENDING_APPROVAL"   # MGT-4.2: nothing is sent until someone other than the requester approves
    SCHEDULED = "SCHEDULED"                 # MGT-4.3: approved, waiting for its change window to open
    PROCESSING = "PROCESSING"
    HALTED = "HALTED"                   # MGT-5: a staged job waiting between waves (a pause, a failed gate, an operator's halt)
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    REJECTED = "REJECTED"                   # MGT-4.3: the approval was refused or the request withdrawn; nothing was sent


class JobEvent(StrEnum):
    """Events that move a WriteConfigJob: the pre-check result, the aggregate of the sub-change outcomes, HALT/RESUME between waves, and the change-window approval
    (REQUEST_APPROVAL, APPROVE now, APPROVE_FOR_WINDOW, START at the window, REJECT)."""
    PRECHECK_PASS = "PRECHECK_PASS"     # noqa: S105 — an event name, not a credential; schema validation + MSAC gate both pass
    PRECHECK_FAIL = "PRECHECK_FAIL"
    AGGREGATE_ALL_APPLIED = "AGGREGATE_ALL_APPLIED"
    AGGREGATE_ALL_REJECTED = "AGGREGATE_ALL_REJECTED"
    AGGREGATE_MIXED = "AGGREGATE_MIXED"
    HALT = "HALT"                       # MGT-5.3/5.4: stop between waves
    RESUME = "RESUME"                   # MGT-5.4: go on with the next wave
    REQUEST_APPROVAL = "REQUEST_APPROVAL"   # MGT-4.2: the job was asked with a change window or `requireApproval`
    APPROVE = "APPROVE"                     # MGT-4.3: approved while its window is open (or it has none): it runs now
    APPROVE_FOR_WINDOW = "APPROVE_FOR_WINDOW"   # MGT-4.3: approved before its window opens: it waits
    START = "START"                         # MGT-4.3: a scheduled job's window is open and it is started
    REJECT = "REJECT"                       # MGT-4.3: refused by an approver, or withdrawn


def aggregate_event(sub_change_statuses: list[str]) -> JobEvent:
    """RAN NF OAM LLD section 3.2: aggregate over the decomposed per-attribute
    PATCH outcomes. Each underlying PATCH stays atomic (TS 28.532's own
    all-or-nothing semantics, never violated); PARTIAL_SUCCESS is a
    framework-level aggregation over multiple atomic calls, computed here.
    """
    applied = sum(1 for s in sub_change_statuses if s == "APPLIED")
    rejected = sum(1 for s in sub_change_statuses if s == "REJECTED")
    if applied and rejected:
        return JobEvent.AGGREGATE_MIXED
    if applied and not rejected:
        return JobEvent.AGGREGATE_ALL_APPLIED
    return JobEvent.AGGREGATE_ALL_REJECTED


def build_write_config_job_fsm() -> StateMachine[JobState, JobEvent]:
    """The WriteConfigJob transition table: PENDING goes to PROCESSING or FAILED on the pre-check; PROCESSING and HALTED end in COMPLETED, FAILED or PARTIAL_SUCCESS on the aggregate event, and move between each other on HALT and RESUME.
    MGT-4: PENDING goes to PENDING_APPROVAL when approval is asked; that is approved to PROCESSING (now) or SCHEDULED (before its window), a SCHEDULED job STARTs to PROCESSING,
    and either is REJECTED.
    """
    fsm: StateMachine[JobState, JobEvent] = StateMachine()
    fsm.add(JobState.PENDING, JobEvent.PRECHECK_PASS, JobState.PROCESSING)
    fsm.add(JobState.PENDING, JobEvent.PRECHECK_FAIL, JobState.FAILED)
    # MGT-4.2/4.3: a change window, or a request for approval, holds the job until someone other than the requester decides
    fsm.add(JobState.PENDING, JobEvent.REQUEST_APPROVAL, JobState.PENDING_APPROVAL)
    fsm.add(JobState.PENDING_APPROVAL, JobEvent.APPROVE, JobState.PROCESSING)
    fsm.add(JobState.PENDING_APPROVAL, JobEvent.APPROVE_FOR_WINDOW, JobState.SCHEDULED)
    fsm.add(JobState.PENDING_APPROVAL, JobEvent.REJECT, JobState.REJECTED)
    fsm.add(JobState.SCHEDULED, JobEvent.START, JobState.PROCESSING)
    fsm.add(JobState.SCHEDULED, JobEvent.REJECT, JobState.REJECTED)
    fsm.add(JobState.PROCESSING, JobEvent.AGGREGATE_ALL_APPLIED, JobState.COMPLETED)
    fsm.add(JobState.PROCESSING, JobEvent.AGGREGATE_ALL_REJECTED, JobState.FAILED)
    fsm.add(JobState.PROCESSING, JobEvent.AGGREGATE_MIXED, JobState.PARTIAL_SUCCESS)
    # MGT-5: a staged job stops between waves and either goes on or ends with what it has (an abort, or the end of an automatic revert)
    fsm.add(JobState.PROCESSING, JobEvent.HALT, JobState.HALTED)
    fsm.add(JobState.HALTED, JobEvent.RESUME, JobState.PROCESSING)
    fsm.add(JobState.HALTED, JobEvent.AGGREGATE_ALL_APPLIED, JobState.COMPLETED)
    fsm.add(JobState.HALTED, JobEvent.AGGREGATE_ALL_REJECTED, JobState.FAILED)
    fsm.add(JobState.HALTED, JobEvent.AGGREGATE_MIXED, JobState.PARTIAL_SUCCESS)
    return fsm


WRITE_CONFIG_JOB_FSM = build_write_config_job_fsm()

# ---------------------------------------------------------------- SoftwareManagementJob

class SwmState(StrEnum):
    """Status of a SoftwareManagementJob. The phase (download, install, activate) is a separate field, `SwmPhase`."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SwmPhase(StrEnum):
    """The phase a software job is in, in order: DOWNLOAD, INSTALL, ACTIVATE (the next phase after each OK event is in `PHASE_ORDER`)."""
    DOWNLOAD = "DOWNLOAD"
    INSTALL = "INSTALL"
    ACTIVATE = "ACTIVATE"


class SwmEvent(StrEnum):
    """Events of a SoftwareManagementJob: START, the OK of each phase, and PHASE_FAILED."""
    START = "START"
    DOWNLOAD_OK = "DOWNLOAD_OK"
    INSTALL_OK = "INSTALL_OK"
    ACTIVATE_OK = "ACTIVATE_OK"
    PHASE_FAILED = "PHASE_FAILED"


def build_software_management_fsm() -> StateMachine[SwmState, SwmEvent]:
    """Status transitions only — phase is tracked as separate data on the
    job row (advanced alongside DOWNLOAD_OK/INSTALL_OK), since v1.3's
    SoftwareManagementJob models phase and status as two independent
    fields, not one combined state.
    """
    fsm: StateMachine[SwmState, SwmEvent] = StateMachine()
    fsm.add(SwmState.PENDING, SwmEvent.START, SwmState.IN_PROGRESS)
    fsm.add(SwmState.IN_PROGRESS, SwmEvent.DOWNLOAD_OK, SwmState.IN_PROGRESS)
    fsm.add(SwmState.IN_PROGRESS, SwmEvent.INSTALL_OK, SwmState.IN_PROGRESS)
    fsm.add(SwmState.IN_PROGRESS, SwmEvent.ACTIVATE_OK, SwmState.COMPLETED)
    fsm.add(SwmState.IN_PROGRESS, SwmEvent.PHASE_FAILED, SwmState.FAILED)
    return fsm


SOFTWARE_MANAGEMENT_FSM = build_software_management_fsm()

PHASE_ORDER = {SwmEvent.DOWNLOAD_OK: SwmPhase.INSTALL, SwmEvent.INSTALL_OK: SwmPhase.ACTIVATE}

# ---------------------------------------------------------------- O1AdaptorEndpoint health

class EndpointHealth(StrEnum):
    """Health of an O1 adaptor endpoint, driven by its heartbeats: DISCOVERED, ACTIVE, DEGRADED, UNREACHABLE."""
    DISCOVERED = "DISCOVERED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"


class EndpointEvent(StrEnum):
    """Events of the endpoint health lifecycle: a heartbeat, missed heartbeats, a deregistration, a re-registration."""
    HEARTBEAT = "HEARTBEAT"
    MISSED_HEARTBEATS = "MISSED_HEARTBEATS"
    DEREGISTERED = "DEREGISTERED"
    RE_REGISTERED = "RE_REGISTERED"


def build_endpoint_health_fsm() -> StateMachine[EndpointHealth, EndpointEvent]:
    """The endpoint health lifecycle of RAN NF OAM LLD section 6: DISCOVERED goes ACTIVE on a heartbeat; ACTIVE goes DEGRADED on missed heartbeats and back on a heartbeat; DEGRADED goes UNREACHABLE on deregistration; UNREACHABLE goes back to DISCOVERED on re-registration.
    """
    fsm: StateMachine[EndpointHealth, EndpointEvent] = StateMachine()
    fsm.add(EndpointHealth.DISCOVERED, EndpointEvent.HEARTBEAT, EndpointHealth.ACTIVE)
    fsm.add(EndpointHealth.ACTIVE, EndpointEvent.MISSED_HEARTBEATS, EndpointHealth.DEGRADED)
    fsm.add(EndpointHealth.DEGRADED, EndpointEvent.HEARTBEAT, EndpointHealth.ACTIVE)
    fsm.add(EndpointHealth.DEGRADED, EndpointEvent.DEREGISTERED, EndpointHealth.UNREACHABLE)
    fsm.add(EndpointHealth.UNREACHABLE, EndpointEvent.RE_REGISTERED, EndpointHealth.DISCOVERED)
    return fsm


ENDPOINT_HEALTH_FSM = build_endpoint_health_fsm()

# ---------------------------------------------------------------- Onboarding of an element (MGT-14.5)

class OnboardingState(StrEnum):
    """States of an element's onboarding (MGT-14.5): from registered (DISCOVERED) to ONBOARDED or FAILED, through the template match and the apply.
    """
    DISCOVERED = "DISCOVERED"               # registered, not yet matched against the templates
    NO_TEMPLATE = "NO_TEMPLATE"             # templates exist, none fits this element
    TEMPLATE_SELECTED = "TEMPLATE_SELECTED"
    APPLYING = "APPLYING"                   # the template's config job is being written
    ONBOARDED = "ONBOARDED"
    FAILED = "FAILED"


class OnboardingEvent(StrEnum):
    """Events of the onboarding lifecycle: a template matched or none did, the apply started, and its outcome."""
    TEMPLATE_MATCHED = "TEMPLATE_MATCHED"
    NO_MATCH = "NO_MATCH"
    APPLY = "APPLY"
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"


def build_onboarding_fsm() -> StateMachine[OnboardingState, OnboardingEvent]:
    """The way an element goes from "registered" to "configured". Selecting again (an operator changed the templates) is allowed wherever the element is not
    being written; applying again is allowed from ONBOARDED, FAILED and TEMPLATE_SELECTED."""
    fsm: StateMachine[OnboardingState, OnboardingEvent] = StateMachine()
    S, E = OnboardingState, OnboardingEvent
    for state in (S.DISCOVERED, S.NO_TEMPLATE, S.TEMPLATE_SELECTED, S.ONBOARDED, S.FAILED):
        fsm.add(state, E.TEMPLATE_MATCHED, S.TEMPLATE_SELECTED)
    for state in (S.DISCOVERED, S.NO_TEMPLATE, S.TEMPLATE_SELECTED, S.ONBOARDED, S.FAILED):
        fsm.add(state, E.NO_MATCH, S.NO_TEMPLATE)
    for state in (S.TEMPLATE_SELECTED, S.ONBOARDED, S.FAILED):
        fsm.add(state, E.APPLY, S.APPLYING)
    fsm.add(S.APPLYING, E.APPLIED, S.ONBOARDED)
    fsm.add(S.APPLYING, E.APPLY_FAILED, S.FAILED)
    return fsm


ONBOARDING_FSM = build_onboarding_fsm()

# ---------------------------------------------------------------- Software campaign (MGT-15)

class CampaignState(StrEnum):
    """States of a software campaign (MGT-15): the run (PENDING, RUNNING, HALTED, COMPLETED, ABORTED) and the rollback (ROLLING_BACK, ROLLED_BACK, ROLLBACK_FAILED).
    """
    PENDING = "PENDING"
    RUNNING = "RUNNING"                     # a wave is in progress
    HALTED = "HALTED"                       # between waves: a pause, a failed gate, an operator's halt
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"                     # ended by an operator before the last wave
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class CampaignEvent(StrEnum):
    """Events of a software campaign: start, halt, resume, finish, abort, and the three rollback events."""
    START = "START"
    HALT = "HALT"
    RESUME = "RESUME"
    FINISH = "FINISH"
    ABORT = "ABORT"
    ROLLBACK = "ROLLBACK"
    ROLLBACK_DONE = "ROLLBACK_DONE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


def build_campaign_fsm() -> StateMachine[CampaignState, CampaignEvent]:
    """The software campaign transition table: PENDING starts to RUNNING; RUNNING halts or finishes; HALTED resumes or aborts; a rollback may start from RUNNING, HALTED, COMPLETED, ABORTED or ROLLBACK_FAILED (so a failed rollback can be retried) and ends in ROLLED_BACK or ROLLBACK_FAILED.
    """
    fsm: StateMachine[CampaignState, CampaignEvent] = StateMachine()
    S, E = CampaignState, CampaignEvent
    fsm.add(S.PENDING, E.START, S.RUNNING)
    fsm.add(S.RUNNING, E.HALT, S.HALTED)
    fsm.add(S.RUNNING, E.FINISH, S.COMPLETED)
    fsm.add(S.HALTED, E.RESUME, S.RUNNING)
    fsm.add(S.HALTED, E.ABORT, S.ABORTED)
    for state in (S.RUNNING, S.HALTED, S.COMPLETED, S.ABORTED, S.ROLLBACK_FAILED):
        fsm.add(state, E.ROLLBACK, S.ROLLING_BACK)
    fsm.add(S.ROLLING_BACK, E.ROLLBACK_DONE, S.ROLLED_BACK)
    fsm.add(S.ROLLING_BACK, E.ROLLBACK_FAILED, S.ROLLBACK_FAILED)
    return fsm


CAMPAIGN_FSM = build_campaign_fsm()
