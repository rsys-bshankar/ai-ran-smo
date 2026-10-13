"""RFC 7807 ProblemDetails — the error model every R1 service group defers
to (CAPIF/TS 29.222 for SME, generic TS 29.500/29.501 for DME/AI-ML-Workflow,
TS 28.532/28.111 for CM/FM).
"""

from fastapi import HTTPException
from pydantic import BaseModel


# Published in the OpenAPI documents as the error body schema, so it carries no docstring (see CODE_DOCUMENTATION.md). RFC 7807 fields: `type`,
# `title` (the error code, one of the names in `FrameworkError`), `status` (the HTTP status), `detail`, `instance`. Routes raise it through
# `problem()` / `framework_error()`, which wrap it in an HTTPException, so the body on the wire is {"detail": {...ProblemDetails...}}.
class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


def problem(status: int, title: str, detail: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ProblemDetails(title=title, status=status, detail=detail).model_dump(),
    )


# Framework-level errors introduced across the LLD passes — never inherited
# from an underlying protocol, these are this SMO's own.
class FrameworkError:
    # RAN NF OAM LLD section 7
    """The catalogue of this SMO's own error codes: each attribute is a `(code, HTTP status)` pair, raised with `framework_error(FrameworkError.X,
    detail=...)`.

    The code is the `title` of the ProblemDetails body and is what clients and tests match on, so a code, once published, is renamed or re-statused
    only as an API change. The comments between the attributes name the design pass or requirement that introduced a group. Codes here are never
    inherited from an underlying protocol.
    """
    ENDPOINT_UNREACHABLE = ("ENDPOINT_UNREACHABLE", 503)
    SCHEMA_VALIDATION_FAILED = ("SCHEMA_VALIDATION_FAILED", 422)
    MSAC_ACCESS_DENIED = ("MSAC_ACCESS_DENIED", 403)
    PROTOCOL_NOT_SUPPORTED = ("PROTOCOL_NOT_SUPPORTED", 409)
    # AI/ML Workflow LLD section 7
    MODEL_NOT_CERTIFIED = ("MODEL_NOT_CERTIFIED", 409)
    NODE_GROUP_NOT_CLEARED = ("NODE_GROUP_NOT_CLEARED", 403)
    COORDINATION_GROUP_MISMATCH = ("COORDINATION_GROUP_MISMATCH", 422)
    COORDINATION_GROUP_TOO_SMALL = ("COORDINATION_GROUP_TOO_SMALL", 422)
    INFERENCE_MODEL_NOT_ACTIVE = ("INFERENCE_MODEL_NOT_ACTIVE", 409)
    MODEL_ALREADY_REGISTERED = ("MODEL_ALREADY_REGISTERED", 409)
    MODEL_IDENTITY_IMMUTABLE = ("MODEL_IDENTITY_IMMUTABLE", 400)
    MODEL_ACCESS_DENIED = ("MODEL_ACCESS_DENIED", 403)
    MODEL_OWNER_IMMUTABLE = ("MODEL_OWNER_IMMUTABLE", 403)
    MODEL_EXPIRED = ("MODEL_EXPIRED", 410)
    FEATURE_GROUP_NAME_INVALID = ("FEATURE_GROUP_NAME_INVALID", 400)
    FEATURE_GROUP_ALREADY_REGISTERED = ("FEATURE_GROUP_ALREADY_REGISTERED", 409)
    # OI-5-aiml-featuregroup-dme: an enable_dme feature group without a DME
    # type, or whose DME data job DME refused; an unknown group.
    FEATURE_GROUP_DME_JOB_REFUSED = ("FEATURE_GROUP_DME_JOB_REFUSED", 422)
    FEATURE_GROUP_NOT_FOUND = ("FEATURE_GROUP_NOT_FOUND", 404)
    # Wave 2 (AI Platform Service Decomposition) — AIMgF's own two real
    # state machines, docs/ARCHITECTURE.md (AIMgF)
    LIFECYCLE_ILLEGAL_TRANSITION = ("LIFECYCLE_ILLEGAL_TRANSITION", 409)
    GOVERNANCE_DECIDER_REQUIRED = ("GOVERNANCE_DECIDER_REQUIRED", 422)
    # Wave 3 (AI Platform Service Decomposition) — DME's revised dual
    # data-plane + O1-mediation role, docs/ARCHITECTURE.md (DME)
    DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE = ("DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE", 422)
    DME_ARTIFACT_NOT_FOUND = ("DME_ARTIFACT_NOT_FOUND", 422)
    # Wave 3 — AIMgF's TrainingJob suspend/resume, HISTORY.md §7's
    # AI/ML Workflow section item 6
    TRAINING_JOB_ILLEGAL_TRANSITION = ("TRAINING_JOB_ILLEGAL_TRANSITION", 409)
    # Wave 3 — Intent Service's consumer-side RMIH selection,
    # docs/ARCHITECTURE.md's Intent Service Wave 3 resolution
    RMIH_CAPABILITY_MISMATCH = ("RMIH_CAPABILITY_MISMATCH", 422)
    # CAPIF event subscriptions (SME)
    SUBSCRIPTION_SCOPE_CONFLICT = ("SUBSCRIPTION_SCOPE_CONFLICT", 422)
    # Foundational Platform LLD section 5
    SERVICE_NAME_CONFLICT = ("SERVICE_NAME_CONFLICT", 409)
    DME_TYPE_VERSION_CONFLICT = ("DME_TYPE_VERSION_CONFLICT", 409)
    # HISTORY.md §7 — DME vs. the real ICS API, Producer/Type conflation
    # finding, closed: a real ICS deleteInfoType 409 ("has one or several
    # active producers"), and its own 404 for an unknown type — the type
    # itself never had a dedicated not-found code before since it was
    # never independently addressable from its producer.
    DME_TYPE_NOT_FOUND = ("DME_TYPE_NOT_FOUND", 404)
    DME_TYPE_HAS_ACTIVE_PRODUCERS = ("DME_TYPE_HAS_ACTIVE_PRODUCERS", 409)
    DELIVERY_METHOD_NOT_OFFERED = ("DELIVERY_METHOD_NOT_OFFERED", 409)
    DATA_JOB_TARGET_IMMUTABLE = ("DATA_JOB_TARGET_IMMUTABLE", 400)
    APF_NOT_REGISTERED = ("APF_NOT_REGISTERED", 403)
    # SO/SA SMOS LLD section 2.1; OI-1-sa-rollback: nothing to roll back to —
    # a rApp instance with no un-rolled-back upgrade in its version history, or
    # an SA SMOS monitor whose target keeps no version history at all.
    ROLLBACK_HISTORY_UNAVAILABLE = ("ROLLBACK_HISTORY_UNAVAILABLE", 409)
    # NFO+FOCOM LLD section 4
    NFDEPLOYMENT_NAME_CONFLICT = ("NFDEPLOYMENT_NAME_CONFLICT", 409)
    NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED = ("NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED", 409)
    NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND = ("NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND", 422)
    NFDEPLOYMENT_ILLEGAL_OPERATION = ("NFDEPLOYMENT_ILLEGAL_OPERATION", 409)
    # Onboarding/rApp Mgmt LLD section 5-6
    RAPP_INSTANCE_NOT_UNDEPLOYED = ("RAPP_INSTANCE_NOT_UNDEPLOYED", 409)
    # SME Trusted Invokers (CAPIF core securityapi)
    INVOKER_NOT_REGISTERED = ("INVOKER_NOT_REGISTERED", 400)
    TRUSTED_INVOKER_NOT_FOUND = ("TRUSTED_INVOKER_NOT_FOUND", 404)
    SECURITY_CONTEXT_INVALID = ("SECURITY_CONTEXT_INVALID", 422)
    # Wave 3 (cross-cutting standardization) — Error Schema: every raw
    # HTTPException(status_code=404, detail="no such ...") across every
    # service now carries a named, importable code through this same
    # ProblemDetails convention, rather than a bare string only a human
    # ever reads.
    SOFTWARE_JOB_NOT_FOUND = ("SOFTWARE_JOB_NOT_FOUND", 404)
    UPSTREAM_FAILED = ("UPSTREAM_FAILED", 502)
    LCM_OPERATION_NOT_FOUND = ("LCM_OPERATION_NOT_FOUND", 404)
    SERVICE_ORDER_NOT_FOUND = ("SERVICE_ORDER_NOT_FOUND", 404)
    VALUE_OUT_OF_RANGE = ("VALUE_OUT_OF_RANGE", 422)
    # a request that names something the database does not hold (foreign key), repeats something it holds once (unique) or breaks a rule the
    # column states (check): the caller's input, found by the authenticated DAST scan (V-7d) as plain-text 500s
    REFERENCED_RESOURCE_NOT_FOUND = ("REFERENCED_RESOURCE_NOT_FOUND", 422)
    RESOURCE_ALREADY_EXISTS = ("RESOURCE_ALREADY_EXISTS", 409)
    CONSTRAINT_VIOLATED = ("CONSTRAINT_VIOLATED", 422)
    INTERNAL_ERROR = ("INTERNAL_ERROR", 500)
    ALARM_NOT_FOUND = ("ALARM_NOT_FOUND", 404)
    O1_ENDPOINT_NOT_FOUND = ("O1_ENDPOINT_NOT_FOUND", 404)
    O1_HOST_KEY_NOT_FOUND = ("O1_HOST_KEY_NOT_FOUND", 404)
    MANAGED_OBJECT_NOT_FOUND = ("MANAGED_OBJECT_NOT_FOUND", 404)
    # MGT-1.5..1.7: CM history, diff and rollback in RAN NF OAM
    CONFIG_JOB_NOT_FOUND = ("CONFIG_JOB_NOT_FOUND", 404)
    CM_SNAPSHOT_NOT_FOUND = ("CM_SNAPSHOT_NOT_FOUND", 404)
    KPI_NOT_FOUND = ("KPI_NOT_FOUND", 404)
    # AI-10.2: a per-rApp limit declared in its manifest
    RAPP_LIMIT_NOT_FOUND = ("RAPP_LIMIT_NOT_FOUND", 404)
    RAPP_RATE_LIMITED = ("RAPP_RATE_LIMITED", 429)
    RAPP_LIMIT_SELF_CHANGE = ("RAPP_LIMIT_SELF_CHANGE", 403)
    RAPP_KILLED = ("RAPP_KILLED", 403)                      # AI-10.4
    RAPP_BLAST_RADIUS_EXCEEDED = ("RAPP_BLAST_RADIUS_EXCEEDED", 403)      # AI-10.3
    RAPP_MAGNITUDE_EXCEEDED = ("RAPP_MAGNITUDE_EXCEEDED", 403)
    RAPP_KILL_NOT_FOUND = ("RAPP_KILL_NOT_FOUND", 404)
    SAFEGUARD_SUBSCRIPTION_NOT_FOUND = ("SAFEGUARD_SUBSCRIPTION_NOT_FOUND", 404)     # AI-10.6
    KPI_SCHEDULE_NOT_FOUND = ("KPI_SCHEDULE_NOT_FOUND", 404)                         # MSG-4
    # AI-11: a human approves an rApp's action before it is written; AI-13: the record of why an rApp acted
    APPROVAL_NOT_FOUND = ("APPROVAL_NOT_FOUND", 404)
    APPROVAL_POLICY_NOT_FOUND = ("APPROVAL_POLICY_NOT_FOUND", 404)
    APPROVAL_SUBSCRIPTION_NOT_FOUND = ("APPROVAL_SUBSCRIPTION_NOT_FOUND", 404)
    APPROVAL_NOT_PENDING = ("APPROVAL_NOT_PENDING", 409)
    APPROVAL_SELF_DECISION = ("APPROVAL_SELF_DECISION", 403)
    DECISION_RECORD_NOT_FOUND = ("DECISION_RECORD_NOT_FOUND", 404)
    # PR-SEC-14: caller roles
    ENROLLMENT_NOT_CONFIGURED = ("ENROLLMENT_NOT_CONFIGURED", 503)
    ENROLLMENT_REFUSED = ("ENROLLMENT_REFUSED", 403)
    ROLE_NOT_PERMITTED = ("ROLE_NOT_PERMITTED", 403)
    # PR-SEC-10: a caller whose scope claim does not cover a target (or a claim an operator or a registration sent that is not valid)
    SCOPE_DENIED = ("SCOPE_DENIED", 403)
    AUTHZ_SCOPE_INVALID = ("AUTHZ_SCOPE_INVALID", 422)
    ROLLBACK_NOT_POSSIBLE = ("ROLLBACK_NOT_POSSIBLE", 422)
    CONFIG_CHANGED_SINCE = ("CONFIG_CHANGED_SINCE", 409)
    # MGT-5: staged rollout of a CM job
    WAVE_PAUSE_NOT_ELAPSED = ("WAVE_PAUSE_NOT_ELAPSED", 409)
    MODEL_NOT_FOUND = ("MODEL_NOT_FOUND", 404)
    TRAINING_JOB_NOT_FOUND = ("TRAINING_JOB_NOT_FOUND", 404)
    VALIDATION_JOB_NOT_FOUND = ("VALIDATION_JOB_NOT_FOUND", 404)
    EMULATION_JOB_NOT_FOUND = ("EMULATION_JOB_NOT_FOUND", 404)
    MLMF_SUBSCRIPTION_NOT_FOUND = ("MLMF_SUBSCRIPTION_NOT_FOUND", 404)
    PRODUCER_NOT_FOUND = ("PRODUCER_NOT_FOUND", 404)
    TYPE_SUBSCRIPTION_NOT_FOUND = ("TYPE_SUBSCRIPTION_NOT_FOUND", 404)
    DATA_JOB_NOT_FOUND = ("DATA_JOB_NOT_FOUND", 404)
    DATA_OFFER_NOT_FOUND = ("DATA_OFFER_NOT_FOUND", 404)
    DME_ACTION_NOT_FOUND = ("DME_ACTION_NOT_FOUND", 404)
    RESOURCE_TYPE_NOT_FOUND = ("RESOURCE_TYPE_NOT_FOUND", 404)
    RESOURCE_POOL_NOT_FOUND = ("RESOURCE_POOL_NOT_FOUND", 404)
    DEPLOYMENT_MANAGER_NOT_FOUND = ("DEPLOYMENT_MANAGER_NOT_FOUND", 404)
    INTENT_HANDLING_FUNCTION_NOT_FOUND = ("INTENT_HANDLING_FUNCTION_NOT_FOUND", 404)
    INTENT_NOT_FOUND = ("INTENT_NOT_FOUND", 404)
    ARTIFACT_FORMAT_INVALID = ("ARTIFACT_FORMAT_INVALID", 415)
    ARTIFACT_VERSION_NOT_FOUND = ("ARTIFACT_VERSION_NOT_FOUND", 404)
    NFDEPLOYMENT_NOT_FOUND = ("NFDEPLOYMENT_NOT_FOUND", 404)
    RAPP_INSTANCE_NOT_FOUND = ("RAPP_INSTANCE_NOT_FOUND", 404)
    ASSURANCE_MONITOR_NOT_FOUND = ("ASSURANCE_MONITOR_NOT_FOUND", 404)
    PUBLISHING_FUNCTION_NOT_FOUND = ("PUBLISHING_FUNCTION_NOT_FOUND", 404)
    # HISTORY.md OI-6.1 — an explicit operator-approval gate on
    # Training->Validation->Emulation, mirroring the existing
    # CERTIFY/PROMOTE governance shape.
    TRAINING_NOT_APPROVED = ("TRAINING_NOT_APPROVED", 409)
    VALIDATION_NOT_APPROVED = ("VALIDATION_NOT_APPROVED", 409)
    # HISTORY.md OI-6.3 — rApp Autonomy Modes.
    AUTONOMY_DISPATCH_NOT_FOUND = ("AUTONOMY_DISPATCH_NOT_FOUND", 404)
    AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE = ("AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE", 409)
    # Wave 4 — TS 28.105 AI/ML NRM resources (aimgf/app/nrm.py, MLMR).
    NRM_OBJECT_NOT_FOUND = ("NRM_OBJECT_NOT_FOUND", 404)
    INFERENCE_FUNCTION_NOT_ACTIVATED = ("INFERENCE_FUNCTION_NOT_ACTIVATED", 409)
    MODEL_NOT_LOADED = ("MODEL_NOT_LOADED", 409)
    # Wave 5 — TS 28.104 MDARequest addressed to an MDAFunction.
    MDA_CAPABILITY_NOT_SUPPORTED = ("MDA_CAPABILITY_NOT_SUPPORTED", 422)
    # Wave 7 — runtime profiles (rApp package lookup) and inference reads.
    PACKAGE_NOT_FOUND = ("PACKAGE_NOT_FOUND", 404)
    # Wave 9 (HISTORY.md W9-01..06) — RAN NF OAM's
    # multi-vendor capability registry, docs/ARCHITECTURE.md
    O1_SERVICE_NOT_SUPPORTED = ("O1_SERVICE_NOT_SUPPORTED", 409)
    VENDOR_CAPABILITY_NOT_FOUND = ("VENDOR_CAPABILITY_NOT_FOUND", 404)
    CM_SCHEMA_NOT_FOUND = ("CM_SCHEMA_NOT_FOUND", 404)
    CM_SCHEMA_CONFLICT = ("CM_SCHEMA_CONFLICT", 409)
    MANAGED_ENTITY_NOT_FOUND = ("MANAGED_ENTITY_NOT_FOUND", 404)
    INFERENCE_JOB_NOT_FOUND = ("INFERENCE_JOB_NOT_FOUND", 404)
    # OI-2-lcm-error-mapping — Onboarding's usage/stop on an unknown (or
    # another package's) registration id, and an rApp upgrade resolved
    # after its upgradeTimeoutSeconds deadline (already rolled back).
    PACKAGE_USAGE_REGISTRATION_NOT_FOUND = ("PACKAGE_USAGE_REGISTRATION_NOT_FOUND", 404)
    RAPP_UPGRADE_TIMED_OUT = ("RAPP_UPGRADE_TIMED_OUT", 409)
    # DOC-CF-1.2 (call flow 06) — a package that an open usage registration (an rApp instance) still holds: deprime, delete and the release of
    # a registration whose instance still exists are refused; and a package no instance may be created from (not AVAILABLE or PRIMED).
    PACKAGE_IN_USE = ("PACKAGE_IN_USE", 409)
    PACKAGE_NOT_DEPLOYABLE = ("PACKAGE_NOT_DEPLOYABLE", 409)
    # PR-ST-2 — a row's version changed between load and write (another request
    # committed first); smo_shared/versioning.py. The caller repeats the request.
    CONCURRENT_MODIFICATION = ("CONCURRENT_MODIFICATION", 409)
    # PR-ST-3 — Idempotency-Key header on command routes; smo_shared/idempotency.py.
    IDEMPOTENCY_KEY_INVALID = ("IDEMPOTENCY_KEY_INVALID", 422)
    IDEMPOTENCY_KEY_REUSED = ("IDEMPOTENCY_KEY_REUSED", 422)
    IDEMPOTENCY_KEY_IN_PROGRESS = ("IDEMPOTENCY_KEY_IN_PROGRESS", 409)


def framework_error(code: tuple[str, int], detail: str | None = None) -> HTTPException:
    """Builds the HTTPException (not raised: the caller raises it) for a `FrameworkError` pair, as a ProblemDetails body with `detail` as the
    human-readable text.
    """
    title, status = code
    return problem(status, title, detail)


def illegal_transition_error(exc, subject: str) -> HTTPException:
    """409 LIFECYCLE_ILLEGAL_TRANSITION for a `smo_shared.statemachine.
    IllegalTransition` raised by a lifecycle route — naming the entity,
    its current state and the refused event, instead of the unhandled
    500 the exception otherwise becomes.
    """
    return framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                           detail=f"{subject}: event {exc.event} is not allowed in state {exc.state}")


def install_out_of_range_handler(app) -> None:
    """A number too large for the database column (Postgres `DataError`, SQLite or date arithmetic `OverflowError`) is the caller's input, so it
    answers 422 VALUE_OUT_OF_RANGE, not a 500. Found by the contract test (PR-V-3): a request with `9223372036854775808` in any integer field
    reached the database and failed there. Installed by `apply_r1_gateway_security`, which every module calls."""
    from fastapi.responses import JSONResponse
    from sqlalchemy.exc import DataError

    title, status = FrameworkError.VALUE_OUT_OF_RANGE

    async def _out_of_range(request, exc):  # noqa: ARG001
        body = ProblemDetails(title=title, status=status, detail="a value in the request is outside the range the service can store").model_dump()
        return JSONResponse(status_code=status, content={"detail": body})

    app.add_exception_handler(OverflowError, _out_of_range)
    app.add_exception_handler(DataError, _out_of_range)


_REFLECTING_ERROR_FIELDS = ("input", "ctx", "url")


def scrub_validation_errors(errors) -> list[dict]:
    """The framework's validation errors with every field that reflects the caller's input removed: `input` (the raw value), `ctx` (can carry
    the value or the exception built from it) and `url` (a pydantic documentation link). `type`, `loc` and `msg` stay, so a client still learns
    which parameter was refused and why."""
    return [{key: value for key, value in dict(error).items() if key not in _REFLECTING_ERROR_FIELDS} for error in errors]


def install_validation_error_handler(app) -> None:
    """A request the framework refuses (RequestValidationError) answers the same 422 `{"detail": [{type, loc, msg}, ...]}` as before, but without
    echoing the caller's input. FastAPI's default puts the raw value in each error's `input`, so a payload such as `total=true AND 1=1 --` came
    back inside the 422 body, and a scanner comparing the `1=1` and `1=2` answers read the differing echo as a boolean-based SQL injection
    (ZAP 40018, `HISTORY.md` QA-9.1); with the echo gone the two bodies are identical. Installed by `apply_r1_gateway_security`."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    async def _validation(request, exc):  # noqa: ARG001
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(scrub_validation_errors(exc.errors()))})

    app.add_exception_handler(RequestValidationError, _validation)


_SQLSTATE_FOREIGN_KEY, _SQLSTATE_UNIQUE, _SQLSTATE_CHECK = "23503", "23505", "23514"


def _orig(exc):
    """What the driver said: the wrapped error of a SQLAlchemy `DBAPIError`, else the error itself."""
    return getattr(exc, "orig", exc)


def _integrity_problem(exc) -> tuple[str, int, str]:
    """Which problem a database integrity error is: by its SQLSTATE when the driver gives one (Postgres), else by its words (SQLite, whose tests run the
    same routes). A state that is none of the three is none of them, whatever the message says."""
    orig = _orig(exc)
    state = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "diag", None), "sqlstate", None)
    text = str(orig).lower()
    foreign_key = state == _SQLSTATE_FOREIGN_KEY if state else "foreign key" in text
    unique = state == _SQLSTATE_UNIQUE if state else ("unique constraint" in text or "unique violation" in text)
    check = state == _SQLSTATE_CHECK if state else "check constraint" in text
    if foreign_key:
        return (*FrameworkError.REFERENCED_RESOURCE_NOT_FOUND, "the request refers to something that does not exist")
    if unique:
        return (*FrameworkError.RESOURCE_ALREADY_EXISTS, "the request repeats something that already exists")
    if check:
        return (*FrameworkError.CONSTRAINT_VIOLATED, "a value in the request is not one the service accepts")
    return (*FrameworkError.INTERNAL_ERROR, "the service could not complete the request")


def install_integrity_handlers(app) -> None:
    """The caller's input that the database refuses (a reference to a row that is not there, a duplicate, a value a CHECK rejects) answers 4xx with a
    problem document, not a bare-text 500; and any other unhandled error answers a problem document too (the exception is still raised on to the
    server's log, so nothing is hidden from the operator). Routes check what they can say precisely first; this is the net under them.
    Found by the authenticated DAST scan (V-7d). Installed by `apply_r1_gateway_security`, with `install_out_of_range_handler`."""
    import logging
    from fastapi.responses import JSONResponse
    from sqlalchemy.exc import IntegrityError

    log = logging.getLogger("smo.errors")

    async def _integrity(request, exc):
        title, status, detail = _integrity_problem(exc)
        log.warning("%s %s answered %s %s: %s", request.method, request.url.path, status, title, str(_orig(exc)).splitlines()[0][:200])
        return JSONResponse(status_code=status, content={"detail": ProblemDetails(title=title, status=status, detail=detail).model_dump()})

    async def _unhandled(request, exc):  # noqa: ARG001
        title, status = FrameworkError.INTERNAL_ERROR
        return JSONResponse(status_code=status, content={"detail": ProblemDetails(title=title, status=status, detail="the service could not complete the request").model_dump()})

    app.add_exception_handler(IntegrityError, _integrity)
    app.add_exception_handler(Exception, _unhandled)
