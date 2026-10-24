"""The ORM tables of SA SMOS: assurance monitors, their remedial actions, and the record of each intent the O1-CM handler enacted.

Read and written by `app/main.py` (monitors, actions) and `app/o1cm.py` (enactments). The schema is the Alembic history in `migrations/`; the model and the revision change together.
References to other modules' rows (orders, coordination groups, rApp instances, intents) are bare UUID columns with no foreign key, so this module can run on its own.
"""

import datetime
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


class AssuranceMonitor(Base):
    """Requirement thresholds for at most one target: a service order (NF deployment remediation), a model coordination group (retrain) or a rApp instance (remediation through rApp Management).

    The CHECK `one_target_only` allows at most one of the three target columns to be set; no target is allowed. `requirement_thresholds` maps each metric to a minimum (a number) or to
    `{"min": n, "max": n}` (either bound may be left out). A monitor registered with `analytics_type` holds the MDAF subscription SA SMOS made for it in `analytics_subscription_id`, and each
    report MDAF pushes for it is evaluated (call flow 04b); otherwise that column is a reference the caller gave. `auto_action` (set by an admin) is what a breach runs by itself, `remedy_changes`
    the configuration CONFIG_CHANGE writes, `last_evaluation` the outcome of the latest pushed report ({at, reportId, breached, breaches, missing}).
    """
    __tablename__ = "assurance_monitor"
    __table_args__ = (
        # Portable boolean form — "::int" cast syntax is Postgres-only and
        # fails on SQLite.
        CheckConstraint(
            "(CASE WHEN target_order_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN target_coordination_group_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN target_rapp_instance_id IS NULL THEN 0 ELSE 1 END) <= 1",
            name="one_target_only",
        ),
    )

    monitor_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    target_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    target_coordination_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # SO/SA SMOS LLD section 2.2: a model coordination group (retrain)
    # OI-1-sa-rollback: a monitor on one rApp instance, so ROLLBACK has a target
    # with a version history (rApp Management's). A bare cross-module ref: the
    # instance id may be superseded by an upgrade; rApp Management resolves it.
    target_rapp_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    analytics_subscription_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    requirement_thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    # DOC-CF-1.8 flow 04: the loop closes on MDAF's reports (04b)
    analytics_type: Mapped[str | None] = mapped_column(String)
    auto_action: Mapped[str | None] = mapped_column(String)          # CONFIG_CHANGE | RECONNECT | ROLLBACK | RETRAIN; admin-only to set
    remedy_changes: Mapped[list | None] = mapped_column(JSON)        # the change list CONFIG_CHANGE sends to RAN NF OAM
    last_evaluation: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))


class RemedialAction(Base):
    """One remedial action taken (or escalated) for a monitor, written once with its final outcome (RESOLVED or ESCALATED).

    `action_type` is what was done: CONFIG_CHANGE, SCALE, RECONNECT, ROLLBACK, RETRAIN (a coordination-group monitor) or ESCALATE (a breach or an operator's escalation with
    nothing dispatched). `auto_executed` is true only for an action a pushed report ran by itself (call flow 04b); `requested_by` is who asked (`smo-gui:<user>`, or `sa-smos` for
    the loop), `detail` why it ended as it did, `result` what the module that acted answered, `report_id` the MDAF report that triggered it. `auto_execution_scope_config` is not
    read or written by any route.
    """
    __tablename__ = "remedial_action"

    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assurance_monitor.monitor_id"))
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    auto_executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_execution_scope_config: Mapped[str | None] = mapped_column(String)
    outcome: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    requested_by: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(String)
    result: Mapped[dict | None] = mapped_column(JSON)
    report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class O1CmEnactment(Base):
    """One record per Intent the generic O1-CM intent handler enacted (HISTORY.md W8-07, decision D-1): the DME actions it issued (and so the RAN NF OAM config jobs), the targets it could not
    enact, and the id of the IntentReport it published.

    `status` is FULFILLED or NOT_FULFILLED. `intent_report_id` is null when the report could not be published. One row per intent: the unique index on
    `intent_id` (revision 0062, SA-REPLAY) is what makes a second push of the intent a replay, also when two arrive at once. `deactivated_at` is when the
    handler learnt the intent was DEACTIVATED (null while it is ACTIVATED; cleared when it is activated again) and `deleted_at` when it learnt the intent was
    deleted (revision 0063, INTENT-LIFECYCLE-PUSH). Neither changes anything in the network: the values written stay as written.
    """
    __tablename__ = "o1_cm_enactment"
    __table_args__ = (Index("ux_o1_cm_enactment_intent_id", "intent_id", unique=True),)

    enactment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # -> intent (Intent Service), bare cross-module ref
    status: Mapped[str] = mapped_column(String, nullable=False)  # FULFILLED | NOT_FULFILLED
    actions: Mapped[list] = mapped_column(JSON, nullable=False)  # [{expectationId, actionId, forwardedJobId, status}]
    unsupported_targets: Mapped[list] = mapped_column(JSON, nullable=False)
    intent_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))
    deactivated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
