"""The EnergySaving rApp's own state (Wave 10.1). In a real deployment an
rApp keeps this in its own store; this reference build runs one shared
Postgres, so the tables live in migrations/001_init.sql beside the SMO's."""

import datetime
import uuid

from sqlalchemy import DateTime, Float, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class EnergySavingInstance(Base):
    """One rApp instance (rapp-mgmt) bound to this service: the cells it
    manages, its actuator (decision D-2), autonomy mode, model and datasets."""
    __tablename__ = "energy_saving_instance"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    cells: Mapped[list] = mapped_column(JSON, nullable=False)
    actuator: Mapped[str] = mapped_column(String, nullable=False, default="ADMINISTRATIVE_STATE")
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    rmih_id: Mapped[str] = mapped_column(String, nullable=False, default="sa-smos")
    operator_notification_uri: Mapped[str | None] = mapped_column(String)
    data_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_version: Mapped[str | None] = mapped_column(String)
    artifact_version: Mapped[int | None] = mapped_column(Integer)
    model_params: Mapped[dict | None] = mapped_column(JSON)
    lifecycle_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # call flow 22e: how often the worker runs a pass (`evaluateEverySeconds` of the instance configuration; null: only "Evaluate now"), when the last
    # one began, and how it went ({at, executionId, trigger, result OK | ERROR | SKIPPED, error?})
    evaluate_every_seconds: Mapped[int | None] = mapped_column(Integer)
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluation: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EnergySavingCell(Base):
    """A managed cell's rApp-internal state (decision D-3)."""
    __tablename__ = "energy_saving_cell"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="SERVING")  # SERVING / PRE_SLEEP / SLEEP
    o1_value: Mapped[str | None] = mapped_column(String)  # last verified actuator value
    last_unlocked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    override_by: Mapped[str | None] = mapped_column(String)
    override_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    pending_dispatch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # call flow 22d: a service-restoring write RAN NF OAM holds for a person (PR-AI-11)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EnergySavingDecision(Base):
    """The audit trail (W10-23): one row per cell per evaluation, joined to
    the platform's own records by id. Its chain is:
        Prediction → Safety → Decision → (Intent) → Action → Verification
        → Rollback → Final state
    `execution_id` is the evaluation's X-Correlation-ID; every direct DME
    action it causes carries the same id."""
    __tablename__ = "energy_saving_decision"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    cell_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    prb: Mapped[float | None] = mapped_column(Float)
    prediction: Mapped[dict | None] = mapped_column(JSON)
    safety: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String, nullable=False)   # LOCK / UNLOCK / NO_CHANGE
    reason: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[dict | None] = mapped_column(JSON)
    verification: Mapped[dict | None] = mapped_column(JSON)
    rollback: Mapped[dict | None] = mapped_column(JSON)
    final_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
