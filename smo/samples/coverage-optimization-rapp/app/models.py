"""The Coverage Optimization rApp's own state (Wave 10.3). A real rApp keeps
this in its own store; this reference build runs one shared Postgres, so the
tables live in migrations/001_init.sql beside the SMO's."""

import datetime
import uuid

from sqlalchemy import DateTime, Float, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class CoverageInstance(Base):
    """One rApp instance (rapp-mgmt) bound to this service. It holds the
    cluster's cells, the tilt/power baselines, the change set under KPI
    observation, an ASSIST dispatch awaiting the operator, and its model and
    datasets."""
    __tablename__ = "coverage_instance"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    cells: Mapped[list] = mapped_column(JSON, nullable=False)            # [cellId]
    baseline_tilt: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    baseline_power: Mapped[int] = mapped_column(Integer, nullable=False, default=43)
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    rmih_id: Mapped[str] = mapped_column(String, nullable=False, default="sa-smos")
    energy_saving_instance_id: Mapped[str | None] = mapped_column(String)   # coordination (D10.3-4c)
    mobility_instance_id: Mapped[str | None] = mapped_column(String)
    operator_notification_uri: Mapped[str | None] = mapped_column(String)
    observing: Mapped[dict | None] = mapped_column(JSON)          # the change set under KPI verification
    pending_dispatch: Mapped[dict | None] = mapped_column(JSON)   # {dispatchId, decisionIds: {cell: id}}
    evaluate_every_seconds: Mapped[int | None] = mapped_column(Integer)   # the worker's scheduled passes (call flow 24f); None: manual only
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluation: Mapped[dict | None] = mapped_column(JSON)            # {at, executionId, trigger, result}
    data_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_version: Mapped[str | None] = mapped_column(String)
    artifact_version: Mapped[int | None] = mapped_column(Integer)
    model_params: Mapped[dict | None] = mapped_column(JSON)
    lifecycle_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CoverageCell(Base):
    """A tuned cell: its tilt and power as last verified, when it last
    changed (pacing), and a revert or rollback of it waiting for a person."""
    __tablename__ = "coverage_cell"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="STEADY")  # STEADY / OBSERVING
    tilt: Mapped[int | None] = mapped_column(Integer)
    power: Mapped[int | None] = mapped_column(Integer)
    last_changed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # a revert or rollback RAN NF OAM holds for a person (PR-AI-11, call flow 24d): its approval and audit row; nothing moves until it is settled
    pending_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CoverageDecision(Base):
    """The audit trail, with one row per cell per evaluation:
    Shares → Joint plan → Safety → Decision → (Intent) → Action
    → Verification → KPI check / revert → Final state."""
    __tablename__ = "coverage_decision"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    cell_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    reports: Mapped[float | None] = mapped_column(Float)
    shares: Mapped[dict | None] = mapped_column(JSON)
    prediction: Mapped[dict | None] = mapped_column(JSON)
    safety: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String, nullable=False)   # DOWNTILT / UPTILT / POWER_UP / POWER_DOWN / REVERT / NO_CHANGE
    reason: Mapped[str] = mapped_column(String, nullable=False)
    from_setting: Mapped[dict | None] = mapped_column(JSON)
    to_setting: Mapped[dict | None] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    kpi: Mapped[dict | None] = mapped_column(JSON)
    intent: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[dict | None] = mapped_column(JSON)
    verification: Mapped[dict | None] = mapped_column(JSON)
    rollback: Mapped[dict | None] = mapped_column(JSON)
    final_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
