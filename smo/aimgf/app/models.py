"""AIMgF's SQLAlchemy tables: the model and runtime lifecycle row, the training / validation / emulation / inference job tables, governance and audit records,
MLMF subscriptions, feature groups, and the TS 28.105 NRM tables.

What it is: every table AIMgF owns (the list is in `aimgf/README.md` 2.2). A job row doubles as its TS 28.105 request (`TrainingJob` is the
MLTrainingRequest, `ValidationJob` the MLTestingRequest), so there is no parallel bookkeeping table for them; the NRM functions, processes, reports, loading
and update resources that have no job equivalent are tables of their own at the end of the file (`docs/STANDARDS.md` decision D-9, flat resources).

Where it sits: read and written by `main.py` and `nrm.py` only. The schema itself is the Alembic history in `migrations/`; this file must agree with it
(`scripts/check_migration_matches_models.py` compares them against Postgres). The unit tests build their SQLite schema from every `Base` class defined in this
module (`tests/test_main.py`, fixture `db_session_factory`), so a new table is picked up there without a test change.

Owns: AIMgF's tables. Does not own: models, repositories and coordination groups (MLMR), deployments and descriptors (NFO), data jobs (DME), the shared
`idempotency_key` and outbox tables (`smo_shared`). A reference to another module's row is a bare UUID column, not an ORM `ForeignKey`: that module runs in its own
process and its tables are not in this metadata (flushing would raise NoReferencedTableError). Migration 0022 also dropped the database-level foreign keys across
module boundaries, so nothing but the owning module's API (called through R1) checks that such an id exists.

Before editing: a column change is a schema revision, not just an edit here (`smo/CLAUDE.md`, "Schema changes are revisions"). Arrays and JSON use a SQLite
`with_variant` so the unit tests run without Postgres. A `DateTime(timezone=True)` read back from SQLite is naive, so elapsed-time maths on `started_at` goes
through `main._aware`.
"""

import datetime
import uuid

from sqlalchemy import ARRAY, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base
from smo_shared.versioning import Versioned


class ModelLifecycle(Versioned, Base):
    """AIMgF's lifecycle state for one model: where it is on the certification path and in its serving runtime. One row per model, keyed by the MLMR model id.

    The row is created lazily by `main._get_or_create_lifecycle` the first time AIMgF is asked about a model (MLMR has no hook into AIMgF), at REGISTERED /
    NOT_DEPLOYED. `model_lifecycle_state` and `runtime_lifecycle_state` are the two independent FSMs of `statemachine.py`. `training_job_id` is the model's current
    training run, which is how a cancel tells whether the run it ends is still the model's current one. `cleared_node_groups` is written by MLLF through
    `PATCH /models/{id}/runtime/node-groups`. `nf_deployment_descriptor_id` and `nf_deployment_id` are the handles onto the serving runtime NFO created, and
    `runtime_profile` is the INFERENCE profile it was sized with. `training_approved` and `validation_approved` are the operator gate flags set by the
    APPROVE_TRAINING and APPROVE_VALIDATION events and reset by every CREATE_TRAINING (HISTORY.md OI-6.1).

    `Versioned` adds `row_version`: every UPDATE is conditional on the version it loaded, so two replicas that move the same model race into a 409
    `CONCURRENT_MODIFICATION` instead of both winning (PR-ST-2).
    """
    __tablename__ = "model_lifecycle"

    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # -> aiml_model (MLMR)
    model_lifecycle_state: Mapped[str] = mapped_column(String, nullable=False, default="REGISTERED")
    runtime_lifecycle_state: Mapped[str] = mapped_column(String, nullable=False, default="NOT_DEPLOYED")
    training_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cleared_node_groups: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    nf_deployment_descriptor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # -> nf_deployment_descriptor (NFO)
    nf_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # -> nf_deployment (NFO)
    # HISTORY.md OI-6.1: operator gate on Training->Validation->
    # Emulation. Reset to False whenever CREATE_TRAINING fires (a fresh
    # training run re-requires approval) — a stale approval from a prior
    # cycle should never silently carry forward into a new one.
    training_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Wave 7 (W7-03): the INFERENCE runtime profile the serving runtime was
    # deployed with.
    runtime_profile: Mapped[dict | None] = mapped_column(JSON)


class ValidationJob(Base):
    """One validation (TS 28.105 MLTestingRequest) run. The row is the request: `GET /ml-testing-requests/{id}` renders it.

    Exactly one of `model_id` and `model_coordination_group_id` is set (CHECK `validation_exactly_one_target`, same shape as `training_job`); a group-targeted run
    tests the group as a unit and drives no single model's lifecycle. `status` is RUNNING, SUSPENDED, COMPLETED, FAILED or CANCELLED. `started_at` plus
    `timeout_seconds` is the deadline `_expire_overdue_jobs` enforces (SUSPENDED runs never expire; resume restarts the clock). The `nf_deployment_*` pair is the
    transient NFO execution runtime, set when the run starts and cleared once it is torn down.
    """
    __tablename__ = "validation_job"

    __table_args__ = (
        CheckConstraint(
            "(model_id IS NOT NULL AND model_coordination_group_id IS NULL) "
            "OR (model_id IS NULL AND model_coordination_group_id IS NOT NULL)",
            name="validation_exactly_one_target",
        ),
    )

    validation_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Wave 4 (TS 28.105 MLTestingRequest.mLModelCoordinationGroupRef):
    # exactly one of model_id/model_coordination_group_id, the same
    # exactly_one_target shape TrainingJob already has — and the same
    # asymmetry: a group-targeted run drives no single model's lifecycle.
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_coordination_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    training_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("training_job.training_job_id"))
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    validation_criteria: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    metrics: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md OI-6.5: same additive pair TrainingJob gained —
    # who to best-effort notify on completion (set at request time, the
    # requester's own callback), and where the validated artifact lives
    # (a DME DmeTypeId, set on completion).
    notification_uri: Mapped[str | None] = mapped_column(String)
    outcome_artifact_dme_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Wave 4 — TS 28.105 MLTestingRequest: the containing MLTestingFunction
    # (optional) and the spec's own cancelRequest/suspendRequest flags.
    ml_testing_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_testing_function.ml_testing_function_id", ondelete="SET NULL"))
    cancel_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Wave 7 (W7-03/W7-04): the compute the run was sized with (from the
    # rApp package's runtimeProfiles, or an explicit override), and its
    # execution timeout — `started_at` + `timeout_seconds` is the deadline
    # `_expire_overdue_jobs` enforces.
    runtime_profile: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    # HISTORY.md OI-6.2: a real NFO-backed execution runtime for
    # this validation run — same bare-UUID cross-module-reference shape as
    # ModelLifecycle's own nf_deployment_descriptor_id/nf_deployment_id
    # (NFO runs in its own process; referential integrity enforced at the
    # DB level by migrations/001_init.sql's own FK on the descriptor
    # column). Set on request, cleared on completion once NFO tears the
    # transient deployment down.
    nf_deployment_descriptor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    nf_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class EmulationJob(Base):
    """One emulation run for a model (the MLEF stage). Always model-targeted.

    `status` is RUNNING, COMPLETED or FAILED. Same deadline and NFO execution-runtime columns as `ValidationJob`. `aiml_inference_emulation_function_id` is the
    optional AIMLInferenceEmulationFunction hosting the run; a successful completion writes an AIMLInferenceReport under it.
    """
    __tablename__ = "emulation_job"

    emulation_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    emulation_criteria: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    metrics: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md OI-6.5: same pair as ValidationJob's own.
    notification_uri: Mapped[str | None] = mapped_column(String)
    outcome_artifact_dme_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Wave 4 — TS 28.105 AIMLInferenceEmulationFunction that hosts this run
    # (optional); completion writes an AIMLInferenceReport under it.
    aiml_inference_emulation_function_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("aiml_inference_emulation_function.aiml_inference_emulation_function_id", ondelete="SET NULL"))
    # Wave 7 (W7-03/W7-04): the compute the run was sized with (from the
    # rApp package's runtimeProfiles, or an explicit override), and its
    # execution timeout — `started_at` + `timeout_seconds` is the deadline
    # `_expire_overdue_jobs` enforces.
    runtime_profile: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    # HISTORY.md OI-6.2: same pair as ValidationJob's own.
    nf_deployment_descriptor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    nf_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class CertificationRecord(Base):
    """The audit record of one governance decision on a model: the event (`decision`), who decided (`decided_by`, required) and an optional rationale.

    Written by `main._fire_model_event` for every event in `statemachine.GOVERNANCE_EVENTS`, in the same transaction as the lifecycle change, and read by
    `GET /models/{id}/governance-history`. DEPRECATE and RETIRE are not governance events and leave no record.
    """
    __tablename__ = "certification_record"

    certification_record_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class LifecycleTransition(Base):
    """The audit trail of every ModelLifecycle and RuntimeLifecycle transition (`fsm` is MODEL or RUNTIME), so "how did this model get here" is a query.

    Written by `_fire_model_event` and `_fire_runtime_event`, in the transaction of the change; read by `GET /models/{id}/lifecycle-history`. Self-loop events
    (APPROVE_TRAINING, APPROVE_VALIDATION) are recorded too, with `from_state == to_state`.
    """
    __tablename__ = "lifecycle_transition"

    lifecycle_transition_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    fsm: Mapped[str] = mapped_column(String, nullable=False)  # "MODEL" or "RUNTIME"
    from_state: Mapped[str] = mapped_column(String, nullable=False)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class TrainingJob(Base):
    """One training run. The row is the TS 28.105 MLTrainingRequest: `GET /ml-training-requests/{id}` renders it, so the spec's attributes live here rather than on a
    parallel table.

    Exactly one of `model_id` and `model_coordination_group_id` is set (CHECK `exactly_one_target`); a group-targeted run drives no single model's lifecycle.
    Both are bare UUIDs into MLMR, not foreign keys (see the module description). `status` is NOT_STARTED, IN_PROGRESS, SUSPENDED, FINISHED, FAILED or CANCELLED:
    TS 28.105's `requestStatus` values plus FAILED; CANCELLING is never produced because cancellation is synchronous. `current_step` is the furthest of
    `TRAINING_STEPS` the runtime has reported; each step's own status is derived from it and `status` (`main._training_steps`). `started_at` plus `timeout_seconds`
    is the deadline `_expire_overdue_jobs` enforces. `ml_update_process_id` is set when an MLUpdateProcess started the run (FINE_TUNING), so its completion advances
    that process. Complex spec datatypes are stored as spec-shaped JSON (`ts28105.dump`).
    """
    __tablename__ = "training_job"
    __table_args__ = (
        # Portable boolean form — "::int" cast syntax is Postgres-only and
        # fails on SQLite (caught by sa-smos/tests, which shares this same
        # exactly-one-of-two-targets shape).
        CheckConstraint(
            "(model_id IS NOT NULL AND model_coordination_group_id IS NULL) "
            "OR (model_id IS NULL AND model_coordination_group_id IS NOT NULL)",
            name="exactly_one_target",
        ),
    )

    training_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_coordination_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    producer_type: Mapped[str] = mapped_column(String, nullable=False, default="rApp")
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    required_data: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md OI-6.4: `requiredData` itself stays the opaque
    # blob it always was — this is a separate, optional, explicitly-typed
    # reference to the real DME DataJob(s) training actually consumed,
    # the same "additive, not replacing the existing field" shape DME's
    # own sourceDomain/sourceContext already used for the analogous
    # producer-side declaration. Validated against DME on request (see
    # `_validate_dme_data_job_ids` in main.py); empty/omitted skips the
    # check, matching every other optional cross-reference in this build.
    dme_data_job_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False, default=list)
    validation_criteria: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md §7's `requestStatus` vocabulary finding, closed: renamed
    # to TS28.105's own real 6-value enum (NOT_STARTED/IN_PROGRESS/
    # SUSPENDED/FINISHED/CANCELLED/CANCELLING) — SUSPENDED/CANCELLED
    # already matched (SUSPENDED adopted in an earlier pass). FAILED is
    # this build's own honest addition beyond the spec (a training job
    # that genuinely fails needs a distinct terminal state the spec
    # doesn't model); CANCELLING is never produced — this build has no
    # asynchronous in-flight-cancellation step, the same "spec value
    # this build's own design never reaches" honesty as
    # PRE_SPECIALISED_TRAINING/FINE_TUNING on `ml_training_type`.
    status: Mapped[str] = mapped_column(String, nullable=False, default="NOT_STARTED")
    notification_uri: Mapped[str | None] = mapped_column(String)
    # NEW section 5: the reference's own TrainingJob (trainingmgr/models/trainingjob.py)
    # carries these too — run_id, distinct training/validation dataset
    # references, separate consumer/producer rApp ids, and a
    # model_metrics writeback target. Its real two-axis (step x status)
    # tracking (steps_state/TrainingJobStatus) is deliberately NOT
    # adopted here — replacing this build's existing flat `status`
    # field with a step state machine is a bigger, riskier rework of
    # already-shipped behavior, not a purely additive field; left for a
    # future pass.
    run_id: Mapped[str | None] = mapped_column(String)
    training_dataset: Mapped[str | None] = mapped_column(String)
    validation_dataset: Mapped[str | None] = mapped_column(String)
    consumer_rapp_id: Mapped[str | None] = mapped_column(String)
    producer_rapp_id: Mapped[str | None] = mapped_column(String)
    model_metrics: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md OI-6.5: where the training run's real output
    # artifact lives — a DME DmeTypeId reference, the same "route it
    # through DME" shape MLModel's own outputDataType already uses for an
    # inference result, rather than inventing a second data-plane path.
    # Set on completion (POST .../complete), not at request time — the
    # artifact doesn't exist until training actually produces one.
    outcome_artifact_dme_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # HISTORY.md §7: TS28.105 AI/ML NRM's own real, closed 4-value
    # mLTrainingType enum on both MLModel and MLTrainingRequest —
    # request_training already computes this exact INITIAL_TRAINING-vs-
    # RE_TRAINING distinction internally (as a ModelEvent.TRAIN/RETRAIN
    # FSM choice) but never stored or returned it anywhere.
    # PRE_SPECIALISED_TRAINING/FINE_TUNING have no equivalent concept in
    # this build, so only two of the spec's four values are ever
    # produced here — an honest partial mapping, not a fabricated one.
    ml_training_type: Mapped[str | None] = mapped_column(String)
    # HISTORY.md OI-6.2: a real NFO-backed execution runtime for
    # this training run — same pair as ValidationJob/EmulationJob's own.
    nf_deployment_descriptor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    nf_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Wave 4 — TS 28.105 MLTrainingRequest: a TrainingJob *is* the spec's
    # MLTrainingRequest (`GET /ml-training-requests/{id}` is this row), so
    # every remaining spec attribute lives here rather than on a parallel
    # table that would have to be kept in sync with it. Complex datatypes
    # (FLRequirement, RLRequirement, ModelPerformance[], ...) are stored as
    # their spec-shaped JSON, validated on the way in (app/ts28105.py).
    ml_training_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_training_function.ml_training_function_id", ondelete="SET NULL"))
    aiml_inference_name: Mapped[str | None] = mapped_column(String)
    fl_requirement: Mapped[dict | None] = mapped_column(JSON)
    candidate_training_data_source: Mapped[list | None] = mapped_column(JSON)
    training_data_quality_score: Mapped[float | None] = mapped_column(Float)
    training_request_source: Mapped[str | None] = mapped_column(String)
    performance_requirements: Mapped[list | None] = mapped_column(JSON)
    rl_requirement: Mapped[dict | None] = mapped_column(JSON)
    cancel_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    training_data_statistical_properties: Mapped[dict | None] = mapped_column(JSON)
    distributed_training_expectation: Mapped[dict | None] = mapped_column(JSON)
    ml_knowledge_name: Mapped[str | None] = mapped_column(String)
    expected_inference_scope: Mapped[list | None] = mapped_column(JSON)
    clustering_info: Mapped[list | None] = mapped_column(JSON)
    # Set when an MLUpdateProcess started this run (FINE_TUNING) — its
    # completion advances that process.
    ml_update_process_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_update_process.ml_update_process_id", ondelete="SET NULL"))
    # Wave 7 (W7-03/W7-04): the compute the run was sized with (from the
    # rApp package's runtimeProfiles, or an explicit override), and its
    # execution timeout — `started_at` + `timeout_seconds` is the deadline
    # `_expire_overdue_jobs` enforces.
    runtime_profile: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    # OI-5-aiml-trainingjob-steps: the furthest step of the run (TRAINING_STEPS,
    # in order) its execution runtime has reported reaching; a run starts in
    # DATA_EXTRACTION. Each step's own status is derived from this and the
    # job's `status` (main.py's `_training_steps`), so the job-level status
    # stays the single source of truth for how the run ended.
    current_step: Mapped[str] = mapped_column(String, nullable=False, default="DATA_EXTRACTION")
    # GUI-9.8 (revision 0037): the epoch the run has reached and how many it will run, as its runtime last reported them (`POST .../progress`, or an
    # `epoch`/`totalEpochs` pair in the metrics writeback), and when. Null until reported; `main._eta_seconds` derives the ETA from them and `started_at`.
    epoch: Mapped[int | None] = mapped_column(Integer)
    total_epochs: Mapped[int | None] = mapped_column(Integer)
    progress_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


# OI-5-aiml-trainingjob-steps: the reference Training Manager's steps
# (trainingmgr's Steps), in the order a run passes through them.
TRAINING_STEPS = ("DATA_EXTRACTION", "TRAINING", "TRAINED_MODEL")


class InferenceJob(Base):
    """One inference request against a model's serving runtime (MLIF).

    Unlike training, validation and emulation, an inference job creates no NFO deployment: `nf_deployment_id` is a read-only copy of the model's live serving
    deployment (`ModelLifecycle.nf_deployment_id`) taken when the job is requested, which is only possible while the runtime is ACTIVE. `status` is RUNNING,
    COMPLETED or FAILED (the FSM is `INFERENCE_JOB_FSM`); the default deadline is five seconds. `consumer_ref` feeds the function's `usedByFunctionRefList`.
    """
    __tablename__ = "inference_job"

    inference_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # bare UUID — see TrainingJob's docstring
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    # HISTORY.md OI-6.2: unlike TrainingJob/ValidationJob/
    # EmulationJob (each a transient batch run that gets its own fresh
    # NFO descriptor+deployment, torn down on completion), an
    # InferenceJob doesn't create a new NFO deployment of its own —
    # request_inference is already gated on RuntimeLifecycleState.ACTIVE,
    # which means ModelLifecycle.nf_deployment_id (deploy_model_runtime's
    # own real NFO call) is already a live serving deployment. Creating a
    # second, parallel one per inference call would duplicate that
    # runtime rather than use it. This is a read-only reference, stamped
    # from the lifecycle at request time — MLIF's own "which NFO
    # deployment actually served this inference" becomes a real,
    # queryable fact instead of unlinked.
    nf_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    notification_destination: Mapped[str | None] = mapped_column(String)
    # Wave 4 — the TS 28.105 AIMLInferenceFunction this inference ran on
    # (optional), and who consumed it (feeds usedByFunctionRefList).
    aiml_inference_function_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("aiml_inference_function.aiml_inference_function_id", ondelete="SET NULL"))
    consumer_ref: Mapped[str | None] = mapped_column(String)
    # Wave 7 (W7-04): inference deadline (default 5 s).
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)


class MLMFSubscription(Base):
    """A model-performance subscription (MLMF): which metrics of a model to watch, the DME type they come from, and the floor below which a report counts as a breach
    (`guard_kpi_floor`, metric name to minimum).

    `notification_destination`, when set, receives every report through the outbox. Reports are `PerformanceReport` rows; deleting a subscription cascades to them.
    """
    __tablename__ = "mlmf_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # bare UUID — see TrainingJob's docstring
    metric_types: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    dme_type_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    guard_kpi_floor: Mapped[dict | None] = mapped_column(JSON)
    # HISTORY.md §7's `MLMFSubscription` finding, closed: every other
    # subscription-shaped resource in this build (DME/MDAF/
    # Intent Service) notifies a real notification_destination and can
    # be torn down with a real DELETE — this one could previously only
    # be created and read.
    notification_destination: Mapped[str | None] = mapped_column(String)


class PerformanceReport(Base):
    """One metrics report against an `MLMFSubscription`; `breached_floor` is True when any metric the subscription guards is below its floor.

    Rows are removed with their subscription (`ON DELETE CASCADE`). Reads list them newest first.
    """
    __tablename__ = "performance_report"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("mlmf_subscription.subscription_id", ondelete="CASCADE"))
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    breached_floor: Mapped[bool] = mapped_column(default=False)
    reported_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class FeatureGroup(Base):
    """A registered feature group: where a set of features lives in the data lake (host, bucket, measurement and the access token) and, when `enable_dme` is set, the
    DME data job created for it.

    `feature_group_name` is unique (the API rule is 3 to 63 word characters). `dme_type_id` and `dme_data_job_id` are bare UUIDs into DME, set only for an
    `enable_dme` group; the job is created before the group is stored and terminated, best effort, when the group is deleted (HISTORY.md OI-5-aiml-featuregroup-dme).
    The data-lake credential is either `token_ref`, the name of a secret the service resolves from its own environment when it connects (the database holds the name, never
    the value), or the deprecated clear-text `token` that older callers still send (stored as given, never returned by a route; SEC-15.2, `aimgf/README.md` 2.8). A group has
    one of the two; `token` is nullable because a group registered with a reference has none.
    The group is AIMgF's rather than MLMR's, MLLF's or DME's because no ownership record placed it elsewhere; the real feature storage behind the reference's
    feature store is not built (HISTORY.md section 5).
    """

    __tablename__ = "feature_group"

    feature_group_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    feature_group_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    feature_list: Mapped[str] = mapped_column(String, nullable=False)
    datalake_source: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[str] = mapped_column(String, nullable=False)
    bucket: Mapped[str] = mapped_column(String, nullable=False)
    token: Mapped[str | None] = mapped_column(String)
    token_ref: Mapped[str | None] = mapped_column(String)
    db_org: Mapped[str] = mapped_column(String, nullable=False)
    measurement: Mapped[str] = mapped_column(String, nullable=False)
    enable_dme: Mapped[bool] = mapped_column(nullable=False, default=False)
    measured_obj_class: Mapped[str | None] = mapped_column(String)
    dme_port: Mapped[str | None] = mapped_column(String)
    source_name: Mapped[str | None] = mapped_column(String)
    # OI-5-aiml-featuregroup-dme: the DME type the group's data job collects,
    # and the job itself (bare cross-module refs, DME owns both).
    dme_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dme_data_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


# ---------------------------------------------------------------- Wave 4: TS 28.105 AI/ML NRM IOCs
#
# Every IOC of TS28105_AiMlNrm.yaml at REST level (docs/STANDARDS.md
# decision D-9): flat REST resources with the spec's own attribute names and
# enums, no DN containment tree (the one recorded deviation — addressing).
# Requests are backed by the real job aggregates (MLTrainingRequest =
# TrainingJob, MLTestingRequest = ValidationJob); everything else that had
# no equivalent is a new table here. MLModel/MLModelRepository/
# MLModelCoordinationGroup are MLMR's (mlmr/app/models.py).

def _now():
    """Returns the current UTC time; the default factory of the `created_at` columns of the NRM tables."""
    return datetime.datetime.now(datetime.UTC)


class MLTrainingFunction(Base):
    """TS 28.105 MLTrainingFunction: a training capability that MLTrainingRequests can name. Created and edited directly through the NRM routes.

    `ml_training_type` is read-only in the spec: it holds the type of the most recent run started for this function (stamped by `_start_training`).
    `ml_model_repository_ref` is a bare UUID into MLMR. The learning-technology, FL and knowledge attributes are stored and returned; nothing acts on them.
    """
    __tablename__ = "ml_training_function"

    ml_training_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    supported_learning_technology: Mapped[dict | None] = mapped_column(JSON)
    fl_participation_info: Mapped[dict | None] = mapped_column(JSON)
    ml_knowledge: Mapped[dict | None] = mapped_column(JSON)
    # readOnly in the spec: the mLTrainingType of the most recent training
    # this function ran, stamped by request_training.
    ml_training_type: Mapped[str | None] = mapped_column(String)
    ml_model_repository_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # -> ml_model_repository (MLMR)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLTrainingProcess(Base):
    """TS 28.105 MLTrainingProcess: the running side of a training request, one per `TrainingJob`, created by `_start_training` so the request/process split exists
    for every run, not only for runs started through the NRM routes.

    `status` and the cancel and suspend flags are kept in step with the job by `_sync_training_process`; `progress_*` is the execution runtime's ProcessMonitor
    write-back. Removed with its job (`ON DELETE CASCADE`).
    """
    __tablename__ = "ml_training_process"

    ml_training_process_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    training_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("training_job.training_job_id", ondelete="CASCADE"), nullable=False, unique=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    termination_conditions: Mapped[str | None] = mapped_column(String)
    # ProcessMonitor
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    progress_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_state_info: Mapped[str | None] = mapped_column(String)
    result_state_info: Mapped[str | None] = mapped_column(String)
    cancel_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    participating_fl_client_refs: Mapped[list | None] = mapped_column(JSON)


class MLTrainingReport(Base):
    """TS 28.105 MLTrainingReport, written on every training completion, successful or not.

    `last_training_report_id` chains to the previous report of the same target (`_write_training_report`). `ml_model_generated_ref` and
    `ml_model_coordination_group_generated_ref` are set only when the run succeeded. Removed with its job (`ON DELETE CASCADE`).
    """
    __tablename__ = "ml_training_report"

    ml_training_report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    training_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("training_job.training_job_id", ondelete="CASCADE"), nullable=False)
    ml_training_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_training_function.ml_training_function_id", ondelete="SET NULL"))
    used_consumer_training_data: Mapped[list | None] = mapped_column(JSON)
    model_confidence_indication: Mapped[int | None] = mapped_column(Integer)
    model_performance_training: Mapped[list | None] = mapped_column(JSON)
    model_performance_validation: Mapped[list | None] = mapped_column(JSON)
    data_ratio_training_and_validation: Mapped[int | None] = mapped_column(Integer)
    are_new_training_data_used: Mapped[bool | None] = mapped_column(Boolean)
    fl_report_per_client: Mapped[list | None] = mapped_column(JSON)
    last_training_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ml_model_generated_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ml_model_coordination_group_generated_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLTestingFunction(Base):
    """TS 28.105 MLTestingFunction: a testing capability that MLTestingRequests can name. Created and deleted directly; deleting it sets the requests' reference to NULL."""
    __tablename__ = "ml_testing_function"

    ml_testing_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLTestingReport(Base):
    """TS 28.105 MLTestingReport, written when a validation run completes or times out. `ml_testing_result` is PASSED or FAILED.

    Removed with its validation job (`ON DELETE CASCADE`).
    """
    __tablename__ = "ml_testing_report"

    ml_testing_report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    validation_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("validation_job.validation_job_id", ondelete="CASCADE"), nullable=False)
    ml_testing_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_testing_function.ml_testing_function_id", ondelete="SET NULL"))
    model_performance_testing: Mapped[list | None] = mapped_column(JSON)
    ml_testing_result: Mapped[str] = mapped_column(String, nullable=False)  # PASSED | FAILED
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class AIMLInferenceFunction(Base):
    """TS 28.105 AIMLInferenceFunction: an inference capability that models are loaded onto and inference jobs can name.

    `activation_status` (ACTIVATED or DEACTIVATED, default DEACTIVATED) gates inference jobs that name the function. `ml_model_refs` is read-only in the spec: the ids
    of the models loaded onto it by an MLModelLoadingProcess, as strings.
    """
    __tablename__ = "aiml_inference_function"

    aiml_inference_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    aiml_inference_name: Mapped[str | None] = mapped_column(String)
    activation_status: Mapped[str] = mapped_column(String, nullable=False, default="DEACTIVATED")  # ACTIVATED | DEACTIVATED
    managed_activation_scope: Mapped[dict | None] = mapped_column(JSON)
    # readOnly in the spec: models loaded onto this function by an
    # MLModelLoadingProcess.
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class AIMLInferenceEmulationFunction(Base):
    """TS 28.105 AIMLInferenceEmulationFunction: the function that hosts emulation runs and owns the AIMLInferenceReports they produce."""
    __tablename__ = "aiml_inference_emulation_function"

    aiml_inference_emulation_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class AIMLInferenceReport(Base):
    """TS 28.105 AIMLInferenceReport: the result of one inference or emulation run, or a report posted directly.

    Belongs to an inference function or an emulation function (the API requires exactly one reference); the foreign keys cascade on delete. `inference_job_id` and
    `emulation_job_id` are set when a job produced the report and become NULL if the job row goes. `ml_model_refs` holds model ids as strings.
    """
    __tablename__ = "aiml_inference_report"

    aiml_inference_report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    aiml_inference_function_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("aiml_inference_function.aiml_inference_function_id", ondelete="CASCADE"))
    aiml_inference_emulation_function_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("aiml_inference_emulation_function.aiml_inference_emulation_function_id", ondelete="CASCADE"))
    inference_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("inference_job.inference_job_id", ondelete="SET NULL"))
    emulation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("emulation_job.emulation_job_id", ondelete="SET NULL"))
    inference_outputs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    potential_impact_info: Mapped[dict | None] = mapped_column(JSON)
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLModelLoadingPolicy(Base):
    """TS 28.105 MLModelLoadingPolicy: the models an AIMLInferenceFunction should load when the policy is triggered (`POST .../trigger`). `policy_for_loading` is stored, not evaluated."""
    __tablename__ = "ml_model_loading_policy"

    ml_model_loading_policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    aiml_inference_function_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("aiml_inference_function.aiml_inference_function_id", ondelete="CASCADE"), nullable=False)
    aiml_inference_name: Mapped[str | None] = mapped_column(String)
    policy_for_loading: Mapped[dict | None] = mapped_column(JSON)  # AIMLManagementPolicy
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class MLModelLoadingRequest(Base):
    """TS 28.105 MLModelLoadingRequest: a request to load models onto an AIMLInferenceFunction. It runs synchronously when created, unless created suspended or cancelled.

    `request_status` is NOT_STARTED, IN_PROGRESS, SUSPENDED, FINISHED or CANCELLED. `ml_model_to_load_refs` holds model ids as strings.
    """
    __tablename__ = "ml_model_loading_request"

    ml_model_loading_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    aiml_inference_function_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("aiml_inference_function.aiml_inference_function_id", ondelete="CASCADE"), nullable=False)
    request_status: Mapped[str] = mapped_column(String, nullable=False, default="NOT_STARTED")
    cancel_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ml_model_to_load_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLModelLoadingProcess(Base):
    """TS 28.105 MLModelLoadingProcess: the record of one loading run, created by a loading request or a policy trigger.

    `loaded_ml_model_refs` grows as each model is brought up; `loading_request_refs` and `loading_policy_refs` say what started it.
    """
    __tablename__ = "ml_model_loading_process"

    ml_model_loading_process_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    aiml_inference_function_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("aiml_inference_function.aiml_inference_function_id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    progress_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_state_info: Mapped[str | None] = mapped_column(String)
    result_state_info: Mapped[str | None] = mapped_column(String)
    cancel_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    loading_request_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    loading_policy_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    loaded_ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class MLUpdateFunction(Base):
    """TS 28.105 MLUpdateFunction: the update capability that MLUpdateRequests can name. Its capability report and model list are refreshed when an update finishes."""
    __tablename__ = "ml_update_function"

    ml_update_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    avail_ml_capability_report: Mapped[dict | None] = mapped_column(JSON)
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLUpdateRequest(Base):
    """TS 28.105 MLUpdateRequest: a request to update the models in `ml_model_refs`. Realised as FINE_TUNING training of each model (see `nrm.create_ml_update_request`).

    `request_status` is IN_PROGRESS from creation, SUSPENDED while suspended, FINISHED once every run is terminal (whether or not all succeeded) and CANCELLED if cancelled.
    """
    __tablename__ = "ml_update_request"

    ml_update_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ml_update_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("ml_update_function.ml_update_function_id", ondelete="SET NULL"))
    performance_gain_threshold: Mapped[list | None] = mapped_column(JSON)
    new_capability_version_ids: Mapped[list | None] = mapped_column(JSON)
    update_time_deadline: Mapped[dict | None] = mapped_column(JSON)
    request_status: Mapped[str] = mapped_column(String, nullable=False, default="NOT_STARTED")
    ml_update_reporting_period: Mapped[dict | None] = mapped_column(JSON)
    cancel_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)


class MLUpdateProcess(Base):
    """TS 28.105 MLUpdateProcess: tracks the training runs of one MLUpdateRequest (`TrainingJob.ml_update_process_id` points here).

    `nrm.advance_ml_update_process` recomputes `progress_percentage` as runs finish and sets `status` to FINISHED only if every run succeeded, otherwise FAILED.
    Removed with its request (`ON DELETE CASCADE`).
    """
    __tablename__ = "ml_update_process"

    ml_update_process_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ml_update_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ml_update_request.ml_update_request_id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="RUNNING")
    progress_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_state_info: Mapped[str | None] = mapped_column(String)
    result_state_info: Mapped[str | None] = mapped_column(String)
    cancel_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False)


class MLUpdateReport(Base):
    """TS 28.105 MLUpdateReport, written once when every run of an update process is terminal; `ml_model_refs` lists the models whose run succeeded. Removed with its process (`ON DELETE CASCADE`)."""
    __tablename__ = "ml_update_report"

    ml_update_report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ml_update_process_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ml_update_process.ml_update_process_id", ondelete="CASCADE"), nullable=False)
    updated_ml_capability: Mapped[dict | None] = mapped_column(JSON)  # AvailMLCapabilityReport
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(default=_now)
