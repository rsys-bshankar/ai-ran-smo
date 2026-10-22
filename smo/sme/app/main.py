"""SME (Service Management and Exposure): the single FastAPI module of the CAPIF-style core, served at `/sme` behind R1 Termination.

What it is: every route of the service (service publish and discovery, provider and invoker onboarding, OAuth2 token issue and introspection,
trusted-invoker security contexts, change-event subscriptions) plus the helpers behind them. It profiles 3GPP TS 29.222 (CAPIF) as R1AP clause 6
requires; the Foundational Platform LLD fixes two points the spec leaves open: section 2.2, one authorization gate for discovery and notification
(`_visible_to`), and section 2.3, the registration conflict rule (`serviceName` is globally unique; the same producer re-registering updates in place,
a different producer gets `SERVICE_NAME_CONFLICT`). Design record: `sme/README.md`; the history entries cited next to the code are in `HISTORY.md`.

Where it sits: R1 Termination calls `/oauth2/introspect` on every proxied request and every module's `R1Client` calls `/invoker-registrations` and
`/oauth2/token`. SME calls nothing itself except subscriber callbacks, and those go through the transactional outbox (`smo_shared.outbox`), never
a direct HTTP call from a route.

Owns: the tables in `models.py`, credential hashing, token lifetime and the scope check at issuance. Does not own: deciding whether a request needs a token
(R1 Termination), which routes an rApp may change (`smo_shared/roles.py`: `INTERNAL_ONLY` and `RAPP_MAY_CHANGE`; a new route is added there with its test),
or what an rApp's scope claim lets it touch (the module that owns the target, `smo_shared/scope.py`).

Before editing: (1) the docstring of a route function is published as its OpenAPI `description` (and so is the docstring of a request model), so changing
one makes `tests_integration/test_openapi_specs.py` fail until `scripts/generate_openapi_specs.py` is rerun; that is why the maintainer notes for routes are `#`
comments below the docstring. (2) Routes change the database and call `notify_*` before `db.commit()`: the outbox row is part of the same transaction
and is sent by a commit hook once it commits. (3) `import httpx` is kept although nothing here calls it: tests patch `app.main.httpx.post` (see `ruff.toml`).
"""

import datetime
import hashlib
import logging
import os
import secrets
import uuid
from typing import Literal

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.timeutil import as_utc
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.outbox import enqueue
from smo_shared import mtls, roles, scope as authz_scope
from smo_shared.invoker import invoker_id
from smo_shared.webhook import is_safe_webhook_destination
from smo_shared.secretfile import read_secret

from .models import (API_INVOKER_EVENTS, EVENT_TYPES, InvokerRegistration, IssuedAccessToken, ProviderRegistration,
                     ServiceAuthzPolicy, ServiceEventSubscription, ServiceProfile, TrustedInvoker, UsedClientAssertion)

log = logging.getLogger(__name__)

ACCESS_TOKEN_TTL_SECONDS = 3600
# OI-2-oauth2-scope: the scopes SMO's own clients request — every module's
# R1Client (smo_shared/r1_client.py) and the GUI BFF. Granted as-is; they
# name no published API, so there is nothing to check them against.
INTERNAL_SCOPES = roles.INTERNAL_SCOPES | {roles.RAPP_SCOPE}      # PR-SEC-14: the internal two need an enrolled invoker (`_check_scope`); `smo-rapp` is for anyone
CAPIF_SCOPE_PREFIX = "3gpp#"
# SA-SME-1-public-key: RFC 7523 client authentication by a signed JWT.
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# The assertion's `aud` must name this token endpoint, as R1 Termination
# advertises it (`{SME_URL}/oauth2/token`, r1-termination/app/main.py).
TOKEN_ENDPOINT_AUDIENCE = os.environ.get("SME_TOKEN_AUDIENCE") or f"{mtls.http_url(os.environ.get('SME_URL', 'http://sme:8000'))}/oauth2/token"   # empty counts as unset (compose passes "")
ASSERTION_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"]
MAX_ASSERTION_LIFETIME_SECONDS = 300
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32

app = FastAPI(title="SME — Service Management and Exposure")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
# SME issues and introspects the R1 bearer token itself (r1-termination's
# own _authorized() calls /oauth2/introspect on every proxied request) —
# these two routes are the one exemption, the same way a token endpoint is
# never itself gated behind the token it hands out.
apply_r1_gateway_security(app, public_paths=frozenset({"/oauth2/token", "/oauth2/introspect"}))
apply_correlation_id(app)


install_health(app, checks=[database_check])  # /live, /ready and the /health alias (PR-ST-7)


def _hash_secret(secret: str) -> str:
    """Returns the stored form of an onboarding secret: `salt_hex:digest_hex` from salted scrypt (stdlib, no extra dependency).

    The secret is a credential an invoker presents later, so only a hash is kept: a database leak (backup, an injection elsewhere, a dump) must not hand out
    reusable client credentials. The salt is random per call and stored inside the value (there is no salt column); it is not secret. The cost parameters are the
    `_SCRYPT_*` constants; changing them makes every stored hash unverifiable because `_verify_secret` reads the same constants.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return f"{salt.hex()}:{digest.hex()}"


def _verify_secret(secret: str, stored: str) -> bool:
    """Returns True when `secret` matches `stored` (a value made by `_hash_secret`).

    The digest comparison is constant-time (`secrets.compare_digest`), so response time does not reveal how many leading bytes matched.
    Raises `ValueError` if `stored` is not `salt_hex:digest_hex`; the token route does not catch it, so a corrupt row would answer 500.
    """
    salt_hex, digest_hex = stored.split(":")
    candidate = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt_hex), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return secrets.compare_digest(candidate, bytes.fromhex(digest_hex))


def _hash_token(token: str) -> str:
    """Returns the SHA-256 hex digest under which an access token is stored and looked up.

    Unlike the onboarding secret, the token is already 256 bits of randomness from `secrets.token_urlsafe`, not a low-entropy chosen secret, so a fast hash is the
    standard choice and a slow KDF would only cost time on every introspection. The raw token is returned to the caller once and never stored.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# Body of POST /published-apis/v1/{apf_id}/service-apis (CAPIF ServiceAPIDescription, flattened). `serviceName` is the globally unique key. `producerId` is required but
# `register_service` does not read it: the `apf_id` in the path is what is stored as the producer. `allowedConsumers` empty means open to all (see `_visible_to`). Fields
# sent that are not declared here (for example `gatesDiscoveryVisibility`) are silently ignored. A docstring is deliberately not used: it would change the OpenAPI schema.
class ServiceRegistration(BaseModel):
    serviceName: str
    producerId: str
    endpoint: str
    version: str
    fullApiVersions: list[str] = []
    serviceCapabilities: dict = {}
    selectionCriteria: dict = {}
    moduleScope: str
    allowedConsumers: list[str] = []
    aefProfiles: list[dict] = []
    apiSuppFeats: str | None = None
    shareableInfo: dict | None = None


# Body of POST /capif-events/v1/{subscriber_id}/subscriptions. Each of `apiIds`, `apiInvokerIds`, `aefIds` is an optional CAPIFEventFilter; None or empty means no filter of that kind.
class EventSubscriptionRequest(BaseModel):
    subscriberId: str
    eventTypes: list[str]
    callbackUri: str
    apiIds: list[str] | None = None
    apiInvokerIds: list[str] | None = None  # OI-5-sme-filters
    aefIds: list[str] | None = None


def _rapp_caller(request: Request) -> str | None:
    """The invoker id of a caller with the rApp role ("" when the gateway gave none), or None for the console or an SMO module, which act for others and are
    not held to the identity rules of call flow 18."""
    return (invoker_id(request) or "") if roles.role_of(request) == roles.ROLE_RAPP else None


def _require_own(request: Request, claimed: str, code: tuple[str, int], what: str) -> None:
    """Call flow 18: refuses (403 `code`) an rApp naming an identity (`claimed`: an apfId, an invoker id, a subscriber id) that is not its own invoker id."""
    caller = _rapp_caller(request)
    if caller is not None and claimed != caller:
        raise framework_error(code, detail=f"an rApp acts at SME only as itself: the {what} must be its own invoker id")


def _delete_provider(db: Session, apf_id: str) -> None:
    """Deletes the provider registered under `apf_id` and every service it published, each announced as SERVICE_API_UNAVAILABLE (call flow 18a); the
    caller commits. Nothing happens for an apfId with neither."""
    for profile in db.scalars(select(ServiceProfile).where(ServiceProfile.producer_id == apf_id)).all():
        notify_service_change(db, profile, "SERVICE_API_UNAVAILABLE")   # before the delete: it reads the live row
        db.delete(profile)
    provider = db.get(ProviderRegistration, apf_id)
    if provider is not None:
        db.delete(provider)


# Body of POST /provider-registrations: `apfId` is the publisher identity (apfId == producerId == rAppId).
class ProviderRegistrationRequest(BaseModel):
    apfId: str
    providerDomainInfo: str | None = None


@app.post("/provider-registrations", status_code=201)
def register_provider(body: ProviderRegistrationRequest, request: Request, db: Session = Depends(get_session)):
    """HISTORY.md §5: Provider (APF) enrolment
    (`PostRegistrations`, `providermanagement.go`) — the real registry
    `register_service`'s own `apf_id` check needs, entirely absent
    before this pass. Idempotent update-in-place on a re-registration of
    the same `apfId`, the same shape `register_service`'s own
    same-producer re-registration already uses — the reference's real
    domain/function-id collision detection doesn't apply here, since
    this build has no separate provider-domain concept to collide on.
    """
    # Route notes (kept out of the docstring because FastAPI publishes it): upsert, so 201 is returned for a re-registration too and the response is only `{"apfId"}`.
    # Call flow 18a: an rApp registers only its own invoker id as apfId (403 `PROVIDER_IDENTITY_MISMATCH`); the console and modules register any. No event.
    _require_own(request, body.apfId, FrameworkError.PROVIDER_IDENTITY_MISMATCH, "apfId")
    provider = db.get(ProviderRegistration, body.apfId)
    if provider is None:
        provider = ProviderRegistration(apf_id=body.apfId)
        db.add(provider)
    provider.provider_domain_info = body.providerDomainInfo
    db.commit()
    return {"apfId": provider.apf_id}


@app.delete("/provider-registrations/{apf_id}", status_code=204)
def deregister_provider(apf_id: str, request: Request, db: Session = Depends(get_session)):
    """DeleteRegistrationsRegistrationId, providermanagement.go — idempotent,
    matching the reference's own delete-if-present-else-still-204 shape.
    """
    # Route notes: call flow 18a. 403 `PROVIDER_IDENTITY_MISMATCH` for an rApp naming another apfId. The provider's published services go with it, each announced
    # SERVICE_API_UNAVAILABLE, in one transaction (they stayed discoverable before). 204, also for an unknown id.
    _require_own(request, apf_id, FrameworkError.PROVIDER_IDENTITY_MISMATCH, "apfId")
    _delete_provider(db, apf_id)
    db.commit()


# Body of POST/PUT /invoker-registrations. `apiInvokerPublicKey` is either a PEM public key (it then verifies RFC 7523 client assertions) or an opaque label.
class InvokerRegistrationRequest(BaseModel):
    apiInvokerPublicKey: str
    # PR-SEC-10.3: which managed elements this invoker may touch, `{"regions": [...], "tenants": [...]}` (either key optional; docs/adr/0005). Absent: unscoped, as
    # before. Checked in the route (422 AUTHZ_SCOPE_INVALID with a fixed message), not by the model, because a validation error of the model repeats the input.
    authzScope: dict | None = None


def _checked_scope(value: dict | None) -> dict | None:
    """Returns the scope claim to store for `value` (the caller-supplied `authzScope`): normalised (sorted lists), or None for None/`{}`.

    Raises the 422 `AUTHZ_SCOPE_INVALID` problem when the claim is malformed. The detail is `smo_shared.scope`'s fixed message, which never repeats the input; `from None`
    drops the exception chain for the same reason.
    """
    try:
        return authz_scope.to_claim(authz_scope.from_claim(value))
    except ValueError as exc:
        raise framework_error(FrameworkError.AUTHZ_SCOPE_INVALID, detail=str(exc)) from None


def _pem_public_key(value: str):
    """Returns the loaded public key when `value` is a PEM public key, else None.

    None also covers text that starts with `-----BEGIN` but does not parse, so callers that must reject that case (`_check_public_key`) test for the prefix themselves.
    An opaque label (what SMO's own clients onboard with) is not an error here.
    """
    if not value.lstrip().startswith("-----BEGIN"):
        return None
    try:
        return load_pem_public_key(value.encode())
    except (ValueError, TypeError):
        return None


def _enrolled(presented: str | None) -> str:
    """Returns the `kind` of a new invoker (PR-SEC-14): `roles.ROLE_INTERNAL` or `roles.ROLE_RAPP`.

    `presented` is the caller's `X-SMO-Enrollment` header (untrusted). The expected secret is read on every call (`read_secret`: the env var or its `_FILE`; it raises if both are set or the file is unreadable), not cached.
    With a secret configured: no header gives `rapp`, a matching header gives `internal`, a non-matching one raises 403 `ENROLLMENT_REFUSED` (never downgraded, so a
    misconfigured module is seen at once). With no secret configured SME cannot tell a module from an rApp: it raises 503 `ENROLLMENT_NOT_CONFIGURED`, unless
    `SME_ALLOW_OPEN_ENROLLMENT` is true, in which case everyone is `internal` (development and the unit tests; conftest.py sets it).
    """
    expected = read_secret("SMO_ENROLLMENT_SECRET")
    if not expected:
        if os.environ.get("SME_ALLOW_OPEN_ENROLLMENT", "").strip().lower() in ("1", "true", "yes", "on"):
            return roles.ROLE_INTERNAL
        raise framework_error(FrameworkError.ENROLLMENT_NOT_CONFIGURED,
                              detail="SME has no enrollment secret (SMO_ENROLLMENT_SECRET[_FILE]): it cannot tell an SMO module from an rApp")
    if presented is None:
        return roles.ROLE_RAPP
    # The comparison is constant-time (hmac.compare_digest inside), so the secret cannot be guessed byte by byte.
    if not roles.enrollment_secret_valid(presented, expected):
        raise framework_error(FrameworkError.ENROLLMENT_REFUSED, detail="the enrollment secret presented is not the one SME holds")
    return roles.ROLE_INTERNAL


def _check_public_key(value: str) -> None:
    """Raises 422 `SECURITY_CONTEXT_INVALID` when `value` looks like a PEM key (starts with `-----BEGIN`) but does not parse; any other value is accepted as a label.

    Rejecting at onboarding means a key that can never verify an assertion is not stored under the impression that it can.
    """
    if value.lstrip().startswith("-----BEGIN") and _pem_public_key(value) is None:
        raise framework_error(FrameworkError.SECURITY_CONTEXT_INVALID, detail="apiInvokerPublicKey is not a valid PEM public key")


@app.post("/invoker-registrations", status_code=201)
def register_invoker(body: InvokerRegistrationRequest, request: Request, db: Session = Depends(get_session),
                     enrollment: str | None = Header(default=None, alias=roles.ENROLLMENT_HEADER)):
    """HISTORY.md §2: API Invoker onboarding
    (`invokermanagement.go`'s `InvokerManager`) — the real registry the
    Security/token API's own `IsInvokerRegistered`/`VerifyInvokerSecret`
    gate needs, entirely absent before this pass.

    HISTORY.md §7 SME item 1: the real CAPIF onboarding flow is
    public-key-based — the client supplies `apiInvokerPublicKey`; the
    server *generates* both `apiInvokerId` and `onboardingSecret` and
    returns them (`apiInvokerId` "shall not be present" in the real
    client request at all). Every call mints a new invoker (the reference
    always creates, never updates, on onboarding).

    SA-SME-1-public-key: a PEM public key is parsed here (422 if it is
    malformed) and later verifies the invoker's signed token requests;
    any other value is kept as an opaque label. OI-5-sme-filters: emits
    API_INVOKER_ONBOARDED.
    """
    # Route notes. Response: 201 `{apiInvokerId, onboardingSecret, role, keyAuthentication, authzScope}`; the secret appears here once and only its scrypt hash is stored.
    # Errors: 422 `SECURITY_CONTEXT_INVALID` (bad PEM), 422 `AUTHZ_SCOPE_INVALID`, 403 `SCOPE_DENIED`, 403 `ENROLLMENT_REFUSED`, 503 `ENROLLMENT_NOT_CONFIGURED`.
    # Transaction: one; the invoker row and the API_INVOKER_ONBOARDED outbox rows commit together.
    _check_public_key(body.apiInvokerPublicKey)
    claim = _checked_scope(body.authzScope)
    # `caller` is the scope the gateway attached to this request (None: unscoped). The block below keeps a scoped caller from widening its reach by minting a new identity.
    caller = authz_scope.request_scope(request)
    if caller is not None:
        # A caller whose claim permits nothing on some axis (an empty set, which is also what an unreadable claim decodes to) has nothing it may hand out.
        if any(getattr(caller, axis) == frozenset() for axis in authz_scope.AXES):
            raise framework_error(FrameworkError.SCOPE_DENIED, detail="the caller's scope claim permits nothing, so it has nothing to hand out")
        # PR-SEC-10.3: a scoped caller cannot mint an invoker with more reach than its own (it would otherwise leave its scope by registering a new identity): the new
        # invoker carries the caller's claim, or a narrower one it names
        if claim is not None and not authz_scope.covers(caller, authz_scope.from_claim(claim)):
            raise framework_error(FrameworkError.SCOPE_DENIED, detail="the scope claim asked for is wider than the caller's own")
        # No claim asked for: the new invoker inherits the caller's claim, so it is never unscoped when its creator is scoped.
        claim = claim or authz_scope.to_claim(caller)
    # Nothing has been written yet when this can raise, so a refused enrolment leaves no row behind.
    kind = _enrolled(enrollment)                                  # PR-SEC-14: `X-SMO-Enrollment` makes the invoker an SMO module's
    api_invoker_id = f"api-invoker-{uuid.uuid4()}"
    onboarding_secret = secrets.token_urlsafe(32)
    inv = InvokerRegistration(api_invoker_id=api_invoker_id, public_key=body.apiInvokerPublicKey,
                               onboarding_secret_hash=_hash_secret(onboarding_secret), kind=kind, authz_scope=claim)
    db.add(inv)
    notify_invoker_change(db, api_invoker_id, "API_INVOKER_ONBOARDED")  # enqueued in this transaction (PR-MSG-1.6)
    db.commit()
    return {"apiInvokerId": inv.api_invoker_id, "onboardingSecret": onboarding_secret, "role": kind,
            "keyAuthentication": _pem_public_key(inv.public_key) is not None, "authzScope": inv.authz_scope}


@app.put("/invoker-registrations/{api_invoker_id}")
def update_invoker(api_invoker_id: str, body: InvokerRegistrationRequest, db: Session = Depends(get_session)):
    """PutOnboardedInvokersApiInvokerId — replaces the invoker's public key
    (key rotation). Assertions signed with the old key stop verifying at
    once. 404 for an unknown invoker. Emits API_INVOKER_UPDATED."""
    # Route notes: the key is replaced as given; SME itself does not check who the caller is. Errors: 400 `INVOKER_NOT_REGISTERED`
    # (note 400, not 404), 422 `SECURITY_CONTEXT_INVALID`. The registration is looked up first so an unknown id is reported before the key is validated.
    # Does not touch the secret, the scope claim or issued tokens (tokens already issued stay valid; only new assertions are checked against the new key).
    inv = db.get(InvokerRegistration, api_invoker_id)
    if inv is None:
        raise framework_error(FrameworkError.INVOKER_NOT_REGISTERED, detail=f"invoker {api_invoker_id} not registered")
    _check_public_key(body.apiInvokerPublicKey)
    inv.public_key = body.apiInvokerPublicKey
    notify_invoker_change(db, api_invoker_id, "API_INVOKER_UPDATED")
    db.commit()
    return {"apiInvokerId": inv.api_invoker_id, "keyAuthentication": _pem_public_key(inv.public_key) is not None}


# Body of PUT /invoker-registrations/{id}/authz-scope: `authzScope` null or `{}` removes the claim.
class AuthzScopeRequest(BaseModel):
    authzScope: dict | None = None


@app.put("/invoker-registrations/{api_invoker_id}/authz-scope")
def set_invoker_authz_scope(api_invoker_id: str, body: AuthzScopeRequest, db: Session = Depends(get_session)):
    """PR-SEC-10.3: set (or, with `authzScope` null or `{}`, remove) the scope claim of an invoker: which managed elements, by region and tenant, it may touch
    (docs/adr/0005-tenant-region-authorization.md). Replaces the claim. For an operator or an SMO module: an rApp is refused at the gateway (the route
    is in `INTERNAL_ONLY` and not in `RAPP_MAY_CHANGE`). Takes effect on the invoker's next request (R1 Termination reads the claim from the
    introspection; with its introspection cache on, within `R1_INTROSPECTION_CACHE_SECONDS`). 404 for an unknown invoker, 422 `AUTHZ_SCOPE_INVALID`."""
    # Route notes: the claim is validated before it is assigned, so a refused edit changes nothing. Emits API_INVOKER_UPDATED in the same transaction.
    inv = db.get(InvokerRegistration, api_invoker_id)
    if inv is None:
        raise framework_error(FrameworkError.INVOKER_NOT_REGISTERED, detail=f"invoker {api_invoker_id} not registered")
    inv.authz_scope = _checked_scope(body.authzScope)
    notify_invoker_change(db, api_invoker_id, "API_INVOKER_UPDATED")
    db.commit()
    return {"apiInvokerId": inv.api_invoker_id, "authzScope": inv.authz_scope}


@app.post("/invoker-registrations/{api_invoker_id}/secret")
def renew_invoker_secret(api_invoker_id: str, db: Session = Depends(get_session)):
    """A new onboarding secret for the same invoker id (call flow 01e): rApp Management uses it to re-send a running instance's credentials (its
    rApp was unreachable when the instance was created, or a rotation), so the instance keeps the id its limits, kill switch and approval policy key on. The old secret stops
    working at once and the invoker's tokens are revoked, so whoever held them can do nothing more. Internal-only at R1 (`roles.INTERNAL_ONLY`).
    Emits API_INVOKER_UPDATED."""
    # Route notes: 200 `{apiInvokerId, onboardingSecret}`, the secret shown here once and stored only as its hash; 400 `INVOKER_NOT_REGISTERED` for an
    # unknown id (as the PUT above). One transaction: the hash, the revoked tokens and the outbox row commit together.
    inv = db.get(InvokerRegistration, api_invoker_id)
    if inv is None:
        raise framework_error(FrameworkError.INVOKER_NOT_REGISTERED, detail=f"invoker {api_invoker_id} not registered")
    onboarding_secret = secrets.token_urlsafe(32)
    inv.onboarding_secret_hash = _hash_secret(onboarding_secret)
    db.query(IssuedAccessToken).filter(IssuedAccessToken.api_invoker_id == api_invoker_id).delete()
    notify_invoker_change(db, api_invoker_id, "API_INVOKER_UPDATED")
    db.commit()
    return {"apiInvokerId": api_invoker_id, "onboardingSecret": onboarding_secret}


@app.delete("/invoker-registrations/{api_invoker_id}", status_code=204)
def offboard_invoker(api_invoker_id: str, db: Session = Depends(get_session)):
    """DeleteOnboardedInvokersApiInvokerId — offboarding. The invoker's
    live tokens and trusted-invoker security context go with it, so
    nothing it was granted outlives it. Idempotent (204 for an unknown id).
    Emits API_INVOKER_OFFBOARDED."""
    # Route notes: delegates to `_offboard`, which commits. An unknown id returns without doing anything.
    inv = db.get(InvokerRegistration, api_invoker_id)
    if inv is None:
        return
    _offboard(db, inv)


def _offboard(db: Session, inv: InvokerRegistration) -> None:
    """Deletes `inv` together with its issued tokens and trusted-invoker context, queues API_INVOKER_OFFBOARDED, and commits.

    Used by the DELETE route and by `purge_stale_invokers`; the commit is inside, so a purge is one transaction per invoker, not one for the whole sweep.
    `UsedClientAssertion` rows of the invoker are not removed.
    """
    api_invoker_id = inv.api_invoker_id
    # Tokens first: introspection must stop answering active for an invoker that no longer exists.
    db.query(IssuedAccessToken).filter(IssuedAccessToken.api_invoker_id == api_invoker_id).delete()
    # Call flow 18b: what the invoker made as itself goes too: its CAPIF event subscriptions, and the provider registered under its id with that provider's services.
    db.query(ServiceEventSubscription).filter(ServiceEventSubscription.subscriber_id == api_invoker_id).delete()
    _delete_provider(db, api_invoker_id)
    trusted = db.get(TrustedInvoker, api_invoker_id)
    if trusted is not None:
        db.delete(trusted)
    db.delete(inv)
    notify_invoker_change(db, api_invoker_id, "API_INVOKER_OFFBOARDED")
    db.commit()


@app.post("/invoker-registrations/purge-stale")
def purge_stale_invokers(unused_for_days: int = Query(gt=0, le=36500), dry_run: bool = True, db: Session = Depends(get_session)):
    """PR-ST-4 housekeeping (this build's own addition; CAPIF has no such operation). Offboards every invoker
    that has obtained no token for `unused_for_days` days (or, if it never did, was onboarded that long ago):
    the leftovers of processes that registered an identity of their own and were replaced. A module that
    comes back later and finds its identity gone is onboarded afresh by `R1Client`. `dry_run` defaults to
    true: it lists what would go and deletes nothing."""
    # Route notes. The staleness test is `coalesce(last_token_issued_at, created_at) < cutoff`, evaluated in the database. `invokerIds` is sorted so a dry run is reproducible.
    # `unused_for_days` is bounded 1..36500 (422 outside). Each offboarded invoker emits API_INVOKER_OFFBOARDED and commits separately.
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=unused_for_days)
    stale = list(db.scalars(select(InvokerRegistration).where(
        func.coalesce(InvokerRegistration.last_token_issued_at, InvokerRegistration.created_at) < cutoff)))
    ids = sorted(inv.api_invoker_id for inv in stale)
    if not dry_run:
        for inv in stale:
            _offboard(db, inv)
    return {"unusedForDays": unused_for_days, "dryRun": dry_run, "count": len(ids), "invokerIds": ids}


# Body of POST /oauth2/token (RFC 6749 client_credentials). Authenticate with exactly one of `client_secret` or the RFC 7523 pair `client_assertion_type` + `client_assertion`.
class AccessTokenRequest(BaseModel):
    grant_type: str
    client_id: str
    client_secret: str | None = None
    scope: str | None = None
    # SA-SME-1-public-key: RFC 7523 section 2.2 client authentication
    client_assertion_type: str | None = None
    client_assertion: str | None = None


def _token_error(error: str, description: str) -> JSONResponse:
    """Returns the RFC 6749 section 5.2 error response (HTTP 400, `{error, error_description}`) the token endpoint uses for every refusal except an unsupported grant type."""
    return JSONResponse(status_code=400, content={"error": error, "error_description": description})


# Claims a client assertion must carry (RFC 7523 section 3). Also the closed list `_assertion_failure` may name, so the response never echoes caller-controlled text.
REQUIRED_ASSERTION_CLAIMS = ("iss", "sub", "aud", "exp", "jti")


def _assertion_failure(exc: jwt.PyJWTError) -> str:
    """Returns a fixed, caller-safe sentence for a PyJWT failure (signature, expiry, audience, missing claim, disallowed algorithm; anything else is "malformed or unverifiable JWT").

    The library's own message is never passed on (it can carry token content and library internals; CodeQL py/stack-trace-exposure). The tests match on the substrings
    "Signature verification failed", "Audience" and "expired".
    """
    if isinstance(exc, jwt.InvalidSignatureError):
        return "Signature verification failed"
    if isinstance(exc, jwt.ExpiredSignatureError):
        return "Signature has expired"
    if isinstance(exc, jwt.InvalidAudienceError):
        return "Audience does not name this token endpoint"
    if isinstance(exc, jwt.MissingRequiredClaimError):
        missing = next((claim for claim in REQUIRED_ASSERTION_CLAIMS if claim == exc.claim), "a required")
        return f"Token is missing the {missing} claim"
    if isinstance(exc, jwt.InvalidAlgorithmError):
        return "The specified alg value is not allowed"
    return "malformed or unverifiable JWT"


def _verify_client_assertion(db: Session, inv: InvokerRegistration, body: AccessTokenRequest, assertion: str) -> str | None:
    """RFC 7523 `private_key_jwt`: returns None when `assertion` authenticates `inv`, else a sentence saying why not (it becomes the `invalid_client` error_description).

    The JWT must be signed with the invoker's onboarded PEM key with an algorithm in `ASSERTION_ALGORITHMS`, carry `iss`, `sub`, `aud`, `exp` and `jti`, name the invoker as both
    `iss` and `sub`, have `aud` equal to `TOKEN_ENDPOINT_AUDIENCE`, expire no more than `MAX_ASSERTION_LIFETIME_SECONDS` from now, and have a `jti` never exchanged before.

    Side effects: deletes expired `UsedClientAssertion` rows and adds one for this `jti`, in the caller's session and WITHOUT committing. The caller commits when it issues the
    token and rolls back on any failure, so a rolled-back request does not burn the `jti`.
    """
    # `body.client_assertion_type` is checked here rather than in the model so the failure is reported in the same `invalid_client` shape as the other assertion failures.
    if body.client_assertion_type != CLIENT_ASSERTION_TYPE:
        return f"client_assertion_type must be {CLIENT_ASSERTION_TYPE}"
    key = _pem_public_key(inv.public_key)
    if key is None:
        return "invoker has no PEM public key to verify an assertion with"
    try:
        # `algorithms` is an allow-list of asymmetric algorithms only; `none` and HMAC are not in it. `require` makes PyJWT reject a missing claim; `exp` and `aud` are verified by default.
        claims = jwt.decode(assertion, key=key, algorithms=ASSERTION_ALGORITHMS,
                            audience=TOKEN_ENDPOINT_AUDIENCE, options={"require": list(REQUIRED_ASSERTION_CLAIMS)})
    except jwt.PyJWTError as exc:
        # A fixed message per failure kind, never the library's own text
        # (CodeQL py/stack-trace-exposure)
        return f"client assertion not valid: {_assertion_failure(exc)}"
    if claims["iss"] != inv.api_invoker_id or claims["sub"] != inv.api_invoker_id:
        return "client assertion iss and sub must both be the client_id"
    now = datetime.datetime.now(datetime.UTC)
    expires_at = datetime.datetime.fromtimestamp(claims["exp"], datetime.UTC)
    # A long-lived assertion would keep its `jti` in the replay table for as long, and would stay replayable-until-used for longer; the cap bounds both.
    if (expires_at - now).total_seconds() > MAX_ASSERTION_LIFETIME_SECONDS:
        return f"client assertion must expire within {MAX_ASSERTION_LIFETIME_SECONDS} s"
    # Purging only rows whose assertion has itself expired is safe: such an assertion no longer verifies, so its `jti` needs no remembering. This is the only place the table is cleaned.
    db.query(UsedClientAssertion).filter(UsedClientAssertion.expires_at <= now).delete()
    # `str()` because `jti` may be any JSON value in a token; the column is a string primary key.
    if db.get(UsedClientAssertion, str(claims["jti"])) is not None:
        return "client assertion jti already used"
    db.add(UsedClientAssertion(jti=str(claims["jti"]), api_invoker_id=inv.api_invoker_id, expires_at=expires_at))
    return None


def _visible_to(service: ServiceProfile, api_invoker_id: str) -> bool:
    """The discovery authorization gate (LLD section 2.2): True when `api_invoker_id` may see `service`, learn of its events, or name it in a token scope.

    Visible when the service has no policy row, the policy's `gates_discovery_visibility` is false, `allowed_consumers` is empty or None (open to all), or the invoker is listed.
    `api_invoker_id` is whatever the caller supplied; this function does not authenticate it.
    """
    policy = service.authz_policy
    return (policy is None or not policy.gates_discovery_visibility or not policy.allowed_consumers
            or api_invoker_id in policy.allowed_consumers)


def _check_scope(db: Session, scope: str | None, api_invoker_id: str, kind: str = roles.ROLE_INTERNAL) -> str | None:
    """Returns None when the requested token `scope` may be granted to the invoker, else the sentence that becomes the `invalid_scope` error_description.

    `kind` is the invoker's `internal`/`rapp` kind. Rules in order: (1) `smo-internal` or `smo-gui` asked by a non-internal invoker is refused in `enforce` mode, and granted with a
    warning log in `audit` mode (PR-SEC-14); (2) no scope, or any of `INTERNAL_SCOPES` (which also holds `smo-rapp`), is granted as given; (3) anything else must be TS 29.222
    `3gpp#aefId:apiName[,apiName][;aefId:...]` where every apiName is a published service, visible to this invoker (`_visible_to`), exposed by that AEF (`_aef_ids`).
    These are the reference's `IsFunctionRegistered` / `IsAPIPublished` checks plus the discovery gate (OI-2-oauth2-scope).
Read only: nothing is written, so callers may roll back freely.
    """
    # This check comes first so the internal-only scopes are never reached by the generic 'internal scope is granted' branch below it.
    if scope in roles.INTERNAL_SCOPES and kind != roles.ROLE_INTERNAL:
        # PR-SEC-14: an rApp does not get the scopes the SMO's own clients use. In audit mode it is granted and counted.
        if roles.enforcement_mode() == "enforce":
            return f"scope {scope!r} is for SMO modules: this invoker did not enroll"
        log.warning("role audit: rApp invoker %s was granted internal scope %r", api_invoker_id, scope)
        return None
    if not scope or scope in INTERNAL_SCOPES:
        return None
    if not scope.startswith(CAPIF_SCOPE_PREFIX):
        return f"scope must be {sorted(INTERNAL_SCOPES)} or {CAPIF_SCOPE_PREFIX}aefId:apiName[,apiName][;aefId:apiName]"
    for part in scope[len(CAPIF_SCOPE_PREFIX):].split(";"):
        aef_id, sep, api_names = part.partition(":")
        names = [n for n in api_names.split(",") if n]
        # `partition` returns an empty separator when there is no colon; empty names (`aef:,`) are dropped by the list comprehension above and then rejected here.
        if not sep or not aef_id or not names:
            return f"malformed scope entry {part!r}, expected aefId:apiName[,apiName]"
        for name in names:
            service = db.scalar(select(ServiceProfile).where(ServiceProfile.service_name == name))
            # An API hidden from this invoker gets the same answer as an unpublished one, so a token request cannot be used to probe for hidden services.
            if service is None or not _visible_to(service, api_invoker_id):
                return f"API {name!r} is not published"
            if aef_id not in _aef_ids(service):
                return f"API {name!r} is not exposed by AEF {aef_id!r}"
    return None


class OAuthError(BaseModel):
    """RFC 6749 section 5.2: what the token and introspection endpoints answer when they refuse, not the ProblemDetails of the other routes."""
    error: str
    error_description: str | None = None


class UnparsableBody(BaseModel):
    """What the framework answers (400) before the route runs, when the body is not JSON."""
    detail: str


@app.post("/oauth2/token", responses={400: {"model": OAuthError | UnparsableBody, "description": "RFC 6749 section 5.2 error"}, 401: {"model": OAuthError | UnparsableBody, "description": "RFC 6749 section 5.2 error"}})
def issue_access_token(body: AccessTokenRequest, db: Session = Depends(get_session)):
    """HISTORY.md §2: "no actual validation code path" for
    R1 Termination's advertised tokenEndPoint — this is that endpoint,
    finally real. Mirrors `PostSecuritiesSecurityIdToken`
    (`securityservice.go`)'s real request/response shape (`client_id`/
    `client_secret`/`grant_type`/`scope`, `access_token`/`expires_in`/
    `token_type`/`scope`) and its real checks — 400 on any failure, same
    as the reference. The reference then delegates actual JWT signing to an
    external Keycloak instance; this build has no real IdP, so it issues its
    own opaque, server-tracked token instead (see `IssuedAccessToken`).

    The client authenticates with exactly one of its onboarding secret
    (`client_secret`) or an RFC 7523 client assertion signed with its
    onboarded public key (`client_assertion`, SA-SME-1-public-key).
    `scope` is checked before anything is issued (`_check_scope`,
    OI-2-oauth2-scope): 400 `invalid_scope` names the first API that is
    not published, not exposed by the named AEF, or not visible to the
    invoker.
    """
    # Route notes (the order of the checks is part of the contract). 1. Grant type, else 400 `unsupported_grant_type` with no description. 2. Secret and assertion together: `invalid_request`.
    # 3. Unknown `client_id`: `invalid_client`. 4. Credentials: an assertion that fails gives `invalid_client`; a wrong secret gives `unauthorized_client` (so an unknown id and a wrong secret are distinguishable by the caller).
    # 5. Scope, `invalid_scope`. 6. Only then a token is created. Every refusal is HTTP 400 with `{error, error_description}`.
    # Transaction: nothing is committed until the end. A failed assertion or scope check calls `db.rollback()` so the `jti` row `_verify_client_assertion` added is discarded.
    # A missing `client_secret` is verified as an empty string, which simply fails.
    if body.grant_type != "client_credentials":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    if body.client_secret is not None and body.client_assertion is not None:
        return _token_error("invalid_request", "use client_secret or client_assertion, not both")
    inv = db.get(InvokerRegistration, body.client_id)
    if inv is None:
        return _token_error("invalid_client", "invoker not registered")
    if body.client_assertion is not None:
        problem = _verify_client_assertion(db, inv, body, body.client_assertion)
        if problem is not None:
            db.rollback()
            return _token_error("invalid_client", problem)
    elif not _verify_secret(body.client_secret or "", inv.onboarding_secret_hash):
        return _token_error("unauthorized_client", "onboarding secret not valid")
    problem = _check_scope(db, body.scope, inv.api_invoker_id, inv.kind)
    if problem is not None:
        db.rollback()
        return _token_error("invalid_scope", problem)
    # 256 bits from the OS CSPRNG; the raw value is returned below and only its hash is persisted.
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)
    db.add(IssuedAccessToken(access_token_hash=_hash_token(token), api_invoker_id=inv.api_invoker_id,
                             expires_at=expires_at, scope=body.scope or None))
    # Feeds the stale-invoker purge (`purge_stale_invokers`): an invoker that keeps getting tokens is never stale.
    inv.last_token_issued_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return {"access_token": token, "expires_in": ACCESS_TOKEN_TTL_SECONDS, "token_type": "Bearer", "scope": body.scope}


# Body of POST /oauth2/introspect (RFC 7662): the raw bearer token to look up.
class IntrospectRequest(BaseModel):
    token: str


@app.post("/oauth2/introspect", responses={400: {"model": OAuthError | UnparsableBody, "description": "RFC 6749 section 5.2 error"}, 401: {"model": OAuthError | UnparsableBody, "description": "RFC 6749 section 5.2 error"}})
def introspect_token(body: IntrospectRequest, db: Session = Depends(get_session)):
    """RFC 7662 — the honest substitute for the reference's own
    self-contained signed-JWT validation (see issue_access_token's own
    docstring for why this build issues opaque tokens instead). R1
    Termination calls this on every proxied request to decide whether to
    forward it; an unauthenticated internal call, matching the same
    network-isolation reasoning /bootstrap's own docstring already gives
    for staying unauthenticated itself (SME<->R1 Termination traffic
    never leaves the docker-compose network). `scope` is the granted scope
    (RFC 7662 section 2.2), for the resource server that enforces it.
    """
    # Route notes. Always 200. `{"active": false}` for an unknown or expired token and nothing else; the answer for a valid one is `{active, client_id, exp, role}` plus `scope` when the
    # token was granted one and `authz_scope` when the invoker has a claim. `role` and `authz_scope` are read from the invoker row at call time, not frozen at issuance, so an
    # operator's change applies to tokens already issued (R1 Termination may cache the answer, `R1_INTROSPECTION_CACHE_SECONDS`). No transaction: read only.
    rec = db.get(IssuedAccessToken, _hash_token(body.token))
    # `as_utc` because SQLite hands back naive datetimes in the unit tests; comparing a naive and an aware value would raise.
    if rec is None or as_utc(rec.expires_at) <= datetime.datetime.now(datetime.UTC):
        return {"active": False}
    registration = db.get(InvokerRegistration, rec.api_invoker_id)
    view = {"active": True, "client_id": rec.api_invoker_id, "exp": int(as_utc(rec.expires_at).timestamp()),
            # A token whose invoker row is gone is reported as the less-privileged role rather than as internal.
            "role": registration.kind if registration is not None else roles.ROLE_RAPP}     # PR-SEC-14: what R1 Termination applies its role policy on
    if rec.scope:
        view["scope"] = rec.scope
    if registration is not None and registration.authz_scope:
        view["authz_scope"] = registration.authz_scope           # PR-SEC-10.3: the scope claim, read live: an edit applies from the invoker's next request
    return view


# One entry of ServiceSecurity.securityInfo (TS 29.222 SecurityInformation). `prefSecurityMethods` must be non-empty; `_validate_service_security` enforces it.
class SecurityInformationRequest(BaseModel):
    aefId: str | None = None
    apiId: str | None = None
    authenticationInfo: str | None = None
    authorizationInfo: str | None = None
    prefSecurityMethods: list[str]


# Body of the trusted-invoker PUT and update routes (TS 29.222 ServiceSecurity).
class ServiceSecurityRequest(BaseModel):
    notificationDestination: str
    requestTestNotification: bool = False
    securityInfo: list[SecurityInformationRequest]


def _validate_service_security(body: ServiceSecurityRequest) -> None:
    """Raises 422 `SECURITY_CONTEXT_INVALID` when the body lacks a non-blank `notificationDestination`, has an empty `securityInfo`, or has an entry with no `prefSecurityMethods`.

    These are the required-field rules of ServiceSecurity that the pydantic model cannot express (empty list, blank string). The destination is only checked for being non-blank;
    it is not contacted and not SSRF-checked here (SME never sends to it).
    """
    if not body.notificationDestination.strip():
        raise framework_error(FrameworkError.SECURITY_CONTEXT_INVALID, detail="ServiceSecurity missing required notificationDestination")
    if not body.securityInfo:
        raise framework_error(FrameworkError.SECURITY_CONTEXT_INVALID, detail="ServiceSecurity missing required securityInfo")
    for info in body.securityInfo:
        if not info.prefSecurityMethods:
            raise framework_error(FrameworkError.SECURITY_CONTEXT_INVALID, detail="SecurityInformation missing required prefSecurityMethods")


def _security_info_with_sel_method(security_info: list[SecurityInformationRequest]) -> list[dict]:
    """Converts the request's `securityInfo` entries to the dicts stored in `TrustedInvoker.security_info`, setting `selSecurityMethod` to the first `prefSecurityMethods`.

    This is `typeupdate.go`'s `PrepareNewSecurityContext`, adapted: CAPIF core picks the method (400 if none) by matching against the AEF's own security-method catalogue; SME stores no such catalogue, so the choice is the caller's first preference and is never
    checked against what the AEF supports (a documented adaptation, see `sme/README.md` section 1.5). Callers have already ensured every entry's list is non-empty.
    """
    return [
        {"aefId": info.aefId, "apiId": info.apiId, "authenticationInfo": info.authenticationInfo,
         "authorizationInfo": info.authorizationInfo, "prefSecurityMethods": info.prefSecurityMethods,
         "selSecurityMethod": info.prefSecurityMethods[0]}
        for info in security_info
    ]


@app.put("/trusted-invokers/{api_invoker_id}", status_code=201)
def register_trusted_invoker(api_invoker_id: str, body: ServiceSecurityRequest, db: Session = Depends(get_session)):
    """PutTrustedInvokersApiInvokerId (`security.go`) — registers (or
    replaces, on a re-PUT) the security context a real AEF would consult
    for this invoker. Gated on the invoker already being onboarded
    (`invokerRegister.IsInvokerRegistered`), same real 400 the reference
    returns otherwise.
    """
    # Route notes. Replaces any previous context for the invoker (PUT, not merge) and always answers 201, including a replacement. Errors: 400 `INVOKER_NOT_REGISTERED`, 422
    # `SECURITY_CONTEXT_INVALID`. The response is `_trusted_invoker_view`, so it contains `authenticationInfo` and `authorizationInfo` unredacted (only the GET route redacts).
    if db.get(InvokerRegistration, api_invoker_id) is None:
        raise framework_error(FrameworkError.INVOKER_NOT_REGISTERED, detail=f"invoker {api_invoker_id} not registered")
    _validate_service_security(body)
    ti = db.get(TrustedInvoker, api_invoker_id)
    if ti is None:
        ti = TrustedInvoker(api_invoker_id=api_invoker_id)
        db.add(ti)
    ti.notification_destination = body.notificationDestination
    ti.request_test_notification = body.requestTestNotification
    ti.security_info = _security_info_with_sel_method(body.securityInfo)
    db.commit()
    return _trusted_invoker_view(ti)


@app.get("/trusted-invokers/{api_invoker_id}")
def get_trusted_invoker(api_invoker_id: str, authentication_info: bool = False, authorization_info: bool = False, db: Session = Depends(get_session)):
    """GetTrustedInvokersApiInvokerId — the real reference redacts
    `authenticationInfo`/`authorizationInfo` to an empty string unless
    the caller explicitly asks for each via its own query params
    (`checkParams`), rather than always returning the raw secrets to
    whoever asks.
    """
    # Route notes: 404 `TRUSTED_INVOKER_NOT_FOUND`. Redaction happens on the copy returned by `_trusted_invoker_view`, never on the stored row.
    ti = db.get(TrustedInvoker, api_invoker_id)
    if ti is None:
        raise framework_error(FrameworkError.TRUSTED_INVOKER_NOT_FOUND, detail=f"invoker {api_invoker_id} not registered as trusted invoker")
    view = _trusted_invoker_view(ti)
    for info in view["securityInfo"]:
        if not authentication_info:
            info["authenticationInfo"] = ""
        if not authorization_info:
            info["authorizationInfo"] = ""
    return view


@app.delete("/trusted-invokers/{api_invoker_id}", status_code=204)
def deregister_trusted_invoker(api_invoker_id: str, db: Session = Depends(get_session)):
    # Route notes: no docstring on purpose (it would become OpenAPI text). Idempotent: 204 for an unknown id. Does not touch the invoker registration.
    ti = db.get(TrustedInvoker, api_invoker_id)
    if ti is not None:
        db.delete(ti)
        db.commit()


@app.post("/trusted-invokers/{api_invoker_id}/update")
def update_trusted_invoker(api_invoker_id: str, body: ServiceSecurityRequest, db: Session = Depends(get_session)):
    """PostTrustedInvokersApiInvokerIdUpdate — update-in-place. Unlike
    the PUT above, the reference never re-checks invoker registration
    here, only that a trusted-invoker context already exists.
    """
    # Route notes: 404 `TRUSTED_INVOKER_NOT_FOUND`, 422 `SECURITY_CONTEXT_INVALID`; 200 with the unredacted view. Replaces the whole securityInfo list.
    ti = db.get(TrustedInvoker, api_invoker_id)
    if ti is None:
        raise framework_error(FrameworkError.TRUSTED_INVOKER_NOT_FOUND, detail=f"invoker {api_invoker_id} not registered as trusted invoker")
    _validate_service_security(body)
    ti.notification_destination = body.notificationDestination
    ti.request_test_notification = body.requestTestNotification
    ti.security_info = _security_info_with_sel_method(body.securityInfo)
    db.commit()
    return _trusted_invoker_view(ti)


# Body of POST /trusted-invokers/{id}/delete (TS 29.222 SecurityNotification). `cause` is validated against the two values CAPIF defines but is not otherwise used.
class SecurityNotificationRequest(BaseModel):
    aefId: str | None = None
    apiIds: list[str]
    apiInvokerId: str
    cause: Literal["OVERLIMIT_USAGE", "UNEXPECTED_REASON"]


@app.post("/trusted-invokers/{api_invoker_id}/delete", status_code=204)
def revoke_trusted_invoker(api_invoker_id: str, body: SecurityNotificationRequest, db: Session = Depends(get_session)):
    """PostTrustedInvokersApiInvokerIdDelete — revocation, not a full
    delete: only the `securityInfo` entries matching the notified
    `aefId` or one of `apiIds` are removed (`revokeTrustedInvoker`); the
    whole trusted-invoker record is only dropped once no entries remain.
    Implements the reference's own stated filter semantics directly
    (keep entries that don't match) rather than its own Go loop, which
    mutates a slice by a stale index mid-iteration — a real bug in
    `capifcore` itself, not behavior worth reproducing.
    """
    # Route notes. 422 `SECURITY_CONTEXT_INVALID` for empty `apiIds`, 404 `TRUSTED_INVOKER_NOT_FOUND`, else 204. The path `api_invoker_id` is authoritative; `body.apiInvokerId` and
    # `body.cause` are not read.
    if not body.apiIds:
        raise framework_error(FrameworkError.SECURITY_CONTEXT_INVALID, detail="SecurityNotification missing required apiIds")
    ti = db.get(TrustedInvoker, api_invoker_id)
    if ti is None:
        raise framework_error(FrameworkError.TRUSTED_INVOKER_NOT_FOUND, detail=f"invoker {api_invoker_id} not registered as trusted invoker")
    # An entry is removed when it matches the notified `aefId` OR one of the `apiIds`. Written as 'keep what matches neither' instead of deleting from the list while iterating it.
    remaining = [info for info in ti.security_info
                 if info.get("aefId") != body.aefId and info.get("apiId") not in body.apiIds]
    # Last entry revoked: drop the record, so a trusted invoker with an empty securityInfo (which PUT would reject) can never exist.
    if not remaining:
        db.delete(ti)
    else:
        ti.security_info = remaining
    db.commit()


def _trusted_invoker_view(ti: TrustedInvoker) -> dict:
    """Returns the wire form of a trusted-invoker row.

    `securityInfo` is the stored list object itself, not a copy. `get_trusted_invoker` blanks fields inside those same dicts; that never reaches the database only because the
    route does not commit and the JSON column does not track in-place changes.
    """
    return {"apiInvokerId": ti.api_invoker_id, "notificationDestination": ti.notification_destination,
            "requestTestNotification": ti.request_test_notification, "securityInfo": ti.security_info}


@app.post("/published-apis/v1/{apf_id}/service-apis", status_code=201)
def register_service(apf_id: str, body: ServiceRegistration, request: Request, db: Session = Depends(get_session)):
    """RegisterService. apfId == producerId == rAppId (Foundational Platform
    LLD section 1) — identity.py's equivalence, enforced here.

    Section 2.3's conflict rule realized correctly: serviceName is globally
    unique. The SAME producer re-registering the same name updates in
    place (idempotent); a DIFFERENT producer registering that name is
    SERVICE_NAME_CONFLICT.

    HISTORY.md §5: previously accepted any apf_id with no check
    that it's an actual registered publisher. The reference's own gate
    (`PostApfIdServiceApis`, `publishservice.go`:
    `serviceRegister.IsPublishingFunctionRegistered(apfId)`, 403
    otherwise) is now real, backed by ProviderRegistration —
    register_provider above.
    """
    # Route notes. Errors: 403 `APF_NOT_REGISTERED` (no provider enrolment for the path's `apf_id`), 409 `SERVICE_NAME_CONFLICT` (name held by another producer). Answers 201 with
    # `{serviceId}` for an update too. The body's `producerId` is ignored; the path's `apf_id` is stored. Transaction: profile, policy and outbox rows commit together.
    # Re-registration replaces every field including `allowedConsumers` and publishes SERVICE_API_UPDATE. Call flow 18a: an rApp publishes only under its own invoker id
    # (403 `PROVIDER_IDENTITY_MISMATCH`), checked first.
    _require_own(request, apf_id, FrameworkError.PROVIDER_IDENTITY_MISMATCH, "apfId")
    if db.get(ProviderRegistration, apf_id) is None:
        raise framework_error(FrameworkError.APF_NOT_REGISTERED, detail=f"{apf_id} is not a registered publishing function")

    # Looked up by name alone: the name is unique across producers (the table's UNIQUE is on service_name only), so a hit by another producer is the conflict.
    existing = db.scalar(select(ServiceProfile).where(ServiceProfile.service_name == body.serviceName))
    if existing is not None and existing.producer_id != apf_id:
        raise framework_error(FrameworkError.SERVICE_NAME_CONFLICT, detail=f"{body.serviceName} already registered by a different producer")

    if existing is not None:
        profile = existing
        # Update in place: the serviceId stays the same, so subscribers' `apiIds` filters keep matching across a re-registration.
        profile.endpoint, profile.version = body.endpoint, body.version
        profile.full_api_versions, profile.service_capabilities = body.fullApiVersions, body.serviceCapabilities
        profile.selection_criteria, profile.module_scope = body.selectionCriteria, body.moduleScope
        profile.aef_profiles, profile.api_supp_feats, profile.shareable_info = body.aefProfiles, body.apiSuppFeats, body.shareableInfo
    else:
        profile = ServiceProfile(
            service_name=body.serviceName, producer_id=apf_id, endpoint=body.endpoint, version=body.version,
            full_api_versions=body.fullApiVersions, service_capabilities=body.serviceCapabilities,
            selection_criteria=body.selectionCriteria, module_scope=body.moduleScope,
            aef_profiles=body.aefProfiles, api_supp_feats=body.apiSuppFeats, shareable_info=body.shareableInfo,
        )
        db.add(profile)
    db.flush()

    if profile.authz_policy is None:
        db.add(ServiceAuthzPolicy(service_id=profile.service_id, allowed_consumers=body.allowedConsumers))
    else:
        profile.authz_policy.allowed_consumers = body.allowedConsumers
    db.flush()
    db.expire(profile, ["authz_policy"])  # the visibility check below reads the policy this request just wrote
    notify_service_change(db, profile, "SERVICE_API_UPDATE" if existing is not None else "SERVICE_API_AVAILABLE")
    db.commit()
    return {"serviceId": str(profile.service_id)}


@app.delete("/published-apis/v1/{apf_id}/service-apis/{service_id}", status_code=204)
def deregister_service(apf_id: str, service_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # Route notes: 403 `PROVIDER_IDENTITY_MISMATCH` for an rApp naming another apfId (call flow 18a); otherwise 204. Only the owning producer's call has any effect; a different `apf_id` is a silent no-op, indistinguishable from an unknown id, and notifies nobody.
    # Emits SERVICE_API_UNAVAILABLE in the same transaction as the delete.
    _require_own(request, apf_id, FrameworkError.PROVIDER_IDENTITY_MISMATCH, "apfId")
    profile = db.get(ServiceProfile, service_id)
    if profile is not None and profile.producer_id == apf_id:
        notify_service_change(db, profile, "SERVICE_API_UNAVAILABLE")  # before delete: notify_service_change needs the still-live row
        db.delete(profile)
        db.commit()


@app.get("/published-apis/v1/{apf_id}/service-apis")
def query_own_services(apf_id: str, db: Session = Depends(get_session)):
    """GetApfIdServiceApis, publishservice.go — mirrors the reference's own
    branch exactly: an apf_id with existing services always gets them back
    (a real producer's own row set wins regardless of its current
    enrolment state — the reference's own map-lookup-first shape), and
    only an apf_id with zero services falls back to the same
    IsPublishingFunctionRegistered gate register_service now uses, to
    distinguish "a real publisher with nothing registered yet" (empty
    list) from "not a publisher at all" (404).
    """
    # Route notes: 200 with a list (possibly empty), or 404 `PUBLISHING_FUNCTION_NOT_FOUND`. Not filtered by `_visible_to`: a producer sees its own services whatever their `allowedConsumers`.
    rows = db.scalars(select(ServiceProfile).where(ServiceProfile.producer_id == apf_id)).all()
    if not rows and db.get(ProviderRegistration, apf_id) is None:
        raise framework_error(FrameworkError.PUBLISHING_FUNCTION_NOT_FOUND, detail=f"{apf_id} is not a registered publishing function")
    return [_service_view(r) for r in rows]


@app.get("/service-apis/v1/allServiceAPIs")
def discover_services(api_invoker_id: str, request: Request, api_name: str | None = None, api_version: str | None = None,
                       aef_id: str | None = None, protocol: str | None = None, data_format: str | None = None,
                       comm_type: str | None = None, db: Session = Depends(get_session)):
    """DiscoverServices. Section 2.2's decision: one gate — an unauthorized
    consumer's query simply never returns the service, it is never told
    the service exists.

    HISTORY.md §5: discover_services only filtered on
    api_name/api_version — the reference (discoverservice.go's
    matchesFilter/checkAefId/checkProtocol/checkDataFormat/
    checkVersionAndCommType) also filters against each service's
    AefProfiles. aefId/protocol/dataFormat/commType are now real
    filters over ServiceProfile.aef_profiles (walked in Python, since
    it's a JSON blob here rather than the reference's relational
    AefProfile/Version/Resource join — same adaptation as the field's
    own storage). apiCat isn't — this build's ServiceProfile has no
    category, and nothing that publishes a service supplies one
    (OI-5-sme-filters).
    """
    # Route notes. `api_invoker_id` is required. Call flow 18e: for an rApp it must be its own invoker id (403 `INVOKER_IDENTITY_MISMATCH`), so it cannot see what
    # another invoker may; the console and modules may discover for any invoker. `api_name` and `api_version`
    # filter in SQL; the AEF filters and the visibility gate are applied in Python afterwards (`aefProfiles` is a JSON column), so all rows matching name/version are loaded first.
    # No pagination: the full visible list is returned.
    _require_own(request, api_invoker_id, FrameworkError.INVOKER_IDENTITY_MISMATCH, "api_invoker_id")
    stmt = select(ServiceProfile)
    if api_name:
        stmt = stmt.where(ServiceProfile.service_name == api_name)
    if api_version:
        stmt = stmt.where(ServiceProfile.version == api_version)
    rows = db.scalars(stmt).all()

    visible = [r for r in rows if _matches_aef_filters(r, aef_id, protocol, data_format, comm_type) and _visible_to(r, api_invoker_id)]
    return [_service_view(r) for r in visible]


def _matches_aef_filters(r: ServiceProfile, aef_id: str | None, protocol: str | None, data_format: str | None, comm_type: str | None) -> bool:
    """True when `r` passes the AEF-level discovery filters; True for any service when none of the four is set.

    Filters combine with AND inside ONE `aefProfiles` entry: some single profile must match aefId, protocol and dataFormat together, and for `comm_type` some resource of some
    of its versions must have that `commType`. A service with no profiles can never match when any filter is set.
    """
    if not any((aef_id, protocol, data_format, comm_type)):
        return True
    for profile in r.aef_profiles or []:
        if aef_id and profile.get("aefId") != aef_id:
            continue
        if protocol and profile.get("protocol") != protocol:
            continue
        if data_format and profile.get("dataFormat") != data_format:
            continue
        if comm_type:
            resources = (res for v in profile.get("versions", []) for res in v.get("resources", []))
            if not any(res.get("commType") == comm_type for res in resources):
                continue
        return True
    return False


@app.post("/capif-events/v1/{subscriber_id}/subscriptions", status_code=201)
def subscribe_events(subscriber_id: str, body: EventSubscriptionRequest, request: Request, db: Session = Depends(get_session)):
    """SubscribeEvent, with TS 29.222's CAPIFEventFilter (OI-5-sme-filters):
    `apiIds` (this build's serviceIds), `apiInvokerIds` and `aefIds`. Each
    filter that is set must share a value with the event — see
    `_filters_match` — and an event that carries no value of that kind
    does not match it."""
    # Route notes. 422 `SUBSCRIPTION_SCOPE_CONFLICT` when `eventTypes` is not a subset of `EVENT_TYPES` (the code name is a reuse; no conflict is involved). An empty `eventTypes` list is accepted
    # and never matches anything. `callbackUri` is stored unchecked; the SSRF guard runs when a notification is enqueued and sent. The path `subscriber_id` is stored and used by `_visible_to`
    # when events are delivered; nothing authenticates it.
    # Call flow 18e: an rApp subscribes only as itself (403 `INVOKER_IDENTITY_MISMATCH`), since the subscriber id decides which services' events it is shown; a callback
    # the SSRF guard refuses is 422 `SCHEMA_VALIDATION_FAILED` (it was stored and every notification to it dropped).
    _require_own(request, subscriber_id, FrameworkError.INVOKER_IDENTITY_MISMATCH, "subscriber id")
    if not set(body.eventTypes) <= EVENT_TYPES:
        raise framework_error(FrameworkError.SUBSCRIPTION_SCOPE_CONFLICT, detail=f"eventTypes must be a subset of {sorted(EVENT_TYPES)}")
    if not is_safe_webhook_destination(body.callbackUri):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="callbackUri is not an allowed http(s) destination")
    sub = ServiceEventSubscription(subscriber_id=subscriber_id, event_types=body.eventTypes, callback_uri=body.callbackUri,
                                   api_ids=body.apiIds, api_invoker_ids=body.apiInvokerIds, aef_ids=body.aefIds)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id)}


@app.delete("/capif-events/v1/{subscriber_id}/subscriptions/{subscription_id}", status_code=204)
def unsubscribe_events(subscriber_id: str, subscription_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # Route notes: no docstring on purpose (it would become OpenAPI text). 403 `INVOKER_IDENTITY_MISMATCH` for an rApp naming another subscriber (call flow 18e);
    # otherwise 204, the row deleted only when it belongs to the `subscriber_id` in the path.
    _require_own(request, subscriber_id, FrameworkError.INVOKER_IDENTITY_MISMATCH, "subscriber id")
    sub = db.get(ServiceEventSubscription, subscription_id)
    if sub is not None and sub.subscriber_id == subscriber_id:
        db.delete(sub)
        db.commit()


def _aef_ids(service: ServiceProfile) -> set[str]:
    """Returns the non-empty `aefId` values of a service's `aefProfiles` (empty set when it has none)."""
    return {p["aefId"] for p in service.aef_profiles or [] if p.get("aefId")}


def _filters_match(sub: ServiceEventSubscription, detail: dict) -> bool:
    """CAPIFEventFilter matching (eventservice.go's getMatchingSubs): True when every filter the subscription sets shares at least one value with what the event carries.

    An unset or empty filter matches anything. A set filter against an event that carries no value of that kind (an apiIds filter on an invoker event) does not match.
    """
    for wanted, carried in ((sub.api_ids, detail.get("apiIds")), (sub.api_invoker_ids, detail.get("apiInvokerIds")),
                            (sub.aef_ids, detail.get("aefIds"))):
        if wanted and not set(wanted) & set(carried or ()):
            return False
    return True


def _deliver(db: Session, event_type: str, detail: dict, payload: dict, visible=lambda sub: True) -> None:
    """Queues one outbox row per subscription that wants `event_type`, passes `_filters_match` for `detail` and `visible(sub)`.

    The payload is `payload` plus `subscriptionId`, `eventType` and `eventDetail`. Nothing is sent and nothing is committed here: `enqueue` adds the row to the caller's
    transaction (PR-MSG-1.6), and the commit hook of `smo_shared.outbox` sends it after the caller commits. A destination the SSRF guard refuses is dropped by `enqueue`.
    Scans every subscription row (no index on event type).
    """
    for sub in db.scalars(select(ServiceEventSubscription)).all():
        if event_type in sub.event_types and _filters_match(sub, detail) and visible(sub):
            enqueue(db, sub.callback_uri, {**payload, "subscriptionId": str(sub.subscription_id),
                                           "eventType": event_type, "eventDetail": detail})
            # A row in the caller's transaction, sent after it commits (PR-MSG-1.6); the caller commits.


def notify_service_change(db: Session, service: ServiceProfile, event_type: str) -> None:
    """Queues the event for a service change (HISTORY.md section 5): SERVICE_API_AVAILABLE on create, SERVICE_API_UPDATE on re-registration, SERVICE_API_UNAVAILABLE on delete.

    Reaches subscribers whose `eventTypes` include `event_type`, whose filters match the service (`apiIds` is its serviceId, `aefIds` its AEFs) and who may see it by the
    discovery gate `_visible_to` (the subscriber id plays the invoker id). The caller commits. For a new or updated service it must have flushed the service and
    refreshed `authz_policy` first, since the gate reads the policy.
    """
    detail = {"apiIds": [str(service.service_id)], "aefIds": sorted(_aef_ids(service))}
    _deliver(db, event_type, detail, {"serviceId": str(service.service_id)},
             visible=lambda sub: _visible_to(service, sub.subscriber_id))


def notify_invoker_change(db: Session, api_invoker_id: str, event_type: str) -> None:
    """Queues API_INVOKER_ONBOARDED, _UPDATED or _OFFBOARDED for `api_invoker_id` to the subscribers of that event type whose filters match (only an `apiInvokerIds` filter can match; `apiIds`
    and `aefIds` filters never do). No visibility gate: invoker events are not hidden per subscriber. The caller commits.
    """
    _deliver(db, event_type, {"apiInvokerIds": [api_invoker_id]}, {"apiInvokerId": api_invoker_id})


def _service_view(r: ServiceProfile) -> dict:
    """Returns the discovery/query wire form of a service. Omits `moduleScope`, the authz policy and the selection criteria, which stay internal to SME."""
    return {
        "serviceId": str(r.service_id),
        "serviceName": r.service_name,
        "producerId": r.producer_id,
        "endpoint": r.endpoint,
        "version": r.version,
        "fullApiVersions": r.full_api_versions or [],
        "serviceCapabilities": r.service_capabilities or {},
        "aefProfiles": r.aef_profiles or [],
        "apiSuppFeats": r.api_supp_feats,
        "shareableInfo": r.shareable_info,
    }


# ---------------------------------------------------------------- registry reads (GUI pass 2)
# The provider, invoker, trusted-invoker and event-subscription registries were
# write-only (or read one known id at a time). No secret ever leaves here:
# invokers expose their id and public key, never the onboarding-secret hash.

@app.get("/provider-registrations")
def list_providers(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: no docstring on purpose (it would become OpenAPI text). Paged (`limit`/`offset`); `serviceCount` is computed per item with one extra query each.
    page = paginate(db, select(ProviderRegistration), limit, offset)
    return {**page, "items": [{"apfId": p.apf_id, "providerDomainInfo": p.provider_domain_info,
             "serviceCount": len(db.scalars(select(ServiceProfile).where(ServiceProfile.producer_id == p.apf_id)).all())}
            for p in page["items"]]}


@app.get("/invoker-registrations")
def list_invokers(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: no docstring on purpose. Lists id, public key, `keyAuthentication`, `authzScope` and whether a trusted-invoker context exists; the secret hash is never included.
    page = paginate(db, select(InvokerRegistration), limit, offset)
    return {**page, "items": [{"apiInvokerId": i.api_invoker_id, "apiInvokerPublicKey": i.public_key,
             "keyAuthentication": _pem_public_key(i.public_key) is not None, "authzScope": i.authz_scope,
             "trusted": db.get(TrustedInvoker, i.api_invoker_id) is not None}
            for i in page["items"]]}


@app.get("/trusted-invokers")
def list_trusted_invokers(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: no docstring on purpose. Unlike the single GET, this list returns `authenticationInfo` and `authorizationInfo` unredacted.
    page = paginate(db, select(TrustedInvoker), limit, offset)
    return {**page, "items": [_trusted_invoker_view(ti) for ti in page["items"]]}


@app.get("/capif-events/v1/{subscriber_id}/subscriptions")
def list_event_subscriptions(subscriber_id: str, request: Request, limit: int = PageLimit, offset: int = PageOffset,
                              db: Session = Depends(get_session)):
    """The real TS29222_CAPIF_Events_API.yaml only ever defines POST on
    this path — no GET/list operation exists in the real spec at all, so
    this is this build's own GUI-pass addition, not a spec-mandated
    shape: free to follow this build's own pagination convention.
    """
    # Route notes: lists only the subscriptions of the `subscriber_id` in the path; an rApp lists only its own (403 `INVOKER_IDENTITY_MISMATCH`, call flow 18e).
    _require_own(request, subscriber_id, FrameworkError.INVOKER_IDENTITY_MISMATCH, "subscriber id")
    stmt = select(ServiceEventSubscription).where(ServiceEventSubscription.subscriber_id == subscriber_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"subscriptionId": str(s.subscription_id), "subscriberId": s.subscriber_id, "eventTypes": s.event_types,
             "callbackUri": s.callback_uri, "apiIds": s.api_ids, "apiInvokerIds": s.api_invoker_ids, "aefIds": s.aef_ids}
            for s in page["items"]]}
