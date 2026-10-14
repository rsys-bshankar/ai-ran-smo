"""The Mobility Optimization rApp's own state (Wave 10.2). A real rApp keeps
this in its own store; this reference build runs one shared Postgres, so the
tables live in migrations/001_init.sql beside the SMO's."""

import datetime
import uuid

from sqlalchemy import DateTime, Float, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class MobilityInstance(Base):
    """One rApp instance (rapp-mgmt) bound to this service. It holds the
    neighbour relations it tunes, the CIO baseline, the DMRO bounds it
    imposes, and its model and datasets."""
    __tablename__ = "mobility_instance"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    relations: Mapped[list] = mapped_column(JSON, nullable=False)          # [{relation, source, target}]
    baseline_cio: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dmro_bounds: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    rmih_id: Mapped[str] = mapped_column(String, nullable=False, default="sa-smos")
    energy_saving_instance_id: Mapped[str | None] = mapped_column(String)  # coordination (D10.2-4c)
    traffic_steering_instance_id: Mapped[str | None] = mapped_column(String)  # CIO arbitration (D10.4-1)
    operator_notification_uri: Mapped[str | None] = mapped_column(String)
    data_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_version: Mapped[str | None] = mapped_column(String)
    artifact_version: Mapped[int | None] = mapped_column(Integer)
    model_params: Mapped[dict | None] = mapped_column(JSON)
    lifecycle_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # call flow 23e: how the DMRO bounds went at deploy or on "Apply DMRO bounds" ({status VERIFIED | VERIFY_FAILED | AWAITING_APPROVAL | REJECTED |
    # APPROVAL_<status>, at, actionId?, approvalId?, observed?, error?}); a CIO change is held until it is VERIFIED
    dmro_status: Mapped[dict | None] = mapped_column(JSON)
    # call flow 23f: how often the worker runs a pass (`evaluateEverySeconds`; null: only "Evaluate now"), when the last one began, and how it went
    evaluate_every_seconds: Mapped[int | None] = mapped_column(Integer)
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluation: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MobilityRelation(Base):
    """A tuned neighbour relation: its current CIO and its last change,
    which is kept until the KPI verification confirms or reverts it."""
    __tablename__ = "mobility_relation"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    relation_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="STEADY")  # STEADY / OBSERVING
    current_cio: Mapped[int | None] = mapped_column(Integer)
    last_change: Mapped[dict | None] = mapped_column(JSON)
    last_changed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    pending_dispatch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # call flow 23d: a revert or rollback RAN NF OAM holds for a person (PR-AI-11)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MobilityDecision(Base):
    """The audit trail, with one row per relation per evaluation:
    Prediction → Safety → Decision → (Intent) → Action → Verification
    → KPI check / revert → Final state."""
    __tablename__ = "mobility_decision"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    relation_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    rate: Mapped[float | None] = mapped_column(Float)
    attempts: Mapped[float | None] = mapped_column(Float)
    prediction: Mapped[dict | None] = mapped_column(JSON)
    safety: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String, nullable=False)   # RAISE_CIO / LOWER_CIO / REVERT_CIO / NO_CHANGE
    reason: Mapped[str] = mapped_column(String, nullable=False)
    from_cio: Mapped[int | None] = mapped_column(Integer)
    to_cio: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    kpi: Mapped[dict | None] = mapped_column(JSON)
    intent: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[dict | None] = mapped_column(JSON)
    verification: Mapped[dict | None] = mapped_column(JSON)
    rollback: Mapped[dict | None] = mapped_column(JSON)
    final_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
