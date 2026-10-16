"""SQLAlchemy models of the Intent Service: intents, their reports, intent handling functions (RMIHs), utility formulas and autonomy dispatches.

What it is: one class per table (`intent`, `intent_report`, `intent_handling_function`, `intent_utility_formula`, `autonomy_dispatch`) on the shared
`smo_shared.db.Base`. The tables come from the Alembic revisions in `migrations/`; `scripts/check_migration_matches_models.py` fails when a column here
differs from the migrated schema. Column list and meaning: `intent-service/README.md` (2.2).

Where it sits: read and written only by `main.py`; the unit tests create the tables on SQLite from every class in this module.

Owns: column types, defaults and the foreign keys with their `ondelete` rule: `intent.rmih_id` and `autonomy_dispatch.rmih_id` CASCADE from the handling
function, `intent_report.intent_id` CASCADE from the intent, `intent.intent_utility_formula_id` and `autonomy_dispatch.intent_id` SET NULL. Does not
own: any rule about what an intent may contain (`ts28312.py`) or who may change it (`main.py`).

Before editing: the cascades are done by the database, not the ORM (no `relationship()` is declared), so on SQLite they do not happen and only
PostgreSQL shows them. A column change is a schema revision (`CLAUDE.md`, "Schema changes are revisions"), made in the same PR.
`instance_id`, `model_id` and `intent_report_reference` are bare UUIDs on purpose (see the comments on the columns).
"""

import datetime
import uuid

from sqlalchemy import ARRAY, Boolean, Float, ForeignKey, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base


class Intent(Base):
    """A TS 28.312 `Intent`, table `intent`: what an intent owner (RMIO) asks of one intent handling function (RMIH).

    The expectation, context, report-control and guarantee-period attributes are stored as the validated JSON of `ts28312.py`. `rmio_id` is the creator's
    identity and is what gates admin-state changes; `intent_report_reference` points at the current report.
    """
    __tablename__ = "intent"

    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    # The TS 28.312 IntentExpectation list as validated by app/ts28312.py (per expectation family), stored as JSON.
    intent_expectations: Mapped[list] = mapped_column(JSON, nullable=False)
    intent_mgmt_purpose: Mapped[str | None] = mapped_column(String)
    intent_admin_state: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVATED")
    intent_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    intent_preemption_capability: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rmio_id: Mapped[str] = mapped_column(String, nullable=False)
    # DOC-CF-1.8 flow 09: the invoker that created the intent (an rApp, a module, the dispatching rApp for a dispatch's intent); null before revision 0057.
    # An rApp changes only the intents it owns; with no owner, `rmio_id` stands in.
    owner_invoker_id: Mapped[str | None] = mapped_column(String)
    # Consumer-side RMIH selection: the intent belongs to the handling function it is addressed to (TS 28.312 NRM containment: an
    # IntentHandlingFunction contains its Intents). ON DELETE CASCADE matches that, so deregistering the function removes its intents
    # (enforced by PostgreSQL; SQLite ignores it).
    rmih_id: Mapped[str] = mapped_column(String, ForeignKey("intent_handling_function.rmih_id", ondelete="CASCADE"), nullable=False)
    # The remaining TS 28.312 Intent attributes.
    context_selectivity: Mapped[str | None] = mapped_column(String)
    consumer_satisfaction_index_threshold: Mapped[int | None] = mapped_column(Integer)
    expectation_selectivity: Mapped[str | None] = mapped_column(String)
    intent_contexts: Mapped[list | None] = mapped_column(JSON)
    intent_report_control: Mapped[list | None] = mapped_column(JSON)
    implicit_intent_index: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guarantee_periods: Mapped[list | None] = mapped_column(JSON)
    intent_handling_info: Mapped[dict | None] = mapped_column(JSON)
    intent_interpretation_assistance_info: Mapped[dict | None] = mapped_column(JSON)
    # readOnly in the spec: the intent's current IntentReport. A bare UUID, not a foreign key, because intent_report already points
    # back at intent (no FK cycle).
    intent_report_reference: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    intent_utility_formula_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("intent_utility_formula.intent_utility_formula_id", ondelete="SET NULL"))
    # GUI-9.8 (revision 0037): what the newest reports say, kept on the row so `GET /intents` can filter on it in SQL (the reports are JSON). Written
    # by `main._apply_report` with every report this module stores; revision 0037 filled them for the intents that existed before. `fulfilment_percent`
    # and `fulfilled` come from the newest fulfilment report (null: none yet); `in_conflict` from the newest report that carried conflict reports.
    fulfilment_percent: Mapped[float | None] = mapped_column(Float)
    fulfilled: Mapped[bool | None] = mapped_column(Boolean)
    in_conflict: Mapped[bool | None] = mapped_column(Boolean)


class IntentUtilityFormula(Base):
    """A TS 28.312 `IntentUtilityFormula` IOC, table `intent_utility_formula`. An intent may name one (`intent.intent_utility_formula_id`); deleting the
    formula sets that reference to null in PostgreSQL.
    """
    __tablename__ = "intent_utility_formula"

    intent_utility_formula_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    utility_function_id: Mapped[str] = mapped_column(String, nullable=False)
    utility_parameter_list: Mapped[list] = mapped_column(JSON, nullable=False)
    utility_scale: Mapped[float] = mapped_column(Float, nullable=False, default=1)
    utility_offset: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class IntentReport(Base):
    """One TS 28.312 `IntentReport`, table `intent_report`: a set of report kinds published for an intent at `last_updated_time`.

    Each kind has its own nullable JSON column and a row holds only the kinds that were published together. Reports are append-only except that a
    negotiation report gets the consumer's feedback written into it. The newest one an intent has is the one `intent_report_reference` names.
    """
    __tablename__ = "intent_report"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("intent.intent_id", ondelete="CASCADE"))  # ON DELETE CASCADE: deleting an intent deletes its reports (PostgreSQL only)
    intent_fulfilment_report: Mapped[dict | None] = mapped_column(JSON)
    intent_conflict_reports: Mapped[list | None] = mapped_column(JSON)
    # One nullable JSON column per further report kind of the spec.
    intent_feasibility_check_report: Mapped[dict | None] = mapped_column(JSON)
    intent_exploration_report: Mapped[dict | None] = mapped_column(JSON)
    intent_utility_reports: Mapped[list | None] = mapped_column(JSON)
    intent_fulfilment_negotiation_report: Mapped[dict | None] = mapped_column(JSON)
    intent_decomposition_report: Mapped[dict | None] = mapped_column(JSON)
    last_updated_time: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class IntentHandlingFunction(Base):
    """A TS 28.312 `IntentHandlingFunction` (RMIH), table `intent_handling_function`; the primary key is the registering module's service name.

    Holds what the function declares it can handle (`intent_handling_capability_list`, optional `intent_handling_scope`, negotiation functionalities)
    and where new intents are pushed (`notification_destination`).
    """
    __tablename__ = "intent_handling_function"

    rmih_id: Mapped[str] = mapped_column(String, primary_key=True)
    sme_service_id: Mapped[str] = mapped_column(String, nullable=False)
    intent_handling_scope: Mapped[list | None] = mapped_column(JSON)
    intent_handling_capability_list: Mapped[list] = mapped_column(JSON, nullable=False)
    # Where a new intent addressed to this function is pushed (an outbox row written by main.py); required.
    notification_destination: Mapped[str] = mapped_column(String, nullable=False)
    # TS 28.312 IntentHandlingFunction attributes.
    supported_negotiation_functionalities: Mapped[list | None] = mapped_column(JSON)
    supported_utility_list: Mapped[list | None] = mapped_column(JSON)


class AutonomyDispatch(Base):
    """One rApp autonomy-mode decision about an inference outcome, table `autonomy_dispatch` (HISTORY.md OI-6.3): one row per `POST /autonomy-dispatches`.

    A separate record from `Intent` because not every mode produces an intent (SHADOW never does; ASSIST only once an operator resolves it).
    `instance_id` and `model_id` are bare UUIDs, not foreign keys, because rApp Management and MLMR are separate processes; `rmih_id` is a real foreign
    key since the handling function lives in this module. `autonomy_mode` is copied from the instance when the row is made.
    """
    __tablename__ = "autonomy_dispatch"

    dispatch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # -> rapp_instance (rApp Mgmt)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # -> aiml_model (MLMR) — the triggering inference, if any
    # Snapshotted from the instance at dispatch time, not re-read later —
    # a later mode change on the instance should never silently rewrite
    # what already happened here.
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    expectations: Mapped[list] = mapped_column(JSON, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rmih_id: Mapped[str] = mapped_column(String, ForeignKey("intent_handling_function.rmih_id", ondelete="CASCADE"), nullable=False)
    intent_mgmt_purpose: Mapped[str | None] = mapped_column(String)
    intent_handling_scope: Mapped[str | None] = mapped_column(String)
    # The scope actually applied — AUTONOMOUS's own pre-configured
    # rapp_instance.region_scope, or an operator's own resolve-time
    # value for ASSIST. Null for SHADOW (nothing is ever enforced) and
    # for an ASSIST dispatch still AWAITING_SCOPE.
    region_scope: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, nullable=False)  # AWAITING_SCOPE | DISPATCHED | SHADOWED | REJECTED
    # Set when an operator rejects an ASSIST dispatch.
    rejected_by: Mapped[str | None] = mapped_column(String)
    rejection_reason: Mapped[str | None] = mapped_column(String)
    # DOC-CF-1.8 flow 09: who asked for the dispatch (the rApp's invoker id, or the module's), and the console user who scoped an ASSIST dispatch.
    dispatched_by: Mapped[str | None] = mapped_column(String)
    resolved_by: Mapped[str | None] = mapped_column(String)
    intent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("intent.intent_id", ondelete="SET NULL"))
    # All three modes always notify the operator — not mode-gated.
    notification_destination: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))
