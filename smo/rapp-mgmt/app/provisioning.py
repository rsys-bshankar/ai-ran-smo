"""What it takes to bring an RAppInstance's resources up, and to release them.

CreateInstance and UpgradeInstance's replacement row go through the same
`provision_instance` (OI-2-upgrade-completeness): package-state check, a
fresh `oauth_client_id` identity, NFO Instantiate and the package usage
registration. TerminateInstance, an upgrade commit (for the old row) and an
upgrade rollback or timeout (for the replacement) all release through the
same `release_instance_resources`: NFO Terminate and usage/stop, best-effort
but recorded on the row (`last_teardown`) — OI-2-terminate-workload.

The DME/SME deregistration and credential revocation half of a teardown is
the FSM's own transition action (statemachine.py's `_terminate_side_effects`),
not repeated here.
"""

import datetime
import os
import uuid
from contextlib import suppress

import httpx
from sqlalchemy.orm import Session

from smo_shared import credential_delivery
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.webhook import normalise_base_url

from .models import RAppInstance
from .statemachine import InstanceState

# A package an instance may be created from: validated (AVAILABLE), or
# validated and primed (PRIMED). Priming is optional; depriming is refused
# while an instance of the package is running (onboarding's DEPRIME guard).
DEPLOYABLE_PACKAGE_STATES = ("AVAILABLE", "PRIMED")


def provision_instance(db: Session, package_id: uuid.UUID, configuration: dict | None,
                       autonomy_mode: str, region_scope: dict | None, approval_policy: dict | None = None, authz_scope: dict | None = None) -> RAppInstance:
    """Requires a validated package: AVAILABLE, or PRIMED (AVAILABLE plus
    pre-provisioned resources; D-SEC-RAPP-1) — 404 PACKAGE_NOT_FOUND for an
    unknown package, 409 PACKAGE_NOT_DEPLOYABLE for any other state. NFO handoff
    per Onboarding/rApp Mgmt LLD section 5: reads the package's TOSCA service
    template and issues NFO.Instantiate. The new row is flushed, not committed.

    Call flow 06: the package usage registration is opened first, before
    anything else is made for the instance, and the instance is not created
    without it — Onboarding's PACKAGE_NOT_DEPLOYABLE (the package changed
    state since it was read) is passed on, any other refusal is 502
    UPSTREAM_FAILED, no answer 503 ENDPOINT_UNREACHABLE. When a later step
    raises, the registration is stopped again (best-effort), so a failed
    create leaves no registration holding the package.
    """
    r1 = R1Client()
    pkg_resp = r1.get(f"/onboarding/packages/{package_id}/onboarding-status")
    if pkg_resp.status_code == 404:
        raise framework_error(FrameworkError.PACKAGE_NOT_FOUND, detail=f"no such package {package_id}")
    if pkg_resp.status_code != 200 or pkg_resp.json().get("state") not in DEPLOYABLE_PACKAGE_STATES:
        raise framework_error(FrameworkError.PACKAGE_NOT_DEPLOYABLE, detail="package is not AVAILABLE or PRIMED")
    nf_deployment_descriptor_id = pkg_resp.json().get("nfDeploymentDescriptorId")
    if not nf_deployment_descriptor_id:
        # Every package that reaches AVAILABLE has one — OnboardPackage's own
        # validation pipeline creates it via NFO's CreateDescriptor (NFO+FOCOM
        # LLD section 2). Missing here means an inconsistent record, not a
        # normal refusal.
        raise framework_error(FrameworkError.PACKAGE_NOT_DEPLOYABLE, detail="package has no nfDeploymentDescriptorId")

    # call flow 01b: where the package says its rApp's operator API is (`operatorApi.baseUrl` in its manifest, checked at onboarding); kept on the object, not
    # stored, for CreateInstance to use when the operator gave no `operatorApiBase`
    declared_base = ((pkg_resp.json().get("aiCapabilities") or {}).get("operatorApi") or {}).get("baseUrl")
    inst = RAppInstance(package_id=package_id, configuration=configuration, state=InstanceState.DEPLOYING,
                        oauth_client_id=str(uuid.uuid4()), autonomy_mode=autonomy_mode, region_scope=region_scope, approval_policy=approval_policy,
                        authz_scope=authz_scope)
    inst.declared_operator_api_base = normalise_base_url(declared_base) if isinstance(declared_base, str) else None
    db.add(inst)
    db.flush()
    inst.package_usage_registration_id = _start_usage(r1, package_id, inst.instance_id)
    try:
        secret = register_instance_invoker(inst)
        # replaces the placeholder identity; the secret goes to the workload's Secret, or, when the deployment has none, is kept on the object (never in
        # the database) for the caller to push to the instance's operator API once the instance is committed (`push_credentials`)
        inst.issued_secret = None if deliver_credentials(inst, secret) else secret

        nfo_resp = r1.post("/nfo/deployments", json={
            "nfDeploymentDescriptorId": nf_deployment_descriptor_id,  # the real descriptor, per section 5
            "name": f"rapp-instance-{inst.instance_id}",  # NFO's own duplication guard (HISTORY.md §5) needs a real name
            "requiredResourceTypeId": (configuration or {}).get("requiredResourceTypeId"),
        })
        # NFO Instantiate answers 202 Accepted (nfo/app/main.py) — a 200-only check
        # dropped every real workloadRef. workload_ref IS the NFO nfDeploymentId,
        # which TERMINATE later hands back to NFO's DELETE /deployments/{id}.
        inst.workload_ref = nfo_resp.json().get("nfDeploymentId") if nfo_resp.status_code in (200, 202) else None
    except Exception:
        # the instance is not created (the flushed row is rolled back with the request), so nothing may keep holding the package for it
        with suppress(httpx.HTTPError):
            r1.post(f"/onboarding/packages/{package_id}/usage/{inst.package_usage_registration_id}/stop")
        raise
    return inst


def _start_usage(r1: R1Client, package_id: uuid.UUID, instance_id: uuid.UUID) -> uuid.UUID:
    """Opens the package usage registration for a new instance (`usage/start`, consumer = the instance id) and returns its id; raises when Onboarding refuses
    or does not answer (see `provision_instance`), so the instance is not created."""
    try:
        resp = r1.post(f"/onboarding/packages/{package_id}/usage/start", params={"consumer_id": str(instance_id)})
    except httpx.HTTPError as exc:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"Onboarding usage/start: {type(exc).__name__}") from exc
    if resp.status_code == 409:
        raise framework_error(FrameworkError.PACKAGE_NOT_DEPLOYABLE, detail="package is not AVAILABLE or PRIMED (Onboarding refused the usage registration)")
    if resp.status_code == 404:
        raise framework_error(FrameworkError.PACKAGE_NOT_FOUND, detail=f"no such package {package_id}")
    if resp.status_code != 200:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"Onboarding usage/start answered {resp.status_code}")
    return uuid.UUID(resp.json()["registrationId"])


def _step(call) -> str:
    """Runs one teardown call and returns its outcome as the text stored in `last_teardown`: `DONE` for a 200, 202, 204 or 404 (already gone counts as done), otherwise
    `FAILED: <exception class or HTTP status>`. It never raises, so one failed release does not stop the others.
    """
    try:
        resp = call()
    except httpx.HTTPError as exc:
        return f"FAILED: {type(exc).__name__}"
    return "DONE" if resp.status_code in (200, 202, 204, 404) else f"FAILED: HTTP {resp.status_code}"


def release_instance_resources(inst: RAppInstance, reason: str) -> dict:
    """NFO Terminate for the instance's workload (`workload_ref`, the NFO
    nfDeploymentId) and usage/stop for its package usage registration —
    each best-effort (an unreachable NFO or Onboarding never blocks the
    teardown itself) but recorded: the returned outcome is what callers
    store in `last_teardown`. A 404 counts as done (already gone). On
    success the usage registration id is cleared, since the registration is
    closed; `workload_ref` is kept as the record of which deployment ran.
    """
    r1 = R1Client()
    if inst.workload_ref:
        nfo = _step(lambda: r1.delete(f"/nfo/deployments/{inst.workload_ref}"))
    else:
        nfo = "SKIPPED: no workloadRef"
    if inst.package_usage_registration_id is not None:
        usage = _step(lambda: r1.post(f"/onboarding/packages/{inst.package_id}/usage/{inst.package_usage_registration_id}/stop"))
        if usage == "DONE":
            inst.package_usage_registration_id = None
    else:
        usage = "SKIPPED: no usage registration"
    return {"instanceId": str(inst.instance_id), "reason": reason, "nfoTerminate": nfo, "usageStop": usage,
            "credentialSecret": credential_delivery.withdraw(inst.instance_id),
            "at": datetime.datetime.now(datetime.UTC).isoformat()}


def _sme_provider_registration_body(provider: dict, apf_id: str) -> dict:
    """A CSAR's own `Files/Sme/providers/*.json` may already be this
    build's own real `ProviderRegistrationRequest` body
    (this build's own established convention —
    `apfId`/`providerDomainInfo`) or the real external CAPIF
    `APIProviderEnrolmentDetails` shape (`apiProvDomInfo`/`apiProvFuncs`,
    grounded against `nonrtric-plt-rappmanager`'s own real sample
    packages) — `providerDomainInfo` reads whichever key is actually
    present. `apfId` is always this instance's own `oauth_client_id`
    (the "one instance, one identity" convention already used for its
    DME producer_id), never whatever a CSAR's own JSON declares — two
    instances of the same package must never collide on one shared,
    hardcoded apfId.
    """
    return {"apfId": apf_id, "providerDomainInfo": provider.get("providerDomainInfo") or provider.get("apiProvDomInfo")}


def _sme_service_registration_body(service_api: dict, apf_id: str) -> dict:
    """A CSAR's own `Files/Sme/serviceapis/*.json` may already be this
    build's own real `ServiceRegistration` body
    (this build's own established convention —
    `serviceName`/`endpoint`/`version`/`moduleScope`/... directly) or
    the real external CAPIF `ServiceAPIDescription` shape
    (`apiName`/`aefProfiles` with nested `versions`/
    `interfaceDescriptions`, grounded against
    `nonrtric-plt-rappmanager`'s own real sample packages) needing a
    real field-by-field mapping — `serviceName`'s presence distinguishes
    the two. `producerId` is always this instance's own `oauth_client_id`
    either way, never whatever the CSAR's own JSON declares, same
    reasoning as `_sme_provider_registration_body` above. For the real
    CAPIF shape, serviceName/endpoint/version/moduleScope are this
    build's own required fields with no CAPIF equivalent, so a fixed,
    documented default fills each one where the real sample has nothing
    to say (its own `apiVersion` is even an empty string).
    `aefProfiles` passes through byte-for-byte in both cases: this
    build's own field is an untyped `list[dict]`, so neither shape needs
    lossy reshaping there.

    `serviceName` is always suffixed with this instance's own apfId —
    caught by running this against this repo's own real CSAR in this
    build's own shape, not assumed: SME's own
    `register_service` treats `serviceName` as globally unique across
    every producer (`sme/app/main.py`'s own documented Section 2.3
    rule), so a CSAR's fixed, package-level `serviceName` would
    otherwise collide the moment a second instance of the same package
    tries to register the identical name under its own, different
    apfId — a real `SERVICE_NAME_CONFLICT`, not a hypothetical one.
    """
    if "serviceName" in service_api:
        return {**service_api, "producerId": apf_id, "serviceName": f"{service_api['serviceName']}-{apf_id}"}
    first_profile = (service_api.get("aefProfiles") or [{}])[0]
    first_version = (first_profile.get("versions") or [{}])[0]
    first_iface = (first_profile.get("interfaceDescriptions") or [{}])[0]
    endpoint = f"http://{first_iface['ipv4Addr']}:{first_iface['port']}" if first_iface.get("ipv4Addr") else "http://unknown"
    return {
        "serviceName": f"{service_api.get('apiName', 'unnamed-service')}-{apf_id}", "producerId": apf_id,
        "endpoint": endpoint, "version": first_version.get("apiVersion") or "1.0",
        "moduleScope": "rapp", "aefProfiles": service_api.get("aefProfiles", []),
    }


def register_instance_invoker(inst: RAppInstance) -> str:
    """PR-SEC-14: the instance's own identity at SME. Registered without the enrollment secret, so SME records it as an rApp's (no internal scope,
    refused on the internal-only routes). Its id is the instance's `oauth_client_id`, which is therefore what R1 Termination vouches for as the
    caller (`X-R1-Invoker-Id`) when the workload calls with these credentials: the limits, the kill switch and the audit trail key on it. Returns the
    onboarding secret, which SME keeps only as a hash: it is handed to the workload once (`POST /instances/{id}/credentials`), never stored here."""
    request: dict = {"apiInvokerPublicKey": f"rapp-instance:{inst.instance_id}"}
    if inst.authz_scope:
        request["authzScope"] = inst.authz_scope              # PR-SEC-10.3: the instance's scope claim is part of its identity, put on the invoker when it is made
    try:
        resp = R1Client().post("/sme/invoker-registrations", json=request)
        created = resp.status_code == 201
    except httpx.HTTPError:
        created = False
    if not created:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="SME did not register an invoker identity for the rApp instance")
    body = resp.json()
    if inst.authz_scope and body.get("authzScope") != inst.authz_scope:
        # An SME that does not know the field (the release before this one) would have made an UNSCOPED invoker: fail closed, take it away again
        with suppress(httpx.HTTPError):
            R1Client().delete(f"/sme/invoker-registrations/{body['apiInvokerId']}")
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail="SME did not record the scope claim of the rApp instance; no identity was kept")
    inst.oauth_client_id = body["apiInvokerId"]
    return body["onboardingSecret"]


def deliver_credentials(inst: RAppInstance, secret: str) -> dict | None:
    """Puts the instance's credentials where its workload will read them, when this deployment says how (`smo_shared/credential_delivery.py`; None when
    it does not). A workload that cannot get its credentials is no use, so a failure is a 503 and the invoker just made is deregistered again."""
    try:
        return credential_delivery.deliver(inst.instance_id, inst.oauth_client_id, secret)
    except credential_delivery.DeliveryFailed as exc:
        with suppress(httpx.HTTPError):
            R1Client().delete(f"/sme/invoker-registrations/{inst.oauth_client_id}")
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"the instance's credentials could not be delivered to its workload: {exc}") from exc


def push_credentials(inst: RAppInstance, secret: str | None) -> bool:
    """Hands the instance's credentials to the rApp that serves it: `POST /instances/{id}/credentials` on its operator API, through R1 Termination
    (`/rapps/{id}/operator/...`, which stamps this module's internal role; the rApp accepts them from no one else). Needs the instance committed with
    an `operatorApiBase`, since the gateway looks the base up here. True when the rApp took them; False when there is no base or no secret, or the
    rApp did not answer 2xx (the operator can issue them again: `POST /instances/{id}/credentials`)."""
    if not inst.operator_api_base or not secret:
        return False
    try:
        resp = R1Client().post(f"/rapps/{inst.instance_id}/operator/instances/{inst.instance_id}/credentials",
                               json={"oauthClientId": inst.oauth_client_id, "oauthClientSecret": secret})
    except httpx.HTTPError:
        return False
    return 200 <= resp.status_code < 300


def onboarding_status(inst: RAppInstance) -> dict:
    """What Onboarding says about this instance's package ({} when it cannot say)."""
    resp = R1Client().get(f"/onboarding/packages/{inst.package_id}/onboarding-status")
    return resp.json() if resp.status_code == 200 else {}


def apply_rapp_limits(inst: RAppInstance, status: dict | None = None) -> None:
    """AI-10.1/10.2: hand the limits the package declares in its manifest (`limits.configJobsPerHour`, read by Onboarding) to RAN NF OAM, which
    enforces them against this instance's own invoker id (its OAuth client id). Runs when the instance finishes bootstrapping, before it is
    allowed to go RUNNING, and fails closed: a package that declares a limit does not run without it, so an unreachable RAN NF OAM or a refused
    push is a 503 and the instance stays DEPLOYING (bootstrap-complete can be called again). A package with no limits needs no call."""
    status = onboarding_status(inst) if status is None else status
    limits = (status.get("aiCapabilities") or {}).get("limits") or {}
    # the manifest's names -> RAN NF OAM's request fields (AI-10.2 rate, AI-10.3 blast radius and magnitude)
    body = {field: limits[name] for name, field in (("configJobsPerHour", "maxConfigJobsPerHour"), ("maxElementsPerJob", "maxElementsPerJob"),
                                                     ("maxChangePercent", "maxChangePercent")) if limits.get(name) is not None}
    if not body:
        return
    try:
        resp = R1Client().put(f"/ran-nf-oam/rapp-limits/{inst.oauth_client_id}", json=body)
        pushed = resp.status_code == 200
    except httpx.HTTPError:
        pushed = False
    if not pushed:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE,
                              detail=f"the package declares limits {limits} but RAN NF OAM did not accept them; the instance stays DEPLOYING")
    inst.rapp_limits_set = True


def apply_approval_policy(inst: RAppInstance) -> None:
    """AI-11.4: hand the approval policy the instance was created with (ASSIST only) to RAN NF OAM, which holds this instance's config jobs for a human
    under its invoker id. Fail closed, like the limits: an instance that was asked to wait for approval does not run writing at once because the
    policy could not be set, so an unreachable RAN NF OAM or a refused push is a 503 and the instance stays DEPLOYING. No policy: no call."""
    if not inst.approval_policy:
        return
    wanted = inst.approval_policy.get("requiredApprovals", 1)
    try:
        resp = R1Client().put(f"/ran-nf-oam/rapp-approval-policy/{inst.oauth_client_id}", json={"requestedBy": "rapp-mgmt", **inst.approval_policy})
        pushed = resp.status_code == 200
        if pushed and wanted > 1:
            # a RAN NF OAM of the previous release ignores the field and would hold the instance's writes for ONE approval: only an answer that says two is accepted
            pushed = resp.json().get("requiredApprovals") == wanted
    except (httpx.HTTPError, ValueError, AttributeError):
        pushed = False
    if not pushed:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE,
                              detail="RAN NF OAM did not accept the approval policy of the instance; it stays DEPLOYING")


def register_sme_declarations(inst: RAppInstance, status: dict | None = None) -> None:
    """HISTORY.md §7's Onboarding/rApp Mgmt finding 3 (SME auto-
    registration): real O-RAN SC rApp Manager behavior
    (SmeDeployer.deployRappInstance) registers a package's CSAR-bundled
    Files/Sme/providers/ + Files/Sme/serviceapis/ declarations with SME
    per *instance*, at deploy time — not at onboarding or priming (the
    reference's own primeRapp is a documented no-op for SME). Runs at
    bootstrap-complete, and for an upgrade replacement that is committed
    without having called bootstrap-complete itself (upgrade.py).

    Uses this instance's own oauth_client_id as its SME apfId, the same
    "one instance, one identity" convention already used for its DME
    producer_id (statemachine.py's _reconsider_dme_registration).
    Best-effort by default: an unreachable SME does not block bootstrap from
    completing. With `RAPP_MGMT_REQUIRE_SME_REGISTRATION` on (the production
    sample turns it on; call flow 01d), every declaration must be registered:
    no answer is 503 ENDPOINT_UNREACHABLE, a refusal 502 UPSTREAM_FAILED, and
    the bootstrap is refused (the instance stays DEPLOYING).
    """
    r1 = R1Client()
    declarations = (onboarding_status(inst) if status is None else status).get("smeDeclarations")
    apf_id = inst.oauth_client_id
    if not declarations or not apf_id:          # no identity yet: nothing to register under
        return
    required = sme_registration_required()
    try:
        for provider in declarations.get("providers", []):
            resp = r1.post("/sme/provider-registrations", json=_sme_provider_registration_body(provider, apf_id))
            if required and resp.status_code not in (200, 201):
                raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME refused the provider registration ({resp.status_code})")
        service_ids = []
        for service_api in declarations.get("serviceApis", []):
            resp = r1.post(f"/sme/published-apis/v1/{apf_id}/service-apis", json=_sme_service_registration_body(service_api, apf_id))
            if resp.status_code == 201:
                service_ids.append(resp.json()["serviceId"])
            elif required:
                raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME refused a service API registration ({resp.status_code})")
        inst.sme_service_ids = service_ids or None
    except httpx.HTTPError as exc:
        if required:
            raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"SME could not register the declarations: {type(exc).__name__}") from exc


def sme_registration_required() -> bool:
    """`RAPP_MGMT_REQUIRE_SME_REGISTRATION`: `true`, `1`, `yes` or `on` makes a failed SME declaration registration refuse the bootstrap (default `false`: best-effort).
    Read on every bootstrap, so a change applies without a restart."""
    return os.environ.get("RAPP_MGMT_REQUIRE_SME_REGISTRATION", "false").strip().lower() in ("1", "true", "yes", "on")
