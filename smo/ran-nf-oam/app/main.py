"""RAN NF OAM SMOS.

SMO Design v1.3 section 3.10, extended by RAN NF OAM LLD sections 1-8:
Option A endpoint registry, multi-function-ME addressing, schema-checked
writes with decomposed-PATCH aggregation, fleet-unique alarm IDs, and the
explicit clarification that SubscribePM is a DME-producer registration,
never a clause-8 call (no such API exists).

CM writes dispatch over the ME's provisioned O1 protocol: NETCONF-shaped
<edit-config> RPCs (netconf_client.py, HISTORY.md's "CM cache sync method"
item) or RFC 8040 RESTCONF requests on the data resource
(restconf_client.py, OI-1-cm-sync-restconf). Any other protocol is
rejected with PROTOCOL_NOT_SUPPORTED rather than silently applied.
"""

import csv
import datetime
import hashlib
import io
import logging
import json
import os
import time
import uuid
import sys
from typing import Any, Literal, NoReturn, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from smo_shared import mtls
from smo_shared.errors import illegal_transition_error
from smo_shared.statemachine import IllegalTransition
from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.timeutil import as_utc
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id, get_correlation_id
from smo_shared.pagination import DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET, PageLimit, PageOffset, paginate, paginate_list
from smo_shared.outbox import enqueue
from smo_shared.webhook import is_safe_webhook_destination
from smo_shared.versioning import install_concurrency_handler
from smo_shared.idempotency import idempotent
from smo_shared.invoker import ON_BEHALF_OF_HEADER, invoker_id
from smo_shared.roles import ROLE_INTERNAL, ROLE_RAPP, role_of
from smo_shared import scope as authz_scope
from smo_shared import audit

from .models import Alarm, AlarmComment, AlarmHistory, ApprovalSubscription, RAppActionApproval, RAppApprovalPolicy, RAppDecisionRecord, RAppKill, RAppLimit, SafeguardRefusal, SafeguardSubscription, CMSchemaCache, CMSnapshot, FileSubscription, KpiDefinition, KpiSchedule, VendorCapability, FMSubscription, ManagedEntity, ManagedObject, O1AdaptorEndpoint, O1AdaptorHostKey, PMFile, PMSubscription, SoftwareManagementJob, WriteConfigJob, WriteConfigSubChange
from . import alarm_history  # noqa: F401  (MGT-8.2: registers the ORM listeners that write every alarm change to alarm_history)
from . import alarm_query
from . import fleet
from . import msac
from . import scoping
from .ldn import check_ref, leaf_class, leaf_id
from . import lifecycle
from . import mo_tree
from . import topology
from . import yang_payload
from . import ves
from . import kpi, kpi_formula
from . import netconf_tls
from . import restconf_client
from . import netconf_ssh
from .netconf_client import NETCONF_TIMEOUT_SECONDS, send_edit_config, send_get_config
from .vendors import MnsService, check_vendor_mode, require_service, router as vendors_router, schema_problems
from .statemachine import (
    ENDPOINT_HEALTH_FSM,
    SOFTWARE_MANAGEMENT_FSM,
    WRITE_CONFIG_JOB_FSM,
    EndpointEvent,
    EndpointHealth,
    JobEvent,
    JobState,
    PHASE_ORDER,
    SwmEvent,
    SwmPhase,
    SwmState,
    aggregate_event,
)

SELF_URL = mtls.http_url("http://ran-nf-oam:8000")   # what DME calls back (https:// with SMO_MTLS=on, PR-SEC-2)

app = FastAPI(title="RAN NF OAM SMOS")
log = logging.getLogger("ran-nf-oam")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
app.include_router(msac.router)
apply_r1_gateway_security(app)
apply_correlation_id(app)

_r1_openapi = app.openapi


def _openapi_with_ves_scheme() -> dict:
    """SB-7.5: the VES listener is the one route that is not behind the R1 bearer token (an O1 adaptor posts to it directly, with HTTP Basic), so the document names that scheme."""
    schema = _r1_openapi()
    schema["components"]["securitySchemes"]["vesBasicAuth"] = {"type": "http", "scheme": "basic"}
    return schema


app.openapi = _openapi_with_ves_scheme  # type: ignore[method-assign]

MISSED_HEARTBEAT_THRESHOLD = datetime.timedelta(seconds=90)

# Wave 10.1 (W10-19): a transient NETCONF failure (timeout / unreachable
# agent) is retried — attempt 1 immediately, then after +5, +10 and +20 s
# (the delays before each attempt; "max 3 retries"). An <rpc-error> is a
# definite answer and never retried. Exhausting the retries raises an
# alarm on the ME. Overridable for demos and tests.
#
# The retries run inside the request that submitted the job (a request thread sleeps; moving them to a job
# runner is MSG-4 / ST-9.3, not built), so their total time is bounded, per sub-change, by a budget (PR-ST-9):
# a retry is not started when the time already spent plus its delay would pass DISPATCH_RETRY_BUDGET_SECONDS,
# and the first attempt is always made. The default, 35 s, is exactly the sum of the default delays, so a
# fast-failing adaptor (connection refused) gets the whole schedule; an unresponsive one (each attempt waits out
# the 30 s exchange timeout) gets two attempts. Worst case per sub-change: the budget plus one more attempt in
# flight (`worst_case_dispatch_seconds()`, 65 s by default). A job's sub-changes are dispatched one after the
# other, so a job of N changes can take N times that. R1 Termination answers 504 after
# R1_UPSTREAM_TIMEOUT_SECONDS (60), while this request goes on: set the budget to 25 or less for a single-change
# caller that must be answered inside that window.
NETCONF_RETRY_DELAYS = [float(d) for d in os.environ.get("RAN_NF_OAM_NETCONF_RETRY_DELAYS", "0,5,10,20").split(",")]
DISPATCH_RETRY_BUDGET_SECONDS = float(os.environ.get("RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS", "35"))
_sleep = time.sleep
_monotonic = time.monotonic
# MGT-1: read the current values of what a change names, just before sending it, and keep them with the result (cm_snapshot).
# The read is one more exchange per sub-change (at most NETCONF_TIMEOUT_SECONDS); set false to skip it and the table.
CM_SNAPSHOTS = os.environ.get("RAN_NF_OAM_CM_SNAPSHOTS", "true").lower() not in ("0", "false", "no")
# MGT-1.8 / DB-3.2: how long a snapshot is kept before `POST /config-history/purge` may delete it, in days; 0 keeps them for ever (the default,
# so nothing is deleted by an upgrade). The purge runs when an operator or a scheduler calls it; nothing in the service deletes on its own.
CM_SNAPSHOT_RETENTION_DAYS = int(os.environ.get("RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS", "0") or 0)


def worst_case_dispatch_seconds() -> float:
    """The longest one sub-change's dispatch can take: the retry time allowed by the budget, plus the last attempt."""
    return min(sum(NETCONF_RETRY_DELAYS), DISPATCH_RETRY_BUDGET_SECONDS) + NETCONF_TIMEOUT_SECONDS


# OI-1-cm-sync-restconf: the O1 protocols this module dispatches CM over —
# o1_protocol -> (edit, read, reason for an unexplained failure). Resolved
# at call time so tests can patch either client function.
def _o1_client(protocol: str, transport: str = "http-mock"):
    """The (edit, read, default rejection reason) functions for an O1 protocol and transport, or None for a protocol this module does not dispatch (the caller then rejects with PROTOCOL_NOT_SUPPORTED). NETCONF over ssh or tls goes to `netconf_ssh`, NETCONF over the HTTP mock to `netconf_client`, RESTCONF to `restconf_client`. The functions are looked up at call time so tests can patch the client modules.
    """
    if protocol == "NETCONF" and transport in ("ssh", "tls"):          # one pair of functions: the URI scheme picks SSH or TLS (PR-SB-2.4)
        return netconf_ssh.send_edit_config, netconf_ssh.send_get_config, "NETCONF_RPC_FAILED"
    if protocol == "NETCONF":
        return send_edit_config, send_get_config, "NETCONF_RPC_FAILED"
    if protocol == "RESTCONF":
        return restconf_client.send_edit, restconf_client.send_get, "RESTCONF_REQUEST_FAILED"
    return None


def worst_case_sub_change_seconds() -> float:
    """`worst_case_dispatch_seconds()` plus the before-image read, when snapshots are on."""
    return worst_case_dispatch_seconds() + (NETCONF_TIMEOUT_SECONDS if CM_SNAPSHOTS else 0.0)


def _capture_before(me, endpoint, change: dict, attribute_changes: dict, ssh_options: dict | None = None) -> tuple[dict | None, str | None]:
    """(before values, error): the NF's current values of the named attributes, or of the whole object when the change names none
    (delete/remove). A failed read does not stop the write; it is recorded so nobody mistakes a missing image for an empty one."""
    read = _o1_client(me.o1_protocol, endpoint.transport)[1]
    try:
        current = read(endpoint.adaptor_uri, change["managedElementRef"], message_id=str(uuid.uuid4()),
                       managed_function_ref=change.get("managedFunctionRef"), **(ssh_options or {}))
    except Exception as exc:                                   # noqa: BLE001 - a client bug must not lose the write itself
        return None, f"before-image read raised {type(exc).__name__}"
    if current is None:
        return None, "before-image read failed"
    return ({name: current.get(name) for name in attribute_changes} if attribute_changes else dict(current)), None


def _ssh_options(db: Session, endpoint) -> dict:
    """What only the ssh clients take, per endpoint: the name of its credential (PR-SB-2.1) and the host keys an operator pinned for it
    (PR-SB-2.3). Empty for any other transport (the HTTP and RESTCONF clients have no such parameters)."""
    if endpoint.transport == "tls":
        return {"credential_ref": endpoint.credential_ref}             # TLS trusts a CA file, not pinned keys
    if endpoint.transport != "ssh":
        return {}
    keys = db.execute(select(O1AdaptorHostKey.key_type, O1AdaptorHostKey.public_key)
                      .where(O1AdaptorHostKey.endpoint_id == endpoint.endpoint_id)).all()
    return {"credential_ref": endpoint.credential_ref, "host_keys": [(k.key_type, k.public_key) for k in keys]}


def _dispatch_with_retries(adaptor_uri: str, change: dict, attribute_changes: dict, message_id: str,
                           operation: str, protocol: str = "NETCONF", transport: str = "http-mock",
                           ssh_options: dict | None = None) -> tuple[bool, str | None, int, str | None]:
    """(applied, rejection reason, attempts, adaptor detail) for one sub-change. The same
    retry policy for both protocols: only a transient failure is retried, and only within the time budget."""
    send_edit, _, default_reason = _o1_client(protocol, transport)
    reason, attempts, detail = None, 0, None
    started = _monotonic()
    for delay in NETCONF_RETRY_DELAYS:
        if attempts and _monotonic() - started + delay > DISPATCH_RETRY_BUDGET_SECONDS:
            break                      # the budget (above) is spent: give up now, as for a non-retryable failure
        if delay:
            _sleep(delay)
        attempts += 1
        result = send_edit(adaptor_uri, change["managedElementRef"], attribute_changes, message_id=message_id,
                           operation=operation, managed_function_ref=change.get("managedFunctionRef"),
                           **(ssh_options or {}))
        if result:
            return True, None, attempts, None
        reason = getattr(result, "reason", None) or default_reason
        detail = getattr(result, "detail", None)
        if not getattr(result, "retryable", False):
            break
    return False, reason, attempts, detail


def _transaction_group_key(me, endpoint) -> str | None:
    """PR-SB-1.10: the element a change is grouped under for a candidate transaction, or None when its endpoint does not take part (an
    ssh or tls NETCONF endpoint registered with `?datastore=candidate`; everything else is dispatched one sub-change at a time)."""
    if me is None or endpoint is None or me.o1_protocol != "NETCONF" or endpoint.transport not in ("ssh", "tls"):
        return None
    return me.managed_element_ref if yang_payload.datastore_of(endpoint.adaptor_uri) == "candidate" else None


def _dispatch_group_with_retries(adaptor_uri: str, changes: list[dict], message_id: str, ssh_options: dict | None
                                 ) -> list[tuple[bool, str | None, int, str | None]]:
    """One candidate transaction for several sub-changes of one element, with the retry policy of `_dispatch_with_retries` applied to
    the whole unit (only a transient failure of the connection is retried). One (applied, reason, attempts, detail) per change."""
    edits = [{"target_ref": c["managedElementRef"], "attribute_changes": c.get("attributeChanges", {}),
              "operation": c.get("operation", "merge"), "managed_function_ref": c.get("managedFunctionRef")} for c in changes]
    results, attempts = [], 0
    started = _monotonic()
    for delay in NETCONF_RETRY_DELAYS:
        if attempts and _monotonic() - started + delay > DISPATCH_RETRY_BUDGET_SECONDS:
            break
        if delay:
            _sleep(delay)
        attempts += 1
        results = netconf_ssh.send_edit_configs(adaptor_uri, edits, message_id, **(ssh_options or {}))
        if all(results) or not any(getattr(r, "retryable", False) for r in results):
            break
    return [(bool(r), None if r else (getattr(r, "reason", None) or "NETCONF_RPC_FAILED"), attempts,
             None if r else getattr(r, "detail", None)) for r in results]


def _raise_dispatch_alarm(db: Session, job_id: uuid.UUID, change: dict, reason: str, attempts: int) -> None:
    """Adds a major COMMUNICATIONS_ALARM on the element for a sub-change that failed after more than one attempt (not committed here; the job's commit keeps it). The source id contains the job id and target, so each failed change has its own alarm.
    """
    target = change.get("managedFunctionRef") or change["managedElementRef"]
    db.add(Alarm(source_alarm_id=f"o1-config:{job_id}:{target}", managed_element_ref=change["managedElementRef"],
                 managed_function_ref=change.get("managedFunctionRef"), severity="major",
                 alarm_type="COMMUNICATIONS_ALARM", probable_cause=reason,
                 specific_problem=f"edit-config to {target} failed after {attempts} attempts"))


# SA-RANOAM-6-severity: TS 28.111 PerceivedSeverity is six upper-case values.
# The alarm table keeps its lowercase wire value (`severity`); the API accepts
# either case, and every alarm view also carries `perceivedSeverity` upper-case.
PERCEIVED_SEVERITIES = ("INDETERMINATE", "CRITICAL", "MAJOR", "MINOR", "WARNING", "CLEARED")
# PR-GUI-9.5: the `after` parameter of the keyset-paged lists (`GET /alarms`, `GET /decision-records`, `alarm_query.py`)
AFTER_DESCRIPTION = ("Keyset paging: the `nextCursor` of the previous page; an empty value asks for the first page. When given, the answer is "
                     "`{items, limit, nextCursor, hasMore}` and `offset` and `total` are not used.")


def _perceived_severity(value: str) -> str:
    """The lowercase stored form of `value`; 422 if it is not a PerceivedSeverity."""
    if value.upper() not in PERCEIVED_SEVERITIES:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"severity {value!r} is not a PerceivedSeverity ({', '.join(PERCEIVED_SEVERITIES)})")
    return value.lower()


def _valid_refs(*refs: str | None) -> None:
    """SA-RANOAM-4: a ref carrying '=' must be a well-formed DN."""
    for ref in refs:
        try:
            check_ref(ref)
        except ValueError as exc:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=str(exc)) from exc


def _msac_reach() -> bool:
    """MGT-2: `RAN_NF_OAM_MSAC_REACH` (off by default). On, the TS 28.319 access rules that guard CM writes also guard the reads and the other changes listed in
    `_require_msac`'s callers. Off, nothing below is asked: an Identity or Role that exists for writes does not begin to refuse reads on upgrade. Read at each call."""
    return msac.reach_on()


def _require_msac(db: Session, request: Request, operation: str, managed_element_ref: str, managed_function_ref: str | None = None) -> None:
    """MGT-2: 403 `MSAC_ACCESS_DENIED` when the caller is a managed identity (an Identity of that name is registered, `POST /msac/identities`) and no AccessRule of its roles
    allows `operation` on the target (`msac.authorize`: DENY beats ALLOW, no rule means refused). The caller is the invoker id the gateway vouches for (`invoker_id`), not
    a name or role it sends: a read has no `requestedBy`, and a role named by the caller itself would let a restricted identity pick a wider one. A caller that is not a
    registered Identity (an operator's tool, an rApp with no Identity, a call that did not come through the gateway) is not asked anything, as for writes. Nothing is
    checked unless `RAN_NF_OAM_MSAC_REACH` is on."""
    if not _msac_reach():
        return
    requester = invoker_id(request)
    if requester is None:
        return
    managed, roles = msac.resolve_roles(db, requester, None)
    if not managed:
        return
    _valid_refs(managed_element_ref, managed_function_ref)
    target = msac.target_path(managed_element_ref, managed_function_ref)
    if not msac.authorize(db, roles, target, operation):
        raise framework_error(FrameworkError.MSAC_ACCESS_DENIED, detail=f"{requester} is not permitted: {operation} {target}")


def _require_msac_everywhere(db: Session, request: Request, operation: str) -> None:
    """MGT-2.5: as `_require_msac`, for a call that is not about one element but all of them (a file subscription gets every file's notice): the target is the root, which only
    a rule on `/*` selects, so an identity allowed on part of the network is refused."""
    if not _msac_reach():
        return
    requester = invoker_id(request)
    managed, roles = msac.resolve_roles(db, requester, None) if requester else (False, [])
    if managed and not msac.authorize(db, roles, "/", operation):
        raise framework_error(FrameworkError.MSAC_ACCESS_DENIED, detail=f"{requester} is not permitted: {operation} /")


def _unreadable_elements(db: Session, request: Request, column) -> list[str]:
    """MGT-2.5: the elements named in `column` (of the rows a route would list) that a managed caller may not read; none for a caller that is not asked."""
    return msac.unreadable_elements(db, request, column)


class KpiGuard(BaseModel):
    """MSG-4: check a KPI where this job wrote, once `observationMinutes` have passed since it ran, and (with `revert`) roll back what regressed. The
    worker runs the check of `POST /config-jobs/{id}/kpi-check` with these settings; an unrelated later change is never overwritten (the revert is
    not forced: a value that differs from what the job wrote leaves the job unreverted and says so)."""
    model_config = ConfigDict(extra="forbid")
    kpi: str
    baselineMinutes: int = Field(default=60, ge=1, le=10080)
    observationMinutes: int = Field(default=60, ge=1, le=10080)
    maxRegressionPercent: float = Field(default=10.0, ge=0)
    direction: Literal["higher", "lower"] = "higher"
    minSamples: int = Field(default=1, ge=1)
    revert: bool = False
    msacRole: str | None = None


class DecisionContext(BaseModel):
    """AI-13.1: why the rApp is asking, kept with the job it makes (`GET /decision-records`): a reference to the inputs it decided on (a data job, a dataset,
    a feature snapshot: a reference, never the data), the version of the model that decided, its stated rationale, and the id of its own action. All optional."""
    inputsRef: str | None = Field(default=None, max_length=1000)
    modelVersion: str | None = Field(default=None, max_length=200)
    rationale: str | None = Field(default=None, max_length=4000)
    actionId: str | None = Field(default=None, max_length=100)


# Request body of `POST /config-jobs`, also the shape a rollback, a revert and an onboarding apply build internally. `accessScope` is required (`scope` is its deprecated alias; both may be sent only if equal).
# Each change is a dict with a string `managedElementRef` and optional `managedFunctionRef`, `attributeChanges` and `operation`; a ref containing '=' must be a well-formed DN (checked by the validators). The wave and KPI-guard fields are described where they are used (`_advance`, `run_due_kpi_guards`).
class WriteConfigRequest(BaseModel):
    requestedBy: str
    decision: DecisionContext | None = None
    # SA-RANOAM-2: `scope` collides with the ProvMnS ScopeType, so the access
    # scope is `accessScope`. `scope` stays as a deprecated alias (same value);
    # at least one is required and both, if sent, must agree.
    accessScope: str | None = None
    scope: str | None = None
    changes: list[dict]  # each: {managedElementRef, managedFunctionRef?, attributeChanges?, operation?}
    msacRole: str | None = None

    @field_validator("changes")
    @classmethod
    def _each_change_names_its_element(cls, changes: list[dict]) -> list[dict]:
        for change in changes:
            if not isinstance(change.get("managedElementRef"), str):
                raise ValueError("every change needs a string managedElementRef")
        return changes

    # MGT-3.1: run every check (MSAC, service presence, data model incl. YANG leaf constraints) and send nothing
    dryRun: bool = False
    # MGT-5.1: a staged rollout. The elements of the job go in waves of `waveSize` elements (all the changes of one element in one wave); after each
    # wave but the last the health gate runs (MGT-5.3): a rejected sub-change, or more than `gateMaxNewAlarms` new critical or major alarms on the
    # wave's elements since it started, fails it. A failed gate halts the job (`onGateFailure` "halt", MGT-5.4) or undoes the applied waves ("revert",
    # MGT-5.5). `wavePauseSeconds` holds the job between waves until that time has passed. No `waveSize`: one wave, as before.
    waveSize: int | None = Field(default=None, ge=1)
    wavePauseSeconds: int = Field(default=0, ge=0)
    gateMaxNewAlarms: int = Field(default=0, ge=0)
    onGateFailure: Literal["halt", "revert"] = "halt"
    # MSG-4: a KPI guard, checked by the worker after the job (ignored by a dry run)
    kpiGuard: KpiGuard | None = None

    @model_validator(mode="after")
    def _scope_and_refs(self):
        """Model validator: `accessScope` or its alias `scope` must be present and agree, `accessScope` is filled from the alias, and every element and function ref that contains '=' must parse as a DN (ValueError, which FastAPI answers as 422).
        """
        if self.accessScope is None and self.scope is None:
            raise ValueError("accessScope is required (scope is its deprecated alias)")
        if self.accessScope is not None and self.scope is not None and self.accessScope != self.scope:
            raise ValueError("accessScope and its deprecated alias scope disagree")
        self.accessScope = self.accessScope if self.accessScope is not None else self.scope
        for change in self.changes:
            for key in ("managedElementRef", "managedFunctionRef"):
                check_ref(change.get(key))  # a ref carrying '=' must be a well-formed DN
        return self


# Request body of `POST /o1-adaptor-endpoints`. The validators check that `transport` and the shape of `adaptorUri` agree (ssh:// needs transport ssh, tls:// needs tls, both carry NETCONF only) and that `region` and `tenant` are valid scope values. `credentialRef` is checked in the route instead, so that a validation error of the body never echoes a pasted secret.
class RegisterO1AdaptorEndpointRequest(BaseModel):
    managedElementRef: str
    adaptorUri: str
    protocolSupport: list[str]
    o1Protocol: str
    entityType: str
    managedFunctionRef: str | None = None
    vendorName: str | None = None
    # Wave 9 (W9-01): the MnS services this adaptor implements; omitted =
    # its vendor's declared capability (vendors.py)
    supportedServices: list[MnsService] | None = None
    # PR-SB-1.2: how the adaptor is reached. `ssh`: NETCONF over SSH (RFC 6242), adaptorUri is ssh://user@host[:port].
    # `tls` (PR-SB-2.4): NETCONF over TLS (RFC 7589), adaptorUri is tls://host[:port], authenticated by a client certificate.
    transport: Literal["http-mock", "ssh", "tls"] = "http-mock"
    # PR-SB-2.1: the name of the credential to use (never the secret); only for transport ssh, and it must be one this service has been given.
    # Checked in the route, not here: a validation error of the body model repeats the input, and a pasted secret must not be echoed.
    credentialRef: str | None = None
    # PR-SEC-10.2: where the element is and whom it belongs to (docs/adr/0005-tenant-region-authorization.md). Optional; a caller whose scope claim restricts regions
    # (tenants) may touch only elements whose region (tenant) it names, so an element registered without them is for unscoped callers only.
    region: str | None = Field(default=None, max_length=100)
    tenant: str | None = Field(default=None, max_length=100)
    # MGT-14.4: the software version the element runs, for the baseline check of an onboarding template (lifecycle.py). Kept only when a template matches the
    # element (an element registered while no template exists has no onboarding row to keep it on).
    softwareVersion: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("region", "tenant")
    @classmethod
    def _valid_scope_value(cls, value: str | None) -> str | None:
        if value is not None and not authz_scope.valid_value(value):
            raise ValueError("must be 1 to 100 characters of letters, digits and . _ : / @ + -, starting with a letter or digit")
        return value

    @model_validator(mode="after")
    def _transport_matches(self):
        """Model validator: transport ssh or tls needs o1Protocol NETCONF and an adaptorUri of that scheme that parses; transport http-mock refuses an ssh:// or tls:// URI. Failures are ValueError (422).
        """
        if self.transport == "ssh":
            if self.o1Protocol != "NETCONF":
                raise ValueError("transport ssh carries NETCONF only")
            try:
                netconf_ssh.parse_ssh_uri(self.adaptorUri)
            except netconf_ssh.NetconfSshError as exc:
                raise ValueError(exc.detail) from exc
        elif self.transport == "tls":
            if self.o1Protocol != "NETCONF":
                raise ValueError("transport tls carries NETCONF only")
            try:
                netconf_tls.parse_tls_uri(self.adaptorUri)
            except netconf_ssh.NetconfSshError as exc:
                raise ValueError(exc.detail) from exc
        elif self.adaptorUri.lower().startswith("ssh:"):
            raise ValueError("an ssh:// adaptorUri needs transport ssh")
        elif self.adaptorUri.lower().startswith("tls:"):
            raise ValueError("a tls:// adaptorUri needs transport tls")
        return self


@app.post("/o1-adaptor-endpoints", status_code=201)
def register_o1_adaptor_endpoint(body: RegisterO1AdaptorEndpointRequest, db: Session = Depends(get_session)):
    """RAN NF OAM LLD section 1's own design intent (Option A,
    `docs/call-flows/03-config-write-with-schema-check.md`: "per ME's O1
    Adaptor registers itself into the MnS Registry NRM") had no concrete
    self-registration route anywhere in this build — the entire
    `O1AdaptorEndpoint`/`ManagedEntity` registry could previously only
    ever be populated by a test fixture reaching directly into the DB,
    never by any real caller; even `endpoint_heartbeat` below implicitly
    assumed the row it pings already existed. `docker-compose.yml`'s own
    comment on `mock-o1-adaptor` names this precisely: a real ME's
    `adaptor_uri` "would point at http://mock-o1-adaptor:8000/edit-config
    once one is ever registered against this service" — until now, none
    ever was.

    Real MnS Registry NRM polling stays out of scope (no such registry
    exists in this build, HISTORY.md's confirmed elision) — this is
    the same honest, lighter self-registration-POST substitute already
    used everywhere else in this build (DME's producer registration,
    SME's provider/invoker registration): the O1 Adaptor itself POSTs
    its own existence here instead of a registry polling it.
    `health_status` starts at `DISCOVERED`, the FSM's own real starting
    state (`statemachine.py`'s `ENDPOINT_HEALTH_FSM`) — not the model's
    column default `ACTIVE` (chosen for other callers' test
    convenience) — since a fresh registration hasn't heartbeated yet.
    """
    # Wave 9 (W9-04): a registered vendor's endpoint must use a transport
    # (vendor mode) the vendor declared, and can't claim services it lacks.
    if body.credentialRef is not None:
        try:
            if body.transport not in ("ssh", "tls"):
                raise ValueError("credentialRef applies to transport ssh or tls only")
            netconf_ssh.check_credential_ref(body.credentialRef)
        except ValueError as exc:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=str(exc)) from None     # the message never repeats the value
    check_vendor_mode(db, body.vendorName, body.o1Protocol)
    if db.scalar(select(O1AdaptorEndpoint).where(O1AdaptorEndpoint.managed_element_ref == body.managedElementRef)) is not None:
        raise framework_error(FrameworkError.SERVICE_NAME_CONFLICT, detail=f"an O1 adaptor is already registered for {body.managedElementRef}")
    _valid_refs(body.managedElementRef, body.managedFunctionRef)
    cap = db.get(VendorCapability, body.vendorName) if body.vendorName else None
    if cap is not None and body.supportedServices is not None and not set(body.supportedServices) <= set(cap.supported_services):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"supportedServices {body.supportedServices} exceed vendor {body.vendorName!r}'s {cap.supported_services}")
    endpoint = O1AdaptorEndpoint(managed_element_ref=body.managedElementRef, adaptor_uri=body.adaptorUri,
                                  protocol_support=body.protocolSupport, health_status=EndpointHealth.DISCOVERED.value,
                                  supported_services=body.supportedServices, transport=body.transport, credential_ref=body.credentialRef)
    db.add(endpoint)
    db.flush()
    me = ManagedEntity(managed_element_ref=body.managedElementRef, managed_function_ref=body.managedFunctionRef,
                        entity_type=body.entityType, vendor_name=body.vendorName, o1_protocol=body.o1Protocol,
                        o1_adaptor_endpoint_id=endpoint.endpoint_id, region=body.region, tenant=body.tenant)
    db.add(me)
    db.flush()
    try:
        mo_tree.sync_registry(db, me)          # PR-SB-6: the element's root (and the function it was registered with) join the containment tree
    except ValueError as exc:                  # a ref that is not a distinguished name
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=str(exc)) from None
    onboarding = lifecycle.on_registered(db, me, body.softwareVersion)         # MGT-14.2: None (nothing else changes) unless an onboarding template is defined
    db.commit()
    return {"endpointId": str(endpoint.endpoint_id), "managedElementRef": me.managed_element_ref, "healthStatus": endpoint.health_status,
            "region": me.region, "tenant": me.tenant,
            **({"onboarding": {"status": onboarding.status, "templateName": onboarding.template_name, "softwareCheck": onboarding.software_check}} if onboarding else {})}


# Request body of the host-key pin: the key type and base64 public key as in a known_hosts line, and who pins it (`pinnedBy` is stored as sent; it is not taken from the caller's token).
class PinHostKeyRequest(BaseModel):
    keyType: str
    publicKey: str
    pinnedBy: str


def _ssh_endpoint(db: Session, endpoint_id: uuid.UUID, request: Request) -> O1AdaptorEndpoint:
    """The endpoint (by the id the system made). PR-SEC-10.9: one of an element outside the caller's scope is a 404, as if there were no such endpoint."""
    endpoint = db.get(O1AdaptorEndpoint, endpoint_id)
    if endpoint is None or not scoping.element_permitted(db, scoping.request_scope(request), endpoint.managed_element_ref):
        raise framework_error(FrameworkError.O1_ENDPOINT_NOT_FOUND, detail=f"no such O1 adaptor endpoint {endpoint_id}")
    if endpoint.transport != "ssh":
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="host keys apply to endpoints with transport ssh")
    return endpoint


def _host_key_view(row: O1AdaptorHostKey) -> dict:
    return {"keyType": row.key_type, "fingerprint": row.fingerprint, "pinnedBy": row.pinned_by, "pinnedAt": as_utc(row.pinned_at).isoformat()}


@app.put("/o1-adaptor-endpoints/{endpoint_id}/host-keys")
def pin_host_key(endpoint_id: uuid.UUID, body: PinHostKeyRequest, request: Request, db: Session = Depends(get_session)):
    """PR-SB-2.3: pin the SSH host key an ssh endpoint must present (one per key type). The operator supplies the public key from a source
    they trust (the device's own label, `ssh-keygen -lf`, a signed inventory): this build never learns a key by connecting, so there is no
    trust on first use. A connection whose server key is not pinned (or differs from the pinned key of its type) is refused. Pinning a
    different key for a type that already has one replaces it (`replaced: true`): the one way to accept a changed key, by a named operator."""
    endpoint = _ssh_endpoint(db, endpoint_id, request)
    try:
        key = netconf_ssh.parse_host_key(body.keyType, body.publicKey)
    except ValueError as exc:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=str(exc)) from None
    existing = db.scalars(select(O1AdaptorHostKey).where(O1AdaptorHostKey.endpoint_id == endpoint.endpoint_id,
                                                         O1AdaptorHostKey.key_type == key.get_name())).first()
    fingerprint = netconf_ssh.host_key_fingerprint(key)
    replaced = existing is not None and existing.fingerprint != fingerprint
    if existing is None:
        existing = O1AdaptorHostKey(endpoint_id=endpoint.endpoint_id, key_type=key.get_name())
        db.add(existing)
    existing.public_key, existing.fingerprint, existing.pinned_by = key.get_base64(), fingerprint, body.pinnedBy
    existing.pinned_at = datetime.datetime.now(datetime.UTC)
    if replaced:
        log.warning("host key for endpoint %s (%s) replaced by %s: %s", endpoint_id, key.get_name(), body.pinnedBy, fingerprint)
    db.commit()
    return {**_host_key_view(existing), "replaced": replaced}


@app.get("/o1-adaptor-endpoints/{endpoint_id}/host-keys")
def list_host_keys(endpoint_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # The fingerprints (not the keys) of the host keys pinned for an ssh endpoint, ordered by key type; no paging. 404 O1_ENDPOINT_NOT_FOUND for an unknown endpoint or one outside the caller's scope, 422 when the endpoint is not an ssh one.
    endpoint = _ssh_endpoint(db, endpoint_id, request)
    rows = db.scalars(select(O1AdaptorHostKey).where(O1AdaptorHostKey.endpoint_id == endpoint.endpoint_id)
                      .order_by(O1AdaptorHostKey.key_type)).all()
    return {"items": [_host_key_view(r) for r in rows]}


@app.delete("/o1-adaptor-endpoints/{endpoint_id}/host-keys/{key_type}", status_code=204)
def unpin_host_key(endpoint_id: uuid.UUID, key_type: str, request: Request, db: Session = Depends(get_session)):
    # 204. 404 O1_HOST_KEY_NOT_FOUND when no key of that type is pinned; endpoint errors as for the list. Without a pinned key (and without NETCONF_SSH_KNOWN_HOSTS) connections to the endpoint are refused.
    endpoint = _ssh_endpoint(db, endpoint_id, request)
    row = db.scalars(select(O1AdaptorHostKey).where(O1AdaptorHostKey.endpoint_id == endpoint.endpoint_id,
                                                    O1AdaptorHostKey.key_type == key_type)).first()
    if row is None:
        raise framework_error(FrameworkError.O1_HOST_KEY_NOT_FOUND, detail=f"no {key_type} host key is pinned for this endpoint")
    db.delete(row)
    db.commit()


# --- PR-SB-6: the managed-object containment tree -------------------------------------------------------------------------------------------


def _managed_object(db: Session, dn: str, request: Request) -> ManagedObject:
    """The node `dn`, or 404 `MANAGED_OBJECT_NOT_FOUND`. PR-SEC-10.9: a node of an element outside the caller's scope is the same 404, as if it were not in the tree
    (the answer for a DN that is not there and for one that is must not differ, or a scoped caller could ask which DNs exist). MGT-2.6: with the MSAC switch on, a managed
    caller needs `read` on the node's element (403 `MSAC_ACCESS_DENIED`; asked after the scope, so only for a node the caller may see)."""
    obj = db.get(ManagedObject, dn)
    if obj is None or not scoping.element_permitted(db, scoping.request_scope(request), obj.managed_element_ref):
        raise framework_error(FrameworkError.MANAGED_OBJECT_NOT_FOUND, detail=f"no managed object {dn!r} in the tree")
    _require_msac(db, request, "read", obj.managed_element_ref)
    return obj


def _tree_filter(db: Session, request: Request):
    """What a list of tree nodes is limited to for this caller: the nodes of the elements inside its scope (PR-SEC-10.9) that its access rules let it read (MGT-2.6)."""
    scope = scoping.request_scope(request)
    return lambda stmt: msac.readable(scoping.scoped_to_elements(stmt, scope, ManagedObject.managed_element_ref), db, request, ManagedObject.managed_element_ref)


@app.get("/managed-objects/{dn}")
def read_managed_object(dn: str, request: Request, db: Session = Depends(get_session)):
    """PR-SB-6: one node of the containment tree by its distinguished name (`ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101`). PR-SEC-10.9: a node of an element
    outside the caller's scope is a 404."""
    return mo_tree.view(_managed_object(db, dn, request))


@app.get("/managed-objects/{dn}/children")
def list_managed_object_children(dn: str, request: Request, limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """PR-SB-6.3: the direct children of a node (404 `MANAGED_OBJECT_NOT_FOUND` when the node itself is not in the tree), ordered by class then id. PR-SEC-10.9: only
    the children inside the caller's scope."""
    _managed_object(db, dn, request)
    page = paginate(db, _tree_filter(db, request)(mo_tree.children_stmt(dn)), limit, offset)
    return {**page, "items": [mo_tree.view(o) for o in page["items"]]}


@app.get("/managed-objects/{dn}/subtree")
def read_managed_object_subtree(dn: str, request: Request, depth: int = Query(default=mo_tree.MAX_SUBTREE_DEPTH, ge=0, le=mo_tree.MAX_SUBTREE_DEPTH),
                                db: Session = Depends(get_session)):
    """PR-SB-6.4: a node and its descendants as a nested tree (`children` on each node), down to `depth` levels below it (default and most 16).
    At most 1000 nodes are returned; `truncated` says when that cut the answer short. PR-SEC-10.9: only the nodes inside the caller's scope."""
    _managed_object(db, dn, request)
    tree, truncated = mo_tree.subtree(db, dn, depth, _tree_filter(db, request))
    return {"tree": tree, "truncated": truncated}


@app.post("/managed-entities/{managed_element_ref}/managed-objects/refresh")
def refresh_managed_objects(managed_element_ref: str, request: Request, db: Session = Depends(get_session)):
    """PR-SB-6.2: read the element's server with a whole-container `get-config` and make the containment tree match what it reports: new objects
    are added with `source=walk`, walked objects it no longer reports are removed, and registry objects are kept. Needs an ssh or tls endpoint
    registered with `?model=` (a server without a model has nothing to walk): 409 `PROTOCOL_NOT_SUPPORTED` otherwise, 503 when the read fails.
    PR-SEC-10.9: 403 `SCOPE_DENIED` for an element outside the caller's scope."""
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])
    me = db.get(ManagedEntity, managed_element_ref)
    if me is None:
        raise framework_error(FrameworkError.MANAGED_ENTITY_NOT_FOUND, detail=f"no managed element {managed_element_ref!r}")
    endpoint = db.get(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id) if me.o1_adaptor_endpoint_id else None
    if endpoint is None:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"{managed_element_ref} has no registered O1 adaptor")
    if endpoint.transport not in ("ssh", "tls") or not yang_payload.model_of(endpoint.adaptor_uri):
        raise framework_error(FrameworkError.PROTOCOL_NOT_SUPPORTED,
                              detail="a walk needs an ssh or tls endpoint registered with ?model=<name>: only a server with a model reports its objects")
    paths = netconf_ssh.send_walk(endpoint.adaptor_uri, str(uuid.uuid4()), **_ssh_options(db, endpoint))
    if paths is None:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"the walk of {managed_element_ref} failed")
    summary = mo_tree.apply_walk(db, managed_element_ref, paths)
    db.commit()
    return {"managedElementRef": managed_element_ref, **summary}


TEIV_RAN_PREFIX = "o-ran-smo-teiv-ran"
TEIV_URN_PREFIX = "urn:oran:smo:teiv"


@app.get("/topology")
def export_topology(request: Request, managed_element_ref: str | None = None, db: Session = Depends(get_session)):
    """PR-SB-6.7: the containment tree in the wire shape FOCOM's `/topology` already uses for the TEIV adapter (entities keyed `<prefix>:<Entity>`
    with `{id, attributes}`; relationships keyed `<prefix>:<A>_<REL>_<B>` with `{id, aSide, bSide, sourceIds}`). One generic `ManagedObject` entity
    per node and one `MANAGEDOBJECT_CHILD_OF_MANAGEDOBJECT` relationship per parent link, the child on the a-side. This is this build's own export
    of what it holds, not the TEIV RAN domain model (which has typed entities such as GNBDUFunction). Link types: `/topology/links`, `/topology/relation` (MGT-10.2)."""
    stmt = _tree_filter(db, request)(select(ManagedObject).order_by(ManagedObject.dn))       # PR-SEC-10.9: the caller's elements only (naming another's gives an empty export)
    if managed_element_ref:
        stmt = stmt.where(ManagedObject.managed_element_ref == managed_element_ref)
    objects = db.scalars(stmt).all()
    urn = lambda dn: f"{TEIV_URN_PREFIX}:ManagedObject:{dn}"  # noqa: E731
    entities = [{f"{TEIV_RAN_PREFIX}:ManagedObject": [
        {"id": urn(o.dn), "attributes": {"dn": o.dn, "class": o.object_class, "objectId": o.object_id,
                                          "managedElementRef": o.managed_element_ref, "source": o.source}} for o in objects]}] if objects else []
    present = {o.dn for o in objects}
    child_of = [{"id": f"{TEIV_URN_PREFIX}:MANAGEDOBJECT_CHILD_OF_MANAGEDOBJECT:{o.dn}", "aSide": urn(o.dn), "bSide": urn(o.parent_dn),
                 "sourceIds": [o.dn, o.parent_dn]} for o in objects if o.parent_dn in present]
    relationships = [{f"{TEIV_RAN_PREFIX}:MANAGEDOBJECT_CHILD_OF_MANAGEDOBJECT": child_of}] if child_of else []
    return {"entities": entities, "relationships": relationships}


def _links_in_place(db: Session, links: list[dict], region: str | None, site_cluster: str | None) -> list[dict]:
    """PR-GUI-9.3: the `links` (from `topology.cell_links`) with an element in the place at either end; all of them when no filter is given.

    The links are derived in Python from every element's cell guards (the type of a link needs every owner of a cell id), so the place is resolved
    in SQL to a set of references and the links are kept by membership."""
    refs = scoping.place_refs(region, site_cluster)
    if refs is None:
        return links
    inside = set(db.scalars(refs))
    return [link for link in links if link["aElement"] in inside or link["bElement"] in inside]


def _link_world(request: Request, db: Session):
    """The elements a caller's topology is made of (`topology.cell_links`' `restrict`): those inside its scope claim (PR-SEC-10.9) that its access
    rules let it read (MGT-2.6); every element for a caller with neither."""
    scope = scoping.request_scope(request)
    return lambda stmt: msac.readable(scoping.scoped_to_elements(stmt, scope, ManagedEntity.managed_element_ref), db, request, ManagedEntity.managed_element_ref)


@app.get("/topology/links")
def topology_links(request: Request, managed_element_ref: str | None = None,
                   link_type: Literal["INTRA_ELEMENT", "INTER_ELEMENT", "AMBIGUOUS", "EXTERNAL"] | None = None,
                   reciprocal: bool | None = Query(None, description="`false`: only the relations the other side does not declare back."),
                   limit: int | None = Query(None, ge=1, le=MAX_LIMIT, description="Page size; given (or `offset`), the answer is the page envelope."),
                   offset: int | None = Query(None, ge=0, le=MAX_OFFSET),
                   region: str | None = scoping.RegionFilter, site_cluster: str | None = scoping.SiteClusterFilter,
                   db: Session = Depends(get_session)):
    """PR-MGT-10.2: the neighbour relations declared in the cell guards, each with its link type (`topology.py`): both cells on one element
    (`INTRA_ELEMENT`), on different elements (`INTER_ELEMENT`), a cell id several elements claim (`AMBIGUOUS`) or none does (`EXTERNAL`), and whether
    the other side declares the relation back (`reciprocal`). `managed_element_ref` keeps the links with that element at either end.

    PR-GUI-9.4: `reciprocal` keeps the reciprocal (`true`) or the one-sided (`false`) relations. With neither `limit` nor `offset` the answer is
    `{"items": [...]}`, every link, as it always was; with either, it is the standard page envelope `{items, total, limit, offset}` (`limit`
    defaults to 100).

    PR-GUI-9.3: `region` and `site_cluster` keep the links with an element of that place at either end (an `EXTERNAL` or `AMBIGUOUS` link has no
    b-side element, so only its a-side counts).

    PR-SEC-10.9: a caller with a scope claim gets the links among the elements inside it. The elements outside are not part of its world, so a neighbour that is declared
    on one of them is `EXTERNAL` for it (and `AMBIGUOUS` and `reciprocal` are worked out among its elements only): the answer never names an element it may not touch.
    MGT-2.6: the same for the elements its access rules do not let it read."""
    links = _links_in_place(db, topology.cell_links(db, managed_element_ref, link_type, _link_world(request, db)), region, site_cluster)
    if reciprocal is not None:
        links = [link for link in links if link["reciprocal"] is reciprocal]
    if limit is None and offset is None:
        return {"items": links}
    return paginate_list(links, limit or DEFAULT_LIMIT, offset or 0)


@app.get("/topology/links/counts")
def topology_link_counts(request: Request, managed_element_ref: str | None = None, region: str | None = scoping.RegionFilter,
                         site_cluster: str | None = scoping.SiteClusterFilter, db: Session = Depends(get_session)):
    """PR-GUI-9.4: how many declared neighbour relations there are, without the list: `{"total", "notReciprocal", "external", "ambiguous",
    "intraElement", "interElement"}`. `managed_element_ref` counts only the links with that element at either end; `region` and `site_cluster`
    (PR-GUI-9.3) only the links with an element of that place at either end. Counted in the caller's world, as `GET /topology/links` lists them
    (PR-SEC-10.9 scope claim, MGT-2.6 access rules)."""
    links = _links_in_place(db, topology.cell_links(db, managed_element_ref, restrict=_link_world(request, db)), region, site_cluster)
    by_type = {kind: sum(1 for link in links if link["linkType"] == kind) for kind in topology.CELL_LINK_TYPES}
    return {"total": len(links), "notReciprocal": sum(1 for link in links if not link["reciprocal"]), "external": by_type["EXTERNAL"],
            "ambiguous": by_type["AMBIGUOUS"], "intraElement": by_type["INTRA_ELEMENT"], "interElement": by_type["INTER_ELEMENT"]}


@app.get("/topology/relation")
def topology_relation(a: str, b: str, request: Request, db: Session = Depends(get_session)):
    """PR-MGT-10.2: how the managed object `a` (a DN) stands to `b` in the containment tree: SAME, ANCESTOR (a contains b), DESCENDANT, SIBLING,
    SAME_ELEMENT or DIFFERENT_ELEMENT. 404 when either is not in the tree. PR-SEC-10.9: a node outside the caller's scope is not in its tree (404), so DIFFERENT_ELEMENT
    is only ever said of two nodes the caller may see."""
    objects = [_managed_object(db, dn, request) for dn in (a, b)]
    return {"a": a, "b": b, "relation": topology.containment_relation(db, *objects)}


def _enforce_mo_tree() -> bool:
    """PR-SB-6.5: `RAN_NF_OAM_ENFORCE_MO_TREE` (off by default): a sub-change whose target DN is not in the containment tree is rejected with
    `MANAGED_OBJECT_NOT_FOUND` before anything is sent. Read at each call, so it can be switched with a restart and in tests."""
    return os.environ.get("RAN_NF_OAM_ENFORCE_MO_TREE", "").strip().lower() in ("1", "true", "yes", "on")


def _dispatch_blocker(db: Session, change: dict):
    """(rejection reason or None, managed entity, endpoint): whether a change can be sent at all, from what is registered now."""
    me = db.get(ManagedEntity, change["managedElementRef"])
    if me is None or me.o1_adaptor_endpoint_id is None:
        return "ENDPOINT_UNREACHABLE", me, None
    endpoint = db.get_one(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id)
    # Live-computed staleness at the point health is actually consulted — the same "no scheduler exists anywhere in this
    # build" pattern as DME's producer health — rather than depending on
    # something having already called POST /o1-adaptor-endpoints/discover first.
    _age_endpoint_health(endpoint, datetime.datetime.now(datetime.UTC))
    if endpoint.health_status in ("UNREACHABLE", "DEGRADED"):
        return "ENDPOINT_UNREACHABLE", me, endpoint
    if _o1_client(me.o1_protocol, endpoint.transport) is None:
        # NETCONF and RESTCONF are dispatched; any other provisioned protocol is rejected rather than silently treated as applied.
        return "PROTOCOL_NOT_SUPPORTED", me, endpoint
    if _enforce_mo_tree() and not mo_tree.exists(db, mo_tree.target_dn(change["managedElementRef"], change.get("managedFunctionRef"))):
        return "MANAGED_OBJECT_NOT_FOUND", me, endpoint        # PR-SB-6.5: the target is not in the containment tree
    return None, me, endpoint


@app.post("/config-jobs", status_code=202, responses={200: {"description": "dryRun: the plan (waves and changes) was validated and nothing was written"}})
@idempotent("ran-nf-oam", status_code=202)
def write_configuration_changes(body: WriteConfigRequest, request: Request, db: Session = Depends(get_session)):
    """WriteConfigurationChanges — RAN NF OAM LLD section 5.1's full
    sequence: MSAC gate, schema check (cache-or-fetch), decompose into
    sub_changes, PATCH each independently, aggregate.
    """
    caller = invoker_id(request)
    scope = scoping.request_scope(request)
    _refuse_if_killed(db, caller, body.requestedBy)
    _enforce_scope(db, scope, caller, body.requestedBy, [c["managedElementRef"] for c in body.changes])
    _enforce_rapp_limit(db, caller, body.requestedBy)
    _enforce_change_limits(db, body, caller)
    return _execute_write(body, db, invoker=caller, actor=_acting_rapp(request), park=True, requester_scope=scope)


def _enforce_scope(db: Session, scope: authz_scope.Scope | None, caller: str | None, requested_by: str | None, refs: list[str]) -> None:
    """PR-SEC-10.4: 403 `SCOPE_DENIED` when any element a request names is outside the caller's scope claim (or not registered: it has no region or tenant), before
    anything is checked, recorded or sent: one element out of scope refuses the whole request, a dry run too. An unscoped caller (no claim) is not asked anything.
    The refusal is recorded like the other safeguard refusals (`GET /safeguard-refusals`, `RAPP_SAFEGUARD_REFUSAL` to subscribers) when the caller is known."""
    try:
        scoping.require_elements(db, scope, refs)
    except HTTPException as error:
        if caller:
            _refuse(db, caller, requested_by, error)
        raise


def _acting_rapp(request: Request) -> str | None:
    """The rApp a request is for, when it is for one (AI-13.2): the id of an rApp that called, or the one an SMO module passed on (`X-R1-On-Behalf-Of`).
    A call by an SMO module on its own account (the GUI, an operator's tool) is not an rApp's action and has no decision record. A request that did not
    come through R1 (no role) is taken at its invoker id, as the safeguards take it."""
    if role_of(request) == ROLE_INTERNAL and not request.headers.get(ON_BEHALF_OF_HEADER):
        return None
    return invoker_id(request)


def _job_owner_filter(request: Request) -> str | None | Literal[False]:
    """PR-SEC-10.11: whose jobs the caller is limited to. `False`: nobody's (the job routes behave as before). Otherwise the invoker id the caller's jobs carry (`None`: the
    caller is limited to its own jobs but R1 named no invoker, so it owns none).

    Applies to an rApp that carries a scope claim (the request is an rApp's own, or an SMO module's on behalf of one): the platform is then shared by parties, and one
    rApp's rollback of, or read of, another's job is a way round the scope (two rApps of one tenant share its elements). An SMO module on its own account (the
    operator's GUI, an admin's tool) is not limited, and neither is an rApp with no claim: an upgrade changes nothing until a claim is set, as for the scope itself.
    This is one decision in one place: to hold every rApp to its own jobs, drop the claim test below."""
    if scoping.request_scope(request) is None:
        return False
    if role_of(request) == ROLE_INTERNAL and not request.headers.get(ON_BEHALF_OF_HEADER):       # an SMO module on its own account
        return False
    return invoker_id(request)


def _job_owned(db: Session, request: Request, job: WriteConfigJob) -> bool:
    owner = _job_owner_filter(request)
    return owner is False or scoping.job_owned_by(db, job, owner)


RATE_WINDOW = datetime.timedelta(hours=1)


SAFEGUARD_EVENT_MIN_INTERVAL = datetime.timedelta(seconds=int(os.environ.get("SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS", "60") or 0))


def _refuse(db: Session, caller: str | None, requested_by: str | None, error) -> NoReturn:
    """AI-10.6: every refusal of an rApp by a safeguard (a kill switch, the rate, blast-radius or magnitude limit) is recorded in
    `safeguard_refusal` and announced: an event goes through the outbox to each subscriber of `/safeguard-subscriptions` that wants that code.
    The same refusal of the same rApp is announced at most once per `SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS` (default 60; 0: every one) so an
    rApp that keeps trying cannot flood its watchers, but every one is recorded. Committed before the error is raised, which would roll it back."""
    code, detail = error.detail["title"], error.detail.get("detail")
    now = datetime.datetime.now(datetime.UTC)
    repeat = SAFEGUARD_EVENT_MIN_INTERVAL.total_seconds() > 0 and db.scalar(
        select(func.count()).select_from(SafeguardRefusal).where(SafeguardRefusal.invoker_id == caller, SafeguardRefusal.code == code,
                                                                  SafeguardRefusal.notified.is_(True),
                                                                  SafeguardRefusal.occurred_at >= now - SAFEGUARD_EVENT_MIN_INTERVAL))
    row = SafeguardRefusal(invoker_id=caller, requested_by=requested_by, code=code, detail=detail, occurred_at=now, notified=not repeat)
    db.add(row)
    db.flush()
    if not repeat:
        event = {"href": "/ran-nf-oam/safeguard-subscriptions", "eventType": "RAPP_SAFEGUARD_REFUSAL", "refusalId": str(row.refusal_id),
                 "refusal": code, "invokerId": caller, "requestedBy": requested_by, "detail": detail, "occurredAt": now.isoformat()}
        for sub in db.scalars(select(SafeguardSubscription)).all():
            if not sub.refusals or code in sub.refusals:
                enqueue(db, sub.callback_uri, event)
    db.commit()
    raise error


def _refuse_if_killed(db: Session, caller: str | None, requested_by: str | None = None) -> None:
    """AI-10.4: 403 `RAPP_KILLED` for a caller an operator stopped (`PUT /rapp-kill/{invoker_id}`). Not for a caller R1 did not identify."""
    kill = db.get(RAppKill, caller) if caller else None
    if kill is not None:
        _refuse(db, caller, requested_by, framework_error(
            FrameworkError.RAPP_KILLED, detail=f"{caller} was stopped by {kill.killed_by} at {as_utc(kill.killed_at).isoformat()}"
            + (f": {kill.reason}" if kill.reason else "")))


def _enforce_rapp_limit(db: Session, caller: str | None, requested_by: str | None = None) -> None:
    """AI-10.2: 429 when `caller` (the invoker id R1 vouches for) has already started as many write jobs in the last hour as its limit
    (`PUT /rapp-limits/{invoker_id}`, from the manifest of the rApp) allows. A caller with no limit, or none that R1 identified, is not counted.
    Rollbacks and automatic reverts do not pass here: undoing a change must never be refused because the rApp used its budget."""
    limit = db.get(RAppLimit, caller) if caller else None
    if limit is None or limit.max_config_jobs_per_hour is None:
        return
    since = datetime.datetime.now(datetime.UTC) - RATE_WINDOW
    used = db.scalar(select(func.count()).select_from(WriteConfigJob).where(WriteConfigJob.invoker_id == caller, WriteConfigJob.created_at >= since)) or 0
    if used >= limit.max_config_jobs_per_hour:
        error = framework_error(FrameworkError.RAPP_RATE_LIMITED,
                                detail=f"{caller} has started {used} config jobs in the last hour; its limit is {limit.max_config_jobs_per_hour}")
        error.headers = {"Retry-After": "60"}
        _refuse(db, caller, requested_by, error)


def _number(value) -> float | None:
    """A value as a number, or None when it is not one (a bool is not; a string of digits, as a NETCONF read returns them, is)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _enforce_change_limits(db: Session, body: WriteConfigRequest, caller: str | None) -> None:
    """AI-10.3: the limits a manifest declares on what one job may do, for a caller R1 identified (a rollback or revert has none: undoing is not limited).

    Blast radius, `maxElementsPerJob`: more distinct managed elements than that is 403 `RAPP_BLAST_RADIUS_EXCEEDED`.
    Magnitude, `maxChangePercent`: for every numeric value a change sets, |new - current| / |current| in percent may not exceed the limit (a current
    value of 0 allows only 0). The current value is read from the NF now (the before-image read); one that cannot be read, or is not a number, cannot
    be measured and is refused as well: a limit that is skipped when it is inconvenient is not a limit. Both are checked before anything is
    recorded or sent, for a dry run too."""
    limit = db.get(RAppLimit, caller) if caller else None
    if limit is None:
        return
    elements = list(dict.fromkeys(c["managedElementRef"] for c in body.changes))
    if limit.max_elements_per_job is not None and len(elements) > limit.max_elements_per_job:
        _refuse(db, caller, body.requestedBy, framework_error(
            FrameworkError.RAPP_BLAST_RADIUS_EXCEEDED,
            detail=f"{caller} may change {limit.max_elements_per_job} managed elements in one job; this one names {len(elements)}"))
    if limit.max_change_percent is None:
        return
    for change in body.changes:
        wanted = {name: new for name, new in (change.get("attributeChanges") or {}).items() if _number(new) is not None}
        if not wanted:
            continue
        _, me, endpoint = _dispatch_blocker(db, change)
        before, error = (None, "the element has no usable endpoint") if me is None or endpoint is None else _capture_before(
            me, endpoint, change, wanted, _ssh_options(db, endpoint))
        for name, new in wanted.items():
            current = _number((before or {}).get(name))
            if current is None:
                _refuse(db, caller, body.requestedBy, framework_error(FrameworkError.RAPP_MAGNITUDE_EXCEEDED, detail=(
                    f"{change['managedElementRef']} {name}: its current value cannot be checked against maxChangePercent="
                    f"{limit.max_change_percent:g} ({error or 'it is not a number'})")))
            target = cast(float, _number(new))              # `wanted` holds only the numeric ones
            moved = abs(target - current)
            percent = 0.0 if moved == 0 else float("inf") if current == 0 else moved / abs(current) * 100
            if percent > limit.max_change_percent:
                _refuse(db, caller, body.requestedBy, framework_error(FrameworkError.RAPP_MAGNITUDE_EXCEEDED, detail=(
                    f"{change['managedElementRef']} {name}: {current:g} to {target:g} is a change of "
                    f"{'more than any' if percent == float('inf') else f'{percent:.1f}%'}; {caller} may change a value by {limit.max_change_percent:g}% at most")))


def _execute_write(body: WriteConfigRequest, db: Session, rollback_of: uuid.UUID | None = None, rollback_forced: bool = False,
                   invoker: str | None = None, actor: str | None = None, park: bool = False, approval: RAppActionApproval | None = None,
                   requester_scope: authz_scope.Scope | None = None):
    """The body of `POST /config-jobs`, shared with the rollback route (MGT-1.6), which builds the same request from a recorded job and so
    goes through the same MSAC, schema, dispatch and snapshot steps as any other write.

    `park` (only the route that takes a new write from a caller sets it): an rApp with an approval policy (AI-11) does not get a job here; the request,
    once it has passed every check, is kept as a `rapp_action_approval` and the answer says `PENDING_APPROVAL`. `approval` is that request when a human
    has approved it (`POST /rapp-approvals/{id}/approve`): the job is made from it and the approval is closed in the same transaction. `actor` is the rApp
    the action is for, when it is one: it gets a decision record (AI-13.2)."""
    # SA-RANOAM-1: TS 28.319 role-based access control, per sub-change, before
    # anything is dispatched. A requester with a registered Identity or a
    # defined Role is evaluated against its AccessRules; any other requester
    # keeps the legacy gate (entire-RAN needs a named msacRole).
    managed, roles = msac.resolve_roles(db, body.requestedBy, body.msacRole)
    if managed:
        denied = []
        for change in body.changes:
            op = msac.CONFIG_OPERATION.get(change.get("operation", "merge"))
            target = msac.target_path(change["managedElementRef"], change.get("managedFunctionRef"))
            if op is None or not msac.authorize(db, roles, target, op):
                denied.append(f"{change.get('operation', 'merge')} {target}")
        if denied:
            raise framework_error(FrameworkError.MSAC_ACCESS_DENIED, detail=f"{body.requestedBy} is not permitted: {'; '.join(denied)}")
    elif body.accessScope == "entire-RAN" and not body.msacRole:
        raise framework_error(FrameworkError.MSAC_ACCESS_DENIED, detail="entire-RAN scope requires an MSAC access tier")
    # Wave 9 (W9-02): the pre-check is real now — every change's ME must
    # implement Provisioning, and its class/attributes/values must exist in
    # the data model its vendor's conformance mode selects. Nothing is
    # dispatched (or recorded) if any change fails.
    problems = []
    for change in body.changes:
        require_service(db, change["managedElementRef"], "PROV")
        problems.extend(schema_problems(db, change))
    if problems:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="; ".join(problems))
    if body.kpiGuard is not None:
        _kpi_or_404(db, body.kpiGuard.kpi)                                 # a guard on a KPI that is not defined is refused up front
    elements = list(dict.fromkeys(c["managedElementRef"] for c in body.changes))
    size = body.waveSize or len(elements) or 1
    wave_of = {element: index // size + 1 for index, element in enumerate(elements)}
    if body.dryRun:
        # MGT-3.1/3.2: the checks above passed; each change's verdict adds what the dispatch loop would decide from the registry
        # (no endpoint, endpoint down, no client for its protocol). No job row, no southbound call, no outbox row.
        verdicts = []
        for c in body.changes:
            blocker = _dispatch_blocker(db, c)[0]
            verdicts.append({"managedElementRef": c["managedElementRef"], "managedFunctionRef": c.get("managedFunctionRef"),
                             "operation": c.get("operation", "merge"),
                             "verdict": "PASS" if blocker is None else "WOULD_REJECT", "reason": blocker})
        return JSONResponse(status_code=200, content={
            "dryRun": True, "status": "VALIDATED" if all(v["verdict"] == "PASS" for v in verdicts) else "WOULD_REJECT_SOME",
            "waves": [[e for e in elements if wave_of[e] == w] for w in range(1, max(wave_of.values(), default=1) + 1)],
            "changes": verdicts})

    if park and invoker:
        policy = db.get(RAppApprovalPolicy, invoker)
        if policy is not None:
            return _park_for_approval(db, body, invoker, policy, requester_scope)

    job = WriteConfigJob(requested_by=body.requestedBy, scope=body.accessScope, msac_role=body.msacRole, rollback_of=rollback_of,
                         rollback_forced=rollback_forced, wave_size=body.waveSize, wave_pause_seconds=body.wavePauseSeconds,
                         wave_count=max(wave_of.values(), default=1), gate_max_new_alarms=body.gateMaxNewAlarms,
                         on_gate_failure=body.onGateFailure, invoker_id=invoker,
                         kpi_guard=body.kpiGuard.model_dump() if body.kpiGuard else None)
    db.add(job)
    db.flush()

    # schema check — cache hit or fetch via Configuration Schema Info (clause 8.3)
    job.schema_validated_at = datetime.datetime.now(datetime.UTC)
    job.status = WRITE_CONFIG_JOB_FSM.fire(JobState.PENDING, JobEvent.PRECHECK_PASS)
    db.flush()

    # MGT-5.2: every sub-change exists from the start, PENDING, with its place in the request and its wave; a wave dispatches its own.
    # HISTORY.md §7 item 3: `operation` is RFC 6241 section 7.2's real edit-config attribute — a delete/remove legitimately carries no
    # attributeChanges at all, so this does not assume the key is always present the way a merge-only model could.
    for index, change in enumerate(body.changes):
        db.add(WriteConfigSubChange(job_id=job.job_id, managed_element_ref=change["managedElementRef"],
                                     managed_function_ref=change.get("managedFunctionRef"),
                                     attribute_changes=change.get("attributeChanges", {}), operation=change.get("operation", "merge"),
                                     status="PENDING", position=index, wave=wave_of[change["managedElementRef"]]))
    db.flush()
    if approval is not None:
        approval.status, approval.job_id = "APPROVED", job.job_id
    if actor:
        _record_decision(db, actor, body, "ROLLBACK" if rollback_of else "APPROVED" if approval is not None else "DIRECT", job_id=job.job_id, approval=approval)
    _advance(db, job)
    db.commit()
    if actor:
        _chain_decisions_quietly(db)
    return _job_summary(job)


def _job_summary(job: WriteConfigJob) -> dict:
    return {"jobId": str(job.job_id), "status": job.status, "wave": job.current_wave, "waveCount": job.wave_count,
            "haltedReason": job.halted_reason}


def _wave_rows(db: Session, job: WriteConfigJob, wave: int) -> list[WriteConfigSubChange]:
    return list(db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job.job_id, WriteConfigSubChange.wave == wave)
                           .order_by(WriteConfigSubChange.position)).all())


def _dispatch_wave(db: Session, job: WriteConfigJob, rows: list[WriteConfigSubChange]) -> None:
    """Dispatch the sub-changes of one wave and record each outcome on its row (and its snapshot)."""
    changes: list[dict[str, Any]] = [{"managedElementRef": r.managed_element_ref, "managedFunctionRef": r.managed_function_ref,
                "attributeChanges": r.attribute_changes, "operation": r.operation} for r in rows]
    # PR-SB-1.10: the sub-changes of one element behind a `?datastore=candidate` endpoint are one candidate transaction (lock once, every
    # edit, one commit): they all take effect or none does. A lone sub-change for an element is the same transaction of one.
    checks = [_dispatch_blocker(db, c) for c in changes]
    members: dict[str, list[int]] = {}
    for index, (blocker, me, endpoint) in enumerate(checks):
        key = _transaction_group_key(me, endpoint) if blocker is None else None
        if key is not None:
            members.setdefault(key, []).append(index)
    grouped = {i: key for key, indexes in members.items() if len(indexes) > 1 for i in indexes}
    group_outcomes: dict[int, tuple] = {}

    for index, (row, change) in enumerate(zip(rows, changes)):
        attribute_changes, operation = change["attributeChanges"], change["operation"]
        blocker, me, endpoint = checks[index]
        if blocker is not None:
            row.status, row.rejection_reason = "REJECTED", blocker
            continue
        ssh_options = _ssh_options(db, endpoint)
        if index in grouped:
            if index not in group_outcomes:
                indexes = members[grouped[index]]
                befores = {i: (_capture_before(me, endpoint, changes[i], changes[i]["attributeChanges"], ssh_options)
                               if CM_SNAPSHOTS else (None, None)) for i in indexes}
                outcomes = _dispatch_group_with_retries(endpoint.adaptor_uri, [changes[i] for i in indexes], str(job.job_id), ssh_options)
                group_outcomes.update({i: (*befores[i], *outcome) for i, outcome in zip(indexes, outcomes)})
            before, before_error, applied, reason, attempts, detail = group_outcomes[index]
        else:
            before, before_error = (_capture_before(me, endpoint, change, attribute_changes, ssh_options) if CM_SNAPSHOTS else (None, None))
            applied, reason, attempts, detail = _dispatch_with_retries(endpoint.adaptor_uri, change, attribute_changes,
                                                               str(job.job_id), operation, me.o1_protocol, endpoint.transport,
                                                                       ssh_options)
        if not applied and attempts > 1:
            _raise_dispatch_alarm(db, job.job_id, change, reason, attempts)
        row.status, row.rejection_reason, row.rejection_detail, row.attempts = ("APPLIED" if applied else "REJECTED"), reason, detail, attempts
        if CM_SNAPSHOTS:
            db.flush()                         # the snapshot's foreign key needs its sub-change row to exist first (Postgres enforces it)
            db.add(CMSnapshot(sub_change_id=row.id, job_id=job.job_id, managed_element_ref=row.managed_element_ref,
                              managed_function_ref=row.managed_function_ref, operation=operation, before=before,
                              before_error=before_error,
                              after=attribute_changes if applied and operation not in ("delete", "remove") else None))
    db.flush()


# MGT-5.3: the health gate hook. Each gate looks at the wave that just ran and returns why it fails, or None. A gate that reads KPIs
# (MGT-11) is another function in this list.
def _gate_rejections(db: Session, job: WriteConfigJob, rows: list[WriteConfigSubChange], started: datetime.datetime) -> str | None:
    """Health gate of a staged CM job: fails when any sub-change of the wave that just ran was REJECTED. Returns the reason (count and the first rejection), or None.
    """
    rejected = [r for r in rows if r.status == "REJECTED"]
    if rejected:
        first = rejected[0]
        return (f"{len(rejected)} sub-change(s) of wave {rows[0].wave} were rejected (first: "
                f"{first.managed_function_ref or first.managed_element_ref}: {first.rejection_reason})")
    return None


def _gate_alarms(db: Session, job: WriteConfigJob, rows: list[WriteConfigSubChange], started: datetime.datetime) -> str | None:
    """Health gate: fails when more critical or major alarms than the job's `gateMaxNewAlarms` were raised on the wave's elements since the wave started. Returns the reason, or None.
    """
    elements = {r.managed_element_ref for r in rows}
    raised = db.scalar(select(func.count()).select_from(Alarm).where(
        Alarm.managed_element_ref.in_(elements), Alarm.raised_at >= started, Alarm.severity.in_(("critical", "major")))) or 0
    if raised > job.gate_max_new_alarms:
        return f"{raised} new critical or major alarm(s) on the elements of wave {rows[0].wave} (limit {job.gate_max_new_alarms})"
    return None


HEALTH_GATES = [_gate_rejections, _gate_alarms]


def _finish(db: Session, job: WriteConfigJob) -> None:
    statuses = [sc.status for sc in db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job.job_id)).all()]
    job.status = WRITE_CONFIG_JOB_FSM.fire(JobState(job.status), aggregate_event(statuses))
    job.next_wave_at = None


def _halt(db: Session, job: WriteConfigJob, reason: str, detail: str | None, next_at: datetime.datetime | None = None) -> None:
    job.status = WRITE_CONFIG_JOB_FSM.fire(JobState(job.status), JobEvent.HALT)
    job.halted_reason, job.halted_detail, job.next_wave_at = reason, detail, next_at
    log.warning("config job %s halted after wave %s of %s: %s %s", job.job_id, job.current_wave, job.wave_count, reason, detail or "")


def _advance(db: Session, job: WriteConfigJob) -> None:
    """Run the waves of a job from the next one until it ends or halts (MGT-5.2 to 5.5)."""
    while True:
        wave = job.current_wave + 1
        started = datetime.datetime.now(datetime.UTC)
        rows = _wave_rows(db, job, wave)
        _dispatch_wave(db, job, rows)
        job.current_wave = wave
        db.flush()
        if wave >= job.wave_count:
            _finish(db, job)
            return
        failure = next((f for f in (gate(db, job, rows, started) for gate in HEALTH_GATES) if f), None)
        if failure:
            if job.on_gate_failure == "revert":
                _auto_revert(db, job, failure)
            else:
                _halt(db, job, "GATE_FAILED", failure)
            return
        if job.wave_pause_seconds > 0:
            _halt(db, job, "WAVE_PAUSE", None, datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=job.wave_pause_seconds))
            return


def _auto_revert(db: Session, job: WriteConfigJob, failure: str) -> None:
    """MGT-5.5: undo what the waves so far applied, with the rollback of MGT-1.6 (a new job, the same checks, the changed-since guard). If
    the revert cannot be made safely, or does not complete, the job halts and says so: nothing is left half-undone in silence."""
    changes, expected, problems = _rollback_plan(db, job)
    changed = _changed_since(db, expected) if not problems else []
    if problems or changed:
        why = "; ".join(problems) if problems else f"{len(changed)} value(s) changed since the waves wrote them"
        _halt(db, job, "REVERT_REFUSED", f"{failure}; not reverted: {why}")
        return
    db.flush()
    undo = _execute_write(WriteConfigRequest(requestedBy=job.requested_by, accessScope=job.scope, msacRole=job.msac_role, changes=changes),
                          db, rollback_of=job.job_id)
    if undo["status"] != "COMPLETED":
        _halt(db, job, "REVERT_REFUSED", f"{failure}; the revert job {undo['jobId']} ended {undo['status']}")
        return
    for row in db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job.job_id)).all():
        if row.status == "APPLIED":
            row.status = "REVERTED"
        elif row.status == "PENDING":
            row.status, row.rejection_reason = "REJECTED", "WAVE_NOT_RUN"
    job.halted_reason, job.halted_detail = "GATE_FAILED", f"{failure}; reverted by job {undo['jobId']}"
    _finish(db, job)


@app.get("/managed-entities/{managed_element_ref}/config")
def read_configuration(managed_element_ref: str, request: Request, managed_function_ref: str | None = None, db: Session = Depends(get_session)):
    """Wave 10.1 (W10-20): read-after-write. Reads the managed object's
    running configuration from its O1 adaptor (NETCONF <get-config>, or a
    RESTCONF GET of the data resource) — the live value on the NF, not what
    this module last asked for — so a caller can verify that a write
    actually took effect.

    PR-SEC-10.5: a caller whose scope claim does not cover the element (or that names one that is not registered) gets 403 `SCOPE_DENIED`, before anything
    is read from the NF; an unscoped caller is not asked anything."""
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])
    _require_msac(db, request, "read", managed_element_ref, managed_function_ref)         # MGT-2.1
    me = db.get(ManagedEntity, managed_element_ref)
    if me is None:      # an element that does not exist is a 404; 503 is for one that is known and cannot be reached (found by the authenticated DAST scan, V-7d)
        raise framework_error(FrameworkError.MANAGED_ENTITY_NOT_FOUND, detail=f"no managed element {managed_element_ref!r}")
    require_service(db, managed_element_ref, "PROV")
    endpoint = db.get(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id) if me.o1_adaptor_endpoint_id else None
    if endpoint is None:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"{managed_element_ref} has no registered O1 adaptor")
    client = _o1_client(me.o1_protocol, endpoint.transport)
    if client is None:
        raise framework_error(FrameworkError.PROTOCOL_NOT_SUPPORTED,
                              detail=f"{managed_element_ref} is provisioned for {me.o1_protocol}, which has no client")
    attributes = client[1](endpoint.adaptor_uri, managed_element_ref, message_id=str(uuid.uuid4()),
                           managed_function_ref=managed_function_ref, **_ssh_options(db, endpoint))
    if attributes is None:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"configuration read on {managed_element_ref} failed")
    return {"managedElementRef": managed_element_ref, "managedFunctionRef": managed_function_ref, "attributes": attributes}


@app.get("/managed-entities/{managed_element_ref}/config-history")
def read_configuration_history(managed_element_ref: str, request: Request, managed_function_ref: str | None = None, limit: int = PageLimit,
                               offset: int = PageOffset, db: Session = Depends(get_session)):
    """MGT-1.4: what each dispatched write to this element replaced and wrote, newest first (`beforeError` says why a before
    image is missing). Read from `cm_snapshot`; a write rejected before dispatch has no row. 403 `SCOPE_DENIED` for an element outside the caller's scope (SEC-10.5)."""
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])
    _require_msac(db, request, "read", managed_element_ref, managed_function_ref)         # MGT-2.1: the history holds the values the element had
    stmt = select(CMSnapshot).where(CMSnapshot.managed_element_ref == managed_element_ref)
    if managed_function_ref:
        stmt = stmt.where(CMSnapshot.managed_function_ref == managed_function_ref)
    page = paginate(db, stmt.order_by(CMSnapshot.created_at.desc(), CMSnapshot.snapshot_id), limit, offset)
    statuses = {sc.id: sc.status for sc in db.scalars(
        select(WriteConfigSubChange).where(WriteConfigSubChange.id.in_([r.sub_change_id for r in page["items"]]))).all()} if page["items"] else {}
    return {**page, "items": [
        {"snapshotId": str(r.snapshot_id), "jobId": str(r.job_id), "subChangeStatus": statuses.get(r.sub_change_id),
         "managedElementRef": r.managed_element_ref, "managedFunctionRef": r.managed_function_ref, "operation": r.operation,
         "before": r.before, "after": r.after, "beforeError": r.before_error, "createdAt": as_utc(r.created_at).isoformat()}
        for r in page["items"]]}


def _snapshot_or_404(db: Session, managed_element_ref: str, snapshot_id: uuid.UUID) -> CMSnapshot:
    row = db.get(CMSnapshot, snapshot_id)
    if row is None or row.managed_element_ref != managed_element_ref:
        raise framework_error(FrameworkError.CM_SNAPSHOT_NOT_FOUND, detail=f"no snapshot {snapshot_id} of {managed_element_ref}")
    return row


def _image(row: CMSnapshot) -> dict:
    """The values of the attributes a snapshot's write touched, as they stood just after it: what was there before, with what the NF
    acknowledged laid over it (a write that was not applied leaves the before image)."""
    image = dict(row.before or {})
    if row.after is not None:
        image.update(row.after)
    return image


@app.get("/managed-entities/{managed_element_ref}/config-history/diff")
def diff_configuration_snapshots(managed_element_ref: str, from_snapshot: uuid.UUID, to_snapshot: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """MGT-1.5: how the attributes two snapshots of one managed object touched differ. Each snapshot's image is `before` with `after` laid
    over it (`_image`); the diff is over the attributes either image holds. Only attributes a write named are ever known, so an attribute
    neither snapshot touched is not reported as unchanged. 403 `SCOPE_DENIED` for an element outside the caller's scope (SEC-10.5)."""
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])
    _require_msac(db, request, "read", managed_element_ref)                                # MGT-2.1: read of the element, whichever function the snapshots are of
    first, second = (_snapshot_or_404(db, managed_element_ref, from_snapshot), _snapshot_or_404(db, managed_element_ref, to_snapshot))
    if first.managed_function_ref != second.managed_function_ref:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"the snapshots are of different managed functions ({first.managed_function_ref!r} and {second.managed_function_ref!r})")
    before_image, after_image = _image(first), _image(second)
    changed = [{"attribute": k, "from": before_image[k], "to": after_image[k]}
               for k in sorted(before_image.keys() & after_image.keys()) if str(before_image[k]) != str(after_image[k])]
    return {"managedElementRef": managed_element_ref, "managedFunctionRef": first.managed_function_ref,
            "fromSnapshot": str(first.snapshot_id), "toSnapshot": str(second.snapshot_id),
            "changed": changed,
            "onlyInFrom": {k: before_image[k] for k in sorted(before_image.keys() - after_image.keys())},
            "onlyInTo": {k: after_image[k] for k in sorted(after_image.keys() - before_image.keys())}}


@app.post("/config-history/purge")
def purge_configuration_history(older_than_days: int | None = None, db: Session = Depends(get_session)):
    """MGT-1.8: delete the snapshots older than `older_than_days` (default `RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS`; 422 when neither is set, so
    a purge never runs with no age). The jobs and their sub-changes stay; a job whose snapshots are gone can no longer be rolled back."""
    days = older_than_days if older_than_days is not None else CM_SNAPSHOT_RETENTION_DAYS
    if days <= 0:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail="older_than_days is required (and positive) unless RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS is set")
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    deleted = cast(CursorResult, db.execute(delete(CMSnapshot).where(CMSnapshot.created_at < cutoff))).rowcount
    db.commit()
    return {"deleted": deleted, "olderThan": cutoff.isoformat()}


# Request body of `POST /config-jobs/{id}/rollback`. `accessScope` defaults to the scope of the job being undone; `force` goes ahead although values changed since the job wrote them.
class RollbackRequest(BaseModel):
    requestedBy: str
    accessScope: str | None = None            # default: the scope of the job being undone
    msacRole: str | None = None
    force: bool = False                       # MGT-1.7: go ahead although values changed since the job wrote them
    dryRun: bool = False


def _job_elements(db: Session, job_id: uuid.UUID) -> list[str]:
    return list(db.scalars(select(WriteConfigSubChange.managed_element_ref).where(WriteConfigSubChange.job_id == job_id).distinct()).all())


def _rollback_plan(db: Session, job: WriteConfigJob, elements: set[str] | None = None) -> tuple[list[dict], dict, list[str]]:
    """(changes that undo the job, in reverse order; the values each target should hold now if nothing touched it since (None: absent);
    problems that make an undo impossible). Only sub-changes that were applied, and only what the snapshots recorded, can be undone. `elements`
    limits the plan to those elements (a revert on a KPI regression undoes only where the KPI regressed)."""
    stmt = (select(CMSnapshot).join(WriteConfigSubChange, WriteConfigSubChange.id == CMSnapshot.sub_change_id)
            .where(CMSnapshot.job_id == job.job_id, WriteConfigSubChange.status == "APPLIED").order_by(CMSnapshot.created_at, CMSnapshot.snapshot_id))
    if elements is not None:
        stmt = stmt.where(CMSnapshot.managed_element_ref.in_(elements))
    rows = db.execute(stmt).scalars().all()
    problems: list[str] = []
    if not rows:
        return [], {}, ["the job applied nothing that has a snapshot (nothing was applied, snapshots are off, or they were purged)"]
    expected: dict[tuple, dict | None] = {}
    undo: list[dict] = []
    for row in rows:
        target = (row.managed_element_ref, row.managed_function_ref)
        label = row.managed_function_ref or row.managed_element_ref
        base = {"managedElementRef": row.managed_element_ref, **({"managedFunctionRef": row.managed_function_ref} if row.managed_function_ref else {})}
        after = row.after or {}
        if row.operation in ("merge", "replace"):
            if row.before is None:
                problems.append(f"{label}: no before image ({row.before_error or 'not recorded'})")
                continue
            absent = sorted(k for k in after if row.before.get(k) is None)       # the before image holds None for an attribute that was not there
            if absent:
                problems.append(f"{label}: {', '.join(absent)} had no value before the write, which cannot be restored")
                continue
            undo.append({**base, "operation": "merge", "attributeChanges": {k: row.before[k] for k in after}})
            expected[target] = {**(expected.get(target) or {}), **after}
        elif row.operation == "create":
            undo.append({**base, "operation": "delete", "attributeChanges": {}})
            expected[target] = {**(expected.get(target) or {}), **after}
        elif row.operation in ("delete", "remove"):
            if not row.before:
                problems.append(f"{label}: the deleted object's values were not recorded")
                continue
            undo.append({**base, "operation": "create", "attributeChanges": dict(row.before)})
            expected[target] = None
        else:
            problems.append(f"{label}: no way to undo a {row.operation!r} write")
    undo.reverse()
    return undo, expected, problems


def _changed_since(db: Session, expected: dict) -> list[dict]:
    """MGT-1.7: read each target now and list what no longer matches what the job left there."""
    found = []
    for (element, function), want in expected.items():
        me = db.get(ManagedEntity, element)
        endpoint = db.get(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id) if me and me.o1_adaptor_endpoint_id else None
        client = _o1_client(me.o1_protocol, endpoint.transport) if me is not None and endpoint is not None else None
        if endpoint is None or client is None:
            found.append({"managedElementRef": element, "managedFunctionRef": function, "attribute": None, "expected": want, "actual": None,
                          "error": "the element has no reachable O1 adaptor to read"})
            continue
        try:
            current = client[1](endpoint.adaptor_uri, element, message_id=str(uuid.uuid4()), managed_function_ref=function, **_ssh_options(db, endpoint))
        except Exception:                                          # noqa: BLE001 - a client bug must not become a silent "unchanged"
            current = None
        if current is None:
            found.append({"managedElementRef": element, "managedFunctionRef": function, "attribute": None, "expected": want, "actual": None,
                          "error": "the current values could not be read"})
        elif want is None:
            if current:
                found.append({"managedElementRef": element, "managedFunctionRef": function, "attribute": None, "expected": None, "actual": current})
        else:
            found.extend({"managedElementRef": element, "managedFunctionRef": function, "attribute": k, "expected": v, "actual": current.get(k)}
                         for k, v in want.items() if str(current.get(k)) != str(v))
    return found


@app.post("/config-jobs/{job_id}/rollback", status_code=202)
@idempotent("ran-nf-oam", status_code=202)
def rollback_configuration_job(job_id: uuid.UUID, body: RollbackRequest, request: Request, db: Session = Depends(get_session)):
    """MGT-1.6: undo a job with a new write job built from its snapshots (reverse order, the recorded before values), which goes through MSAC,
    the schema check and dispatch like any other write: `requestedBy` is the actor and the new job names `rollbackOf`. MGT-1.7: if what the job
    wrote has been changed since, 409 `CONFIG_CHANGED_SINCE` unless `force`; `dryRun` returns the plan and the differences without writing."""
    job = db.get(WriteConfigJob, job_id)
    if job is None or not _job_owned(db, request, job):         # PR-SEC-10.11: another rApp's job is not the caller's to undo, nor to know of: 404, before the scope is asked
        raise framework_error(FrameworkError.CONFIG_JOB_NOT_FOUND, detail=f"no configuration job {job_id}")
    # PR-SEC-10.4: undoing a job writes to every element it wrote to, so every one of them must be inside the caller's scope (a dry run too). The detail names none
    # of them: the caller did not send them.
    if scoping.denied_refs(db, scoping.request_scope(request), _job_elements(db, job_id)):
        error = scoping.scope_denied("the job wrote to managed elements outside the caller's scope")
        caller = invoker_id(request)
        if caller:
            _refuse(db, caller, body.requestedBy, error)
        raise error
    changes, expected, problems = _rollback_plan(db, job)
    if problems:
        raise framework_error(FrameworkError.ROLLBACK_NOT_POSSIBLE, detail="; ".join(problems))
    changed = _changed_since(db, expected)
    if body.dryRun:
        return JSONResponse(status_code=200, content={"dryRun": True, "rollbackOf": str(job_id), "changes": changes, "changedSince": changed,
                                                      "status": "CHANGED_SINCE" if changed else "VALIDATED"})
    if changed and not body.force:
        first = changed[0]
        raise framework_error(FrameworkError.CONFIG_CHANGED_SINCE,
                              detail=f"{len(changed)} value(s) differ from what job {job_id} wrote, for example "
                                     f"{first['managedFunctionRef'] or first['managedElementRef']} {first['attribute']}: expected {first['expected']!r}, "
                                     f"found {first['actual']!r}; send force=true to restore anyway")
    request_body = WriteConfigRequest(requestedBy=body.requestedBy, accessScope=body.accessScope or job.scope, msacRole=body.msacRole, changes=changes)
    result = _execute_write(request_body, db, rollback_of=job_id, rollback_forced=bool(changed), actor=_acting_rapp(request))
    return {**result, "rollbackOf": str(job_id), "forced": bool(changed)}


# Request body of `POST /config-jobs/{id}/kpi-check`; the worker builds the same request from a job's `kpiGuard` (`run_due_kpi_guards`).
class KpiCheckRequest(BaseModel):
    requestedBy: str
    kpi: str
    baselineMinutes: int = Field(default=60, ge=1, le=10080)             # the KPI over this long before the job ran
    observationMinutes: int = Field(default=60, ge=1, le=10080)          # ... and over this long from when it ran
    maxRegressionPercent: float = Field(default=10.0, ge=0)
    direction: Literal["higher", "lower"] = "higher"                      # which way is better: a drop (higher) or a rise (lower) is a regression
    minSamples: int = Field(default=1, ge=1)                              # per window and element: fewer is INSUFFICIENT_DATA, never a verdict
    revert: bool = False
    accessScope: str | None = None
    msacRole: str | None = None
    force: bool = False                                                   # revert although values changed since (MGT-1.7)


@app.post("/config-jobs/{job_id}/kpi-check")
def check_configuration_job_kpi(job_id: uuid.UUID, body: KpiCheckRequest, db: Session = Depends(get_session)):
    """AI-10.5: did a KPI regress where this job wrote? For each element the job applied changes to, the KPI over the window before the job
    (`baselineMinutes`) is compared with the KPI over the window from the job on (`observationMinutes`); a worse result than
    `maxRegressionPercent` is REGRESSED. With `revert`, the regressed elements are rolled back with the rollback of MGT-1.6 (a new job,
    MSAC, the changed-since guard); where the data is too thin the verdict is INSUFFICIENT_DATA and nothing is reverted. This is a check
    to call, by an rApp, the SMO's autonomy or a scheduler, once the observation window has some data. A job that declared a `kpiGuard`
    is checked by the worker without anyone calling this (`run_due_kpi_guards`)."""
    job = db.get(WriteConfigJob, job_id)
    if job is None:
        raise framework_error(FrameworkError.CONFIG_JOB_NOT_FOUND, detail=f"no configuration job {job_id}")
    return _kpi_check(db, job, body)


def _kpi_check(db: Session, job: WriteConfigJob, body: KpiCheckRequest) -> dict:
    """The body of the KPI check (AI-10.5): for each element the job applied changes to, compares the KPI over the baseline window before the job's schema-validation time with the window after it. An element is REGRESSED when it got worse by more than `maxRegressionPercent` in the chosen direction, and INSUFFICIENT_DATA when either window has fewer than `minSamples` or the baseline is zero or undefined; the job verdict is REGRESSED if any element is, else INSUFFICIENT_DATA if any is (or there are no elements), else OK. With `revert` and regressed elements it rolls back only those elements through `_execute_write` (409 CONFIG_CHANGED_SINCE unless `force`, 422 ROLLBACK_NOT_POSSIBLE when no undo can be built). Writes only when it reverts; raises HTTPException from the helpers.
    """
    job_id = job.job_id
    definition = _kpi_or_404(db, body.kpi)
    anchor = as_utc(job.schema_validated_at) if job.schema_validated_at else datetime.datetime.now(datetime.UTC)
    before_window = (anchor - datetime.timedelta(minutes=body.baselineMinutes), anchor)
    after_window = (anchor, anchor + datetime.timedelta(minutes=body.observationMinutes))
    elements = sorted({r.managed_element_ref for r in db.scalars(select(WriteConfigSubChange).where(
        WriteConfigSubChange.job_id == job_id, WriteConfigSubChange.status == "APPLIED")).all()})
    results = []
    for element in elements:
        base = kpi.compute(db, definition, *before_window, "all", element)["items"][0]
        seen = kpi.compute(db, definition, *after_window, "all", element)["items"][0]
        entry = {"managedElementRef": element, "baseline": base["value"], "observed": seen["value"],
                 "baselineSamples": base["samples"], "observedSamples": seen["samples"], "changePercent": None}
        if base["samples"] < body.minSamples or seen["samples"] < body.minSamples or base["value"] is None or seen["value"] is None:
            entry["verdict"], entry["reason"] = "INSUFFICIENT_DATA", "NOT_ENOUGH_SAMPLES_OR_UNDEFINED"
        elif base["value"] == 0:
            entry["verdict"], entry["reason"] = "INSUFFICIENT_DATA", "BASELINE_ZERO"
        else:
            worse = (base["value"] - seen["value"]) if body.direction == "higher" else (seen["value"] - base["value"])
            entry["changePercent"] = round(-100.0 * worse / abs(base["value"]), 4)         # signed like the KPI: negative is a drop
            entry["verdict"] = "REGRESSED" if 100.0 * worse / abs(base["value"]) > body.maxRegressionPercent else "OK"
        results.append(entry)
    regressed = [r["managedElementRef"] for r in results if r["verdict"] == "REGRESSED"]
    verdict = "REGRESSED" if regressed else ("INSUFFICIENT_DATA" if any(r["verdict"] == "INSUFFICIENT_DATA" for r in results) or not results else "OK")
    answer = {"jobId": str(job_id), "kpi": body.kpi, "verdict": verdict, "elements": results, "reverted": False, "revertJobId": None}
    if regressed and body.revert:
        changes, expected, problems = _rollback_plan(db, job, set(regressed))
        if problems:
            raise framework_error(FrameworkError.ROLLBACK_NOT_POSSIBLE, detail="; ".join(problems))
        changed = _changed_since(db, expected)
        if changed and not body.force:
            raise framework_error(FrameworkError.CONFIG_CHANGED_SINCE,
                                  detail=f"{len(changed)} value(s) differ from what job {job_id} wrote; the KPI regressed on {', '.join(regressed)} "
                                         "but the revert would overwrite a later change; send force=true to restore anyway")
        undo = _execute_write(WriteConfigRequest(requestedBy=body.requestedBy, accessScope=body.accessScope or job.scope, msacRole=body.msacRole,
                                                 changes=changes), db, rollback_of=job_id, rollback_forced=bool(changed))
        answer.update(reverted=undo["status"] == "COMPLETED", revertJobId=undo["jobId"], revertStatus=undo["status"])
    return answer


KPI_GUARD_GRACE_MINUTES = int(os.environ.get("RAN_NF_OAM_KPI_GUARD_GRACE_MINUTES", "60") or 0)


def run_due_kpi_guards(db: Session, now: datetime.datetime | None = None) -> list[dict]:
    """What the worker's `run-kpi-guards` task runs: every finished job with a `kpiGuard` whose observation window has passed is checked as
    `POST /config-jobs/{id}/kpi-check` would, with the job's own settings. A final verdict (OK, REGRESSED, or a revert that was refused) is kept
    and the job is not checked again. Too little data (INSUFFICIENT_DATA) or a transient failure is kept as the latest answer and tried again on
    the next run until `RAN_NF_OAM_KPI_GUARD_GRACE_MINUTES` (default 60) after the window; then it is final too, so a KPI that never has data
    does not make the worker look for ever. The revert is never forced."""
    now = now or datetime.datetime.now(datetime.UTC)
    ran = []
    ids = db.scalars(select(WriteConfigJob.job_id).where(
        WriteConfigJob.kpi_guard.is_not(None), WriteConfigJob.kpi_guard_checked_at.is_(None),
        WriteConfigJob.status.in_([JobState.COMPLETED, JobState.PARTIAL_SUCCESS])).order_by(WriteConfigJob.created_at)).all()
    for job_id in ids:
        job = db.get_one(WriteConfigJob, job_id)
        guard = cast(dict, job.kpi_guard)                          # selected above as not null
        anchor = as_utc(job.schema_validated_at) if job.schema_validated_at else as_utc(job.created_at)
        window_ends = anchor + datetime.timedelta(minutes=guard["observationMinutes"])
        if now < window_ends:
            continue
        body = KpiCheckRequest(requestedBy=f"kpi-guard:{job.requested_by}", kpi=guard["kpi"], baselineMinutes=guard["baselineMinutes"],
                               observationMinutes=guard["observationMinutes"], maxRegressionPercent=guard["maxRegressionPercent"],
                               direction=guard["direction"], minSamples=guard["minSamples"], revert=guard["revert"], accessScope=job.scope,
                               msacRole=guard.get("msacRole") or job.msac_role, force=False)
        try:
            result = _kpi_check(db, job, body)
        except HTTPException as exc:                      # the check or the revert was refused (no such KPI, rollback impossible, values changed since)
            db.rollback()
            result = {"verdict": "REGRESSED" if exc.status_code in (409, 422) and guard["revert"] else "ERROR", "reverted": False,
                      "error": str(exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail)[:500]}
        except Exception as exc:                          # noqa: BLE001 (transient: the next run tries again; one job must not stop the others)
            db.rollback()
            result = {"verdict": "ERROR", "reverted": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
        final = result["verdict"] in ("OK", "REGRESSED") or (now >= window_ends + datetime.timedelta(minutes=KPI_GUARD_GRACE_MINUTES))
        job = db.get_one(WriteConfigJob, job_id)
        job.kpi_guard_result = {**result, "checkedAt": now.isoformat()}
        job.kpi_guard_checked_at = now if final else None
        db.commit()
        ran.append({"jobId": str(job_id), "verdict": result["verdict"], "final": final, "reverted": result.get("reverted", False)})
    return ran


# Request body of the continue, halt and abort routes of a staged CM job: who asks, and for `continue` whether to go on although the pause between waves has not elapsed.
class WaveActionRequest(BaseModel):
    requestedBy: str
    force: bool = False                       # continue: go on although the pause has not elapsed


def _halted_job(db: Session, job_id: uuid.UUID, event: JobEvent) -> WriteConfigJob:
    """The job with this id if it is HALTED: 404 CONFIG_JOB_NOT_FOUND for an unknown id, 409 (illegal transition for `event`) for a job in any other state.
    """
    job = db.get(WriteConfigJob, job_id)
    if job is None:
        raise framework_error(FrameworkError.CONFIG_JOB_NOT_FOUND, detail=f"no configuration job {job_id}")
    if job.status != JobState.HALTED:
        raise illegal_transition_error(IllegalTransition(JobState(job.status), event), f"configuration job {job_id}")
    return job


def _resume(db: Session, job: WriteConfigJob) -> dict:
    """Moves a HALTED job back to PROCESSING, clears the halt, and runs the waves from the next one until the job ends or halts again; commits and returns the job summary.
    """
    job.status = WRITE_CONFIG_JOB_FSM.fire(JobState.HALTED, JobEvent.RESUME)
    job.halted_reason = job.halted_detail = job.next_wave_at = None
    db.flush()
    _advance(db, job)
    db.commit()
    return _job_summary(job)


@app.post("/config-jobs/{job_id}/continue", status_code=202)
def continue_configuration_job(job_id: uuid.UUID, body: WaveActionRequest, db: Session = Depends(get_session)):
    """MGT-5.4: run the next wave of a halted job. A job held by its wave pause goes on only once the pause has elapsed, unless `force`; after a
    failed gate or an operator's halt, calling this is the operator's decision to go on."""
    job = _halted_job(db, job_id, JobEvent.RESUME)
    _refuse_if_killed(db, job.invoker_id, body.requestedBy)               # AI-10.4: a stopped rApp's job does not go on to its next wave
    if job.halted_reason == "WAVE_PAUSE" and job.next_wave_at and as_utc(job.next_wave_at) > datetime.datetime.now(datetime.UTC) and not body.force:
        raise framework_error(FrameworkError.WAVE_PAUSE_NOT_ELAPSED,
                              detail=f"the pause between waves ends at {as_utc(job.next_wave_at).isoformat()}; send force=true to go on now")
    log.info("config job %s continued by %s after %s", job_id, body.requestedBy, job.halted_reason)
    return _resume(db, job)


@app.post("/config-jobs/{job_id}/halt")
def halt_configuration_job(job_id: uuid.UUID, body: WaveActionRequest, db: Session = Depends(get_session)):
    """MGT-5.4: stop a job that is waiting between waves from going on by itself (a pause becomes an operator halt). A job already halted for
    another reason stays as it is. A job is only ever between waves while HALTED, so any other state is 409."""
    job = _halted_job(db, job_id, JobEvent.HALT)
    if job.halted_reason == "WAVE_PAUSE":
        job.halted_reason, job.halted_detail, job.next_wave_at = "OPERATOR_HALT", f"halted by {body.requestedBy}", None
        db.commit()
    return _job_summary(job)


@app.post("/config-jobs/{job_id}/abort")
def abort_configuration_job(job_id: uuid.UUID, body: WaveActionRequest, db: Session = Depends(get_session)):
    """MGT-5.4: end a halted job here. The waves that have run stay as they are (undo them with the rollback route); the waves that have not run
    are rejected `WAVE_NOT_RUN`, and the job ends `PARTIAL_SUCCESS` or `FAILED` from what it did."""
    job = _halted_job(db, job_id, JobEvent.AGGREGATE_MIXED)
    for row in db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job_id, WriteConfigSubChange.status == "PENDING")).all():
        row.status, row.rejection_reason = "REJECTED", "WAVE_NOT_RUN"
    db.flush()
    job.halted_detail = f"aborted by {body.requestedBy} ({job.halted_reason})"
    _finish(db, job)
    db.commit()
    return _job_summary(job)


@app.post("/config-jobs/advance-due")
def advance_due_configuration_jobs(db: Session = Depends(get_session)):
    """MGT-5.1: for a scheduler. Runs the next wave of every job whose pause between waves has elapsed; a job halted for any other reason is left
    for an operator. Returns what each advanced job did."""
    return {"advanced": advance_due(db)}


def advance_due(db: Session) -> list[dict]:
    """The body of `advance-due`, also what the worker's `advance-waves` task runs (`app/tasks.py`)."""
    now = datetime.datetime.now(datetime.UTC)
    due = db.scalars(select(WriteConfigJob).where(WriteConfigJob.status == JobState.HALTED, WriteConfigJob.halted_reason == "WAVE_PAUSE",
                                                  WriteConfigJob.next_wave_at <= now).order_by(WriteConfigJob.next_wave_at)).all()
    return [_resume(db, job) for job in due]


# ---------------------------------------------------------------- KPIs (PR-MGT-11)


# One entry of a KPI definition's counter table: the PM counter, the formula variable it feeds (default: the counter name with non-identifier characters replaced by '_') and how its samples are combined.
class KpiCounter(BaseModel):
    counter: str
    variable: str | None = None
    aggregation: Literal["sum", "avg", "min", "max", "last", "count"] = "sum"


# Request body of `PUT /kpi-definitions/{name}`. Without `counters` each variable of the formula is a counter of the same name, summed.
class KpiDefinitionRequest(BaseModel):
    formula: str
    counters: list[KpiCounter] | None = None
    unit: str | None = None
    description: str | None = None


def _kpi_view(row: KpiDefinition) -> dict:
    return {"name": row.name, "formula": row.formula, "counters": row.counters, "unit": row.unit, "description": row.description}


def _kpi_or_404(db: Session, name: str) -> KpiDefinition:
    row = db.get(KpiDefinition, name)
    if row is None:
        raise framework_error(FrameworkError.KPI_NOT_FOUND, detail=f"no KPI {name!r}")
    return row


@app.get("/kpi-definitions/standard")
def standard_kpi_set():
    """MGT-11.6: the KPIs `POST /kpi-definitions/standard` seeds, as they would be defined. They are over the counters this build carries; they are
    not the TS 28.554 definitions (see `kpi.py`). Nothing is written."""
    return {"items": kpi.STANDARD_KPIS}


@app.post("/kpi-definitions/standard")
def seed_standard_kpi_set(db: Session = Depends(get_session)):
    """MGT-11.6: define each standard KPI that is not defined yet; one that is (possibly edited by an operator) is kept as it is. Idempotent.
    Internal-only at R1."""
    created, kept = kpi.seed_standard_kpis(db)
    db.commit()
    return {"created": created, "kept": kept}


@app.put("/kpi-definitions/{name}")
def define_kpi(name: str, body: KpiDefinitionRequest, db: Session = Depends(get_session)):
    """MGT-11.1: create or replace a KPI: a formula over named counters (`kpi_formula.py`: arithmetic, comparisons, a few functions, nothing else)
    and the counter table that says which PM counter feeds which variable and how its samples are combined. Refused (422) when the formula is not
    acceptable or the table does not match it."""
    if name == "standard":
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="'standard' is the name of the seeded set, not of a KPI")
    if not kpi.NAME.match(name):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="a KPI name starts with a letter and uses letters, digits, '_', '.', '-' (64 at most)")
    try:
        table = kpi.normalise_counters(body.formula, [c.model_dump() for c in body.counters] if body.counters else None)
    except kpi_formula.FormulaError as exc:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=str(exc)) from None
    row = db.get(KpiDefinition, name)
    if row is None:
        row = KpiDefinition(name=name)
        db.add(row)
    row.formula, row.counters, row.unit, row.description = body.formula.strip(), table, body.unit, body.description
    db.commit()
    return _kpi_view(row)


@app.get("/kpi-definitions")
def list_kpi_definitions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list ordered by name.
    page = paginate(db, select(KpiDefinition).order_by(KpiDefinition.name), limit, offset)
    return {**page, "items": [_kpi_view(r) for r in page["items"]]}


@app.get("/kpi-definitions/{name}")
def read_kpi_definition(name: str, db: Session = Depends(get_session)):
    # 404 KPI_NOT_FOUND for an unknown name.
    return _kpi_view(_kpi_or_404(db, name))


@app.delete("/kpi-definitions/{name}", status_code=204)
def delete_kpi_definition(name: str, db: Session = Depends(get_session)):
    # 204; 404 KPI_NOT_FOUND for an unknown name. Schedules and job guards that name the KPI are not touched; a schedule then records an ERROR at its next run.
    db.delete(_kpi_or_404(db, name))
    db.commit()
    return Response(status_code=204)


class RAppLimitRequest(BaseModel):
    """What one rApp may do here; at least one of the three. A `PUT` replaces the whole set: a limit not named is removed."""
    maxConfigJobsPerHour: int | None = Field(default=None, ge=1, le=100_000)
    maxElementsPerJob: int | None = Field(default=None, ge=1, le=10_000)
    maxChangePercent: float | None = Field(default=None, gt=0, le=10_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.maxConfigJobsPerHour is None and self.maxElementsPerJob is None and self.maxChangePercent is None:
            raise ValueError("name at least one limit: maxConfigJobsPerHour, maxElementsPerJob or maxChangePercent")
        return self


def _limit_view(row: RAppLimit) -> dict:
    return {"invokerId": row.invoker_id, "maxConfigJobsPerHour": row.max_config_jobs_per_hour, "maxElementsPerJob": row.max_elements_per_job,
            "maxChangePercent": row.max_change_percent, "updatedAt": row.updated_at}


def _limit_or_404(db: Session, invoker: str) -> RAppLimit:
    row = db.get(RAppLimit, invoker)
    if row is None:
        raise framework_error(FrameworkError.RAPP_LIMIT_NOT_FOUND, detail=f"no limit is set for {invoker}")
    return row


def _not_own_limit(request: Request, invoker: str, what: str = "limit") -> None:
    """An rApp may not change its own limit (or its own approval policy). (Any valid token reaches every route behind R1 today; this at least keeps a
    caller from lifting its own cap.)"""
    if invoker_id(request) == invoker:
        raise framework_error(FrameworkError.RAPP_LIMIT_SELF_CHANGE, detail=f"a caller cannot change its own {what}")


@app.put("/rapp-limits/{invoker_id_}")
def set_rapp_limit(invoker_id_: str, body: RAppLimitRequest, request: Request, db: Session = Depends(get_session)):
    """AI-10.1/10.2: set what one rApp (by its OAuth client id) may do here. Called by rApp Management when the instance finishes bootstrapping,
    with the limits its manifest declares."""
    _not_own_limit(request, invoker_id_)
    row = db.get(RAppLimit, invoker_id_)
    if row is None:
        row = RAppLimit(invoker_id=invoker_id_)
        db.add(row)
    row.max_config_jobs_per_hour, row.max_elements_per_job, row.max_change_percent = (
        body.maxConfigJobsPerHour, body.maxElementsPerJob, body.maxChangePercent)
    db.commit()
    return _limit_view(row)


@app.get("/rapp-limits/{invoker_id_}")
def read_rapp_limit(invoker_id_: str, db: Session = Depends(get_session)):
    """The limit set for an rApp, and how many config jobs it has started in the last hour."""
    row = _limit_or_404(db, invoker_id_)
    since = datetime.datetime.now(datetime.UTC) - RATE_WINDOW
    used = db.scalar(select(func.count()).select_from(WriteConfigJob).where(WriteConfigJob.invoker_id == invoker_id_, WriteConfigJob.created_at >= since)) or 0
    return {**_limit_view(row), "configJobsLastHour": used}


@app.delete("/rapp-limits/{invoker_id_}", status_code=204)
def delete_rapp_limit(invoker_id_: str, request: Request, db: Session = Depends(get_session)):
    # 204. 403 RAPP_LIMIT_SELF_CHANGE when the caller is the rApp the limit is for; 404 RAPP_LIMIT_NOT_FOUND when none is set.
    _not_own_limit(request, invoker_id_)
    db.delete(_limit_or_404(db, invoker_id_))
    db.commit()
    return Response(status_code=204)


# Request body of `PUT /rapp-kill/{invoker_id}`: who stops the rApp and an optional reason.
class RAppKillRequest(BaseModel):
    requestedBy: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=500)


def _kill_view(row: RAppKill) -> dict:
    return {"invokerId": row.invoker_id, "killedBy": row.killed_by, "reason": row.reason, "killedAt": row.killed_at}


@app.put("/rapp-kill/{invoker_id_}")
def kill_rapp(invoker_id_: str, body: RAppKillRequest, db: Session = Depends(get_session)):
    """AI-10.4: stop an rApp (by its invoker id): from now its config jobs are refused with 403 `RAPP_KILLED`, and a job of its that waits
    between waves does not go on. Undoing changes is not refused (rollback, revert, halt, abort). Repeating it updates the reason and keeps the time
    of the first. Reversed by `DELETE`."""
    row = db.get(RAppKill, invoker_id_)
    if row is None:
        db.add(RAppKill(invoker_id=invoker_id_, killed_by=body.requestedBy, reason=body.reason))
        log.warning("rApp %s stopped by %s: %s", invoker_id_, body.requestedBy, body.reason)
    else:
        row.reason, row.killed_by = body.reason, body.requestedBy
    db.commit()
    return _kill_view(db.get_one(RAppKill, invoker_id_))


@app.get("/rapp-kill")
def list_killed_rapps(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, most recently stopped first.
    page = paginate(db, select(RAppKill).order_by(RAppKill.killed_at.desc(), RAppKill.invoker_id), limit, offset)
    return {**page, "items": [_kill_view(r) for r in page["items"]]}


@app.get("/rapp-kill/{invoker_id_}")
def read_rapp_kill(invoker_id_: str, db: Session = Depends(get_session)):
    # 404 RAPP_KILL_NOT_FOUND when the rApp is not stopped.
    row = db.get(RAppKill, invoker_id_)
    if row is None:
        raise framework_error(FrameworkError.RAPP_KILL_NOT_FOUND, detail=f"{invoker_id_} is not stopped")
    return _kill_view(row)


@app.delete("/rapp-kill/{invoker_id_}", status_code=204)
def lift_rapp_kill(invoker_id_: str, db: Session = Depends(get_session)):
    """AI-10.4: let a stopped rApp write again."""
    row = db.get(RAppKill, invoker_id_)
    if row is None:
        raise framework_error(FrameworkError.RAPP_KILL_NOT_FOUND, detail=f"{invoker_id_} is not stopped")
    db.delete(row)
    db.commit()
    log.warning("rApp %s allowed to write again", invoker_id_)
    return Response(status_code=204)


# Request body of `POST /safeguard-subscriptions`: the callback and the refusal codes to be told of (empty means all five).
class SafeguardSubscriptionRequest(BaseModel):
    callbackUri: str = Field(min_length=1, max_length=2000)
    refusals: list[Literal["RAPP_KILLED", "RAPP_RATE_LIMITED", "RAPP_BLAST_RADIUS_EXCEEDED", "RAPP_MAGNITUDE_EXCEEDED", "SCOPE_DENIED"]] = []


def _subscription_view(sub: SafeguardSubscription) -> dict:
    return {"subscriptionId": str(sub.subscription_id), "callbackUri": sub.callback_uri, "refusals": sub.refusals or [], "createdAt": sub.created_at}


@app.post("/safeguard-subscriptions", status_code=201)
def subscribe_to_safeguard_refusals(body: SafeguardSubscriptionRequest, db: Session = Depends(get_session)):
    """AI-10.6: be told (a POST to `callbackUri`, through the outbox) each time the platform refuses an rApp (a kill switch, a limit or, PR-SEC-10, a scope): `refusals` narrows it to those codes,
    empty means all five. A destination the SSRF guard refuses is a 422 here rather than a silent drop later."""
    if not is_safe_webhook_destination(body.callbackUri):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="callbackUri is not an acceptable destination")
    sub = SafeguardSubscription(callback_uri=body.callbackUri, refusals=sorted(set(body.refusals)))
    db.add(sub)
    db.commit()
    return _subscription_view(sub)


@app.get("/safeguard-subscriptions")
def list_safeguard_subscriptions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, oldest first.
    page = paginate(db, select(SafeguardSubscription).order_by(SafeguardSubscription.created_at), limit, offset)
    return {**page, "items": [_subscription_view(s) for s in page["items"]]}


@app.delete("/safeguard-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_from_safeguard_refusals(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    # 204; 404 SAFEGUARD_SUBSCRIPTION_NOT_FOUND for an unknown id. Notices already queued in the outbox are not recalled.
    sub = db.get(SafeguardSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.SAFEGUARD_SUBSCRIPTION_NOT_FOUND, detail=f"no subscription {subscription_id}")
    db.delete(sub)
    db.commit()
    return Response(status_code=204)


@app.get("/safeguard-refusals")
def list_safeguard_refusals(invoker_id_: str | None = Query(default=None, alias="invoker_id"), code: str | None = None,
                            since: datetime.datetime | None = None, limit: int = PageLimit, offset: int = PageOffset,
                            db: Session = Depends(get_session)):
    """AI-10.6: the refusals recorded, newest first, whether or not anyone was subscribed. Narrow by rApp (`invoker_id`), `code` and `since`.

    Not narrowed by `region` or `site_cluster` (PR-GUI-9.3): a refusal records the rApp and the code, not the elements of the change it refused,
    so it cannot be placed; the list is fleet-wide."""
    stmt = select(SafeguardRefusal).order_by(SafeguardRefusal.occurred_at.desc(), SafeguardRefusal.refusal_id)
    if invoker_id_:
        stmt = stmt.where(SafeguardRefusal.invoker_id == invoker_id_)
    if code:
        stmt = stmt.where(SafeguardRefusal.code == code)
    if since:
        stmt = stmt.where(SafeguardRefusal.occurred_at >= as_utc(since))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"refusalId": str(r.refusal_id), "occurredAt": r.occurred_at, "invokerId": r.invoker_id, "requestedBy": r.requested_by,
                               "refusal": r.code, "detail": r.detail, "announced": r.notified} for r in page["items"]]}


SAFEGUARD_REFUSAL_RETENTION_DAYS = int(os.environ.get("SAFEGUARD_REFUSAL_RETENTION_DAYS", "0") or 0)


def purge_safeguard_refusals(db: Session, older_than_days: int) -> int:
    """Delete the refusal records older than `older_than_days` days; returns how many. Subscriptions are not touched."""
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=older_than_days)
    deleted = cast(CursorResult, db.execute(delete(SafeguardRefusal).where(SafeguardRefusal.occurred_at < cutoff))).rowcount
    db.commit()
    return deleted


@app.post("/safeguard-refusals/purge")
def purge_refusals(older_than_days: int | None = None, db: Session = Depends(get_session)):
    """MSG-4: delete the refusal records older than `older_than_days` (default `SAFEGUARD_REFUSAL_RETENTION_DAYS`; 422 when neither is set, so a
    purge never runs with no age). The worker runs the same purge daily when the variable is set. Internal-only at R1."""
    days = older_than_days if older_than_days is not None else SAFEGUARD_REFUSAL_RETENTION_DAYS
    if days <= 0:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail="older_than_days is required (and positive) unless SAFEGUARD_REFUSAL_RETENTION_DAYS is set")
    return {"deleted": purge_safeguard_refusals(db, days), "olderThanDays": days}


# ---------------------------------------------------------------- human approval of rApp actions (PR-AI-11) and the decision record (PR-AI-13)

APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "REFUSED")
SYSTEM_DECIDER = "system:timeout"
APPROVAL_ALREADY_GIVEN = ("APPROVAL_ALREADY_GIVEN", 409)       # kept here, not in smo_shared.errors (a mutation-tested module), as lifecycle.py keeps its own codes
APPROVAL_SWEEP_BATCH = 100


class ApprovalPolicyRequest(BaseModel):
    """What happens to the config jobs of one rApp: each waits for a human. `timeoutSeconds` (a minute to a week, default an hour) is how long a request
    may wait; `onTimeout` is what a request nobody decided becomes: `EXPIRE` (the default) or `REJECT` (the platform rejects it). Neither writes anything:
    there is deliberately no option that approves by itself."""
    requestedBy: str = Field(min_length=1)
    timeoutSeconds: int = Field(default=3600, ge=60, le=604_800)
    onTimeout: Literal["EXPIRE", "REJECT"] = "EXPIRE"
    # 1 (the default) is one approval, as before. 2 asks for two different people: the first approval keeps the request waiting, the second makes the job. The
    # requester's own approval never counts, and one person's approval counts once. A rejection by any one person ends the request.
    requiredApprovals: Literal[1, 2] = 1


def _policy_view(row: RAppApprovalPolicy) -> dict:
    view = {"invokerId": row.invoker_id, "timeoutSeconds": row.timeout_seconds, "onTimeout": row.on_timeout, "setBy": row.set_by, "updatedAt": row.updated_at}
    if row.required_approvals > 1:                         # absent means 1: a policy nobody opted into reads exactly as it did
        view["requiredApprovals"] = row.required_approvals
    return view


def _policy_or_404(db: Session, invoker: str) -> RAppApprovalPolicy:
    row = db.get(RAppApprovalPolicy, invoker)
    if row is None:
        raise framework_error(FrameworkError.APPROVAL_POLICY_NOT_FOUND, detail=f"{invoker} has no approval policy: its config jobs are not held for approval")
    return row


@app.put("/rapp-approval-policy/{invoker_id_}")
def set_approval_policy(invoker_id_: str, body: ApprovalPolicyRequest, request: Request, db: Session = Depends(get_session)):
    """AI-11.4: from now on the config jobs of this rApp (by its invoker id) wait for a human to approve them (`/rapp-approvals`). Replaces an earlier
    policy. Jobs already running, rollbacks, reverts and dry runs are not held. Called by rApp Management when an instance created with an approval
    policy finishes bootstrapping; an admin may also set it from the GUI. An rApp cannot set its own."""
    _not_own_limit(request, invoker_id_, "approval policy")
    row = db.get(RAppApprovalPolicy, invoker_id_)
    if row is None:
        row = RAppApprovalPolicy(invoker_id=invoker_id_)
        db.add(row)
    row.timeout_seconds, row.on_timeout, row.set_by = body.timeoutSeconds, body.onTimeout, body.requestedBy
    row.required_approvals = body.requiredApprovals
    db.commit()
    return _policy_view(db.get_one(RAppApprovalPolicy, invoker_id_, populate_existing=True))


@app.get("/rapp-approval-policy/{invoker_id_}")
def read_approval_policy(invoker_id_: str, db: Session = Depends(get_session)):
    # 404 APPROVAL_POLICY_NOT_FOUND when the rApp has none (its jobs are not held).
    return _policy_view(_policy_or_404(db, invoker_id_))


@app.delete("/rapp-approval-policy/{invoker_id_}", status_code=204)
def delete_approval_policy(invoker_id_: str, request: Request, db: Session = Depends(get_session)):
    """The rApp's config jobs are written at once again. Requests already waiting stay waiting (and can still be decided or lapse)."""
    _not_own_limit(request, invoker_id_, "approval policy")
    db.delete(_policy_or_404(db, invoker_id_))
    db.commit()
    return Response(status_code=204)


# Request body of `POST /approval-subscriptions`: the callback that is told of every approval request and lapse.
class ApprovalSubscriptionRequest(BaseModel):
    callbackUri: str = Field(min_length=1, max_length=2000)


def _approval_subscription_view(sub: ApprovalSubscription) -> dict:
    return {"subscriptionId": str(sub.subscription_id), "callbackUri": sub.callback_uri, "createdAt": sub.created_at}


@app.post("/approval-subscriptions", status_code=201)
def subscribe_to_approvals(body: ApprovalSubscriptionRequest, db: Session = Depends(get_session)):
    """AI-11.5: be told (a POST to `callbackUri`, through the outbox) when an rApp action needs a decision (`RAPP_APPROVAL_REQUESTED`) and when a request
    lapsed with nobody deciding (`RAPP_APPROVAL_LAPSED`). A destination the SSRF guard refuses is a 422 here rather than a silent drop later."""
    if not is_safe_webhook_destination(body.callbackUri):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="callbackUri is not an acceptable destination")
    sub = ApprovalSubscription(callback_uri=body.callbackUri)
    db.add(sub)
    db.commit()
    return _approval_subscription_view(sub)


@app.get("/approval-subscriptions")
def list_approval_subscriptions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, oldest first.
    page = paginate(db, select(ApprovalSubscription).order_by(ApprovalSubscription.created_at), limit, offset)
    return {**page, "items": [_approval_subscription_view(s) for s in page["items"]]}


@app.delete("/approval-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_from_approvals(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    # 204; 404 APPROVAL_SUBSCRIPTION_NOT_FOUND for an unknown id. Notices already queued in the outbox are not recalled.
    sub = db.get(ApprovalSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.APPROVAL_SUBSCRIPTION_NOT_FOUND, detail=f"no subscription {subscription_id}")
    db.delete(sub)
    db.commit()
    return Response(status_code=204)


def _notify_approvers(db: Session, event_type: str, row: RAppActionApproval) -> None:
    """AI-11.5: one outbox row per subscriber, in the transaction that parks (or lapses) the request, so a request exists exactly when its notice does."""
    event = {"href": "/ran-nf-oam/rapp-approvals", "eventType": event_type, "approvalId": str(row.approval_id), "invokerId": row.invoker_id,
             "requestedBy": row.requested_by, "status": row.status, "managedElements": row.managed_elements, "changeCount": row.change_count,
             "expiresAt": as_utc(row.expires_at).isoformat(), "occurredAt": datetime.datetime.now(datetime.UTC).isoformat()}
    if row.required_approvals > 1:                         # only a request that needs two people says so: every other notice is what it was
        event["requiredApprovals"], event["approvalsGiven"] = row.required_approvals, len(row.approvals or [])
    for sub in db.scalars(select(ApprovalSubscription)).all():
        enqueue(db, sub.callback_uri, event)


def _park_for_approval(db: Session, body: WriteConfigRequest, invoker: str, policy: RAppApprovalPolicy, requester_scope: authz_scope.Scope | None = None) -> dict:
    """AI-11.4: keep a write that passed every check as a request for a human, and tell the approvers. Nothing is dispatched and no job exists yet."""
    now = datetime.datetime.now(datetime.UTC)
    elements = list(dict.fromkeys(c["managedElementRef"] for c in body.changes))
    row = RAppActionApproval(invoker_id=invoker, requested_by=body.requestedBy, status="PENDING", request=body.model_dump(mode="json"),
                             managed_elements=elements, change_count=len(body.changes), created_at=now,
                             expires_at=now + datetime.timedelta(seconds=policy.timeout_seconds), on_timeout=policy.on_timeout,
                             correlation_id=get_correlation_id(), requester_scope=authz_scope.to_claim(requester_scope),
                             required_approvals=policy.required_approvals)
    db.add(row)
    db.flush()
    _notify_approvers(db, "RAPP_APPROVAL_REQUESTED", row)
    db.commit()
    return {"status": "PENDING_APPROVAL", "jobId": None, "approvalId": str(row.approval_id), "expiresAt": as_utc(row.expires_at).isoformat(),
            "wave": None, "waveCount": None, "haltedReason": None}


def _approval_view(row: RAppActionApproval, detail: bool = False) -> dict:
    """The JSON view of an approval request. With `detail` it also carries the changes and access scope that were asked for (the list leaves them out). `approvals` is the votes so far (`_approvals_given`).
    """
    view = {"approvalId": str(row.approval_id), "invokerId": row.invoker_id, "requestedBy": row.requested_by, "status": row.status,
            "managedElements": row.managed_elements, "changeCount": row.change_count, "createdAt": as_utc(row.created_at).isoformat(),
            "expiresAt": as_utc(row.expires_at).isoformat(), "onTimeout": row.on_timeout, "decidedBy": row.decided_by,
            "decidedAt": as_utc(row.decided_at).isoformat() if row.decided_at else None, "decisionReason": row.decision_reason,
            "jobId": str(row.job_id) if row.job_id else None, "refusalCode": row.refusal_code, "correlationId": row.correlation_id,
            "decision": (row.request or {}).get("decision"), "requiredApprovals": row.required_approvals, "approvals": _approvals_given(row)}
    if detail:
        view["changes"] = (row.request or {}).get("changes", [])
        view["accessScope"] = (row.request or {}).get("accessScope")
    return view


def _approvals_given(row: RAppActionApproval) -> list[dict]:
    """The approvals so far, in the order given. A request that needs one approval keeps its approver in `decided_by`; once approved it reads as that one."""
    if row.approvals:
        return list(row.approvals)
    if row.status == "APPROVED" and row.decided_by:
        return [{"by": row.decided_by, "at": as_utc(row.decided_at).isoformat() if row.decided_at else None, "reason": row.decision_reason}]
    return []


def _same_person(a: str, b: str) -> bool:
    """Two decider names that differ only in case or surrounding space are one person (`smo-gui:Alice` is `smo-gui:alice `): an approver cannot count twice by typing."""
    return a.strip().casefold() == b.strip().casefold()


def _vote(body: "ApprovalDecisionRequest", now: datetime.datetime) -> dict:
    return {"by": body.decidedBy, "at": now.isoformat(), "reason": body.reason}


def _approval_or_404(db: Session, approval_id: uuid.UUID, lock: bool = False) -> RAppActionApproval:
    """The approval request by id, reloaded from the database (not from a cached identity), or 404 APPROVAL_NOT_FOUND. With `lock` the row is selected FOR UPDATE, which the decision routes use so two deciders (or a decider and the timeout sweep) cannot act on it at once.
    """
    stmt = select(RAppActionApproval).where(RAppActionApproval.approval_id == approval_id).execution_options(populate_existing=True)
    row = db.scalars(stmt.with_for_update() if lock else stmt).one_or_none()
    if row is None:
        raise framework_error(FrameworkError.APPROVAL_NOT_FOUND, detail=f"no approval request {approval_id}")
    return row


def _lapse_if_due(db: Session, row: RAppActionApproval, now: datetime.datetime | None = None) -> bool:
    """AI-11.3: a PENDING request past `expires_at` becomes EXPIRED or REJECTED (the policy it was parked under), decided by `system:timeout`, with a
    decision record and a notice to the approvers. Flushes, does not commit. True when it lapsed."""
    now = now or datetime.datetime.now(datetime.UTC)
    if row.status != "PENDING" or as_utc(row.expires_at) > now:
        return False
    row.status = "REJECTED" if row.on_timeout == "REJECT" else "EXPIRED"
    row.decided_by, row.decided_at = SYSTEM_DECIDER, now
    row.decision_reason = f"nobody decided before {as_utc(row.expires_at).isoformat()}"
    _record_decision(db, row.invoker_id, WriteConfigRequest.model_validate(row.request), row.status, approval=row)
    _notify_approvers(db, "RAPP_APPROVAL_LAPSED", row)
    db.flush()
    return True


def lapse_due_approvals(db: Session) -> list[str]:
    """The body of `POST /rapp-approvals/expire-due`, also what the worker's `expire-approvals` task runs and what a list runs first, so a timeout holds
    whether or not a scheduler is running. Each request is lapsed and committed on its own (a row another replica holds is skipped)."""
    now = datetime.datetime.now(datetime.UTC)
    ids = list(db.scalars(select(RAppActionApproval.approval_id).where(RAppActionApproval.status == "PENDING", RAppActionApproval.expires_at <= now)
                          .order_by(RAppActionApproval.expires_at).limit(APPROVAL_SWEEP_BATCH)).all())
    lapsed = []
    for approval_id in ids:
        row = db.scalars(select(RAppActionApproval).where(RAppActionApproval.approval_id == approval_id).with_for_update(skip_locked=True)
                         .execution_options(populate_existing=True)).one_or_none()
        if row is not None and _lapse_if_due(db, row, now):
            db.commit()
            lapsed.append(str(approval_id))
        else:
            db.rollback()
    if lapsed:
        _chain_decisions_quietly(db)
    return lapsed


@app.post("/rapp-approvals/expire-due")
def expire_due_approvals(db: Session = Depends(get_session)):
    """AI-11.3: for a scheduler. Lapses every pending request whose time has passed (EXPIRED or REJECTED, by the policy it was parked under). Internal-only at R1."""
    return {"lapsed": lapse_due_approvals(db)}


@app.get("/rapp-approvals")
def list_approvals(status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "REFUSED"] | None = None,
                   invoker_id_: str | None = Query(default=None, alias="invoker_id"), since: datetime.datetime | None = None,
                   region: str | None = scoping.RegionFilter, site_cluster: str | None = scoping.SiteClusterFilter,
                   limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """AI-11.2: the approval queue, newest first. `status=PENDING` is the inbox. Requests whose time has passed are lapsed first. Names other rApps'
    actions: internal-only at R1 (an rApp reads its own request by id). PR-GUI-9.3: `region` and `site_cluster` keep the requests whose change
    touches at least one element of that place (`managedElements`, recorded when the request was parked)."""
    lapse_due_approvals(db)
    stmt = select(RAppActionApproval).order_by(RAppActionApproval.created_at.desc(), RAppActionApproval.approval_id)
    if status:
        stmt = stmt.where(RAppActionApproval.status == status)
    if invoker_id_:
        stmt = stmt.where(RAppActionApproval.invoker_id == invoker_id_)
    if since:
        stmt = stmt.where(RAppActionApproval.created_at >= as_utc(since))
    in_place = scoping.json_refs_in_place(db, RAppActionApproval.managed_elements, region, site_cluster)
    if in_place is not None:
        stmt = stmt.where(in_place)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_approval_view(r) for r in page["items"]]}


@app.get("/rapp-approvals/{approval_id}")
def read_approval(approval_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """One request with the changes it asks for. The rApp that made it polls this for the outcome (`jobId` once approved). PR-SEC-10.11: an rApp with a scope claim reads
    only its own."""
    row = _approval_or_404(db, approval_id)
    owner = _job_owner_filter(request)
    if owner is not False and row.invoker_id != owner:           # PR-SEC-10.11: another rApp's request is not the caller's to read: 404, as if it did not exist
        raise framework_error(FrameworkError.APPROVAL_NOT_FOUND, detail=f"no approval request {approval_id}")
    if _lapse_if_due(db, row):
        db.commit()
        _chain_decisions_quietly(db)
        row = _approval_or_404(db, approval_id)
    return _approval_view(row, detail=True)


# Request body of the approve and reject routes: who decides (as the decider names themselves) and an optional reason. A decider equal to the requester is refused.
class ApprovalDecisionRequest(BaseModel):
    decidedBy: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


def _decidable(db: Session, approval_id: uuid.UUID, request: Request, body: ApprovalDecisionRequest) -> RAppActionApproval:
    """The request to decide, locked: 404, 403 for an rApp (it can never decide, whatever it is deciding) or for the requester itself, and 409 when it is
    no longer pending (decided, or lapsed just now)."""
    if role_of(request) == ROLE_RAPP:
        raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail="an rApp cannot decide an approval request")
    row = _approval_or_404(db, approval_id, lock=True)
    if body.decidedBy in (row.invoker_id, row.requested_by) or invoker_id(request) == row.invoker_id or (
            row.required_approvals > 1 and (_same_person(body.decidedBy, row.invoker_id) or _same_person(body.decidedBy, row.requested_by))):
        raise framework_error(FrameworkError.APPROVAL_SELF_DECISION, detail="the requester of an action cannot decide it")
    if _lapse_if_due(db, row):
        db.commit()
        _chain_decisions_quietly(db)
        row = _approval_or_404(db, approval_id)
    if row.status != "PENDING":
        raise framework_error(FrameworkError.APPROVAL_NOT_PENDING, detail=f"the request is {row.status}"
                              + (f" ({row.decision_reason})" if row.decision_reason else ""))
    return row


@app.post("/rapp-approvals/{approval_id}/approve")
def approve_action(approval_id: uuid.UUID, body: ApprovalDecisionRequest, request: Request, db: Session = Depends(get_session)):
    """AI-11.2: approve a waiting request. The safeguards are checked again now (a kill switch thrown while it waited refuses it: 403 `RAPP_KILLED`, the
    request is closed REFUSED), then MSAC, the schema check and the dispatch run as for any write, and the job is made from the request as the rApp
    sent it. 200 with the request (now APPROVED, `jobId`) and the job's status. 404, 403 (an rApp, or the requester), 409 (not pending).

    A request parked under a policy of `requiredApprovals: 2` needs two different people: the first approval is recorded and the request stays PENDING (200,
    `jobStatus` null, the approvals so far in `approvals`), a second approval by the same person is 409 `APPROVAL_ALREADY_GIVEN`, and the second person's approval
    runs everything above. The requester's own approval never counts (403 `APPROVAL_SELF_DECISION`). The first approval checks nothing and writes nothing."""
    row = _decidable(db, approval_id, request, body)
    now = datetime.datetime.now(datetime.UTC)
    if row.required_approvals > 1:
        given = list(row.approvals or [])
        if any(_same_person(v["by"], body.decidedBy) for v in given):
            raise framework_error(APPROVAL_ALREADY_GIVEN, detail=f"{body.decidedBy} has already approved this request: a different person must give the other approval")
        if len(given) + 1 < row.required_approvals:
            row.approvals = [*given, _vote(body, now)]
            db.commit()
            return {**_approval_view(_approval_or_404(db, approval_id), detail=True), "jobStatus": None}
        row.approvals = [*given, _vote(body, now)]         # the last approval: kept with the job's transaction, and re-added by `_close_refused` if the checks refuse
    try:
        _refuse_if_killed(db, row.invoker_id, row.requested_by)
        _enforce_rapp_limit(db, row.invoker_id, row.requested_by)
        replay = WriteConfigRequest.model_validate(row.request)
        # PR-SEC-10.4: the requester's claim as it was when the request was parked, against the targets as they are now (an element moved to another region or
        # tenant while the request waited is refused: the request is closed REFUSED with code SCOPE_DENIED)
        _enforce_scope(db, authz_scope.from_introspection(row.requester_scope), row.invoker_id, row.requested_by, [c["managedElementRef"] for c in replay.changes])
        _enforce_change_limits(db, replay, row.invoker_id)
        row.decided_by, row.decided_at, row.decision_reason = body.decidedBy, datetime.datetime.now(datetime.UTC), body.reason
        result = _execute_write(replay, db, invoker=row.invoker_id, actor=row.invoker_id, approval=row)
    except HTTPException as exc:
        db.rollback()                                       # a refusal committed its own record; the rest of this attempt is undone
        _close_refused(db, approval_id, body, exc)
        raise
    return {**_approval_view(_approval_or_404(db, approval_id), detail=True), "jobStatus": result["status"]}


def _close_refused(db: Session, approval_id: uuid.UUID, body: ApprovalDecisionRequest, exc: HTTPException) -> None:
    """The request was approved but refused when it was run (a safeguard, MSAC, the schema check): it is closed REFUSED with the code, so it does not
    wait for a decision that cannot succeed. The approver sees the refusal as the answer to the approve call."""
    row = _approval_or_404(db, approval_id, lock=True)
    if row.status != "PENDING":
        return
    detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
    row.status, row.refusal_code = "REFUSED", str(detail.get("title") or "REFUSED")
    row.decided_by, row.decided_at, row.decision_reason = body.decidedBy, datetime.datetime.now(datetime.UTC), body.reason
    if row.required_approvals > 1 and not any(_same_person(v["by"], body.decidedBy) for v in row.approvals or []):
        # the approval was given and the request was refused after it. A refusal that committed its own record (a safeguard's) took the vote with it; one that did not
        # (MSAC, the schema) was rolled back with it: either way it is in the list once
        row.approvals = [*(row.approvals or []), _vote(body, row.decided_at)]
    _record_decision(db, row.invoker_id, WriteConfigRequest.model_validate(row.request), "REFUSED", approval=row)
    db.commit()
    _chain_decisions_quietly(db)


@app.post("/rapp-approvals/{approval_id}/reject")
def reject_action(approval_id: uuid.UUID, body: ApprovalDecisionRequest, request: Request, db: Session = Depends(get_session)):
    """AI-11.2: reject a waiting request. Nothing is written; the rApp reads the outcome (`status` REJECTED, the reason) from the request. 404, 403, 409 as for approve."""
    row = _decidable(db, approval_id, request, body)
    row.status, row.decided_by, row.decided_at, row.decision_reason = "REJECTED", body.decidedBy, datetime.datetime.now(datetime.UTC), body.reason
    _record_decision(db, row.invoker_id, WriteConfigRequest.model_validate(row.request), "REJECTED", approval=row)
    db.commit()
    _chain_decisions_quietly(db)
    return _approval_view(_approval_or_404(db, approval_id), detail=True)


# ---- the decision record (PR-AI-13)

def _stamp(moment: datetime.datetime | None) -> str | None:
    return as_utc(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if moment else None


def _decision_hash(rec: RAppDecisionRecord) -> str:
    """SHA-256 over every field of the record but `audit_seq`, as canonical JSON: what is written to the audit chain and compared when the record is read."""
    body = {"decision_id": str(rec.decision_id), "occurred_at": _stamp(rec.occurred_at), "invoker_id": rec.invoker_id, "requested_by": rec.requested_by,
            "disposition": rec.disposition, "job_id": str(rec.job_id) if rec.job_id else None, "approval_id": str(rec.approval_id) if rec.approval_id else None,
            "action_id": rec.action_id, "inputs_ref": rec.inputs_ref, "model_version": rec.model_version, "rationale": rec.rationale,
            "decided_by": rec.decided_by, "decided_at": _stamp(rec.decided_at), "managed_elements": rec.managed_elements,
            "change_count": rec.change_count, "correlation_id": rec.correlation_id}
    if rec.approvers is not None:                          # only a two-person record: every other record hashes as it always did
        body["approvers"] = rec.approvers
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _record_decision(db: Session, invoker: str, body: WriteConfigRequest, disposition: str, job_id: uuid.UUID | None = None,
                     approval: RAppActionApproval | None = None) -> RAppDecisionRecord:
    """AI-13.2: the record of one rApp action, in the transaction that makes (or ends) it: `DIRECT` job (no approval was needed), `APPROVED` job,
    `ROLLBACK` job, or an approval that ended with no job (`REJECTED`, `EXPIRED`, `REFUSED`). Flushed, not committed; it is chained after the commit."""
    context = body.decision or DecisionContext()
    rec = RAppDecisionRecord(
        decision_id=uuid.uuid4(), occurred_at=datetime.datetime.now(datetime.UTC), invoker_id=invoker, requested_by=body.requestedBy, disposition=disposition,
        job_id=job_id, approval_id=approval.approval_id if approval is not None else None, action_id=context.actionId, inputs_ref=context.inputsRef,
        model_version=context.modelVersion, rationale=context.rationale, decided_by=approval.decided_by if approval is not None else None,
        decided_at=approval.decided_at if approval is not None else None,
        managed_elements=list(dict.fromkeys(c["managedElementRef"] for c in body.changes)), change_count=len(body.changes), correlation_id=get_correlation_id(),
        approvers=[v["by"] for v in approval.approvals or []] if approval is not None and approval.required_approvals > 1 else None)
    rec.content_hash = _decision_hash(rec)
    db.add(rec)
    db.flush()
    return rec


def chain_decisions(db: Session) -> int:
    """AI-13.1: write the hash of each decision record not yet chained to the shared audit chain (`smo_shared.audit`: who, what, the record's id and
    `contentHash`), one row and one commit each so the chain's head is locked briefly. Done a moment after the commit that made the record, not in it:
    that transaction can run for a minute (a dispatch) and would hold the head against every other writer. A record whose chain write failed is picked up by
    the worker's `chain-decisions` task. Returns how many were chained."""
    done = 0
    for _ in range(APPROVAL_SWEEP_BATCH):
        # one record at a time, selected and locked afresh: after a commit another replica may have taken the next one, and a skipped (locked) row is theirs
        rec = db.scalars(select(RAppDecisionRecord).where(RAppDecisionRecord.audit_seq.is_(None)).order_by(RAppDecisionRecord.occurred_at)
                         .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True)).first()
        if rec is None:
            break
        entry = audit.record(db, actor=rec.invoker_id, role=ROLE_RAPP, action="DECISION", target=f"/ran-nf-oam/decision-records/{rec.decision_id}",
                             result=rec.disposition, correlation_id=rec.correlation_id,
                             detail={"decisionId": str(rec.decision_id), "jobId": str(rec.job_id) if rec.job_id else None, "contentHash": rec.content_hash})
        rec.audit_seq = entry.seq
        db.commit()
        done += 1
    db.rollback()                                          # ends the read that found nothing
    return done


def _chain_decisions_quietly(db: Session) -> None:
    """After a commit: chain what that commit recorded. A failure is logged and left to the worker; it never fails the request that has been served."""
    try:
        chain_decisions(db)
    except Exception:                                      # noqa: BLE001 — the request's own outcome is already committed
        log.exception("decision records could not be chained to the audit log; the worker will retry")
        db.rollback()


def _integrity(db: Session, rec: RAppDecisionRecord) -> dict:
    """Whether this record is what was written: its fields still hash to `content_hash`, and the audit row it names carries that same hash for this record.
    It does not walk the chain (`python -m smo_shared.audit verify` does); a record whose chain write has not happened yet is UNCHAINED."""
    if _decision_hash(rec) != rec.content_hash:
        return {"status": "MISMATCH", "reason": "the record no longer hashes to the value written when it was made"}
    if rec.audit_seq is None:
        return {"status": "UNCHAINED", "reason": "not yet written to the audit chain"}
    entry = db.execute(select(audit.AuditEntry.detail, audit.AuditEntry.hash).where(audit.AuditEntry.seq == rec.audit_seq)).first()
    carried = (entry.detail or {}) if entry else {}
    if entry is None or carried.get("contentHash") != rec.content_hash or carried.get("decisionId") != str(rec.decision_id):
        return {"status": "MISMATCH", "reason": f"audit row {rec.audit_seq} does not carry this record's hash"}
    return {"status": "VERIFIED", "auditSeq": rec.audit_seq, "auditHash": entry.hash}


def _decision_view(rec: RAppDecisionRecord, integrity: dict | None = None) -> dict:
    """The JSON view of a decision record; `integrity` (from `_integrity`) is included only for a read of one record."""
    view: dict[str, Any] = {"decisionId": str(rec.decision_id), "occurredAt": _stamp(rec.occurred_at), "invokerId": rec.invoker_id, "requestedBy": rec.requested_by,
            "disposition": rec.disposition, "jobId": str(rec.job_id) if rec.job_id else None, "approvalId": str(rec.approval_id) if rec.approval_id else None,
            "actionId": rec.action_id, "inputsRef": rec.inputs_ref, "modelVersion": rec.model_version, "rationale": rec.rationale,
            "approvedBy": rec.decided_by if rec.disposition == "APPROVED" else None, "decidedBy": rec.decided_by, "decidedAt": _stamp(rec.decided_at),
            "managedElements": rec.managed_elements, "changeCount": rec.change_count, "correlationId": rec.correlation_id,
            "contentHash": rec.content_hash, "auditSeq": rec.audit_seq, "approvers": rec.approvers}
    if integrity is not None:
        view["integrity"] = integrity
    return view


@app.get("/decision-records")
def list_decision_records(invoker_id_: str | None = Query(default=None, alias="invoker_id"), job_id: uuid.UUID | None = None,
                          approval_id: uuid.UUID | None = None, disposition: Literal["DIRECT", "APPROVED", "ROLLBACK", "REJECTED", "EXPIRED", "REFUSED"] | None = None,
                          model_version: str | None = None, since: datetime.datetime | None = None, until: datetime.datetime | None = None,
                          after: str | None = Query(None, description=AFTER_DESCRIPTION),
                          region: str | None = scoping.RegionFilter, site_cluster: str | None = scoping.SiteClusterFilter,
                          limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """AI-13.3: why rApps acted, newest first: one record per config job an rApp made (and per approval that ended with none). Narrow by rApp (`invoker_id`),
    `job_id`, `approval_id`, `disposition`, `model_version` and the time (`since` inclusive, `until` exclusive). `?total=false` skips the count. Names other rApps'
    actions: internal-only at R1 (an rApp reads a record by id). PR-GUI-9.5: `after` pages by keyset in the same order (occurredAt newest first, decisionId):
    the answer is then `{items, limit, nextCursor, hasMore}`. PR-GUI-9.3: `region` and `site_cluster` keep the records whose `managedElements` name at
    least one element of that place (any element matches; a record with no element never does)."""
    stmt = _decision_filters(select(RAppDecisionRecord), invoker_id_=invoker_id_, job_id=job_id, approval_id=approval_id, disposition=disposition,
                             model_version=model_version, since=since, until=until, db=db, region=region, site_cluster=site_cluster)
    if after is not None:
        if after:
            t, i = alarm_query.decode_cursor("d", after, 2)
            t, i = alarm_query.parse_time(t), alarm_query.parse_uuid(i)
            stmt = stmt.where(or_(RAppDecisionRecord.occurred_at < t, and_(RAppDecisionRecord.occurred_at == t, RAppDecisionRecord.decision_id > i)))
        rows = db.scalars(stmt.order_by(RAppDecisionRecord.occurred_at.desc(), RAppDecisionRecord.decision_id).limit(int(limit) + 1)).all()
        page_rows = rows[:int(limit)]
        next_cursor = (alarm_query.encode_cursor("d", page_rows[-1].occurred_at, page_rows[-1].decision_id)
                       if len(rows) > int(limit) and page_rows else None)
        return {"items": [_decision_view(r) for r in page_rows], "limit": int(limit), "nextCursor": next_cursor, "hasMore": next_cursor is not None}
    page = paginate(db, stmt.order_by(RAppDecisionRecord.occurred_at.desc(), RAppDecisionRecord.decision_id), limit, offset)
    return {**page, "items": [_decision_view(r) for r in page["items"]]}


def _decision_filters(stmt, *, invoker_id_: str | None = None, job_id: uuid.UUID | None = None, approval_id: uuid.UUID | None = None,
                      disposition: str | None = None, model_version: str | None = None, since: datetime.datetime | None = None,
                      until: datetime.datetime | None = None, db: Session | None = None, region: str | None = None, site_cluster: str | None = None):
    """`stmt` (a select over `rapp_decision_record`) narrowed by the filters of `GET /decision-records`; `None` leaves one out. `since` inclusive, `until` exclusive.
    `region` and `site_cluster` (PR-GUI-9.3) keep the records touching an element of that place and need `db` (the SQL dialect of the JSON expansion)."""
    for column, value in ((RAppDecisionRecord.invoker_id, invoker_id_), (RAppDecisionRecord.job_id, job_id), (RAppDecisionRecord.approval_id, approval_id),
                          (RAppDecisionRecord.disposition, disposition), (RAppDecisionRecord.model_version, model_version)):
        if value is not None:
            stmt = stmt.where(column == value)
    if since:
        stmt = stmt.where(RAppDecisionRecord.occurred_at >= as_utc(since))
    if until:
        stmt = stmt.where(RAppDecisionRecord.occurred_at < as_utc(until))
    if db is not None:
        in_place = scoping.json_refs_in_place(db, RAppDecisionRecord.managed_elements, region, site_cluster)
        if in_place is not None:
            stmt = stmt.where(in_place)
    return stmt


DECISION_EXPORT_MAX_DAYS = 31
DECISION_EXPORT_MAX_ROWS = 1_000_000
DECISION_EXPORT_BATCH = 1000
DECISION_CSV_COLUMNS = ("decisionId", "occurredAt", "invokerId", "requestedBy", "disposition", "jobId", "approvalId", "actionId", "inputsRef",
                        "modelVersion", "rationale", "decidedBy", "decidedAt", "managedElements", "changeCount", "correlationId", "contentHash", "auditSeq")


def _csv_cell(value) -> str:
    """One CSV cell: `None` empty, a list joined by `;`, and a text that a spreadsheet would run as a formula (`= + - @`, tab, CR first) prefixed with `'`.

    The record's text fields (rationale, inputs, action id) are written by rApps: opened in a spreadsheet, `=HYPERLINK(...)` would otherwise be live."""
    if value is None:
        return ""
    if isinstance(value, list):
        value = ";".join(str(v) for v in value)
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _decision_csv_rows(db: Session, stmt):
    """The CSV text of the records `stmt` selects, oldest first, in chunks of `DECISION_EXPORT_BATCH` rows read by keyset (never one huge result held
    in memory, never an open server-side cursor across the response), stopping after `DECISION_EXPORT_MAX_ROWS`. A generator for a streamed response."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(DECISION_CSV_COLUMNS)
    sent, last = 0, None
    while sent < DECISION_EXPORT_MAX_ROWS:
        page = stmt
        if last is not None:
            page = page.where(or_(RAppDecisionRecord.occurred_at > last[0], and_(RAppDecisionRecord.occurred_at == last[0], RAppDecisionRecord.decision_id > last[1])))
        rows = db.scalars(page.order_by(RAppDecisionRecord.occurred_at, RAppDecisionRecord.decision_id)
                          .limit(min(DECISION_EXPORT_BATCH, DECISION_EXPORT_MAX_ROWS - sent))).all()
        for rec in rows:
            view = _decision_view(rec)
            writer.writerow([_csv_cell(view.get(column)) for column in DECISION_CSV_COLUMNS])
        sent += len(rows)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        if len(rows) < DECISION_EXPORT_BATCH:
            return
        last = (rows[-1].occurred_at, rows[-1].decision_id)


@app.get("/decision-records/export.csv", response_class=StreamingResponse, responses={200: {"content": {"text/csv": {}}, "description": "The records as CSV"}})
def export_decision_records(since: datetime.datetime, until: datetime.datetime | None = None, invoker_id_: str | None = Query(default=None, alias="invoker_id"),
                            disposition: Literal["DIRECT", "APPROVED", "ROLLBACK", "REJECTED", "EXPIRED", "REFUSED"] | None = None,
                            region: str | None = scoping.RegionFilter, site_cluster: str | None = scoping.SiteClusterFilter,
                            db: Session = Depends(get_session)):
    """PR-GUI-9.5: the decision records of a time span as a streamed CSV file (`Content-Disposition: attachment`), oldest first, one header row then one
    row per record (the fields of `GET /decision-records`; `managedElements` joined by `;`). `since` (inclusive) is required, `until` (exclusive)
    defaults to now, and the span is at most 31 days (422 otherwise); at most 1,000,000 rows. `invoker_id`, `disposition`, `region` and `site_cluster`
    (as on the list) narrow it. Internal-only at R1, like the list."""
    start, end = as_utc(since), as_utc(until) if until else datetime.datetime.now(datetime.UTC)
    if end <= start:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="`until` must be after `since`")
    if end - start > datetime.timedelta(days=DECISION_EXPORT_MAX_DAYS):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"the span from `since` to `until` is at most {DECISION_EXPORT_MAX_DAYS} days")
    stmt = _decision_filters(select(RAppDecisionRecord), invoker_id_=invoker_id_, disposition=disposition, since=start, until=end,
                             db=db, region=region, site_cluster=site_cluster)
    name = f"decision-records-{start:%Y%m%dT%H%M%SZ}-{end:%Y%m%dT%H%M%SZ}.csv"
    return StreamingResponse(_decision_csv_rows(db, stmt), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/decision-records/{decision_id}")
def read_decision_record(decision_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """AI-13.3: one record with its `integrity` (VERIFIED, UNCHAINED while the chain write is pending, or MISMATCH when the record or its audit row was changed).
    PR-SEC-10.11: an rApp with a scope claim reads only its own (404 for another's)."""
    rec = db.scalars(select(RAppDecisionRecord).where(RAppDecisionRecord.decision_id == decision_id).execution_options(populate_existing=True)).one_or_none()
    owner = _job_owner_filter(request)
    if rec is None or (owner is not False and rec.invoker_id != owner):
        raise framework_error(FrameworkError.DECISION_RECORD_NOT_FOUND, detail=f"no decision record {decision_id}")
    return _decision_view(rec, _integrity(db, rec))


@app.get("/kpis/{name}")
def compute_kpi(name: str, from_time: datetime.datetime, request: Request, to_time: datetime.datetime | None = None, group_by: Literal[
                "cell", "element", "sectorGroup", "incidentZone", "all"] = "cell", managed_element_ref: str | None = None,
                cell_id: str | None = None, db: Session = Depends(get_session)):
    """MGT-11.3/11.4/11.5: the KPI over [from_time, to_time) (to_time: now), per cell, per element, per sector group or incident zone (the cell
    guards of the registry), or over everything asked for. A ratio is computed from the group's summed counters, not from its cells' ratios.
    `managed_element_ref` and `cell_id` narrow what is read. A group without data has a null `value` and a `reason`.
    PR-SEC-10.6: a caller with a scope claim gets the KPI over the performance files of the elements inside it only (so `all` is its elements, never the network's).
    MGT-2.6: with the MSAC switch on, a caller that is a registered Identity gets it over the elements its access rules let it `read` only."""
    definition = _kpi_or_404(db, name)
    start, end = _kpi_window(from_time, to_time)
    return kpi.compute(db, definition, start, end, group_by, managed_element_ref, cell_id, scoping.request_scope(request),
                       _unreadable_elements(db, request, PMFile.managed_element_ref))


def _kpi_window(from_time: datetime.datetime, to_time: datetime.datetime | None) -> tuple[datetime.datetime, datetime.datetime]:
    """The [from, to) window of a KPI request as timezone-aware datetimes (naive input is taken as UTC, a missing `to` is now); 422 SCHEMA_VALIDATION_FAILED when the window is empty or reversed.
    """
    start = from_time if from_time.tzinfo else from_time.replace(tzinfo=datetime.UTC)
    end = (to_time if to_time is None or to_time.tzinfo else to_time.replace(tzinfo=datetime.UTC)) or datetime.datetime.now(datetime.UTC)
    if end <= start:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="to_time must be after from_time")
    return start, end


KPI_RESULT_SCHEMA = {"type": "object", "properties": {
    "kpi": {"type": "string"}, "unit": {"type": ["string", "null"]}, "groupBy": {"type": "string"}, "group": {"type": "object"},
    "value": {"type": ["number", "null"]}, "samples": {"type": "integer"}, "reason": {"type": ["string", "null"]},
    "windowStart": {"type": "string", "format": "date-time"}, "windowEnd": {"type": "string", "format": "date-time"}}}


@app.post("/kpis/{name}/publish")
def publish_kpi(name: str, from_time: datetime.datetime, to_time: datetime.datetime | None = None, group_by: Literal[
                "cell", "element", "sectorGroup", "incidentZone", "all"] = "cell", managed_element_ref: str | None = None,
                cell_id: str | None = None, db: Session = Depends(get_session)):
    """MGT-11.7: compute the KPI as `GET /kpis/{name}` does and deliver one DME record per group to every data job open on its DME type
    `RAN.KPI.<name>` (registered here, idempotently, as RAN NF OAM's own production capability, like the PM counters). An rApp reads KPIs the way
    it reads any other data: a data job on that type, never this module. Nothing runs this on a schedule: a scheduler or an operator calls it.
    Internal-only at R1."""
    definition = _kpi_or_404(db, name)
    start, end = _kpi_window(from_time, to_time)
    result = kpi.compute(db, definition, start, end, group_by, managed_element_ref, cell_id)
    db.commit()                                              # end the read transaction before calling DME (nothing of ours is written)
    jobs, delivered = _publish_kpi_to_dme(definition, result)
    return {"kpi": name, "typeName": f"RAN.KPI.{name}", "groups": len(result["items"]), "dataJobs": jobs, "recordsDelivered": delivered}


def _publish_kpi_to_dme(definition: KpiDefinition, result: dict) -> tuple[int, int]:
    """(open data jobs, records delivered) for a computed KPI; registers the DME type first."""
    r1 = R1Client()
    type_name = f"RAN.KPI.{definition.name}"
    r1.post("/dme/production-capabilities", json={
        "namespace": "RAN", "name": f"KPI.{definition.name}", "version": "1.0.0", "typeName": type_name, "producerId": "ran-nf-oam",
        "dataProductionSchema": KPI_RESULT_SCHEMA, "producerHealthCallbackUrl": f"{SELF_URL}/health",
        "jobCallbackUrl": f"{SELF_URL}/dme-jobs"})
    dme_type = next((t for t in r1.get("/dme/dme-types", params={"data_category": "RAN"}).json() if t["typeName"] == type_name), None)
    jobs = r1.get("/dme/data-jobs", params={"dme_type_id": dme_type["dmeTypeId"], "limit": 500}).json()["items"] if dme_type else []
    delivered = 0
    for item in result["items"]:
        payload = {"kpi": definition.name, "unit": definition.unit, "groupBy": result["groupBy"], "group": item["group"], "value": item["value"],
                   "samples": item["samples"], "reason": item["reason"], "windowStart": result["from"], "windowEnd": result["to"]}
        for job in jobs:
            r1.post(f"/dme/data-jobs/{job['dataJobId']}/records", json={"payload": payload})
            delivered += 1
    return len(jobs), delivered


# ---------------------------------------------------------------- KPI schedules (PR-MSG-4)


# Request body of `PUT /kpi-schedules/{id}`: which KPI to publish to DME, how often (60 s to a day), over what look-back (default: the interval, so consecutive windows meet), grouped and narrowed as `GET /kpis/{name}`.
class KpiScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kpi: str
    intervalSeconds: int = Field(ge=60, le=86400)
    lookbackSeconds: int | None = Field(default=None, ge=60, le=604800)       # default: the interval, so consecutive windows meet
    groupBy: Literal["cell", "element", "sectorGroup", "incidentZone", "all"] = "cell"
    managedElementRef: str | None = None
    cellId: str | None = None
    enabled: bool = True


def _schedule_view(row: KpiSchedule) -> dict:
    """The JSON view of a KPI schedule, with the next run time (last run plus interval) only for an enabled schedule that has run."""
    last = as_utc(row.last_run_at) if row.last_run_at else None
    return {"scheduleId": row.schedule_id, "kpi": row.kpi, "intervalSeconds": row.interval_seconds, "lookbackSeconds": row.lookback_seconds,
            "groupBy": row.group_by, "managedElementRef": row.managed_element_ref, "cellId": row.cell_id, "enabled": row.enabled,
            "lastRunAt": last, "lastStatus": row.last_status, "lastDetail": row.last_detail,
            "nextRunAt": (last + datetime.timedelta(seconds=row.interval_seconds)) if (last and row.enabled) else None}


def _schedule_visible(db: Session, request: Request, row: KpiSchedule) -> bool:
    """PR-SEC-10.9: a schedule is the caller's to see when it names an element inside the caller's scope. One with no element is a schedule of the whole network, which a
    caller with a claim (that restricts the network) cannot be said to touch: only an unscoped caller sees it."""
    scope = scoping.request_scope(request)
    if scope is None:
        return True
    return row.managed_element_ref is not None and scoping.element_permitted(db, scope, row.managed_element_ref)


@app.put("/kpi-schedules/{schedule_id}")
def put_kpi_schedule(schedule_id: str, body: KpiScheduleRequest, request: Request, db: Session = Depends(get_session)):
    """MSG-4: publish `kpi` to DME every `intervalSeconds` (over the last `lookbackSeconds`), as `POST /kpis/{name}/publish` does. The worker runs it
    (`ran-nf-oam-worker`); with no worker nothing happens. 404 for a KPI that is not defined. Replaces the schedule of that id (its `last*` stay).
    Internal-only at R1. PR-SEC-10.9: for a caller with a scope claim, 403 `SCOPE_DENIED` when the schedule names an element outside it, when it names none (a schedule of
    the whole network), or when the id is a schedule the caller may not see (it is not replaced)."""
    scope = scoping.request_scope(request)
    if scope is not None:
        if body.managedElementRef is None:
            raise scoping.scope_denied("a schedule with no managedElementRef covers the whole network, which the caller's scope does not")
        scoping.require_elements(db, scope, [body.managedElementRef])
    _kpi_or_404(db, body.kpi)
    existing = db.get(KpiSchedule, schedule_id)
    if existing is not None and not _schedule_visible(db, request, existing):
        raise scoping.scope_denied("the caller's scope does not cover this schedule")
    row = existing or KpiSchedule(schedule_id=schedule_id)
    row.kpi, row.interval_seconds, row.group_by = body.kpi, body.intervalSeconds, body.groupBy
    row.lookback_seconds = body.lookbackSeconds or body.intervalSeconds
    row.managed_element_ref, row.cell_id, row.enabled = body.managedElementRef, body.cellId, body.enabled
    db.add(row)
    db.commit()
    return _schedule_view(row)


@app.get("/kpi-schedules")
def list_kpi_schedules(request: Request, db: Session = Depends(get_session)):
    """PR-SEC-10.9: a caller with a scope claim sees the schedules of the elements inside it (a schedule of the whole network is for an unscoped caller)."""
    rows = [r for r in db.scalars(select(KpiSchedule).order_by(KpiSchedule.schedule_id)).all() if _schedule_visible(db, request, r)]
    return {"items": [_schedule_view(r) for r in rows]}


@app.get("/kpi-schedules/{schedule_id}")
def get_kpi_schedule(schedule_id: str, request: Request, db: Session = Depends(get_session)):
    # 404 KPI_SCHEDULE_NOT_FOUND for an unknown id and for one outside the caller's scope.
    row = db.get(KpiSchedule, schedule_id)
    if row is None or not _schedule_visible(db, request, row):                  # PR-SEC-10.9: one outside the caller's scope is a 404, as if it were not there
        raise framework_error(FrameworkError.KPI_SCHEDULE_NOT_FOUND, detail=f"no KPI schedule {schedule_id!r}")
    return _schedule_view(row)


@app.delete("/kpi-schedules/{schedule_id}", status_code=204)
def delete_kpi_schedule(schedule_id: str, request: Request, db: Session = Depends(get_session)):
    # 204; 404 KPI_SCHEDULE_NOT_FOUND for an unknown id and for one outside the caller's scope (it is not deleted).
    row = db.get(KpiSchedule, schedule_id)
    if row is None or not _schedule_visible(db, request, row):
        raise framework_error(FrameworkError.KPI_SCHEDULE_NOT_FOUND, detail=f"no KPI schedule {schedule_id!r}")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


def run_due_kpi_schedules(db: Session, now: datetime.datetime | None = None) -> list[dict]:
    """What the worker's `publish-kpis` task runs: every enabled schedule whose interval has passed computes its KPI over its look-back window and
    publishes it to DME. A schedule that fails is marked (`lastStatus` ERROR, `lastDetail`) and waits for its next interval; the others still run."""
    now = now or datetime.datetime.now(datetime.UTC)
    ran = []
    for schedule_id in db.scalars(select(KpiSchedule.schedule_id).where(KpiSchedule.enabled.is_(True)).order_by(KpiSchedule.schedule_id)).all():
        row = db.get_one(KpiSchedule, schedule_id)
        last = as_utc(row.last_run_at) if row.last_run_at else None
        if last and last + datetime.timedelta(seconds=row.interval_seconds) > now:
            continue
        status, detail = "OK", None
        try:
            definition = db.get(KpiDefinition, row.kpi)
            if definition is None:
                raise LookupError(f"no KPI {row.kpi!r}")
            result = kpi.compute(db, definition, now - datetime.timedelta(seconds=row.lookback_seconds), now, row.group_by,
                                 row.managed_element_ref, row.cell_id)
            db.commit()                                      # end the read transaction before calling DME
            jobs, delivered = _publish_kpi_to_dme(definition, result)
            detail = f"{len(result['items'])} groups, {delivered} records to {jobs} data jobs"
        except Exception as exc:                             # noqa: BLE001 (one schedule must not stop the others)
            db.rollback()
            status, detail = "ERROR", f"{type(exc).__name__}: {exc}"[:500]
        row = db.get_one(KpiSchedule, schedule_id)
        row.last_run_at, row.last_status, row.last_detail = now, status, detail
        db.commit()
        ran.append({"scheduleId": schedule_id, "status": status, "detail": detail})
    return ran


@app.get("/config-jobs/{job_id}")
def query_write_config_job_status(job_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # 404 CONFIG_JOB_NOT_FOUND for an unknown job, for another rApp's job when the caller is a scoped rApp (PR-SEC-10.11), and for a job that touched an element outside the caller's scope: all three answer the same. Sub-changes are listed in request order.
    job = db.get(WriteConfigJob, job_id)
    # PR-SEC-10: a job that touched an element outside the caller's scope is not shown to it: 404, as if it did not exist. PR-SEC-10.11: nor is another rApp's job
    if job is None or not _job_owned(db, request, job) or scoping.denied_refs(db, scoping.request_scope(request), _job_elements(db, job_id)):
        raise framework_error(FrameworkError.CONFIG_JOB_NOT_FOUND, detail=f"unknown jobId {job_id}")
    sub_changes = db.scalars(select(WriteConfigSubChange).where(WriteConfigSubChange.job_id == job_id).order_by(WriteConfigSubChange.position)).all()
    return {"jobId": str(job.job_id), "status": job.status, "requestedBy": job.requested_by,
            "rollbackOf": str(job.rollback_of) if job.rollback_of else None, "rollbackForced": job.rollback_forced,
            "waveSize": job.wave_size, "waveCount": job.wave_count, "currentWave": job.current_wave,
            "wavePauseSeconds": job.wave_pause_seconds, "onGateFailure": job.on_gate_failure, "gateMaxNewAlarms": job.gate_max_new_alarms,
            "haltedReason": job.halted_reason, "haltedDetail": job.halted_detail,
            "kpiGuard": job.kpi_guard, "kpiGuardResult": job.kpi_guard_result,
            "kpiGuardCheckedAt": as_utc(job.kpi_guard_checked_at).isoformat() if job.kpi_guard_checked_at else None,
            "nextWaveAt": as_utc(job.next_wave_at).isoformat() if job.next_wave_at else None,
            "subChanges": [{"managedElementRef": sc.managed_element_ref, "wave": sc.wave, "managedFunctionRef": sc.managed_function_ref,
                            "operation": sc.operation, "status": sc.status, "rejectionReason": sc.rejection_reason,
                            "rejectionDetail": sc.rejection_detail, "attempts": sc.attempts} for sc in sub_changes]}


AckState = Literal["ACKNOWLEDGED", "UNACKNOWLEDGED"]
AlarmGroupBy = Literal["severity", "ack_state", "probable_cause", "managed_element_ref", "region", "hour"]
MAX_ALARM_GROUPS = 50                   # the high-cardinality groupings (cause, element, region) answer the top 50 by count: a tile row, not a table
ALARM_HOURS = 24                        # `group_by=hour`: the last 24 hourly buckets
MAX_CORRELATED = 200                    # `GET /alarms/{id}/correlated`: an alarm storm on one element is cut here, the answer says so


def _alarm_filters(managed_element_ref: str | None = None, severity: str | None = None, managed_function_ref: str | None = None,
                   ack_state: AckState | None = None, open_only: bool = Query(False, description="Leave cleared alarms out."),
                   probable_cause: str | None = None,
                   since: datetime.datetime | None = Query(None, description="raisedAt at or after this time."),
                   until: datetime.datetime | None = Query(None, description="raisedAt before this time."),
                   region: str | None = Query(None, description="The region of the alarm's managed element (ADR 0005)."),
                   site_cluster: str | None = scoping.SiteClusterFilter) -> dict:
    """The filters `GET /alarms`, `/alarms/counts` share, as keyword arguments for `alarm_query.filtered_alarms`; 422 for a severity outside PerceivedSeverity."""
    return {"managed_element_ref": managed_element_ref, "managed_function_ref": managed_function_ref,
            "severity": _perceived_severity(severity) if severity else None, "ack_state": ack_state, "open_only": open_only,
            "probable_cause": probable_cause, "since": since, "until": until, "region": region, "site_cluster": site_cluster}


@app.get("/alarms")
def query_alarms(request: Request, filters: dict = Depends(_alarm_filters), after: str | None = Query(None, description=AFTER_DESCRIPTION),
                 limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """`severity` filter (GUI pass) — the alarm console filters by ME and
    by perceivedSeverity; `severity=cleared` isolates the cleared history.
    `managed_function_ref` (W10-alarm-cellref) narrows to the alarms raised
    on one managed function, e.g. a cell's `NRCellDU=101`.

    PR-GUI-9.4/9.5: `ack_state`, `open_only` (no cleared alarms), `probable_cause`, `since`/`until` (raisedAt, inclusive/exclusive), `region` and
    (PR-GUI-9.3) `site_cluster` (the managed element's) narrow further. `after` switches to keyset paging in the console order: severity (critical first), raisedAt newest first, alarmId;
    the answer is then `{items, limit, nextCursor, hasMore}` (`nextCursor` null on the last page). Without `after` the answer and its order are unchanged.

    PR-SEC-10.6: a caller with a scope claim sees only the alarms of the elements inside it (the list is filtered, never refused: naming an element outside
    the scope in `managed_element_ref` gives an empty page, the same as an element with no alarms).
    """
    stmt = scoping.scoped_to_elements(select(Alarm), scoping.request_scope(request), Alarm.managed_element_ref)
    stmt = msac.readable(stmt, db, request, Alarm.managed_element_ref)                         # MGT-2.6: the alarms of elements the caller's access rules do not let it read are left out
    stmt = alarm_query.filtered_alarms(stmt, **filters)
    if after is not None:
        rows, next_cursor = alarm_query.alarm_keyset_page(db, stmt, after, int(limit))
        return {"items": [_alarm_view(a) for a in rows], "limit": int(limit), "nextCursor": next_cursor, "hasMore": next_cursor is not None}
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_alarm_view(a) for a in page["items"]]}


@app.get("/alarms/counts")
def count_alarms(request: Request, group_by: AlarmGroupBy, filters: dict = Depends(_alarm_filters), db: Session = Depends(get_session)):
    """PR-GUI-9.4: how many alarms there are per `group_by`, with the filters of `GET /alarms`, counted in SQL: `{"groupBy", "groups": [{"key", "count"}]}`.

    `severity`, `ack_state`: every value present. `probable_cause`, `managed_element_ref`, `region` (the element's region): the top 50 groups by count
    (then by key); a NULL cause or region is the key `null`. `hour`: the last 24 hourly buckets of raisedAt, oldest first, every hour present (count 0
    when none), keys `2026-10-10T08:00:00Z` (UTC), each with `bySeverity` `{critical, major, minor, warning}` (a cleared or indeterminate alarm counts
    in `count` only). Filtered to the caller's scope claim like the list.
    """
    stmt_base = scoping.scoped_to_elements(select(Alarm), scoping.request_scope(request), Alarm.managed_element_ref)
    stmt_base = msac.readable(stmt_base, db, request, Alarm.managed_element_ref)               # MGT-2.6: counted as the list shows them
    filtered = alarm_query.filtered_alarms(stmt_base, **filters)
    if group_by == "hour":
        return {"groupBy": "hour", "groups": _hourly_alarm_counts(db, filtered)}
    alarms = filtered.subquery()
    key: ColumnElement[Any] | InstrumentedAttribute[str | None]
    if group_by == "region":
        key = ManagedEntity.region
        stmt = select(key.label("key"), func.count().label("n")).select_from(alarms).outerjoin(
            ManagedEntity, ManagedEntity.managed_element_ref == alarms.c.managed_element_ref)
    else:
        key = alarms.c[group_by]
        stmt = select(key.label("key"), func.count().label("n")).select_from(alarms)
    rows = db.execute(stmt.group_by(key).order_by(func.count().desc(), key).limit(MAX_ALARM_GROUPS)).all()
    return {"groupBy": group_by, "groups": [{"key": row.key, "count": row.n} for row in rows]}


def _hourly_alarm_counts(db: Session, filtered) -> list[dict]:
    """The last `ALARM_HOURS` hourly buckets of the alarms `filtered` selects, oldest first: one SQL GROUP BY (hour, severity), at most 24 x 6 rows,
    then every hour of the window filled in so a chart has no gaps."""
    now = datetime.datetime.now(datetime.UTC)
    first = now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=ALARM_HOURS - 1)
    alarms = filtered.where(Alarm.raised_at >= first).subquery()
    bucket = alarm_query.hour_bucket(db, alarms.c.raised_at)
    rows = db.execute(select(bucket.label("hour"), alarms.c.severity, func.count().label("n")).select_from(alarms).group_by(bucket, alarms.c.severity)).all()
    hours = [(first + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z") for h in range(ALARM_HOURS)]
    out: dict[str, dict[str, Any]] = {h: {"key": h, "count": 0, "bySeverity": dict.fromkeys(alarm_query.GRADED_SEVERITIES, 0)} for h in hours}
    for row in rows:
        group = out.get(row.hour)
        if group is None:                       # an alarm raised in the future (a clock ahead of ours) is outside the window
            continue
        group["count"] += row.n
        if row.severity in group["bySeverity"]:
            group["bySeverity"][row.severity] += row.n
    return list(out.values())


@app.get("/alarms/stats")
def alarm_stats(request: Request, window_hours: int = Query(24, ge=1, le=24 * 31), region: str | None = None,
                site_cluster: str | None = scoping.SiteClusterFilter, db: Session = Depends(get_session)):
    """PR-GUI-9.8: `{"windowHours", "mttaSeconds", "acked", "open"}`. `mttaSeconds`: the mean time to acknowledge, the mean of ackTime - raisedAt over
    the alarms acknowledged within the last `window_hours` (null when none was); `acked`: how many those are; `open`: the alarms not cleared now
    (whatever their age). `region` and `site_cluster` narrow to the elements of one region or site cluster; filtered to the caller's scope claim. An alarm acknowledged before the
    ack time was recorded (revision 0036) has none and is not counted."""
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=window_hours)
    base = alarm_query.filtered_alarms(msac.readable(scoping.scoped_to_elements(select(Alarm), scoping.request_scope(request), Alarm.managed_element_ref),
                                                     db, request, Alarm.managed_element_ref),
                                       region=region, site_cluster=site_cluster)
    acked = base.where(Alarm.acknowledged_at.is_not(None), Alarm.acknowledged_at >= since).subquery()
    mean, n = db.execute(select(func.avg(alarm_query.seconds_between(db, acked.c.acknowledged_at, acked.c.raised_at)), func.count()).select_from(acked)).one()
    open_count = db.scalar(select(func.count()).select_from(base.where(Alarm.severity != "cleared").subquery()))
    return {"windowHours": window_hours, "mttaSeconds": round(float(mean), 1) if mean is not None and n else None, "acked": n, "open": open_count}


def _readable_alarm(db: Session, alarm_id: uuid.UUID, request: Request) -> Alarm:
    """An alarm the caller may read: 404 `ALARM_NOT_FOUND` for an unknown one, one outside its scope claim (PR-SEC-10.6) or one on an element its
    access rules do not let it read (MGT-2.6), as the list leaves it out."""
    alarm = db.get(Alarm, alarm_id)
    if alarm is None or not scoping.element_permitted(db, scoping.request_scope(request), alarm.managed_element_ref) \
            or not msac.may_read(db, request, alarm.managed_element_ref):
        raise framework_error(FrameworkError.ALARM_NOT_FOUND, detail=f"no such alarm {alarm_id}")
    return alarm


@app.get("/alarms/{alarm_id}/history")
def alarm_history_list(alarm_id: uuid.UUID, request: Request, limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """MGT-8.2: what happened to the alarm, oldest first: `{items: [{at, event, from, to, by}], total, limit, offset}`. `event` is RAISED,
    ACKNOWLEDGED, UNACKNOWLEDGED, CLEARED or SEVERITY_CHANGED; `from` / `to` the ack state or severity before and after; `by` who, when known.
    Written by `alarm_history.py` wherever the alarm changes. An alarm raised before revision 0039 has no rows for what happened before it. 404
    as `GET /alarms/{id}/correlated`."""
    _readable_alarm(db, alarm_id, request)
    page = paginate(db, select(AlarmHistory).where(AlarmHistory.alarm_id == alarm_id).order_by(AlarmHistory.at, AlarmHistory.history_id), limit, offset)
    return {**page, "items": [{"at": as_utc(h.at).isoformat(), "event": h.event, "from": h.from_value, "to": h.to_value, "by": h.by} for h in page["items"]]}


class AlarmCommentRequest(BaseModel):
    """MGT-8.3: a comment on an alarm. `author` is who wrote it (the GUI BFF sets it to `smo-gui:<user>`, never the browser's value)."""
    model_config = ConfigDict(extra="forbid")
    author: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """A comment says something: surrounding space is trimmed and a blank one is refused (422)."""
        value = value.strip()
        if not value:
            raise ValueError("a comment cannot be blank")
        return value


def _comment_view(c: AlarmComment) -> dict:
    """The JSON of one comment."""
    return {"commentId": str(c.comment_id), "alarmId": str(c.alarm_id), "createdAt": as_utc(c.created_at).isoformat(), "author": c.author, "text": c.text}


@app.get("/alarms/{alarm_id}/comments")
def alarm_comments(alarm_id: uuid.UUID, request: Request, limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """MGT-8.3: the comments on the alarm, oldest first, paged with `total`. 404 as `GET /alarms/{id}/history`."""
    _readable_alarm(db, alarm_id, request)
    page = paginate(db, select(AlarmComment).where(AlarmComment.alarm_id == alarm_id).order_by(AlarmComment.created_at, AlarmComment.comment_id), limit, offset)
    return {**page, "items": [_comment_view(c) for c in page["items"]]}


@app.post("/alarms/{alarm_id}/comments", status_code=201)
def add_alarm_comment(alarm_id: uuid.UUID, body: AlarmCommentRequest, request: Request, db: Session = Depends(get_session)):
    """MGT-8.3: add a comment to the alarm (201, the comment). The same check as acknowledging it (404 outside the scope; MGT-2.3, an `update` on
    the alarm's element for a managed caller). Comments are only added: there is no edit or delete."""
    alarm = _get_alarm(db, alarm_id, request)
    comment = AlarmComment(alarm_id=alarm.alarm_id, created_at=datetime.datetime.now(datetime.UTC), author=body.author, text=body.text)
    db.add(comment)
    db.commit()
    return _comment_view(comment)


@app.get("/alarms/{alarm_id}/correlated")
def correlated_alarms(alarm_id: uuid.UUID, request: Request, window_seconds: int = Query(60, ge=1, le=3600), db: Session = Depends(get_session)):
    """PR-GUI-9.8 (PR-MGT-9): the alarms that probably belong with this one, by a stated heuristic rather than a root-cause analysis: the other alarms
    on the same managed element raised within `window_seconds` before or after it (cleared ones included), oldest first, at most 200.
    `{"alarmId", "rule": "same-element-within-window", "windowSeconds", "items", "truncated"}`. 404 `ALARM_NOT_FOUND` for an unknown alarm or one
    outside the caller's scope claim or on an element its access rules do not let it read (MGT-2.6)."""
    alarm = _readable_alarm(db, alarm_id, request)
    raised, window = as_utc(alarm.raised_at), datetime.timedelta(seconds=window_seconds)
    rows = db.scalars(select(Alarm).where(Alarm.managed_element_ref == alarm.managed_element_ref, Alarm.alarm_id != alarm.alarm_id,
                                          Alarm.raised_at >= raised - window, Alarm.raised_at <= raised + window)
                      .order_by(Alarm.raised_at, Alarm.alarm_id).limit(MAX_CORRELATED + 1)).all()
    return {"alarmId": str(alarm.alarm_id), "rule": "same-element-within-window", "windowSeconds": window_seconds,
            "items": [_alarm_view(a) for a in rows[:MAX_CORRELATED]], "truncated": len(rows) > MAX_CORRELATED}


@app.post("/alarms/ingest")
def ingest_alarm(source_alarm_id: str, managed_element_ref: str, severity: str, correlation_group: str | None = None,
                  probable_cause: str | None = None, specific_problem: str | None = None, root_cause_indicator: bool = False,
                  correlated_notifications: list[uuid.UUID] = Query(default=[]), proposed_repair_actions: str | None = None,
                  alarm_type: str | None = None, managed_function_ref: str | None = None, db: Session = Depends(get_session)):
    """alarmId is ALWAYS a fresh UUID minted here, never the raising ME's
    native ID — RAN NF OAM LLD section 3.3, closing R1UCR's own flagged,
    unresolved collision risk under a fleet of N MEs.

    probableCause/specificProblem/rootCauseIndicator/correlatedNotifications/
    proposedRepairActions (HISTORY.md §5): the standard fault
    fields 3GPP TS 28.532 FaultMnS's NotifyNewAlarm carries, previously
    entirely absent from this alarm model. alarmType (HISTORY.md §7,
    TS28111_FaultNrm.yaml's AlarmRecord) was the one of these fields
    still missing after that pass.

    W10-alarm-cellref: `managed_function_ref` is the managed function the
    alarm is about inside the element (AlarmRecord's objectInstance below
    the ME), e.g. `NRCellDU=101`, so a consumer can hold that one cell
    rather than the whole element. Omitted = the element as a whole.
    """
    alarm = _raise_alarm(db, source_alarm_id=source_alarm_id, managed_element_ref=managed_element_ref, severity=severity,
                         correlation_group=correlation_group, probable_cause=probable_cause, specific_problem=specific_problem,
                         root_cause_indicator=root_cause_indicator, correlated_notifications=correlated_notifications,
                         proposed_repair_actions=proposed_repair_actions, alarm_type=alarm_type, managed_function_ref=managed_function_ref)
    return {"alarmId": str(alarm.alarm_id)}


def _raise_alarm(db: Session, *, source_alarm_id: str, managed_element_ref: str, severity: str, correlation_group: str | None = None,
                 probable_cause: str | None = None, specific_problem: str | None = None, root_cause_indicator: bool = False,
                 correlated_notifications: list[uuid.UUID] | None = None, proposed_repair_actions: str | None = None,
                 alarm_type: str | None = None, managed_function_ref: str | None = None) -> Alarm:
    """The alarm a new-alarm report makes, shared by `POST /alarms/ingest` and the VES receiver (SB-7.2): the service check, the severity, the refs, the row."""
    require_service(db, managed_element_ref, "FM")  # Wave 9 (W9-01)
    severity = _perceived_severity(severity)
    _valid_refs(managed_element_ref, managed_function_ref)
    alarm = Alarm(source_alarm_id=source_alarm_id, managed_element_ref=managed_element_ref,
                  managed_function_ref=managed_function_ref, severity=severity, correlation_group=correlation_group,
                  probable_cause=probable_cause, specific_problem=specific_problem, root_cause_indicator=root_cause_indicator,
                  correlated_notifications=correlated_notifications or [], proposed_repair_actions=proposed_repair_actions,
                  alarm_type=alarm_type)
    db.add(alarm)
    db.commit()
    return alarm


def _get_alarm(db: Session, alarm_id: uuid.UUID, request: Request) -> Alarm:
    """MGT-8.1: an unknown alarm is a 404, not a 500 from `None.ack_state`. So is an alarm of an element outside the caller's scope (PR-SEC-10.6): it is not shown to it."""
    alarm = db.get(Alarm, alarm_id)
    if alarm is None or not scoping.element_permitted(db, scoping.request_scope(request), alarm.managed_element_ref):
        raise framework_error(FrameworkError.ALARM_NOT_FOUND, detail=f"no such alarm {alarm_id}")
    _require_msac(db, request, "update", alarm.managed_element_ref, alarm.managed_function_ref)       # MGT-2.3: acknowledging or clearing changes the alarm record
    return alarm


@app.patch("/alarms/{alarm_id}/ack")
def change_alarm_ack_state(alarm_id: uuid.UUID, new_state: Literal["ACKNOWLEDGED", "UNACKNOWLEDGED"], request: Request, ack_user_id: str | None = None, db: Session = Depends(get_session)):
    """ackUserId (HISTORY.md §7, TS28111_FaultNrm.yaml's AlarmRecord) —
    who acknowledged it, never recorded before. alarmChangedTime (the
    spec's own "last mutated" timestamp, distinct from raised_at/
    cleared_at) updates here and in clear_alarm below, the two places
    this build actually mutates an existing alarm.
    """
    alarm = _get_alarm(db, alarm_id, request)
    now = datetime.datetime.now(datetime.UTC)
    # PR-GUI-9.8: the ack time is the moment the alarm became acknowledged: a repeated ack keeps the first, an un-ack drops it (ackTime null again)
    if new_state == "UNACKNOWLEDGED":
        alarm.acknowledged_at = None
    elif alarm.ack_state != "ACKNOWLEDGED":             # an alarm acknowledged before revision 0036 keeps its unknown (NULL) ack time
        alarm.acknowledged_at = now
    alarm.ack_state = new_state
    alarm.ack_user_id = ack_user_id
    alarm.changed_at = now
    db.commit()
    return _alarm_view(alarm)


@app.patch("/alarms/{alarm_id}/clear")
def clear_alarm(alarm_id: uuid.UUID, request: Request, clear_user_id: str | None = None, db: Session = Depends(get_session)):
    """HISTORY.md §5: no alarm-cleared lifecycle existed at
    all — `/alarms/{id}/ack` only ever toggled ack_state, so an alarm
    that stopped recurring on the NF had no way to ever be marked
    resolved. Matches the reference's own NotifyClearedAlarm shape:
    setting severity to 'cleared' (already a valid value in this
    build's own CHECK constraint) rather than a separate state field.
    """
    alarm = _get_alarm(db, alarm_id, request)
    alarm.severity = "cleared"
    alarm.cleared_at = datetime.datetime.now(datetime.UTC)
    alarm.clear_user_id = clear_user_id
    alarm.changed_at = alarm.cleared_at
    db.commit()
    return _alarm_view(alarm)


@app.post("/pm-subscriptions")
def subscribe_pm(managed_element_ref: str, counter_type: str, delivery_method: str, request: Request, granularity_period: int | None = None,
                  db: Session = Depends(get_session)):
    """SubscribePM — RAN NF OAM LLD section 3.5: this is a DME-producer
    registration wrapper, NOT a clause-8 API call. No R1AP endpoint exists
    for PM at all; that's the spec's own documented design intent.

    granularityPeriod (HISTORY.md §7 item 4, TS28550_PerfMeasJobCtrlMnS.yaml's
    measJobCreation-RequestType) — the one real job-control field worth
    carrying despite the wrapper scope cut; everything else on that
    schema (schedule/priority/multi-instance/reportingPeriod) stays out.
    """
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])        # PR-SEC-10.6: 403 SCOPE_DENIED outside the caller's scope
    _require_msac(db, request, "read", managed_element_ref)                                    # MGT-2.2: a subscription is the right to be sent the element's PM
    if db.get(ManagedEntity, managed_element_ref) is None:      # the subscription refers to the element: no element, no subscription (a 404, not the foreign key's 500)
        raise framework_error(FrameworkError.MANAGED_ENTITY_NOT_FOUND, detail=f"no managed element {managed_element_ref!r}")
    require_service(db, managed_element_ref, "PM")  # Wave 9 (W9-01)
    engine = {"pull": "ProvMnS", "push": "PMJobControl", "stream": "StreamingDataReporting"}.get(delivery_method, "FileDataReporting")
    sub = PMSubscription(managed_element_ref=managed_element_ref, counter_type=counter_type, delivery_method=delivery_method,
                          southbound_engine=engine, granularity_period=granularity_period)
    db.add(sub)
    db.commit()

    r1 = R1Client()
    r1.post("/dme/production-capabilities", json={
        "namespace": "RAN", "name": f"PMCounters.{counter_type}", "version": "1.0.0",
        "typeName": f"RAN.PMCounters.{counter_type}", "producerId": "ran-nf-oam",
        "dataProductionSchema": {}, "producerHealthCallbackUrl": f"{SELF_URL}/health",
        "jobCallbackUrl": f"{SELF_URL}/dme-jobs",
    })
    return {"subscriptionId": str(sub.subscription_id), "southboundEngine": engine, "granularityPeriod": sub.granularity_period}


# One PM sample: a cell, a time and either one `value` or several counters in `values` (at least one is required); `relation` names the neighbour relation the counters are measured on.
class PmMeasurement(BaseModel):
    cellId: str
    timestamp: datetime.datetime
    value: float | None = None
    # Wave 10.2 (W10.2-03): a measurement can carry several counters of one
    # family at once (a PM file's measInfo with several measTypes, e.g. the
    # handover counters MM.HoExeAtt / MM.HoFailTooLate / ...), and can be
    # per neighbour relation rather than per cell.
    values: dict[str, float] | None = None
    relation: str | None = None  # the neighbour relation (e.g. "201-202") the counters are measured on

    @model_validator(mode="after")
    def _has_a_value(self):
        if self.value is None and not self.values:
            raise ValueError("a measurement needs value or values")
        return self


# Request body of `POST /pm-reports`: measurements of one counter type for one element, delivered to DME.
class PmReportRequest(BaseModel):
    managedElementRef: str
    counterType: str
    measurements: list[PmMeasurement]


@app.post("/pm-reports", status_code=201)
def receive_pm_report(body: PmReportRequest, db: Session = Depends(get_session)):
    """Wave 10.1 (W10-04): the PM data path O1 PM → RAN NF OAM → DME. An
    NF's PM report (here a simplified JSON shape of a measurement file or
    stream) for a counter RAN NF OAM has a PM subscription on is delivered
    as DME records — one per cell and sample — to every data job open on
    that counter's DME type (`RAN.PMCounters.<counterType>`, registered by
    SubscribePM). Consumers (e.g. the EnergySaving rApp reading
    PRB_UTILIZATION) only ever see DME, never this module."""
    return _accept_pm_report(db, body)


def _accept_pm_report(db: Session, body: "PmReportRequest") -> dict:
    """The PM report path, shared by `POST /pm-reports` and the VES receiver (SB-7.4): the service check, the subscription check, the fan-out to DME."""
    require_service(db, body.managedElementRef, "PM")
    subscribed = db.scalars(select(PMSubscription).where(PMSubscription.managed_element_ref == body.managedElementRef,
                                                         PMSubscription.counter_type == body.counterType)).first()
    if subscribed is None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"no PM subscription for {body.counterType} on {body.managedElementRef}")
    db.commit()  # end the read transaction before fanning out to DME (nothing of ours is written)
    jobs, delivered = _fan_out_to_dme(body.managedElementRef, body.counterType, body.measurements)
    return {"managedElementRef": body.managedElementRef, "counterType": body.counterType,
            "measurements": len(body.measurements), "dataJobs": jobs, "recordsDelivered": delivered}


def _fan_out_to_dme(managed_element_ref: str, counter_type: str, measurements: list[PmMeasurement]) -> tuple[int, int]:
    """(open data jobs, records delivered): each measurement goes to every
    data job open on `RAN.PMCounters.<counterType>`."""
    r1 = R1Client()
    type_name = f"RAN.PMCounters.{counter_type}"
    dme_type = next((t for t in r1.get("/dme/dme-types", params={"data_category": "RAN"}).json()
                     if t["typeName"] == type_name), None)
    jobs = r1.get("/dme/data-jobs", params={"dme_type_id": dme_type["dmeTypeId"], "limit": 500}).json()["items"] if dme_type else []
    delivered = 0
    for m in measurements:
        payload: dict[str, Any] = {"managedElementRef": managed_element_ref, "cellId": m.cellId, "counter": counter_type,
                   "value": m.value, "timestamp": m.timestamp.isoformat()}
        if m.values is not None:
            payload["values"] = m.values
        if m.relation is not None:
            payload["relation"] = m.relation
        for job in jobs:
            r1.post(f"/dme/data-jobs/{job['dataJobId']}/records", json={"payload": payload})
            delivered += 1
    return len(jobs), delivered


# ---------------------------------------------------------------- file data reporting
# SA-RANOAM-8: TS 28.532 File Data Reporting MnS (TS28532_FileDataReportingMnS.yaml).
# The NF's O1 adaptor reports a finished performance file (`POST /pm-files`); its
# measurements go to DME exactly as `POST /pm-reports` does, the file itself is
# kept and served (`GET /pm-files/{id}/file`, listed by `GET /files`), and every
# file subscription gets notifyFileReady. Streaming (TS28532_StreamingDataMnS) is
# not built: there is no streaming transport, and `delivery_method=stream`
# remains a registration only.

FileDataType = Literal["Performance", "Trace", "Analytics", "Proprietary"]


# Request body of `POST /pm-files`: a finished performance file (its measurements, format, data type, and optional expiry) as the element's adaptor reports it.
class PmFileRequest(BaseModel):
    managedElementRef: str
    counterType: str
    measurements: list[PmMeasurement]
    fileDataType: FileDataType = "Performance"
    fileFormat: str = "json"
    fileCompression: str | None = None
    jobId: str | None = None
    fileExpirationTime: datetime.datetime | None = None


class FileSubscriptionRequest(BaseModel):
    """TS 28.623 Subscription. `filter` (a Jex condition) is not supported and
    is refused; `fileDataType` narrows the subscription to one data type."""
    model_config = ConfigDict(extra="forbid")
    consumerReference: str
    timeTick: int | None = None
    fileDataType: FileDataType | None = None


def _file_info(f: PMFile) -> dict:
    """The TS 28.532 FileInfo of a stored PM file; its `fileLocation` is the download route of this service."""
    return {"fileLocation": f"/ran-nf-oam/pm-files/{f.file_id}/file", "fileSize": f.file_size,
            "fileReadyTime": as_utc(f.file_ready_time).isoformat(),
            "fileExpirationTime": as_utc(f.file_expiration_time).isoformat() if f.file_expiration_time else None,
            "fileCompression": f.file_compression, "fileFormat": f.file_format, "fileDataType": f.file_data_type,
            "jobId": f.job_id}


@app.post("/pm-files", status_code=201)
def report_pm_file(body: PmFileRequest, db: Session = Depends(get_session)):
    # Order: the FILE service check, then a PM subscription for the counter on the element must exist (422 otherwise). The file, the new sequence numbers of the matching file subscriptions and one outbox row per subscriber (`notifyFileReady`) are committed together; only after that are the measurements fanned out to DME, so a DME failure does not undo the stored file. Unlike the reads, this write is not filtered by the caller's scope.
    require_service(db, body.managedElementRef, "FILE")
    subscribed = db.scalars(select(PMSubscription).where(PMSubscription.managed_element_ref == body.managedElementRef,
                                                         PMSubscription.counter_type == body.counterType)).first()
    if subscribed is None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"no PM subscription for {body.counterType} on {body.managedElementRef}")
    content = json.dumps({"managedElementRef": body.managedElementRef, "counterType": body.counterType,
                          "measurements": [m.model_dump(mode="json", exclude_none=True) for m in body.measurements]})
    pm_file = PMFile(managed_element_ref=body.managedElementRef, counter_type=body.counterType, file_data_type=body.fileDataType,
                     file_format=body.fileFormat, file_compression=body.fileCompression, job_id=body.jobId, content=content,
                     file_size=len(content.encode()), file_expiration_time=body.fileExpirationTime)
    db.add(pm_file)
    db.flush()
    info = _file_info(pm_file)
    targets = [(sub.subscription_id, sub.consumer_reference, sub.sequence_no + 1) for sub in db.scalars(select(FileSubscription)).all()
               if sub.file_data_type in (None, body.fileDataType)]
    for sub in db.scalars(select(FileSubscription)).all():
        if sub.file_data_type in (None, body.fileDataType):
            sub.sequence_no += 1
    file_id = str(pm_file.file_id)
    for subscription_id, consumer, sequence_no in targets:  # outbox rows, committed with the file and the new sequence numbers (PR-MSG-1.9)
        enqueue(db, consumer, {"href": "/ran-nf-oam/file-subscriptions", "notificationId": sequence_no,
                               "notificationType": "notifyFileReady", "eventTime": info["fileReadyTime"],
                               "sequenceNo": sequence_no, "subscriptionId": str(subscription_id),
                               "fileInfoList": [info]})
    db.commit()
    jobs, delivered = _fan_out_to_dme(body.managedElementRef, body.counterType, body.measurements)
    return {"fileId": file_id, **info, "notified": len(targets), "dataJobs": jobs, "recordsDelivered": delivered}


@app.get("/files")
def read_file_info(fileDataType: FileDataType, request: Request, beginTime: datetime.datetime | None = None, endTime: datetime.datetime | None = None,
                   limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """TS 28.532 `GET /files`: FileInfo for the files of a data type, selected by
    the time they became available. Paginated like every list here. PR-SEC-10.6: a caller with a scope claim sees only the files of elements inside it."""
    stmt = scoping.scoped_to_elements(select(PMFile), scoping.request_scope(request), PMFile.managed_element_ref).where(PMFile.file_data_type == fileDataType)
    unreadable = _unreadable_elements(db, request, PMFile.managed_element_ref)                       # MGT-2.5: the files of elements the caller's rules do not let it read are left out
    if unreadable:
        stmt = stmt.where(PMFile.managed_element_ref.not_in(unreadable))
    if beginTime:
        stmt = stmt.where(PMFile.file_ready_time >= beginTime)
    if endTime:
        stmt = stmt.where(PMFile.file_ready_time <= endTime)
    page = paginate(db, stmt.order_by(PMFile.file_ready_time), limit, offset)
    return {**page, "items": [_file_info(f) for f in page["items"]]}


@app.get("/pm-files/{file_id}/file")
def download_pm_file(file_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # 404 NRM_OBJECT_NOT_FOUND for an unknown file, for one of an element outside the caller's scope, and for one past its expiry time; 403 MSAC_ACCESS_DENIED when the MSAC switch is on and a managed caller may not read the element. Returns the stored JSON as is.
    f = db.get(PMFile, file_id)
    if f is None or not scoping.element_permitted(db, scoping.request_scope(request), f.managed_element_ref):      # PR-SEC-10.6: outside the scope is a 404
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such file {file_id}")
    _require_msac(db, request, "read", f.managed_element_ref)                                          # MGT-2.5
    if f.file_expiration_time and as_utc(f.file_expiration_time) < datetime.datetime.now(datetime.UTC):
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"file {file_id} has expired")
    return Response(content=f.content, media_type="application/json")


@app.post("/file-subscriptions", status_code=201)
def create_file_subscription(body: FileSubscriptionRequest, request: Request, db: Session = Depends(get_session)):
    """TS 28.532: subscribe to `notifyFileReady`. A subscription is sent the notice of every file, of every element, so there is no part of it a caller with a scope
    claim could hold: PR-SEC-10.9 refuses it, 403 `SCOPE_DENIED` (an unscoped caller is unchanged). MGT-2.5: with the MSAC switch on, a managed caller needs `read` on the
    whole network."""
    if scoping.request_scope(request) is not None:
        raise scoping.scope_denied("a file subscription is sent the files of every managed element, which the caller's scope does not cover")
    _require_msac_everywhere(db, request, "read")                                                    # MGT-2.5: it is sent the ready-notice of every file, so it needs to read everything
    sub = FileSubscription(consumer_reference=body.consumerReference, file_data_type=body.fileDataType)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id), "consumerReference": sub.consumer_reference,
            "timeTick": body.timeTick, "fileDataType": sub.file_data_type}


@app.delete("/file-subscriptions/{subscription_id}", status_code=204)
def delete_file_subscription(subscription_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Idempotent. A subscription covers every element, so it is never inside a caller's scope claim (PR-SEC-10.9) and a managed caller needs `read` on the whole network
    to remove it (MGT-2.6, as to create it): for any other the answer is 204 and nothing is removed, as for an id that is not there."""
    sub = db.get(FileSubscription, subscription_id)
    if sub is not None and scoping.request_scope(request) is None and msac.may_read_everywhere(db, request):
        db.delete(sub)
        db.commit()


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/dme-jobs")
def receive_dme_job(body: dict):
    """DME's own job-push callback (HISTORY.md §5): DME's
    create_data_job now actually POSTs the job to jobCallbackUrl on
    create — subscribe_pm registers this exact URL, so this closes the
    same class of dangling-callback bug the /health route closed for
    the health-supervision URL. Phase 1: acks only, no real per-job
    state tracked producer-side.
    """
    return {"status": "accepted"}


@app.delete("/dme-jobs/{data_job_id}", status_code=204)
def stop_dme_job(data_job_id: str):
    # Does nothing and answers 204: this module keeps no per-job state for DME's data jobs.
    pass


@app.post("/software-management-jobs", status_code=202)
def software_update(managed_element_ref: str, request: Request, ru_instance_id: str | None = None, db: Session = Depends(get_session)):
    # 202 with the new job, started at once (IN_PROGRESS, phase DOWNLOAD). With the MSAC switch on a managed caller needs `exec` on the element; 409 O1_SERVICE_NOT_SUPPORTED when the element's services lack SWM. The element's registration is not checked here and the caller's scope is not asked.
    _require_msac(db, request, "exec", managed_element_ref)                                    # MGT-2.4: a software job runs a procedure on the element
    require_service(db, managed_element_ref, "SWM")  # Wave 9 (W9-01)
    job = lifecycle.start_software_job(db, managed_element_ref, ru_instance_id)
    db.commit()
    return {"jobId": str(job.job_id), "status": job.status, "phase": job.phase}


@app.post("/software-management-jobs/{job_id}/advance")
def advance_software_job(job_id: uuid.UUID, succeeded: bool, db: Session = Depends(get_session)):
    # Reports the outcome of the job's current phase: `succeeded=false` fails the job; true completes the phase (DOWNLOAD moves to INSTALL, INSTALL to ACTIVATE, ACTIVATE completes the job). 404 SOFTWARE_JOB_NOT_FOUND; 409 when the job has already ended (a late report after a campaign timed it out). After the commit, a job that belongs to a campaign lets the campaign go on, in its own transaction. The caller's scope is not asked.
    job = db.get(SoftwareManagementJob, job_id)
    if job is None:
        raise framework_error(FrameworkError.SOFTWARE_JOB_NOT_FOUND, detail=f"unknown jobId {job_id}")
    event = {"DOWNLOAD": SwmEvent.DOWNLOAD_OK, "INSTALL": SwmEvent.INSTALL_OK, "ACTIVATE": SwmEvent.ACTIVATE_OK}[job.phase]
    try:
        if not succeeded:
            job.status = SOFTWARE_MANAGEMENT_FSM.fire(SwmState(job.status), SwmEvent.PHASE_FAILED)
        else:
            job.status = SOFTWARE_MANAGEMENT_FSM.fire(SwmState(job.status), event)
            if event in PHASE_ORDER:
                job.phase = PHASE_ORDER[event]
    except IllegalTransition as exc:                    # a report for a job that has ended (a campaign timed it out, MGT-15.7): 409, as for a config job, not a 500
        raise illegal_transition_error(exc, f"software management job {job_id}") from None
    db.commit()
    if job.campaign_id is not None:
        lifecycle.on_job_advanced(db, job.campaign_id)          # MGT-15: the job belongs to a campaign, which decides what comes next (its own transaction)
    return {"jobId": str(job.job_id), "status": job.status, "phase": job.phase}


def _age_endpoint_health(ep: O1AdaptorEndpoint, now: datetime.datetime) -> None:
    """Ages a single endpoint's health status in place if it has missed its
    heartbeat window. Shared by the bulk `/discover` sweep below and by
    write_configuration_changes's own gate, so staleness is caught the
    moment it's actually consulted, not only when something has separately
    polled `/discover` first — no scheduler exists anywhere in this build
    (same elision as DME's producer health), so a live-computed check at the point of use is this
    build's substitute for a periodic sweep.
    """
    if ep.health_status == "ACTIVE" and ep.last_heartbeat_at and now - as_utc(ep.last_heartbeat_at) > MISSED_HEARTBEAT_THRESHOLD:
        ep.health_status = ENDPOINT_HEALTH_FSM.fire(EndpointHealth.ACTIVE, EndpointEvent.MISSED_HEARTBEATS)


@app.post("/o1-adaptor-endpoints/discover")
def discover_endpoints(db: Session = Depends(get_session)):
    """RAN NF OAM LLD section 1.2/5.2 — the endpoint discovery loop, meant
    to run on a timer against the MnS Registry NRM. Phase 1: still a
    heartbeat-aging stub rather than real registry polling — there is no
    real MnS Registry NRM in this build to poll — but staleness is no
    longer only visible through this route: write_configuration_changes's
    own gate now ages an endpoint live at dispatch time too (see
    `_age_endpoint_health`), so a stale endpoint can't silently pass a
    write attempt just because nothing called this route first. This route
    stays as the bulk equivalent of a registry poll — check every endpoint
    at once, e.g. from an operator dashboard or an external timer.
    """
    endpoints = db.scalars(select(O1AdaptorEndpoint)).all()
    now = datetime.datetime.now(datetime.UTC)
    for ep in endpoints:
        _age_endpoint_health(ep, now)
    db.commit()
    return {"checked": len(endpoints)}


@app.post("/o1-adaptor-endpoints/{endpoint_id}/heartbeat")
def endpoint_heartbeat(endpoint_id: uuid.UUID, db: Session = Depends(get_session)):
    # 404 O1_ENDPOINT_NOT_FOUND for an unknown endpoint. Records the time; DISCOVERED or DEGRADED becomes ACTIVE. The first heartbeat (from DISCOVERED) applies an `autoApply` onboarding template; a failure of that never fails the heartbeat. The caller's scope is not asked.
    ep = db.get(O1AdaptorEndpoint, endpoint_id)
    if ep is None:
        raise framework_error(FrameworkError.O1_ENDPOINT_NOT_FOUND, detail=f"unknown endpointId {endpoint_id}")
    return _record_heartbeat(db, ep)


def _record_heartbeat(db: Session, ep: O1AdaptorEndpoint) -> dict:
    """A heartbeat of an endpoint, shared by its route and the VES receiver (SB-7.3): the time, and DISCOVERED or DEGRADED becomes ACTIVE."""
    ep.last_heartbeat_at = datetime.datetime.now(datetime.UTC)
    current = EndpointHealth(ep.health_status) if ep.health_status in EndpointHealth.__members__.values() else EndpointHealth.DISCOVERED
    if current in (EndpointHealth.DISCOVERED, EndpointHealth.DEGRADED):
        ep.health_status = ENDPOINT_HEALTH_FSM.fire(current, EndpointEvent.HEARTBEAT)
    db.commit()
    if current == EndpointHealth.DISCOVERED:
        lifecycle.on_first_heartbeat(db, ep.managed_element_ref)       # MGT-14.3: an autoApply onboarding template is written now (never fails the heartbeat)
    return {"endpointId": str(ep.endpoint_id), "healthStatus": ep.health_status}


# ---------------------------------------------------------------- VES event receiver (SB-7; the mapping and the schema are in ves.py)

def _ves_listener_auth(request: Request) -> None:
    ves.authenticate(request.headers.get("authorization"))


# Per-event result in the VES answer: the event's position, its domain, what became of it, and the fixed codes that say why.
class VesResult(BaseModel):
    index: int
    domain: str
    outcome: Literal["APPLIED", "PARTIAL", "IGNORED", "REJECTED"]
    codes: list[str]


# Response body of the VES listener: events received, how many were applied (or partly applied), and one result per event.
class VesAnswer(BaseModel):
    events: int
    applied: int
    results: list[VesResult]


def _ves_alarm(db: Session, action: ves.AlarmAction) -> tuple[bool, str]:
    """SB-7.2: raise, update or clear the alarm of one VES fault event. An open alarm of the same element and condition is the same alarm."""
    if db.get(ManagedEntity, action.managed_element_ref) is None:
        return False, FrameworkError.MANAGED_ENTITY_NOT_FOUND[0]
    open_alarm = db.scalars(select(Alarm).where(Alarm.managed_element_ref == action.managed_element_ref, Alarm.source_alarm_id == action.source_alarm_id,
                                                Alarm.severity != "cleared").order_by(Alarm.raised_at.desc())).first()
    now = datetime.datetime.now(datetime.UTC)
    if action.severity is None:
        if open_alarm is None:
            return True, "NO_OPEN_ALARM"
        open_alarm.severity, open_alarm.cleared_at, open_alarm.clear_user_id, open_alarm.changed_at = "cleared", now, "ves", now
        db.commit()
        return True, "ALARM_CLEARED"
    if open_alarm is not None:
        if open_alarm.severity == action.severity:
            return True, "ALARM_ALREADY_OPEN"
        open_alarm.severity, open_alarm.changed_at = action.severity, now
        db.commit()
        return True, "ALARM_UPDATED"
    _raise_alarm(db, source_alarm_id=action.source_alarm_id, managed_element_ref=action.managed_element_ref, severity=action.severity,
                 probable_cause=action.probable_cause, specific_problem=action.specific_problem)
    return True, "ALARM_RAISED"


def _ves_heartbeat(db: Session, action: ves.HeartbeatAction) -> tuple[bool, str]:
    """SB-7.3: the heartbeat of the O1 adaptor endpoint of the element the event is from."""
    me = db.get(ManagedEntity, action.managed_element_ref)
    if me is None:
        return False, FrameworkError.MANAGED_ENTITY_NOT_FOUND[0]
    endpoint = db.get(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id) if me.o1_adaptor_endpoint_id else None
    if endpoint is None:
        return False, FrameworkError.O1_ENDPOINT_NOT_FOUND[0]
    _record_heartbeat(db, endpoint)
    return True, "HEARTBEAT_RECORDED"


def _ves_pm(db: Session, action: ves.PmReport) -> tuple[bool, str]:
    """SB-7.4: one PM report, by the path `POST /pm-reports` takes (a PM subscription on the counter is needed, as there)."""
    try:
        _accept_pm_report(db, PmReportRequest.model_validate({"managedElementRef": action.managed_element_ref, "counterType": action.counter_type,
                                                              "measurements": action.measurements}))
    except HTTPException as exc:
        db.rollback()
        return False, str(exc.detail["title"]) if isinstance(exc.detail, dict) else "REJECTED"
    return True, "PM_REPORT_ACCEPTED"


def _ves_apply(db: Session, action: ves.Action) -> tuple[bool, str]:
    """Applies one VES action through the shared helpers and returns (succeeded, fixed code). A refusal by a helper (HTTPException) is rolled back and returned as (False, its error title); nothing is raised, so one refused event never stops the others in a post.
    """
    try:
        if isinstance(action, ves.AlarmAction):
            return _ves_alarm(db, action)
        if isinstance(action, ves.HeartbeatAction):
            return _ves_heartbeat(db, action)
        return _ves_pm(db, action)
    except HTTPException as exc:                 # a refusal of the shared helpers (a service the element does not offer, a malformed ref): its fixed code, not its text
        db.rollback()
        return False, str(exc.detail["title"]) if isinstance(exc.detail, dict) else "REJECTED"


@app.post("/ves/eventListener/v7", status_code=202, response_model=VesAnswer, dependencies=[Depends(_ves_listener_auth)],
          openapi_extra={"security": [{"vesBasicAuth": []}]}, tags=["ves"],
          responses={400: {"description": "the body is not a VES post: no event or eventList, a header member missing or of the wrong type"},
                     401: {"description": "no valid Basic credentials"}, 404: {"description": "the listener is not enabled (no password configured)"}})
@app.post("/ves/eventListener/v7/eventBatch", status_code=202, response_model=VesAnswer, dependencies=[Depends(_ves_listener_auth)],
          openapi_extra={"security": [{"vesBasicAuth": []}]}, tags=["ves"], include_in_schema=False)
def receive_ves_events(body: ves.VesEnvelope, db: Session = Depends(get_session)):
    """SB-7.1: a VES event (`{"event": {...}}`) or a batch (`{"eventList": [...]}`) from an O1 adaptor, HTTP Basic (SB-7.5). The whole post is checked first (a header that
    is not a VES `commonEventHeader` is a 400 and nothing is applied); then each event is applied on its own and the 202 says what became of it: `APPLIED`,
    `PARTIAL` (a measurement event with several reports, some refused), `IGNORED` (a domain this receiver does not map) or `REJECTED` (an unknown element, a PM report
    with no PM subscription, a service the element does not offer), each with fixed codes. A refused event does not stop the others, and is not a 4xx: the sender
    would send it again for ever. Not behind the R1 gateway; see `ves.py`."""
    parsed = ves.parse_events(ves.events_of(body))
    results = []
    for event in parsed:
        try:
            actions = ves.actions_for(event)
        except ves.Unsupported as why:
            results.append(VesResult(index=event.index, domain=event.header.domain, outcome="IGNORED", codes=[why.reason]))
            continue
        done = [_ves_apply(db, action) for action in actions]
        ok = sum(1 for good, _ in done if good)
        results.append(VesResult(index=event.index, domain=event.header.domain, outcome="APPLIED" if ok == len(done) else ("PARTIAL" if ok else "REJECTED"),
                                 codes=sorted({code for _, code in done})))
    return VesAnswer(events=len(results), applied=sum(1 for r in results if r.outcome in ("APPLIED", "PARTIAL")), results=results)


def _alarm_view(a: Alarm) -> dict:
    """The JSON view of an alarm: the stored lower-case `severity`, the upper-case `perceivedSeverity`, and the class and id of its managed function split from its DN.
    """
    return {"alarmId": str(a.alarm_id), "sourceAlarmId": a.source_alarm_id, "managedElementRef": a.managed_element_ref,
            "managedFunctionRef": a.managed_function_ref,
            "managedFunctionClass": leaf_class(a.managed_function_ref), "managedFunctionId": leaf_id(a.managed_function_ref),
            "severity": a.severity, "perceivedSeverity": a.severity.upper(), "ackState": a.ack_state,
            "raisedAt": a.raised_at.isoformat() if a.raised_at else None, "correlationGroup": a.correlation_group,
            "probableCause": a.probable_cause, "specificProblem": a.specific_problem,
            "rootCauseIndicator": a.root_cause_indicator,
            "correlatedNotifications": [str(c) for c in a.correlated_notifications],
            "proposedRepairActions": a.proposed_repair_actions, "alarmType": a.alarm_type,
            "ackUserId": a.ack_user_id, "changedAt": a.changed_at.isoformat() if a.changed_at else None,
            "clearedAt": a.cleared_at.isoformat() if a.cleared_at else None, "clearUserId": a.clear_user_id,
            # PR-GUI-9.8: the console's names; `clearTime` is `clearedAt` again, `ackTime` is new (null while unacknowledged)
            "ackTime": as_utc(a.acknowledged_at).isoformat() if a.acknowledged_at else None,
            "clearTime": as_utc(a.cleared_at).isoformat() if a.cleared_at else None}


# ---------------------------------------------------------------- list reads (GUI pass)
# PM subscriptions, O1 adaptor endpoints, CM write jobs and software jobs were
# all write-only (or read-by-id only): an operator had no way to see what was
# registered without already holding every id.

@app.get("/pm-subscriptions")
def list_pm_subscriptions(request: Request, managed_element_ref: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                           db: Session = Depends(get_session)):
    # Paged list, optionally of one element. A caller with a scope claim and a managed caller (MSAC switch on) see only the subscriptions of the elements they may touch and read.
    stmt = scoping.scoped_to_elements(select(PMSubscription), scoping.request_scope(request), PMSubscription.managed_element_ref)      # PR-SEC-10.6
    stmt = msac.readable(stmt, db, request, PMSubscription.managed_element_ref)                                                         # MGT-2.6
    if managed_element_ref:
        stmt = stmt.where(PMSubscription.managed_element_ref == managed_element_ref)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"subscriptionId": str(s.subscription_id), "managedElementRef": s.managed_element_ref,
             "counterType": s.counter_type, "deliveryMethod": s.delivery_method,
             "southboundEngine": s.southbound_engine, "granularityPeriod": s.granularity_period}
            for s in page["items"]]}


@app.delete("/pm-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_pm(subscription_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """`docs/call-flows/20-alarm-pm-subscription-lifecycle.md`'s own
    gap, closed: every other subscription-shaped resource in this build
    (DME's type subscriptions, MDAF's, Intent
    Service's RMIH registration, MLMF's) has a real unsubscribe route —
    `PMSubscription` could previously only be created and listed, never
    torn down through this build's own API. Idempotent, matching all of
    those.
    """
    sub = db.get(PMSubscription, subscription_id)
    if sub is not None and scoping.element_permitted(db, scoping.request_scope(request), sub.managed_element_ref) \
            and msac.may_read(db, request, sub.managed_element_ref):                    # PR-SEC-10.6, MGT-2.6: one outside the scope, or not readable to the caller, is left alone, and 204 says nothing
        db.delete(sub)
        db.commit()


@app.post("/fm-subscriptions")
def subscribe_fm(managed_element_ref: str, delivery_method: str, request: Request, db: Session = Depends(get_session)):
    """SubscribeFM — HISTORY.md OI-6.7, closed: mirrors
    subscribe_pm's own DME-producer registration wrapper shape exactly
    (RAN NF OAM LLD section 3.5's SubscribePM pattern), for alarms
    instead of PM counters. Unlike PM, there is no per-counter-type
    identity — every ME's fault records register under one shared
    `RAN.FaultRecords` DME type, joined many-to-many across every
    managed element that subscribes (the same DMEType join behavior
    call flow 11 walks for any other multi-producer type). Gives an
    rApp/AI-ML model DME-mediated visibility into outstanding-active/
    historical alarms — it does NOT give DME or a consuming rApp any way
    to clear an alarm; that stays RAN NF OAM's own
    PATCH /alarms/{alarm_id}/clear, called by the source NF or an
    operator, unaffected by whether FM is DME-registered.
    """
    scoping.require_elements(db, scoping.request_scope(request), [managed_element_ref])        # PR-SEC-10.6: 403 SCOPE_DENIED outside the caller's scope
    _require_msac(db, request, "read", managed_element_ref)                                    # MGT-2.2: a subscription is the right to be sent the element's alarms
    if db.get(ManagedEntity, managed_element_ref) is None:      # the subscription refers to the element: no element, no subscription (a 404, not the foreign key's 500)
        raise framework_error(FrameworkError.MANAGED_ENTITY_NOT_FOUND, detail=f"no managed element {managed_element_ref!r}")
    require_service(db, managed_element_ref, "FM")  # Wave 9 (W9-01)
    engine = {"pull": "FaultMnS", "push": "FaultMnS", "stream": "StreamingDataReporting"}.get(delivery_method, "FaultMnS")
    sub = FMSubscription(managed_element_ref=managed_element_ref, delivery_method=delivery_method, southbound_engine=engine)
    db.add(sub)
    db.commit()

    r1 = R1Client()
    r1.post("/dme/production-capabilities", json={
        "namespace": "RAN", "name": "FaultRecords", "version": "1.0.0",
        "typeName": "RAN.FaultRecords", "producerId": "ran-nf-oam",
        "dataProductionSchema": {}, "producerHealthCallbackUrl": f"{SELF_URL}/health",
        "jobCallbackUrl": f"{SELF_URL}/dme-jobs",
    })
    return {"subscriptionId": str(sub.subscription_id), "southboundEngine": engine}


@app.get("/fm-subscriptions")
def list_fm_subscriptions(request: Request, managed_element_ref: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                           db: Session = Depends(get_session)):
    # Paged list, optionally of one element, filtered by scope and by MSAC read rules as the PM list is.
    stmt = scoping.scoped_to_elements(select(FMSubscription), scoping.request_scope(request), FMSubscription.managed_element_ref)      # PR-SEC-10.6
    stmt = msac.readable(stmt, db, request, FMSubscription.managed_element_ref)                                                         # MGT-2.6
    if managed_element_ref:
        stmt = stmt.where(FMSubscription.managed_element_ref == managed_element_ref)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"subscriptionId": str(s.subscription_id), "managedElementRef": s.managed_element_ref,
             "deliveryMethod": s.delivery_method, "southboundEngine": s.southbound_engine}
            for s in page["items"]]}


@app.delete("/fm-subscriptions/{subscription_id}", status_code=204)
def unsubscribe_fm(subscription_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Idempotent, matching pm-subscriptions' own unsubscribe route and
    every other subscription-shaped resource in this build.
    """
    sub = db.get(FMSubscription, subscription_id)
    if sub is not None and scoping.element_permitted(db, scoping.request_scope(request), sub.managed_element_ref) \
            and msac.may_read(db, request, sub.managed_element_ref):                    # PR-SEC-10.6, MGT-2.6: one outside the scope, or not readable to the caller, is left alone
        db.delete(sub)
        db.commit()


@app.get("/o1-adaptor-endpoints")
def list_o1_adaptor_endpoints(request: Request, health_status: str | None = None, region: str | None = scoping.RegionFilter,
                               site_cluster: str | None = scoping.SiteClusterFilter, limit: int = PageLimit, offset: int = PageOffset,
                               db: Session = Depends(get_session)):
    """The registered O1 adaptor endpoints, one per managed element, with the element's `region` and `tenant` (PR-SEC-10.2). A caller with a scope claim sees only the
    endpoints of the elements inside it (PR-SEC-10.6). `region` and `site_cluster` keep the endpoints of the elements of that place (PR-GUI-9.3)."""
    stmt = scoping.scoped_to_elements(select(O1AdaptorEndpoint), scoping.request_scope(request), O1AdaptorEndpoint.managed_element_ref)
    stmt = scoping.narrowed_to_place(stmt, O1AdaptorEndpoint.managed_element_ref, region, site_cluster)
    stmt = msac.readable(stmt, db, request, O1AdaptorEndpoint.managed_element_ref)                         # MGT-2.6
    if health_status:
        stmt = stmt.where(O1AdaptorEndpoint.health_status == health_status)
    page = paginate(db, stmt, limit, offset)
    places = {row.managed_element_ref: (row.region, row.tenant) for row in db.execute(          # column select: not a possibly stale entity
        select(ManagedEntity.managed_element_ref, ManagedEntity.region, ManagedEntity.tenant)
        .where(ManagedEntity.managed_element_ref.in_([ep.managed_element_ref for ep in page["items"]])))} if page["items"] else {}
    return {**page, "items": [{"endpointId": str(ep.endpoint_id), "managedElementRef": ep.managed_element_ref, "adaptorUri": ep.adaptor_uri,
             "protocolSupport": ep.protocol_support, "registeredVia": ep.registered_via, "transport": ep.transport, "credentialRef": ep.credential_ref, "healthStatus": ep.health_status,
             "lastHeartbeatAt": ep.last_heartbeat_at.isoformat() if ep.last_heartbeat_at else None,
             "supportedServices": ep.supported_services,
             "region": places.get(ep.managed_element_ref, (None, None))[0], "tenant": places.get(ep.managed_element_ref, (None, None))[1]}
            for ep in page["items"]]}


@app.get("/config-jobs")
def list_write_config_jobs(request: Request, status: str | None = None, region: str | None = scoping.RegionFilter,
                            site_cluster: str | None = scoping.SiteClusterFilter, limit: int = PageLimit, offset: int = PageOffset,
                            db: Session = Depends(get_session)):
    """The config jobs. `status` narrows; `region` and `site_cluster` (PR-GUI-9.3) keep the jobs with at least one target element (a sub-change's
    element) in that place. A scoped caller sees only jobs all of whose elements are inside its scope (an unregistered element counts as outside);
    a managed caller does not see jobs that wrote to an element it may not read; a scoped rApp sees only its own jobs, including the rollbacks of
    them (PR-SEC-10.11)."""
    stmt = select(WriteConfigJob)
    place = scoping.place_refs(region, site_cluster)
    if place is not None:
        stmt = stmt.where(select(WriteConfigSubChange.id).where(WriteConfigSubChange.job_id == WriteConfigJob.job_id,
                                                                WriteConfigSubChange.managed_element_ref.in_(place)).exists())
    scope = scoping.request_scope(request)
    if scope is not None:
        # PR-SEC-10: only the jobs all of whose elements are inside the caller's scope (an element not registered counts as outside it)
        outside = (select(WriteConfigSubChange.id).outerjoin(ManagedEntity, ManagedEntity.managed_element_ref == WriteConfigSubChange.managed_element_ref)
                   .where(WriteConfigSubChange.job_id == WriteConfigJob.job_id,
                          ManagedEntity.managed_element_ref.is_(None) | authz_scope.denied_condition(scope, ManagedEntity.region, ManagedEntity.tenant)))
        stmt = stmt.where(~outside.exists())
    unreadable = _unreadable_elements(db, request, WriteConfigSubChange.managed_element_ref)                  # MGT-2.6: a job that wrote to an element the caller's access rules do not let it read is left out
    if unreadable:
        stmt = stmt.where(~select(WriteConfigSubChange.id).where(WriteConfigSubChange.job_id == WriteConfigJob.job_id,
                                                                 WriteConfigSubChange.managed_element_ref.in_(unreadable)).exists())
    owner = _job_owner_filter(request)
    if owner is not False:                                                                                    # PR-SEC-10.11: a scoped rApp sees the jobs that are its own
        stmt = stmt.where(WriteConfigJob.job_id.in_(scoping.owned_jobs(owner)))
    if status:
        stmt = stmt.where(WriteConfigJob.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"jobId": str(j.job_id), "requestedBy": j.requested_by, "accessScope": j.scope, "scope": j.scope,
             "status": j.status, "msacRole": j.msac_role} for j in page["items"]]}


@app.get("/software-management-jobs")
def list_software_management_jobs(request: Request, managed_element_ref: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                                   db: Session = Depends(get_session)):
    # Paged list, optionally of one element, filtered by scope and MSAC read rules; campaign and rollback links are included for jobs that have them.
    stmt = scoping.scoped_to_elements(select(SoftwareManagementJob), scoping.request_scope(request), SoftwareManagementJob.managed_element_ref)     # PR-SEC-10.6
    stmt = msac.readable(stmt, db, request, SoftwareManagementJob.managed_element_ref)                                                              # MGT-2.6
    if managed_element_ref:
        stmt = stmt.where(SoftwareManagementJob.managed_element_ref == managed_element_ref)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"jobId": str(j.job_id), "managedElementRef": j.managed_element_ref, "ruInstanceId": j.ru_instance_id,
             "phase": j.phase, "status": j.status,
             **({"campaignId": str(j.campaign_id), "campaignWave": j.campaign_wave} if j.campaign_id else {}),
             **({"rollbackOf": str(j.rollback_of)} if j.rollback_of else {})} for j in page["items"]]}


# Wave 9 — multi-vendor capability registry, CM schemas, cell guards (vendors.py)
app.include_router(fleet.router)         # before the vendors router: `/managed-entities/health` must not be read as an element ref
app.include_router(vendors_router)
app.include_router(lifecycle.router)      # MGT-14, MGT-15: onboarding templates, element onboarding, software campaigns
lifecycle.bind_main(sys.modules[__name__])
