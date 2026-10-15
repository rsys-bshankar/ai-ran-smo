"""The Traffic Steering rApp's own state (Wave 10.4). A real rApp keeps this
in its own store; this reference build runs one shared Postgres, so the
tables live in migrations/001_init.sql beside the SMO's."""

import datetime
import uuid

from sqlalchemy import DateTime, Float, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class TrafficInstance(Base):
    """One rApp instance (rapp-mgmt) bound to this service: its cells and
    their frequency layers, the CIO / priority baselines, the instances it
    coordinates with, the recent steering (anti-oscillation), an ASSIST
    dispatch awaiting the operator, and its model and datasets."""
    __tablename__ = "traffic_instance"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    package_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    cells: Mapped[list] = mapped_column(JSON, nullable=False)            # [{cellId, layer}]
    baseline_cio: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    rmih_id: Mapped[str] = mapped_column(String, nullable=False, default="sa-smos")
    energy_saving_instance_id: Mapped[str | None] = mapped_column(String)   # coordination (D10.4-4c)
    mobility_instance_id: Mapped[str | None] = mapped_column(String)
    coverage_instance_id: Mapped[str | None] = mapped_column(String)
    operator_notification_uri: Mapped[str | None] = mapped_column(String)
    steering_log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{source, targets, at}]
    pending_dispatch: Mapped[dict | None] = mapped_column(JSON)   # {dispatchId, decisionIds: {cell: id}}
    evaluate_every_seconds: Mapped[int | None] = mapped_column(Integer)   # the worker's scheduled passes (call flow 25f); None: manual only
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluation: Mapped[dict | None] = mapped_column(JSON)            # {at, executionId, trigger, result}
    data_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model_version: Mapped[str | None] = mapped_column(String)
    artifact_version: Mapped[int | None] = mapped_column(Integer)
    model_params: Mapped[dict | None] = mapped_column(JSON)
    lifecycle_jobs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TrafficCell(Base):
    """A source cell: the steering this rApp has in force from it, and its
    last change, kept until the KPI verification confirms or reverts it."""
    __tablename__ = "traffic_cell"

    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cell_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="STEADY")  # STEADY / OBSERVING
    steering: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)    # {"cio": {target: dB}, "prio": {layer: steps}}
    last_change: Mapped[dict | None] = mapped_column(JSON)
    last_changed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # a revert or rollback RAN NF OAM holds for a person (PR-AI-11, call flow 25e): its approval and audit row; the cell is left alone until settled
    pending_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TrafficDecision(Base):
    """The audit trail, with one row per source cell per evaluation:
    Score / forecast → Safety → Decision → (Intent) → Action → Verification
    → KPI check / revert → Final state."""
    __tablename__ = "traffic_decision"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    cell_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[float | None] = mapped_column(Float)
    forecast: Mapped[float | None] = mapped_column(Float)
    prediction: Mapped[dict | None] = mapped_column(JSON)
    safety: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str] = mapped_column(String, nullable=False)   # STEER_IDLE / STEER_CONNECTED / RELEASE_* / REVERT / NO_CHANGE
    reason: Mapped[str] = mapped_column(String, nullable=False)
    knob: Mapped[str | None] = mapped_column(String)                # IDLE / CONNECTED
    managed_ref: Mapped[str | None] = mapped_column(String)          # NRFreqRelation=… / NRCellRelation=…
    targets: Mapped[list | None] = mapped_column(JSON)
    from_value: Mapped[int | None] = mapped_column(Integer)
    to_value: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    kpi: Mapped[dict | None] = mapped_column(JSON)
    intent: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[dict | None] = mapped_column(JSON)
    verification: Mapped[dict | None] = mapped_column(JSON)
    rollback: Mapped[dict | None] = mapped_column(JSON)
    final_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
