"""SQLAlchemy tables of SME and the CAPIF event-type sets (`SERVICE_API_EVENTS`, `API_INVOKER_EVENTS`, `EVENT_TYPES`).

What it is: the eight tables behind `app/main.py`, and the MnS registry's store `mns_registry` (`app/mns_registry.py`, plan task S1.12) (service registry and its authorization policy, provider and invoker registrations, issued tokens, used client
assertions, trusted invokers, event subscriptions). `main.py` is the only code that reads or writes them; no other module touches them, and none of these tables
references another module's. Design record: `sme/README.md` section 2.2 and the `HISTORY.md` entries cited on each class.

Before editing: the schema is the Alembic history in `smo/migrations/versions/`, not these classes. Change a column here and add a revision in the same change (expand
before contract, see `smo/CLAUDE.md`), then check it with `scripts/check_migration_matches_models.py` against Postgres. Postgres types (`ARRAY`, `JSON`, `Uuid`) carry a
SQLite variant so the unit tests run on SQLite (`ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite")`); SQLite returns datetimes naive, so elapsed-time
code must pass them through `smo_shared.timeutil.as_utc`.
"""

import datetime
import uuid

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smo_shared.db import Base
from smo_shared.mns.store import store_model


class ServiceProfile(Base):
    """One published service API (CAPIF ServiceAPIDescription, flattened): the row discovery returns and events describe.

    `service_name` is unique across all producers; `producer_id` is the publishing `apfId`. `service_id` is the `serviceId` of the API and the `apiId` of event filters.
    `aef_profiles` is kept as one JSON list (see the comment below). Deleting the row cascades to its `ServiceAuthzPolicy`.
    """
    __tablename__ = "service_profile"
    # UNIQUE on service_name ALONE, not (service_name, producer_id) — Foundational
    # Platform LLD section 2.3's actual decision is that a DIFFERENT producer
    # registering the same serviceName is the conflict; the PAIR being unique
    # would instead only catch the same producer double-registering, which is
    # supposed to be an idempotent update-in-place, not blocked at all. Caught
    # by sme/tests/test_main.py's conflict test — the original migration had
    # this backwards relative to the LLD's own stated rule.
    __table_args__ = (UniqueConstraint("service_name"),)

    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    full_api_versions: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    service_capabilities: Mapped[dict | None] = mapped_column(JSON)
    selection_criteria: Mapped[dict | None] = mapped_column(JSON)
    module_scope: Mapped[str] = mapped_column(String, nullable=False)
    # NEW section 5: the reference's own ServiceAPIDescription carries these
    # three — ServiceProfile was flattened, missing all of them. Stored as
    # JSON rather than normalized child tables (aefProfiles is registered
    # and read back wholesale, never independently CRUD'd — same adaptation
    # as DMEType.collection_spec/TrainingJob.required_data elsewhere in this
    # build). Only the fields discover_services' own new filters need are
    # kept (aefId, protocol, dataFormat, versions[].resources[].commType) —
    # not the full CAPIF AefProfile/Resource schema (aefLocation,
    # domainName, interfaceDescriptions, custOperations, etc.).
    aef_profiles: Mapped[list[dict] | None] = mapped_column(JSON)
    api_supp_feats: Mapped[str | None] = mapped_column(String)
    shareable_info: Mapped[dict | None] = mapped_column(JSON)

    authz_policy: Mapped["ServiceAuthzPolicy"] = relationship(back_populates="service", uselist=False, cascade="all, delete-orphan")


class ServiceAuthzPolicy(Base):
    """Who may discover a service: one row per `ServiceProfile` (primary key is the foreign key, deleted with the service).

    `allowed_consumers` None or empty means everyone; otherwise only the listed invoker ids see the service and are notified about it (`main._visible_to`). When
    `gates_discovery_visibility` is false the list is ignored. The flag defaults to true and no API changes it: every registration creates the row with the default.
    """
    __tablename__ = "service_authz_policy"

    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_profile.service_id", ondelete="CASCADE"), primary_key=True
    )
    allowed_consumers: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    gates_discovery_visibility: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    service: Mapped["ServiceProfile"] = relationship(back_populates="authz_policy")


class ProviderRegistration(Base):
    """HISTORY.md §5: the reference's own Provider (APF) enrolment
    (`providermanagement.go`'s `ProviderManager`, `POST`/`DELETE
    /registrations`) — the real registry `register_service`'s own
    `IsPublishingFunctionRegistered` gate needs, which this build never
    modeled at all before this pass. `apf_id` is this build's own
    flattened identity (`apfId == producerId == rAppId`, already
    established by `register_service`'s own docstring) — no separate
    provider-domain-id/function-id split, since nothing else in this
    build tracks that CAPIF three-tier (domain -> APF/AEF/AMF functions)
    hierarchy either.
    """
    __tablename__ = "provider_registration"

    apf_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_domain_info: Mapped[str | None] = mapped_column(String)


class InvokerRegistration(Base):
    """HISTORY.md §2: the reference's own API Invoker onboarding
    (`invokermanagement.go`'s `InvokerRegister`) — a real prerequisite for
    the Security/token API (`securityservice.go`'s
    `PostSecuritiesSecurityIdToken`, gated on
    `IsInvokerRegistered`/`VerifyInvokerSecret`), which this build never
    modeled at all.

    HISTORY.md §7 SME item 1: the real CAPIF core's
    `APIInvokerEnrolmentDetails`/`OnboardingInformation` onboarding is
    public-key-based — the client supplies `apiInvokerPublicKey`
    (`public_key` here); the server *generates* both `api_invoker_id`
    and the onboarding secret and hands them back
    (`invokermanagementapi/typeupdate.go`'s `createId`/
    `getOnboardingSecret`). This build previously took both as
    client-supplied input instead — a self-asserted identity and a
    client-chosen secret, the weaker trust direction the real spec
    documents `apiInvokerId` as "shall not be present" in the client's
    own request. Flipped to match. `public_key`, when it is a PEM public
    key, authenticates the invoker's token requests by an RFC 7523
    `private_key_jwt` client assertion (SA-SME-1-public-key); any other
    value is an opaque label and the invoker can only use its onboarding
    secret. `onboarding_secret_hash` is still a genuine, checked
    secret at token-issuance time, never stored in cleartext — only a
    salted `scrypt` hash (`salt:digest` hex), so a DB leak (backup, SQL
    injection elsewhere, a dump) can't hand out reusable client
    credentials directly.
    """
    __tablename__ = "invoker_registration"

    api_invoker_id: Mapped[str] = mapped_column(String, primary_key=True)
    public_key: Mapped[str] = mapped_column(String, nullable=False)
    onboarding_secret_hash: Mapped[str] = mapped_column(String, nullable=False)
    # PR-ST-4 housekeeping: when it was onboarded and when it last obtained a token, so the stale-invoker purge
    # can tell a live invoker from one a restarted process left behind.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.UTC))
    last_token_issued_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # PR-SEC-14: 'internal' for an invoker that presented the enrollment secret (an SMO module or the GUI), 'rapp' for every other. Introspection
    # reports it as the token's `role`. The column default is 'internal' because the release before this one inserts rows without it and its
    # invokers are SMO modules (a rolling upgrade); this code always sets it.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="internal", server_default="internal")
    # PR-SEC-10.3: the scope claim, {"regions": [...], "tenants": [...]} (either key optional); NULL: unscoped. Introspection returns it as `authz_scope`
    # and R1 Termination forwards it, so the modules that own the targets can decide (smo_shared/scope.py).
    authz_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class IssuedAccessToken(Base):
    """HISTORY.md §2: "No real OAuth2/token enforcement at R1
    Termination — only a comment and a tokenEndPoint URI in the bootstrap
    response; no actual validation code path." The reference's own
    AccessTokenRsp is a real signed JWT (`keycloak.GetToken`, an external
    IdP this build doesn't run — the same no-real-southbound-integration
    elision as everywhere else); this is the honest, opaque-token
    substitute: a real, server-tracked bearer token with a real expiry,
    validated by R1 Termination via POST /oauth2/introspect (RFC 7662) on
    every proxied request rather than by self-contained signature
    verification. Security review: the raw token is returned to the
    caller once and never stored — only its SHA-256 hash
    (`access_token_hash`), so a DB leak can't hand out live, reusable
    bearer tokens directly (unlike `onboarding_secret_hash`, a `scrypt`
    KDF isn't needed here: the token is already 256 bits of real
    randomness from `secrets.token_urlsafe`, not a low-entropy
    human-chosen secret).
    """
    __tablename__ = "issued_access_token"

    access_token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    api_invoker_id: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # OI-2-oauth2-scope: the scope granted at issuance, after it was checked
    # (main.py's _check_scope); returned by introspection. None = unscoped.
    scope: Mapped[str | None] = mapped_column(String)


class UsedClientAssertion(Base):
    """SA-SME-1-public-key: the `jti` of every RFC 7523 client assertion
    already exchanged for a token, kept until the assertion itself expires,
    so one signed assertion buys exactly one token (RFC 7523 section 3,
    replay protection)."""
    __tablename__ = "used_client_assertion"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    api_invoker_id: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrustedInvoker(Base):
    """HISTORY.md §7 SME item 2: the real CAPIF core's "Trusted Invokers"
    security-context subsystem (`capifcore/internal/securityservice/
    security.go`'s `PUT`/`GET`/`DELETE /trusted-invokers/{apiInvokerId}`
    plus revocation, `POST .../delete`) — a second, separate real
    mechanism beyond OAuth2 token issuance, managing per-AEF+API
    `SecurityInfo` (authentication/authorization info) that a real AEF
    (resource server) would consult directly, not something CAPIF core's
    own token-issuance path (`PostSecuritiesSecurityIdToken`) ever reads
    — confirmed by inspection: that function never touches
    `trustedInvokers` at all. Previously entirely absent from this build
    (large/structural, per the earlier triage).

    `security_info` is stored as one JSON list per invoker, matching
    this build's own established "flatten to what this build's own
    identity model needs" adaptation (already used for `aefProfiles`/
    `DMEType.collection_spec` elsewhere) rather than a normalized child
    table — the real CAPIF core itself keeps this as one in-memory
    `ServiceSecurity` struct per invoker too (`map[string]
    ServiceSecurity`), not normalized rows.
    """
    __tablename__ = "trusted_invoker"

    api_invoker_id: Mapped[str] = mapped_column(String, primary_key=True)
    notification_destination: Mapped[str] = mapped_column(String, nullable=False)
    request_test_notification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_info: Mapped[list] = mapped_column(JSON, nullable=False)


class ServiceEventSubscription(Base):
    """A CAPIF event subscription: `subscriber_id` wants `event_types` POSTed to `callback_uri`, narrowed by the optional filters.

    A filter column that is NULL or empty does not restrict (`main._filters_match`). `api_ids` holds serviceIds (as strings), `api_invoker_ids` invoker ids, `aef_ids` AEF ids.
    """
    __tablename__ = "service_event_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscriber_id: Mapped[str] = mapped_column(String, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    callback_uri: Mapped[str] = mapped_column(String, nullable=False)
    api_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))  # NEW section 5: CAPIFEventFilter.apiIds
    # OI-5-sme-filters: the rest of TS 29.222's CAPIFEventFilter
    api_invoker_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    aef_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))


# Events about a published service; their `eventDetail` carries `apiIds` and `aefIds`.
SERVICE_API_EVENTS = {"SERVICE_API_AVAILABLE", "SERVICE_API_UNAVAILABLE", "SERVICE_API_UPDATE"}
# OI-5-sme-filters: TS 29.222 CAPIFEvent's invoker-onboarding events
API_INVOKER_EVENTS = {"API_INVOKER_ONBOARDED", "API_INVOKER_OFFBOARDED", "API_INVOKER_UPDATED"}
# Every event type a subscription may ask for: `subscribe_events` refuses anything outside this set.
EVENT_TYPES = SERVICE_API_EVENTS | API_INVOKER_EVENTS


# The MnS registry's store (plan task S1.12): the ProvMnS store of `smo_shared.mns` under the name `mns_registry` (app/mns_registry.py maps it on the
# router's own base). A copy of it is put on the shared `Base`, as Echo does for its `moi_store`, so that scripts/check_migration_matches_models.py
# checks it against the migrated schema and a unit test's `Base.metadata.create_all` makes it.
MNS_REGISTRY = store_model("mns_registry").__table__.to_metadata(Base.metadata)
