"""Software Package Onboarding SMOS: the HTTP routes that onboard an rApp package (a TOSCA CSAR) and govern its lifecycle.

What it is: the FastAPI app of the `onboarding` module (R1 route `/onboarding` via R1 Termination). `POST /packages` fetches a CSAR from a caller-given
location, validates it (`package_validation.py`), registers its artifacts, asks NFO to create the NF deployment descriptor, and leaves the package
AVAILABLE or FAILED. The other routes move the package through its lifecycle (`statemachine.py`), register usage by rApp instances, and list packages,
artifacts and usages.

Where it sits: rApp Management, AIMgF and the GUI BFF call it over R1; it calls NFO (`POST /nfo/descriptors`, and `DELETE /nfo/descriptors/{id}` when a
package is deleted) and, to release an orphaned usage registration, rApp Management (`GET /rapp-mgmt/instances/{id}`) through `R1Client`, and fetches the package
itself with a plain HTTP GET (not over R1). Design: SMO Design v1.3 section 3.4 and Onboarding/rApp Mgmt LLD sections 1-4; module README sections 1.5 and 2.

What it owns: the `application_package`, `artifact` and `package_usage_registration` tables (`models.py`). It does not store the package bytes, deploy
anything or register SME declarations (rApp Management does that per instance).

Before editing: the names re-exported from `package_validation` below (marked noqa F401) are imported from here by the tests and by
`fuzz/fuzz_csar_parsers.py`; keep them importable. `POST /packages` commits twice on purpose (see `onboard_package`).
"""

import logging
import os
import uuid
from typing import Literal

import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared import csar_signing
from smo_shared.logconfig import install_logging
from smo_shared.metrics import count_by, install_metrics, register_query_gauge
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import framework_error, FrameworkError, illegal_transition_error
from smo_shared.r1_client import R1Client
from smo_shared.statemachine import IllegalTransition
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.runtime_resources import container_resources
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.webhook import is_safe_webhook_destination
from smo_shared.versioning import install_concurrency_handler

from .package_validation import (EXECUTION_MODES, LIMIT_SPECS, PARSE_FAILURES, PackageValidationFailed, _asd_identity, _parse_ai_capabilities,  # noqa: F401  (re-exported: the tests and the fuzz target import them from here)
                                 _parse_sme_declarations, _validate_limits, _validate_runtime_profiles, validate_package_bytes)
from .models import ApplicationPackage, Artifact, PackageUsageRegistration
from .statemachine import ONBOARDING_FSM, PackageEvent, PackageState


class DescriptorCreationFailed(Exception):
    """NFO's CreateDescriptor call (NFO+FOCOM LLD section 2) did not answer 201.

    Raised by `_create_nf_deployment_descriptor`; listed in ONBOARD_VALIDATION_FAILURES, so the package ends FAILED like any other validation failure.
    """


# What counts as "this package fails to validate" in this route: everything package_validation.PARSE_FAILURES lists (bad zip, missing entry, malformed
# YAML or JSON, PackageValidationFailed), plus the package location being unreachable or answering an error (httpx.HTTPError) and NFO refusing the
# descriptor (DescriptorCreationFailed). Each of these ends the request in a FAILED package with a normal 202, not an HTTP error. Anything not listed
# here is not caught and is a 500.
ONBOARD_VALIDATION_FAILURES = (*PARSE_FAILURES, httpx.HTTPError, DescriptorCreationFailed)

log = logging.getLogger(__name__)

app = FastAPI(title="Software Package Onboarding SMOS")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
register_query_gauge("smo_rapp_packages", "rApp packages, by lifecycle state.", ["state"],
                     lambda s: count_by(s, ApplicationPackage.state, PackageState))  # PR-OBS-4
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


# Request body of POST /packages. `location` is caller-supplied and untrusted: it is checked for `.csar` and against the SSRF guard before it is fetched
# (`_validate_package`). `applicationType` is limited to the values the column's CHECK constraint accepts, so any other value is a 422 here and not a database error.
class OnboardRequest(BaseModel):
    location: str
    applicationType: Literal["rApp", "xApp", "CloudifiedNF", "PNF"] = "rApp"      # the column's CHECK (migrations/001_init.sql): any other value was a 500


@app.post("/packages", status_code=202)
def onboard_package(body: OnboardRequest, db: Session = Depends(get_session)):
    """OnboardPackage(location) — async. Onboarding LLD section 3's
    concretized validation pipeline: fetch, open TOSCA.meta, verify
    signature (internal-consistency only, D-SEC-ONBD-1), parse
    Definitions/<name>.yaml, register Artifacts, THEN reach AVAILABLE.
    """
    # Maintainer notes (not published). Answers 202 with {packageId, trackingId[, failureReason]} whenever the body is valid, whether or not the package validates:
    # the verdict is the package's state (AVAILABLE or FAILED), read from onboarding-status. A body without `location` or with an unknown applicationType is a 422.
    # Order of work: insert the row as ONBOARDING and commit; fetch and validate; reject a duplicate content hash; copy identity, artifacts and declarations onto the
    # row; ask NFO for the descriptor; fire VALIDATE_OK; set the state and commit. An exception in ONBOARD_VALIDATION_FAILURES fires VALIDATE_FAILED instead.
    # The identity fields and Artifact rows set before a late failure (NFO refusing the descriptor) are committed with the FAILED package; a failure during the
    # fetch or the validation never reaches them.
    # Any other exception (for example NFO answering 201 with a body that is not a UUID) also ends the package FAILED (call flow 06d): the session is rolled
    # back, so nothing written after the first commit is kept, and the row committed first is set FAILED, from where it can be deleted. Before DOC-CF-1.2 such
    # a package stayed in ONBOARDING for good.
    # `failureReason` is the exact message for PackageValidationFailed and only the exception class name for anything else, so internals are not returned;
    # it is logged and never stored.
    pkg = ApplicationPackage(
        application_type=body.applicationType,
        name="unresolved-until-validated",
        version="0.0.0",
        manifest_ref=body.location,
        state=PackageState.ONBOARDING,
    )
    db.add(pkg)
    # A real commit, not just flush() — _create_nf_deployment_descriptor
    # below calls NFO over a real network hop (a separate service, its own
    # DB connection in production), and NFO's own NFDeploymentDescriptor
    # row has a real FK on application_package.package_id. flush() only
    # makes this row visible within THIS session's own uncommitted
    # transaction; under Postgres's real READ COMMITTED isolation, NFO's
    # separate connection can't see it yet, so its own INSERT would hit a
    # genuine ForeignKeyViolation — a real bug (masked in this build's own
    # SQLite test harness, whose single shared StaticPool connection gives
    # every nested session an accidental dirty-read view of this session's
    # uncommitted work).
    db.commit()

    failure_reason: str | None = None
    try:
        entry_definitions, artifacts, integrity_hash, identity = _validate_package(body.location)
        # AsdDescriptorValidator's own duplicate-descriptor-id detection,
        # adapted to this build's own package identity (a content hash,
        # since real ASD descriptor data doesn't exist here) — a
        # byte-identical package already onboarded is rejected the same
        # way, not silently onboarded a second time. A package that is
        # DELETING (terminal: deleted, its row kept) or FAILED no longer
        # counts (OI-2-package-redeploy) — the same CSAR can be onboarded
        # again once its earlier package was deleted or failed.
        existing = db.scalar(select(ApplicationPackage).where(
            ApplicationPackage.integrity_hash == integrity_hash, ApplicationPackage.package_id != pkg.package_id,
            ApplicationPackage.state.not_in([PackageState.DELETING, PackageState.FAILED]),
        ).limit(1))
        if existing is not None:
            raise PackageValidationFailed(f"package with integrity hash {integrity_hash} already onboarded as {existing.package_id}")
        pkg.tosca_entry_definitions = entry_definitions
        pkg.integrity_hash = integrity_hash
        pkg.name = identity.get("name", pkg.name)
        pkg.version = identity.get("version", pkg.version)
        pkg.vendor = identity.get("vendor", pkg.vendor)
        pkg.descriptor_id = identity.get("descriptor_id")
        pkg.descriptor_invariant_id = identity.get("descriptor_invariant_id")
        pkg.descriptor_version = identity.get("descriptor_version")
        pkg.schema_version = identity.get("schema_version")
        pkg.ai_capabilities = identity.get("ai_capabilities")
        pkg.sme_declarations = identity.get("sme_declarations")
        # PR-RAPP-1: with no trust store configured this stays what it always was (the package validated; nothing was checked against a key); with one it is
        # true only for a package whose signature verified, and false for an unsigned one the policy lets through
        pkg.signature_verified = identity.get("signature_verified", True)
        if identity.get("signed_by"):
            log.info("package %s is signed by publisher %s", pkg.package_id, identity["signed_by"])
        for path, access_url in artifacts:
            db.add(Artifact(package_id=pkg.package_id, path=path, access_url=access_url))
        pkg.nf_deployment_descriptor_id = _create_nf_deployment_descriptor(pkg, entry_definitions)
        # The state machine only computes the target state; it is stored by `pkg.state = new_state` after the try block, so both outcomes commit in one place.
        new_state = ONBOARDING_FSM.fire(PackageState.ONBOARDING, PackageEvent.VALIDATE_OK, db=db, package=pkg)
    except ONBOARD_VALIDATION_FAILURES as exc:
        new_state = ONBOARDING_FSM.fire(PackageState.ONBOARDING, PackageEvent.VALIDATE_FAILED, db=db, package=pkg)
        # The message of a PackageValidationFailed is written for the operator (the place and the rule broken). Any other exception reports only its class name, so a library message (paths, URLs) is not returned.
        failure_reason = str(exc) if isinstance(exc, PackageValidationFailed) else type(exc).__name__
        log.warning("package %s failed validation: %s", pkg.package_id, failure_reason)
    except Exception as exc:  # noqa: BLE001  (call flow 06d: no package is left in ONBOARDING, whatever went wrong)
        log.exception("package %s failed to onboard", pkg.package_id)
        db.rollback()
        new_state = ONBOARDING_FSM.fire(PackageState.ONBOARDING, PackageEvent.VALIDATE_FAILED, db=db, package=pkg)
        failure_reason = type(exc).__name__

    pkg.state = new_state
    db.commit()
    accepted = {"packageId": str(pkg.package_id), "trackingId": str(pkg.package_id)}
    if failure_reason is not None:
        accepted["failureReason"] = failure_reason     # GUI-8.2: the precise message of a refused package (a declaration's place and rule); not stored
    return accepted


def _create_nf_deployment_descriptor(pkg: ApplicationPackage, entry_definitions: str) -> uuid.UUID:
    """Asks NFO to create the NF deployment descriptor for `pkg` and returns its id.

    NFO+FOCOM LLD section 2: the descriptor is derived from the package's TOSCA Definitions at onboarding time, so rApp Management deploys with a real
    nfDeploymentDescriptorId and not the packageId. Phase 1: `workloadTemplate` is a thin reference to the entry definitions, not a parsed TOSCA node template.
    Per-mode container resources from the manifest's `runtimeProfiles` are added as `containerResourcesByMode` (PR-RAPP-2.1); a package without profiles gets
    no such key.

    Calls NFO over R1 (`POST /nfo/descriptors`), which needs the `application_package` row to be committed already (see `onboard_package`). Raises
    DescriptorCreationFailed for any status other than 201.
    """
    workload: dict = {"toscaEntryDefinitions": entry_definitions}
    # PR-RAPP-2.1: the manifest's CPU and memory per execution mode as the container resources a deployment manager applies (smo_shared/runtime_resources.py);
    # absent for a package with no runtimeProfiles, so its descriptor is what it was
    # A mode whose profile gives no cpu or memory (a gpu-only profile) yields no resources (the walrus drops falsy results) and is left out of the map.
    by_mode = {mode: resources for mode, profile in ((pkg.ai_capabilities or {}).get("runtimeProfiles") or {}).items() if (resources := container_resources(profile))}
    if by_mode:
        workload["containerResourcesByMode"] = by_mode
    resp = R1Client().post("/nfo/descriptors", json={
        "packageId": str(pkg.package_id), "name": entry_definitions,
        "workloadTemplate": workload,
    })
    if resp.status_code != 201:
        raise DescriptorCreationFailed(f"NFO CreateDescriptor returned {resp.status_code}")
    return uuid.UUID(resp.json()["nfDeploymentDescriptorId"])



def _validate_package(location: str) -> tuple[str, list[tuple[str, str]], str, dict]:
    """Checks the location, fetches the package and validates it; returns (entry definitions, artifacts, integrity hash, identity) from `validate_package_bytes`.

    Per Onboarding LLD section 1. Raises PackageValidationFailed when the location does not end in `.csar` (NamingValidator, HISTORY.md §5) or fails the SSRF
    guard, before anything is fetched; httpx.HTTPError when the fetch fails or the server answers a non-2xx status; and whatever `validate_package_bytes` raises
    (PARSE_FAILURES). The caller treats all of them as "this package fails".

    The reference's FileExistenceValidator also requires `Files/Acm/definition/compositions.json` (an ONAP ACM file); that is deliberately not adopted, because
    this build never calls ONAP ACM and the file would be a dependency it does not have (README section 1.2).
    """
    if not location.endswith(".csar"):
        raise PackageValidationFailed(f"package location {location!r} does not end with .csar")
    # The location is caller-supplied and fetched from here (CodeQL py/full-ssrf):
    # the same scheme + loopback/link-local/metadata guard every other outbound
    # call in this build goes through (smo_shared.webhook), before anything is fetched.
    if not is_safe_webhook_destination(location):
        raise PackageValidationFailed(f"package location {location!r} is not an allowed http(s) destination")
    # A plain GET with a 30 s timeout and httpx's default of not following redirects: a 3xx answer is raised as an error by raise_for_status below and the package fails.
    resp = httpx.get(location, timeout=30.0)
    resp.raise_for_status()
    data = resp.content
    # The signature policy is read after the fetch and handed to validate_package_bytes, which checks the signature before parsing anything in the package.
    trust, require_signed = _signing_policy()
    return validate_package_bytes(data, location, trust=trust, require_signed=require_signed)


def _signing_policy() -> tuple["csar_signing.TrustStore | None", bool]:
    """Reads the package-signature policy from the environment for one package and returns (trust store or None, require signed).

    PR-RAPP-1.3/1.5. Read for every package, so a changed ConfigMap or a rotated key is seen without a restart. `ONBOARDING_TRUST_STORE`: a file or a directory of PEM
    public keys of the publishers whose packages are accepted (empty: no store, no signature is checked). `ONBOARDING_REQUIRE_SIGNED_PACKAGES`: `true`, `1`, `yes` or
    `on` refuses a package that is not signed by one of them (default `false`). Raises PackageValidationFailed when the trust store cannot be loaded; it does not fall
    back to "trust nothing, check nothing" (the failure carries csar_signing's message, which names the problem and not the mounted path).
    """
    location = os.environ.get("ONBOARDING_TRUST_STORE", "").strip()
    require = os.environ.get("ONBOARDING_REQUIRE_SIGNED_PACKAGES", "false").strip().lower() in ("1", "true", "yes", "on")
    if not location:
        return None, require
    try:
        return csar_signing.load_trust_store(location), require
    except csar_signing.TrustStoreError as exc:
        raise PackageValidationFailed(f"the trust store cannot be used: {exc}") from exc


def _get_package_or_404(db: Session, package_id: uuid.UUID) -> ApplicationPackage:
    """Returns the package or raises the PACKAGE_NOT_FOUND framework error (404)."""
    pkg = db.get(ApplicationPackage, package_id)
    if pkg is None:
        raise framework_error(FrameworkError.PACKAGE_NOT_FOUND, detail=f"no such package {package_id}")
    return pkg


def _fire(db: Session, pkg: ApplicationPackage, event: PackageEvent, guard_refusal: str | None = None) -> PackageState:
    """Fires `event` on `pkg` and returns the new state; the caller stores it and commits.

    An IllegalTransition becomes a 409. When the event has an edge from the current state, the refusal came from that edge's guard (active usage or blocking
    dependents) and is reported as 409 PACKAGE_IN_USE with `guard_refusal`; otherwise the event is not allowed from this state at all: 409
    LIFECYCLE_ILLEGAL_TRANSITION naming the state and the event (OI-2-lcm-error-mapping). A guard refusal with no `guard_refusal` text is reported as an illegal
    transition.
    """
    state = PackageState(pkg.state)
    try:
        return ONBOARDING_FSM.fire(state, event, db=db, package=pkg)
    except IllegalTransition as exc:
        if guard_refusal is not None and event in ONBOARDING_FSM.legal_events(state):
            raise framework_error(FrameworkError.PACKAGE_IN_USE, detail=guard_refusal) from exc
        raise illegal_transition_error(exc, f"package {pkg.package_id}") from exc


@app.get("/packages/{package_id}/onboarding-status")
def query_onboarding_status(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """404 PACKAGE_NOT_FOUND for an unknown package."""
    # Maintainer notes (not published). rApp Management's CreateInstance and AIMgF read this route: `smeDeclarations` rides along for bootstrap-complete's per-instance
    # SME registration (HISTORY.md §7, Onboarding/rApp Mgmt finding 3) and `aiCapabilities` for the runtime profiles AIMgF sizes a runtime with (W7-03). Read only.
    pkg = _get_package_or_404(db, package_id)
    return {
        "packageId": str(pkg.package_id), "state": pkg.state,
        "nfDeploymentDescriptorId": str(pkg.nf_deployment_descriptor_id) if pkg.nf_deployment_descriptor_id else None,
        # rapp-mgmt's CreateInstance already calls this exact route to read
        # state/nfDeploymentDescriptorId — smeDeclarations rides along here
        # rather than a new dedicated route, for bootstrap-complete's own
        # per-instance SME registration (HISTORY.md §7's Onboarding/rApp
        # Mgmt finding 3).
        "smeDeclarations": pkg.sme_declarations,
        # Wave 7 (W7-03): AIMgF reads the package's runtimeProfiles from here
        # when it sizes a Training/Validation/Emulation/Inference runtime.
        "aiCapabilities": pkg.ai_capabilities,
    }


@app.get("/packages")
def query_packages(state: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                    db: Session = Depends(get_session)):
    # Maintainer notes (not published). Lists packages in `_package_view` form, optionally filtered by an exact `state` string (an unknown state gives an empty
    # page, not an error). Paginated by smo_shared.pagination, which orders an unordered statement by primary key (package_id, a random UUID), so the order is stable but not chronological.
    stmt = select(ApplicationPackage)
    if state:
        stmt = stmt.where(ApplicationPackage.state == state)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_package_view(p) for p in page["items"]]}


@app.post("/packages/{package_id}/deprecate")
def deprecate_package(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """AVAILABLE -> DEPRECATED; 409 LIFECYCLE_ILLEGAL_TRANSITION from any other state."""
    # Maintainer notes (not published). One FSM event, one commit; the guard-less edge, so the only refusal is the 409 from `_fire`.
    pkg = _get_package_or_404(db, package_id)
    pkg.state = _fire(db, pkg, PackageEvent.DEPRECATE)
    db.commit()
    return _package_view(pkg)


@app.post("/packages/{package_id}/prime")
def prime_package(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """HISTORY.md §5: the reference's real
    COMMISSIONED->PRIMING->PRIMED lifecycle, missing entirely — this
    build went ONBOARDING->AVAILABLE directly. Real ACM/DME/SME
    resource pre-provisioning behind priming is out of scope (same
    elision as this build's other southbound calls), so both
    transitions fire within this one request. 409
    LIFECYCLE_ILLEGAL_TRANSITION from any state but AVAILABLE.
    """
    # Maintainer notes (not published). PRIME and PRIME_COMPLETE are fired one after the other and committed once, so PRIMING is never stored or visible. If the
    # first event is refused the second is not tried and nothing is written.
    pkg = _get_package_or_404(db, package_id)
    pkg.state = _fire(db, pkg, PackageEvent.PRIME)
    pkg.state = _fire(db, pkg, PackageEvent.PRIME_COMPLETE)
    db.commit()
    return _package_view(pkg)


@app.post("/packages/{package_id}/deprime")
def deprime_package(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """The reference's own deprimeRapp guard: blocked while any rApp
    instance still references this package (mirrors the cascade-delete
    guard's active-usage-registration check): 409 PACKAGE_IN_USE.
    Any state but PRIMED: 409 LIFECYCLE_ILLEGAL_TRANSITION.
    """
    # Maintainer notes (not published). DEPRIME carries a guard (no open usage registration), so a refusal while the package is PRIMED is reported as 409
    # PACKAGE_IN_USE through `guard_refusal`; DEPRIME_COMPLETE follows in the same request and the same commit.
    pkg = _get_package_or_404(db, package_id)
    pkg.state = _fire(db, pkg, PackageEvent.DEPRIME, guard_refusal="blocked by an active usage registration")
    pkg.state = _fire(db, pkg, PackageEvent.DEPRIME_COMPLETE)
    db.commit()
    return _package_view(pkg)


@app.post("/packages/{package_id}/cancel-delete")
def cancel_delete(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """DEPRECATED -> AVAILABLE; 409 LIFECYCLE_ILLEGAL_TRANSITION from any other state."""
    # Maintainer notes (not published). The only edge is DEPRECATED -> AVAILABLE; one event, one commit.
    pkg = _get_package_or_404(db, package_id)
    pkg.state = _fire(db, pkg, PackageEvent.CANCEL_DELETE)
    db.commit()
    return _package_view(pkg)


@app.delete("/packages/{package_id}")
def delete_package(package_id: uuid.UUID, db: Session = Depends(get_session)):
    """AVAILABLE/DEPRECATED -> DELETING behind the cascade-delete guard (409
    PACKAGE_IN_USE when a dependent child package or an active usage
    registration blocks it); a FAILED package's row is deleted directly.
    Either way the package's NF deployment descriptor is deleted at NFO first
    (409 PACKAGE_IN_USE while an NFO deployment still uses it, 503
    ENDPOINT_UNREACHABLE when NFO cannot be asked; the package is unchanged).
    Any other state (PRIMED, DELETING, ...): 409 LIFECYCLE_ILLEGAL_TRANSITION.
    """
    # Maintainer notes (not published). Two paths (call flow 06c, 06d). FAILED: the descriptor (if NFO made one) and the package's stopped usage registrations
    # go, then the row (its Artifact rows go with it through the ondelete CASCADE), no FSM event, and the answer is {"status": "deleted"}. usage/start refuses a
    # package that is not AVAILABLE or PRIMED, so a FAILED package has no open registration. Otherwise: the DELETE event runs its guard
    # (`_no_blocking_dependents`) before NFO is called, and the answer is the package view in state DELETING with no descriptor, the row kept.
    pkg = _get_package_or_404(db, package_id)
    if pkg.state == PackageState.FAILED:
        # FAILED never reached AVAILABLE, so nothing can depend on it — cascade
        # check skipped entirely, per Onboarding LLD section 3.
        _delete_descriptor(pkg)
        db.query(PackageUsageRegistration).filter(PackageUsageRegistration.package_id == package_id).delete()
        db.delete(pkg)
        db.commit()
        return {"status": "deleted"}
    new_state = _fire(db, pkg, PackageEvent.DELETE,
                      guard_refusal="blocked by a dependent child package or active usage registration")
    _delete_descriptor(pkg)
    pkg.nf_deployment_descriptor_id = None
    pkg.state = new_state
    db.commit()
    return _package_view(pkg)


def _delete_descriptor(pkg: ApplicationPackage) -> None:
    """Deletes the package's NF deployment descriptor at NFO (`DELETE /nfo/descriptors/{id}`); nothing to do for a package without one.

    NFO answers 204 (deleted, or already gone) or 409 while a deployment still uses the descriptor, raised here as PACKAGE_IN_USE; anything else, or
    no answer, is 503 ENDPOINT_UNREACHABLE. The caller has not changed the package yet, so a refusal leaves it as it was.
    """
    if not pkg.nf_deployment_descriptor_id:
        return
    try:
        resp = R1Client().delete(f"/nfo/descriptors/{pkg.nf_deployment_descriptor_id}")
    except httpx.HTTPError as exc:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"NFO could not be asked to delete the descriptor: {type(exc).__name__}") from exc
    if resp.status_code == 409:
        raise framework_error(FrameworkError.PACKAGE_IN_USE, detail=f"an NFO deployment still uses descriptor {pkg.nf_deployment_descriptor_id}")
    if resp.status_code not in (200, 204, 404):
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"NFO DeleteDescriptor returned {resp.status_code}")


@app.post("/packages/{package_id}/usage/start")
def register_usage_start(package_id: uuid.UUID, consumer_id: str, db: Session = Depends(get_session)):
    """404 PACKAGE_NOT_FOUND for an unknown package, 409 PACKAGE_NOT_DEPLOYABLE for a package that is not AVAILABLE or PRIMED."""
    # Maintainer notes (not published). Called by rApp Management only (the GUI cannot reach it, the BFF lists no rule for it; an rApp may not change
    # /onboarding). `consumer_id` (the rApp instance id) is a query parameter, taken as given. Every call inserts a new registration, so a repeated
    # call by the same consumer opens a second one. The state check closes the race with a deprecate or a delete after rApp Management read the state (call
    # flow 06), and keeps registrations off a FAILED package, whose direct delete would otherwise trip on them. The open registration (no stopped_at) is what
    # blocks DEPRIME and DELETE.
    pkg = _get_package_or_404(db, package_id)
    if pkg.state not in (PackageState.AVAILABLE, PackageState.PRIMED):
        raise framework_error(FrameworkError.PACKAGE_NOT_DEPLOYABLE, detail=f"package {package_id} is {pkg.state}, not AVAILABLE or PRIMED")
    reg = PackageUsageRegistration(package_id=package_id, consumer_id=consumer_id)
    db.add(reg)
    db.commit()
    return {"registrationId": str(reg.id)}


@app.post("/packages/{package_id}/usage/{registration_id}/stop")
def register_usage_stop(package_id: uuid.UUID, registration_id: uuid.UUID, db: Session = Depends(get_session)):
    """404 PACKAGE_NOT_FOUND for an unknown package, 404
    PACKAGE_USAGE_REGISTRATION_NOT_FOUND for a registration id that is
    unknown or belongs to another package. Idempotent: stopping an already
    stopped registration keeps its original stoppedAt.
    """
    # Maintainer notes (not published). The registration must belong to the package in the path, otherwise it is the same 404 as an unknown one. The stop time is
    # written once; a second stop keeps it.
    import datetime

    _get_package_or_404(db, package_id)
    reg = db.get(PackageUsageRegistration, registration_id)
    if reg is None or reg.package_id != package_id:
        raise framework_error(FrameworkError.PACKAGE_USAGE_REGISTRATION_NOT_FOUND,
                              detail=f"no usage registration {registration_id} for package {package_id}")
    if reg.stopped_at is None:
        reg.stopped_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {"status": "stopped"}



# The rApp instance states in which an instance no longer holds its package: it was torn down. Any other state (or an unknown state) holds it.
_RELEASED_INSTANCE_STATES = ("UNDEPLOYED",)


@app.post("/packages/{package_id}/usage/{registration_id}/release")
def release_orphaned_usage(package_id: uuid.UUID, registration_id: uuid.UUID, db: Session = Depends(get_session)):
    """Closes a usage registration that no rApp instance holds any more (an orphan), so it stops blocking deprime and delete.

    The registration's consumer is looked up at rApp Management: when that instance no longer exists, or is UNDEPLOYED, the registration is stopped as by
    usage/stop. 409 PACKAGE_IN_USE while the instance exists in any other state (terminate it instead), 503 ENDPOINT_UNREACHABLE when rApp Management
    cannot answer, 404 PACKAGE_NOT_FOUND / PACKAGE_USAGE_REGISTRATION_NOT_FOUND as usage/stop. Idempotent for a registration already stopped.
    """
    # Maintainer notes (not published). The administrator's tool for the one case the usage contract cannot close itself: a teardown whose usage/stop failed
    # (rApp Management records it in `last_teardown`), or a registration left by an instance that is gone (call flow 06c). A consumer id that is not a UUID was
    # never an rApp instance (the GUI's old "test usage" registrations), so it is released without asking.
    import datetime

    _get_package_or_404(db, package_id)
    reg = db.get(PackageUsageRegistration, registration_id)
    if reg is None or reg.package_id != package_id:
        raise framework_error(FrameworkError.PACKAGE_USAGE_REGISTRATION_NOT_FOUND,
                              detail=f"no usage registration {registration_id} for package {package_id}")
    if reg.stopped_at is not None:
        return {"status": "stopped"}
    try:
        instance_id = uuid.UUID(reg.consumer_id)
    except ValueError:
        instance_id = None
    if instance_id is not None:
        try:
            resp = R1Client().get(f"/rapp-mgmt/instances/{instance_id}")
        except httpx.HTTPError as exc:
            raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"rApp Management could not be asked: {type(exc).__name__}") from exc
        if resp.status_code == 200 and resp.json().get("state") not in _RELEASED_INSTANCE_STATES:
            raise framework_error(FrameworkError.PACKAGE_IN_USE,
                                  detail=f"rApp instance {instance_id} is {resp.json().get('state')} and still holds the package: terminate it")
        if resp.status_code not in (200, 404):
            raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"rApp Management answered {resp.status_code}")
    reg.stopped_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    log.info("usage registration %s of package %s released (consumer %s held it no longer)", registration_id, package_id, reg.consumer_id)
    return {"status": "stopped"}

def _package_view(pkg: ApplicationPackage) -> dict:
    """The JSON view of a package that the lifecycle routes and the list route return.

    Identity, descriptor ids, state, signatureVerified, aiCapabilities and smeDeclarations, as camelCase keys. `manifest_ref`, `integrity_hash` and `parent_package_id`
    are not part of it.
    """
    return {
        "packageId": str(pkg.package_id),
        "name": pkg.name,
        "version": pkg.version,
        "vendor": pkg.vendor,
        "applicationType": pkg.application_type,
        "state": pkg.state,
        "toscaEntryDefinitions": pkg.tosca_entry_definitions,
        "descriptorId": pkg.descriptor_id,
        "descriptorInvariantId": pkg.descriptor_invariant_id,
        "descriptorVersion": pkg.descriptor_version,
        "schemaVersion": pkg.schema_version,
        "signatureVerified": pkg.signature_verified,
        "nfDeploymentDescriptorId": str(pkg.nf_deployment_descriptor_id) if pkg.nf_deployment_descriptor_id else None,
        "aiCapabilities": pkg.ai_capabilities,
        "smeDeclarations": pkg.sme_declarations,
    }


@app.get("/packages/{package_id}/artifacts")
def list_package_artifacts(package_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                            db: Session = Depends(get_session)):
    """(GUI pass 2) The artifacts registered during validation (Onboarding LLD section 1)."""
    # Maintainer notes (not published). Artifacts are the rows written during validation: `path` inside the CSAR and `accessUrl` (`<location>#<path>`); the package
    # bytes are not stored. 404 PACKAGE_NOT_FOUND for an unknown package.
    _get_package_or_404(db, package_id)
    stmt = select(Artifact).where(Artifact.package_id == package_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"artifactId": str(a.artifact_id), "path": a.path, "accessUrl": a.access_url}
            for a in page["items"]]}


@app.get("/packages/{package_id}/usage")
def list_package_usage(package_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                        db: Session = Depends(get_session)):
    """(GUI pass 2) Usage registrations behind the cascade-delete guard (call flow 06):
    any row still missing stoppedAt blocks deprime and delete, so the operator
    can now see why a delete was refused.
    """
    _get_package_or_404(db, package_id)
    stmt = select(PackageUsageRegistration).where(PackageUsageRegistration.package_id == package_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"registrationId": str(r.id), "consumerId": r.consumer_id,
             "stoppedAt": r.stopped_at.isoformat() if r.stopped_at else None, "active": r.stopped_at is None}
            for r in page["items"]]}
