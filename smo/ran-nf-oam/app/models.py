"""The SQLAlchemy tables of RAN NF OAM: the O1 endpoint and managed-entity registries, alarms and PM files, CM write jobs and their snapshots, the MSAC access-control tables, software jobs and campaigns, onboarding, KPI definitions and the rApp safeguard, approval and decision-record tables.

Used by `main.py`, `lifecycle.py`, `msac.py`, `vendors.py`, `scoping.py` and the worker tasks; nothing here has behaviour beyond column defaults. The unit tests build their SQLite schema from these classes, while Postgres gets its schema from the Alembic revisions in `migrations/versions/`, so a column added here needs a revision there
(`scripts/check_migration_matches_models.py` compares the two, and `migrations/table_owners.json` names the owner of each table; no foreign key may cross a module boundary).
A table with `Versioned` has a `row_version` column: a concurrent update of the same row fails with 409 `CONCURRENT_MODIFICATION` instead of overwriting it.
"""

import datetime
import uuid

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid, false, true
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base
from smo_shared.versioning import Versioned


class O1AdaptorEndpoint(Base):
    """One registered O1 adaptor, at most one per managed element: where its CM is reached (`adaptor_uri` and `transport`), the name of the credential used to reach it (never the secret), the MnS services it declares (NULL: its vendor's) and its heartbeat-driven health.
    """
    __tablename__ = "o1_adaptor_endpoint"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    adaptor_uri: Mapped[str] = mapped_column(String, nullable=False)
    protocol_support: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    registered_via: Mapped[str] = mapped_column(String, nullable=False, default="MNS_REGISTRY_NRM")
    health_status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # Wave 9 (W9-01, docs/ARCHITECTURE.md axis 2): the MnS services
    # this adaptor declares (PROV/FM/PM/FILE/STREAM/SWM/SUBSCRIPTION/HEARTBEAT).
    # NULL means "whatever its vendor's capability declares".
    supported_services: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    # PR-SB-1.2: how the adaptor is reached: 'http-mock' (the XML-over-HTTP mock), 'ssh' (NETCONF over SSH, RFC 6242) or 'tls' (RFC 7589, PR-SB-2.4)
    transport: Mapped[str] = mapped_column(String, nullable=False, default="http-mock", server_default="http-mock")
    # PR-SB-2.1: the NAME of the credential this adaptor is reached with (resolved at connect time from the service's own secrets,
    # netconf_ssh.credentials_for); never the secret. NULL: the shared credential of PR-SB-1.
    credential_ref: Mapped[str | None] = mapped_column(String)


class O1AdaptorHostKey(Base):
    """PR-SB-2.3: a host key an operator pinned for an ssh endpoint (the public key only; at most one per key type)."""
    __tablename__ = "o1_adaptor_host_key"
    __table_args__ = (UniqueConstraint("endpoint_id", "key_type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("o1_adaptor_endpoint.endpoint_id", ondelete="CASCADE"), nullable=False)
    key_type: Mapped[str] = mapped_column(String, nullable=False)
    public_key: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    pinned_by: Mapped[str] = mapped_column(String, nullable=False)
    pinned_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                          default=lambda: datetime.datetime.now(datetime.UTC))


class ManagedObject(Base):
    """PR-SB-6.1: one node of the managed-object containment tree (`mo_tree.py`), keyed by its distinguished name."""
    __tablename__ = "managed_object"

    dn: Mapped[str] = mapped_column(String, primary_key=True)
    parent_dn: Mapped[str | None] = mapped_column(String, ForeignKey("managed_object.dn", ondelete="CASCADE"), index=True)
    object_class: Mapped[str] = mapped_column(String, nullable=False)
    object_id: Mapped[str] = mapped_column(String, nullable=False)
    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref", ondelete="CASCADE"),
                                                      nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)                     # 'registry' or 'walk'
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))


class ManagedEntity(Base):
    """A managed element (or function) known to the SMO, keyed by `managed_element_ref`: its vendor and O1 protocol, its adaptor endpoint, the per-cell guard attributes rApps read, and the region and tenant that scope claims are checked against (NULL: visible to unscoped callers only).
    """
    __tablename__ = "managed_entity"

    managed_element_ref: Mapped[str] = mapped_column(String, primary_key=True)
    managed_function_ref: Mapped[str | None] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    vendor_name: Mapped[str | None] = mapped_column(String)
    o1_protocol: Mapped[str] = mapped_column(String, nullable=False)
    o1_adaptor_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("o1_adaptor_endpoint.endpoint_id"))
    # Wave 9 (W9-06, decision D-5): per-cell guard attributes any rApp may
    # query — {cellId: {cellClass, sectorGroup, incidentZone, neighbourRefs}}.
    cell_guards: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # PR-SEC-10.2 (docs/adr/0005-tenant-region-authorization.md): where the element is and whom it belongs to. NULL: not set. A caller whose scope claim
    # restricts regions (tenants) may touch only elements whose region (tenant) is one it names, so an element without one is for unscoped callers only.
    region: Mapped[str | None] = mapped_column(String, index=True)
    tenant: Mapped[str | None] = mapped_column(String, index=True)
    # PR-GUI-9.8: the site cluster the element belongs to (an operator's grouping below the region, e.g. "metro-a"; `PUT /managed-entities/{me}/site-cluster`).
    # NULL: not set. Only a grouping for the health map and the filters: it is not part of the scope rule of ADR 0005.
    site_cluster: Mapped[str | None] = mapped_column(String, index=True)


class Alarm(Base):
    """A fault record on a managed element (TS 28.532 / 28.111 AlarmRecord fields). `severity` is stored lower-case, and clearing an alarm sets it to `cleared` and fills `cleared_at` and `clear_user_id`, rather than using a separate state column; `source_alarm_id` is the adaptor's own id of the alarm.
    """
    __tablename__ = "alarm"

    alarm_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_alarm_id: Mapped[str] = mapped_column(String, nullable=False)
    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref"))
    managed_function_ref: Mapped[str | None] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # this build's own wire name for 3GPP's perceivedSeverity
    ack_state: Mapped[str] = mapped_column(String, nullable=False, default="UNACKNOWLEDGED")
    correlation_group: Mapped[str | None] = mapped_column(String)
    # PR-GUI-9.4: indexed for the time filters, the hourly buckets and the keyset order of the alarm console (revision 0036)
    raised_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC), index=True)
    # HISTORY.md §5: standard 3GPP TS 28.532 FaultMnS NotifyNewAlarm
    # fields (per oam's own stndDefined-r16-notify-new-alarm.json VES template)
    # this alarm model was missing entirely.
    probable_cause: Mapped[str | None] = mapped_column(String)
    specific_problem: Mapped[str | None] = mapped_column(String)
    root_cause_indicator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    correlated_notifications: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False, default=list)
    proposed_repair_actions: Mapped[str | None] = mapped_column(String)
    # HISTORY.md §7: TS28111_FaultNrm.yaml's AlarmRecord requires alarmType
    # (a closed 11-value enum), which this model never had at all — nullable
    # here since not every real caller of /alarms/ingest necessarily knows
    # it, unlike the spec's own readOnly/required framing.
    alarm_type: Mapped[str | None] = mapped_column(String)
    # HISTORY.md §5: no alarm-cleared lifecycle existed at all.
    # The reference's own NotifyClearedAlarm reuses perceivedSeverity=CLEARED
    # rather than a separate state field — this build's `severity` CHECK
    # constraint already allows 'cleared' for exactly this reason, so
    # clearing an alarm sets severity to 'cleared' rather than adding a
    # parallel, redundant lifecycle field.
    cleared_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    clear_user_id: Mapped[str | None] = mapped_column(String)
    # HISTORY.md §7: the spec's AlarmRecord also carries ackUserId (who
    # acknowledged it — PATCH /alarms/{id}/ack never recorded this) and
    # alarmChangedTime (distinct from raised_at/cleared_at — the spec's own
    # "last mutated" timestamp, set whenever ack_state or severity changes).
    ack_user_id: Mapped[str | None] = mapped_column(String)
    # PR-GUI-9.8: when the alarm was acknowledged (the first ack of the current acknowledged state; NULL while unacknowledged, and for an
    # alarm acknowledged before revision 0036). Mean time to acknowledge (`GET /alarms/stats`) is computed from it.
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


# MGT-8.2 / MGT-8.3 (revision 0039): what happened to an alarm, and what operators said about it.
ALARM_HISTORY_EVENTS = ("RAISED", "ACKNOWLEDGED", "UNACKNOWLEDGED", "CLEARED", "SEVERITY_CHANGED")


class AlarmHistory(Base):
    """MGT-8.2: one change of one alarm, written by `alarm_history.py`'s ORM listeners in the transaction that made the change, so no path that
    raises, acknowledges, clears or re-grades an alarm can skip it. `event` is one of `ALARM_HISTORY_EVENTS` (a CHECK); `from_value` / `to_value`
    are the ack state or the severity before and after (null where there was none); `by` is who did it, when the alarm says (`ack_user_id`,
    `clear_user_id`), else null (a raise or a re-grade comes from the network). Deleted with its alarm."""
    __tablename__ = "alarm_history"

    history_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    alarm_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("alarm.alarm_id", ondelete="CASCADE"), nullable=False, index=True)
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    from_value: Mapped[str | None] = mapped_column(String)
    to_value: Mapped[str | None] = mapped_column(String)
    by: Mapped[str | None] = mapped_column(String)


class AlarmComment(Base):
    """MGT-8.3: a note an operator left on an alarm (`POST /alarms/{id}/comments`): who (`author`, the GUI user through the BFF), when, and the
    text (1 to 2000 characters, checked by the route). Comments are only added: there is no edit or delete. Deleted with its alarm."""
    __tablename__ = "alarm_comment"

    comment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    alarm_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("alarm.alarm_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)


class MsacIdentity(Base):
    """TS 28.319 Identity. `credential` is write-only: only its hash is kept."""
    __tablename__ = "msac_identity"

    identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identity_type: Mapped[str] = mapped_column(String, nullable=False)
    identity_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    credential_hash: Mapped[str | None] = mapped_column(String)
    role_list: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # Role ids


class MsacRole(Base):
    """A TS 28.319 Role: a unique name and the ids of its AccessRules. Evaluation is in `msac.authorize`."""
    __tablename__ = "msac_role"

    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    role_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    access_rules_list: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # AccessRule ids


class MsacAccessRule(Base):
    """A TS 28.319 AccessRule: a `data_node_selector` (an absolute DN path with `*` wildcards), the operations it covers, and whether it ALLOWs or DENYs them (DENY wins).
    """
    __tablename__ = "msac_access_rule"

    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rule_name: Mapped[str] = mapped_column(String, nullable=False)
    data_node_selector: Mapped[str] = mapped_column(String, nullable=False)
    operations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    actions: Mapped[str] = mapped_column(String, nullable=False)  # ALLOW | DENY
    component_c_data: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class PMFile(Base):
    """A performance data file (TS 28.532 File Data Reporting MnS FileInfo)."""
    __tablename__ = "pm_file"

    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False, index=True)
    counter_type: Mapped[str] = mapped_column(String, nullable=False)
    file_data_type: Mapped[str] = mapped_column(String, nullable=False, default="Performance")
    file_format: Mapped[str] = mapped_column(String, nullable=False, default="json")
    file_compression: Mapped[str | None] = mapped_column(String)
    job_id: Mapped[str | None] = mapped_column(String)
    content: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_ready_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    file_expiration_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class KpiDefinition(Base):
    """MGT-11.1: a KPI as a formula over counters. `counters` is a list of {counter, variable, aggregation}: which PM counter feeds which variable of
    the formula, and how that counter's samples are combined over the period (and over the cells of a group): sum, avg, min, max, last or count."""
    __tablename__ = "kpi_definition"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    formula: Mapped[str] = mapped_column(String, nullable=False)
    counters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    unit: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC),
                                                          onupdate=lambda: datetime.datetime.now(datetime.UTC))


class KpiSchedule(Base):
    """MSG-4: publish a KPI to DME on a timer. The worker (`app/tasks.py`) computes `kpi` over the last `lookback_seconds` every `interval_seconds` and
    delivers it as `POST /kpis/{name}/publish` does. `last_*` say what the previous run did; a run that fails is recorded, not retried before the next
    interval, so a KPI that cannot be computed is visible here and is not hammered."""
    __tablename__ = "kpi_schedule"

    schedule_id: Mapped[str] = mapped_column(String, primary_key=True)
    kpi: Mapped[str] = mapped_column(String, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    lookback_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    group_by: Mapped[str] = mapped_column(String, nullable=False, default="cell")
    managed_element_ref: Mapped[str | None] = mapped_column(String)
    cell_id: Mapped[str | None] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String)
    last_detail: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC),
                                                          onupdate=lambda: datetime.datetime.now(datetime.UTC))


class RAppLimit(Base):
    """AI-10.1/10.2: what one rApp (by invoker id, its OAuth client id) may do through this module, taken from the `limits` of its manifest and pushed
    here by rApp Management when the instance finishes bootstrapping. `max_config_jobs_per_hour` caps the CM write jobs it may start in any rolling hour."""
    __tablename__ = "rapp_limit"

    invoker_id: Mapped[str] = mapped_column(String, primary_key=True)
    max_config_jobs_per_hour: Mapped[int | None] = mapped_column(Integer)
    # AI-10.3: how many managed elements one job may touch (blast radius), and how far one attribute may move from its current value in one write,
    # in percent of that value (magnitude). NULL: no such limit. At least one of the three is set.
    max_elements_per_job: Mapped[int | None] = mapped_column(Integer)
    max_change_percent: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC),
                                                          onupdate=lambda: datetime.datetime.now(datetime.UTC))


class RAppKill(Base):
    """AI-10.4: an operator stopped this rApp (by invoker id). While a row exists the rApp's config jobs are refused (`RAPP_KILLED`), and a job it
    started that is waiting between waves does not go on. Undoing is not refused: rollbacks and reverts, and halting or aborting a job, still work."""
    __tablename__ = "rapp_kill"

    invoker_id: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String)
    killed_by: Mapped[str] = mapped_column(String, nullable=False)
    killed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                          default=lambda: datetime.datetime.now(datetime.UTC))


class SafeguardSubscription(Base):
    """AI-10.6: who is told when the platform refuses an rApp (a kill switch, a rate, blast-radius or magnitude limit): `callback_uri` receives each
    refusal event (through the outbox), narrowed to the codes in `refusals` when that is not empty."""
    __tablename__ = "safeguard_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    callback_uri: Mapped[str] = mapped_column(String, nullable=False)
    refusals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                           default=lambda: datetime.datetime.now(datetime.UTC))


class SafeguardRefusal(Base):
    """AI-10.6: one refusal of an rApp by a safeguard, kept whether or not anyone subscribed. `notified` is whether events went out for it: a
    repeat of the same refusal for the same rApp within `SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS` is recorded but not announced again."""
    __tablename__ = "safeguard_refusal"

    refusal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True,
                                                            default=lambda: datetime.datetime.now(datetime.UTC))
    invoker_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    requested_by: Mapped[str | None] = mapped_column(String)
    code: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(String)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class RAppApprovalPolicy(Base):
    """AI-11.4: an rApp (by invoker id) whose config jobs wait for a human. While a row exists, a write by that rApp that passed every check is parked as a
    `rapp_action_approval` instead of being dispatched. `on_timeout` says what happens to a request nobody decided within `timeout_seconds`:
    `EXPIRE` (it lapses, status EXPIRED) or `REJECT` (the platform rejects it, status REJECTED, decided by `system:timeout`); neither writes anything."""
    __tablename__ = "rapp_approval_policy"

    invoker_id: Mapped[str] = mapped_column(String, primary_key=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, server_default="3600")
    on_timeout: Mapped[str] = mapped_column(String, nullable=False, default="EXPIRE", server_default="EXPIRE")
    set_by: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # How many different people must approve a request before the job is made: 1 (a single approval, as before) or 2 (two distinct approvers; the requester's own
    # approval never counts). Revision 0034.
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class RAppActionApproval(Base):
    """AI-11.1: one rApp action waiting for, or given, a human decision. `request` is the whole write request (what the job will be made from when it
    is approved), `status` is PENDING, APPROVED (a job was made: `job_id`), REJECTED, EXPIRED, or REFUSED (approved, but a safeguard or a check refused
    it at that moment: `refusal_code`). It is decided once."""
    __tablename__ = "rapp_action_approval"

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    invoker_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING", index=True)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    managed_elements: Mapped[list] = mapped_column(JSON, nullable=False, default=list)       # the distinct elements the action touches, for the list view
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    on_timeout: Mapped[str] = mapped_column(String, nullable=False, default="EXPIRE")         # the policy as it was when the request was parked
    decided_by: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    refusal_code: Mapped[str | None] = mapped_column(String)
    correlation_id: Mapped[str | None] = mapped_column(String)
    # PR-SEC-10.4: the scope claim of the requester when the request was parked ({"regions": [...], "tenants": [...]}; NULL: unscoped), checked again against
    # the targets as they are when the request is approved
    requester_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    # Revision 0034: the number of distinct approvers the policy asked for when the request was parked (a later change of the policy does not change what a
    # waiting request needs), and the approvals given so far, `[{"by", "at", "reason"}]` in the order given (NULL while there are none; a request that needs one
    # approval keeps its single approver in `decided_by`, as before). Changed only under the row lock of a decision.
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    approvals: Mapped[list | None] = mapped_column(JSON(none_as_null=True))


class ApprovalSubscription(Base):
    """AI-11.5: who is told (a POST to `callback_uri`, through the outbox) when an rApp action needs a decision, and when one lapses."""
    __tablename__ = "approval_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    callback_uri: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class LifecycleSubscription(Base):
    """MGT-14.7, MGT-15.6: who is told (a POST to `callback_uri`, through the outbox) when an element's onboarding fails (`ONBOARDING_FAILED`), a software campaign
    halts (`CAMPAIGN_HALTED`) or its rollback fails (`CAMPAIGN_ROLLBACK_FAILED`). `events` narrows it to those types; empty means all three."""
    __tablename__ = "lifecycle_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    callback_uri: Mapped[str] = mapped_column(String, nullable=False)
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class RAppDecisionRecord(Base):
    """AI-13.1: why an rApp acted, written when its config job is made (and when an action that needed approval ended without a job). Written once:
    `content_hash` covers every field but `audit_seq`, which is set a moment later and is the row of the shared hash chain (`smo_shared.audit`) that carries that hash, so a
    changed record no longer matches its link in the chain. `disposition`: DIRECT (no approval was needed), APPROVED, REJECTED, EXPIRED, REFUSED."""
    __tablename__ = "rapp_decision_record"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    invoker_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action_id: Mapped[str | None] = mapped_column(String)
    inputs_ref: Mapped[str | None] = mapped_column(String)
    model_version: Mapped[str | None] = mapped_column(String, index=True)
    rationale: Mapped[str | None] = mapped_column(String)
    decided_by: Mapped[str | None] = mapped_column(String)                                  # the approver (APPROVED, REJECTED) or `system:timeout`
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    managed_elements: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_seq: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))     # NULL until the chain entry is written (a moment after the commit)
    # Revision 0034: who approved, in the order given, when the request needed two approvals. NULL for every other record, which then hashes exactly as it did
    # before this column existed (the hash covers `approvers` only when it is set).
    approvers: Mapped[list | None] = mapped_column(JSON(none_as_null=True))


class FileSubscription(Base):
    """A File Data Reporting MnS subscription: notifyFileReady goes to `consumer_reference`."""
    __tablename__ = "file_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    consumer_reference: Mapped[str] = mapped_column(String, nullable=False)
    file_data_type: Mapped[str | None] = mapped_column(String)  # None = every type
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CMSchemaCache(Base):
    """A loaded CM schema, keyed by (schema_name, revision): its location and type and the class descriptor that CM writes are checked against (`vendors.schema_problems`). The schemas bundled in `app/cm_schemas/` are not rows here.
    """
    __tablename__ = "cm_schema_cache"

    schema_name: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[str] = mapped_column(String, primary_key=True, default="")
    location: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False, default="YANG")
    cached_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    # Wave 9 (W9-02): the capability descriptor itself — {"classes": {IOC:
    # {attribute: {type, enum?}}}}, generated by scripts/ingest_cm_schema.py.
    descriptor: Mapped[dict | None] = mapped_column(JSON)


class VendorCapability(Base):
    """Wave 9 (W9-01/W9-04) — the per-vendor Capability Registry entry:
    which MnS services the vendor's O1 terminations implement (axis 2), whose
    data model its CM conforms to (axis 3: OWN / SPEC / COMBINED, with the
    vendor's own descriptor and the spec descriptor it is checked against),
    and which O1 transports (vendor modes) it speaks."""
    __tablename__ = "vendor_capability"

    vendor_name: Mapped[str] = mapped_column(String, primary_key=True)
    supported_services: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    conformance_mode: Mapped[str] = mapped_column(String, nullable=False, default="SPEC")
    supported_vendor_modes: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String)
    schema_revision: Mapped[str | None] = mapped_column(String)
    spec_schema_name: Mapped[str | None] = mapped_column(String)
    spec_schema_revision: Mapped[str | None] = mapped_column(String)
    discovery_uri: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))


class WriteConfigJob(Versioned, Base):
    """A CM write job (`POST /config-jobs`): who asked, its state (`JobState`), the staged-rollout settings and progress (waves, pause, gate), the rollback link, the KPI guard declared with it, the change window and its approval (MGT-4), and the rApp invoker id that rate limits and job ownership use. Its changes are the `write_config_sub_change` rows.
    """
    __tablename__ = "write_config_job"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    # when the checks passed and the job was let run; a job held for a change window (MGT-4) is stamped again when it starts, since its KPI guard measures from here
    schema_validated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    conflict_resolution: Mapped[str | None] = mapped_column(String)
    msac_role: Mapped[str | None] = mapped_column(String)
    # MGT-1.6: set on a job that undoes another one. `rollback_forced` is true when the guard (MGT-1.7) found values changed since and the
    # requester went ahead anyway: the audit trail of an override.
    rollback_of: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    rollback_forced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # MGT-5.1: staged rollout. `wave_size` is how many elements go in one wave (NULL: one wave, the job as before); the waves are made when the
    # job is created, `current_wave` counts the ones that have run, `next_wave_at` is when a paused job may go on, `halted_reason` why a HALTED job
    # stopped (GATE_FAILED, OPERATOR_HALT, WAVE_PAUSE, REVERT_REFUSED). The gate (MGT-5.3) and what happens when it fails (MGT-5.5) are settings too.
    wave_size: Mapped[int | None] = mapped_column(Integer)
    wave_pause_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    wave_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_wave: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gate_max_new_alarms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    on_gate_failure: Mapped[str] = mapped_column(String, nullable=False, default="halt", server_default="halt")
    next_wave_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    halted_reason: Mapped[str | None] = mapped_column(String)
    halted_detail: Mapped[str | None] = mapped_column(String)
    # AI-10.2: who asked, as R1 Termination vouches for it (the token's client id; NULL for a call that did not come through R1), and when. The
    # per-rApp rate limit counts a caller's jobs from these.
    invoker_id: Mapped[str | None] = mapped_column(String, index=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    # MSG-4: a KPI guard declared with the job: once the observation window has passed the worker runs the check of AI-10.5 (and reverts what regressed
    # when `revert` is set). `kpi_guard_result` is the last answer; `kpi_guard_checked_at` is set when the verdict is final.
    kpi_guard: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    kpi_guard_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    kpi_guard_checked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # MGT-4.1/4.3: the change window the job was asked for (`scheduled_at` its opening, `window_end` its close, either NULL: open-ended), and who
    # decided the approval it waited for (PENDING_APPROVAL), when, and why. NULL on a job that never waited.
    scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String)


class WriteConfigSubChange(Base):
    """One attribute change of a job on one element or function: the edit-config `operation`, its outcome (`status`, `rejection_reason` as a stable code and `rejection_detail` as the adaptor's text), the dispatch attempts made, and its position and wave in the job.
    """
    __tablename__ = "write_config_sub_change"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("write_config_job.job_id"))
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    managed_function_ref: Mapped[str | None] = mapped_column(String)
    attribute_changes: Mapped[dict] = mapped_column(JSON, nullable=False)
    # HISTORY.md §7 item 3: TS28532_ProvMnS.yaml defines four distinct MOI
    # lifecycle operations (create/replace/merge/delete) but this sub-change
    # had no operation-type field at all — every write was implicitly a
    # merge. Grounded in RFC 6241 section 7.2's real edit-config `operation`
    # attribute (this build's actually-implemented southbound protocol,
    # netconf_client.py) rather than ProvMnS's HTTP-verb-level framing.
    operation: Mapped[str] = mapped_column(String, nullable=False, default="merge")
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    rejection_reason: Mapped[str | None] = mapped_column(String)
    # PR-SB-1.7: what the adaptor said (its <rpc-error>: tag, path, message), bounded; the reason above stays the stable code
    rejection_detail: Mapped[str | None] = mapped_column(String)
    # Wave 10.1 (W10-19): edit-config attempts made, retries included
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # MGT-5.2: the request order of the sub-change and the wave it belongs to (1 for a job without waves)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    wave: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PMSubscription(Base):
    """A PM subscription: the registration of this service as a DME producer of one counter type for an element, with the delivery method and optional granularity period.
    """
    __tablename__ = "pm_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref"))
    counter_type: Mapped[str] = mapped_column(String, nullable=False)
    delivery_method: Mapped[str] = mapped_column(String, nullable=False)
    southbound_engine: Mapped[str] = mapped_column(String, nullable=False)
    # HISTORY.md §7 item 4 (formerly 7): TS28550_PerfMeasJobCtrlMnS.yaml's
    # measJobCreation-RequestType carries a granularityPeriod (the sampling
    # interval, in seconds) alongside reportingPeriod/schedule/priority —
    # subscribe_pm's own docstring already confirms most of that job-control
    # shape is a deliberate scope cut (this is a DME-producer registration
    # wrapper, not a real clause-8 PM job), but granularityPeriod is needed
    # by any real PM subscription regardless of wrapper shape, and was
    # fully absent. Nullable: optional in the real spec too.
    granularity_period: Mapped[int | None] = mapped_column(Integer)


class FMSubscription(Base):
    """An FM subscription: the registration of this service as a DME producer of the element's fault records."""
    __tablename__ = "fm_subscription"

    # HISTORY.md OI-6.7: unlike PM (subscribe_pm registers RAN NF
    # OAM as a DME producer for PMCounters.{counter_type}), FM/alarms had
    # no DME producer registration at all — an rApp/AI-ML model wanting
    # outstanding-active-alarm/alarm-history context had no DME-mediated
    # way to get it. This mirrors PMSubscription's own shape; alarm
    # clearing itself is unaffected (stays RAN NF OAM's own
    # PATCH /alarms/{id}/clear, never DME's or a consuming rApp's call).
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref"))
    delivery_method: Mapped[str] = mapped_column(String, nullable=False)
    southbound_engine: Mapped[str] = mapped_column(String, nullable=False)


class SoftwareManagementJob(Versioned, Base):
    """A software job on one element (`SwmState`, with the `phase` of download, install, activate kept as a separate column). A job made by a software campaign carries the campaign, its wave and, for an undo, the job it reverses.
    """
    __tablename__ = "software_management_job"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref"))
    ru_instance_id: Mapped[str | None] = mapped_column(String)  # reserved, section 3.4
    phase: Mapped[str] = mapped_column(String, nullable=False, default="DOWNLOAD")
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    # MGT-15.1: set on a job a software campaign made (`software_campaign`): the campaign, the wave it belongs to, and, on a job that undoes another one,
    # that job (a rollback). All NULL for a job started the way it always was (`POST /software-management-jobs`).
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    campaign_wave: Mapped[int | None] = mapped_column(Integer)
    rollback_of: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    software_version: Mapped[str | None] = mapped_column(String)


class OnboardingTemplate(Base):
    """MGT-14.1: the initial configuration of an element type. A newly registered element of `entity_type` (and `vendor_name`, when the template names one)
    is matched to it; `changes` is the list of CM changes applied to the element (each without a `managedElementRef`: it is the element's own).
    `software_baseline` is the software version the element is expected to run (MGT-14.4); `require_baseline` makes a different version stop the onboarding
    rather than only flag it; `auto_apply` applies the template when the element first reports in (its first heartbeat) instead of waiting for an operator."""
    __tablename__ = "onboarding_template"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    vendor_name: Mapped[str | None] = mapped_column(String)
    software_baseline: Mapped[str | None] = mapped_column(String)
    require_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    changes: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))


class ElementOnboarding(Versioned, Base):
    """MGT-14.5: where one element is in its onboarding (`OnboardingState`). One row per element that was matched against the templates; an element registered
    while no template exists has none. `software_check` is the baseline check (MGT-14.4): NOT_CHECKED, MATCH or MISMATCH."""
    __tablename__ = "element_onboarding"

    managed_element_ref: Mapped[str] = mapped_column(String, ForeignKey("managed_entity.managed_element_ref"), primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DISCOVERED")
    template_name: Mapped[str | None] = mapped_column(String)
    software_version: Mapped[str | None] = mapped_column(String)
    software_baseline: Mapped[str | None] = mapped_column(String)
    software_check: Mapped[str] = mapped_column(String, nullable=False, default="NOT_CHECKED", server_default="NOT_CHECKED")
    config_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    detail: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))


class SoftwareCampaign(Versioned, Base):
    """MGT-15.1: a software change over many elements, run in waves (`CampaignState`). `elements` is the ordered list of references, fixed when the campaign is
    made; every wave starts one software management job per element (`software_management_job.campaign_id`). `wave_log` is the outcome of each wave's health gate."""
    __tablename__ = "software_campaign"

    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    software_version: Mapped[str | None] = mapped_column(String)
    selector: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    elements: Mapped[list] = mapped_column(JSON, nullable=False)
    wave_size: Mapped[int | None] = mapped_column(Integer)
    wave_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_wave: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    wave_pause_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gate_max_new_alarms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    on_gate_failure: Mapped[str] = mapped_column(String, nullable=False, default="halt", server_default="halt")
    # MGT-15.7: NULL = no timeout (a wave waits for every job, as before); seconds a wave's (or a rollback step's) jobs may take before the sweep fails those still running.
    # `rollback_order`: "all" starts every revert job at once (as before), "reverse" the last wave first and the next only when the one before it has ended.
    job_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    rollback_order: Mapped[str] = mapped_column(String, nullable=False, default="all", server_default="all")
    wave_started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    next_wave_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    halted_reason: Mapped[str | None] = mapped_column(String)
    halted_detail: Mapped[str | None] = mapped_column(String)
    wave_log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class CMSnapshot(Base):
    """MGT-1.1: what one dispatched sub-change replaced and wrote. `before` holds the current values of the attributes the change
    names (the whole object's attributes for a delete/remove), read from the NF just before the write; NULL with `before_error`
    when that read failed. `after` is what the NF acknowledged: the written values, NULL when the change was not applied or
    removed the object. One row per dispatched sub-change; removing the sub-change removes it."""
    __tablename__ = "cm_snapshot"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sub_change_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("write_config_sub_change.id", ondelete="CASCADE"), nullable=False, unique=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("write_config_job.job_id", ondelete="CASCADE"), nullable=False)
    managed_element_ref: Mapped[str] = mapped_column(String, nullable=False)
    managed_function_ref: Mapped[str | None] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    after: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    before_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                          default=lambda: datetime.datetime.now(datetime.UTC))
