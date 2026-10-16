"""Database tables of MDAF: reports, subscriptions, and the TS 28.104 MDA functions, requests and deliveries.

Used by `main.py`, `mda.py` and `tasks.py`; the schema is created by the Alembic revisions in `migrations/`. Report and
subscription rows are written by two publishing paths (the legacy `POST /reports` and the spec-shaped `POST /mda-reports`),
which is why a report has both a free-form `output` and typed `mda_outputs`. `mdaf_report.generated_at` is what the retention
task purges on.
"""

import datetime
import uuid

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


class MDAFReport(Base):
    """One published report. `output` is the free-form result (for a spec-shaped report, the flattened entries); `mda_outputs` holds the
    typed outputs of a spec-shaped report and is None for a legacy one. `input_sources` are DME data job ids, checked at publish
    time and not a foreign key. `report_kind` is ANALYTICS, PREDICTION or DRIFT. `mda_function_id` and `mda_request_id` are
    cleared when that function or request is deleted. `subscriber_attribution` is not written by any code in this module.
    """
    __tablename__ = "mdaf_report"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analytics_type: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[dict | None] = mapped_column(JSON)
    input_sources: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    output: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    subscriber_attribution: Mapped[str | None] = mapped_column(String)
    # DOC-CF-1.8 flow 08: who published it (the rApp's invoker id, or the console user / SMO module); null for a report published before revision 0056
    published_by: Mapped[str | None] = mapped_column(String)
    # Wave 4-10 roadmap, Wave 5 — TS 28.104 MDAReport. A report published
    # through `POST /mda-reports` carries the spec's own typed mDAOutputs;
    # one published through the original `POST /reports` keeps its
    # free-form `output` (and is shown as MDAOutputEntry pairs). The
    # report kind (W5-02) types it as ANALYTICS / PREDICTION / DRIFT.
    report_kind: Mapped[str] = mapped_column(String, nullable=False, default="ANALYTICS")
    mda_type: Mapped[str | None] = mapped_column(String)
    mda_outputs: Mapped[list | None] = mapped_column(JSON)
    mda_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("mda_function.mda_function_id", ondelete="SET NULL"))
    mda_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("mda_request.mda_request_id", ondelete="SET NULL"))


class MDASubscription(Base):
    """A consumer's standing interest in one analytics type. `threshold_info` is the wire-shaped list of thresholds the subscriber
    declared and `threshold_state` the side (ABOVE or BELOW) last seen per monitored output, which makes notification
    edge-triggered (see `main._threshold_crossed`). Without thresholds every report of the type is notified.
    """
    __tablename__ = "mda_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analytics_type: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[dict | None] = mapped_column(JSON)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    notification_destination: Mapped[str | None] = mapped_column(String)  # NEW section 5: publish_report's actual delivery target — see main.py
    # Wave 3 (AI Platform Service Decomposition) — TS28.104 ThresholdInfo
    # (HISTORY.md §7's MDAF section, cribbed from AIMgF's own
    # MLMFSubscription.guard_kpi_floor). threshold_info is the wire-shaped
    # list of {monitoredMDAOutputIE, thresholdDirection, thresholdValue,
    # hysteresis} the subscriber declared; threshold_state is this
    # subscription's own last-known ABOVE/BELOW side per monitored IE —
    # real edge-triggered bookkeeping, not a decorative field, since a
    # hysteresis band that never actually gates anything is exactly the
    # class of "declared but silently unused" bug this build's own audits
    # repeatedly catch elsewhere (DME's data_category, MDAF's own
    # previously no-op subscriber notify).
    threshold_info: Mapped[list[dict] | None] = mapped_column(JSON)
    threshold_state: Mapped[dict | None] = mapped_column(JSON)


# ---------------------------------------------------------------- Wave 5: TS 28.104 MDA NRM IOCs

class MDAFunction(Base):
    """TS 28.104 MDAFunction — what an MDA producer can analyse
    (supportedMDACapabilities) and in which domain."""
    __tablename__ = "mda_function"

    mda_function_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    supported_mda_capabilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    supported_mda_domain: Mapped[str | None] = mapped_column(String)  # CN | RAN | CROSS_DOMAIN
    ml_model_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    aiml_inference_function_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))


class MDARequest(Base):
    """TS 28.104 MDARequest — the consumer-driven side of MDA: what
    outputs are wanted, for which scope and time, delivered how. Published
    reports are matched against every open request (see app/mda.py)."""
    __tablename__ = "mda_request"

    mda_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mda_function_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("mda_function.mda_function_id", ondelete="SET NULL"))
    requested_by: Mapped[str | None] = mapped_column(String)
    requested_mda_outputs: Mapped[list] = mapped_column(JSON, nullable=False)
    reporting_method: Mapped[str] = mapped_column(String, nullable=False)  # FILE | STREAMING | NOTIFICATION
    reporting_target: Mapped[str | None] = mapped_column(String)
    analytics_scope: Mapped[dict | None] = mapped_column(JSON)
    start_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    stop_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    recommendation_filter: Mapped[dict | None] = mapped_column(JSON)
    performance_threshold_info: Mapped[list | None] = mapped_column(JSON)
    analysis_requirements: Mapped[dict | None] = mapped_column(JSON)
    threshold_monitor_refs: Mapped[list | None] = mapped_column(JSON)
    # Per-IE ABOVE/BELOW state for the request's own mDAOutputIEFilters
    # thresholds — the same edge-triggered bookkeeping MDASubscription has.
    threshold_state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))


class MDAReportDelivery(Base):
    """Which MDARequest a report was delivered to, and how — a report can
    satisfy several open requests."""
    __tablename__ = "mda_report_delivery"

    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("mdaf_report.report_id", ondelete="CASCADE"), nullable=False)
    mda_request_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("mda_request.mda_request_id", ondelete="CASCADE"), nullable=False)
    reporting_method: Mapped[str] = mapped_column(String, nullable=False)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    delivered_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
