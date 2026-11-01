# SME (`sme/`)

> Service Management and Exposure: the CAPIF-style core where services are published and discovered, API invokers are onboarded, and the bearer tokens that R1 Termination enforces are issued and introspected.

| | |
|---|---|
| Standards basis | O-RAN R1 SME on 3GPP CAPIF (TS 23.222 / TS 29.222) |
| R1 route / port | `/sme` via R1 Termination (container `:8000`) |
| Depends on (over R1) | Caller-registered event callback URLs only; no other module |
| Called by | R1 Termination (`/oauth2/introspect` on every proxied request); every module's `R1Client` (invoker onboarding and `/oauth2/token`); rApps and producers (publish, discover, subscribe); rApp Management (registers a package's declared providers and service APIs per instance); RAN Analytics (producer registration creates a service); GUI BFF |
| Database tables | `service_profile`, `service_authz_policy`, `provider_registration`, `invoker_registration`, `issued_access_token`, `used_client_assertion`, `trusted_invoker`, `service_event_subscription` |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/test-counts/sme.md](../docs/test-counts/sme.md)) |
| Status | Done for the subset in 1.2 |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

SME gives the platform three things:

1. **Service exposure.** A registered publisher (APF) publishes a service API; a consumer discovers it. One authorization gate decides discoverability: a consumer who is not allowed never learns the service exists.
2. **Identity and tokens.** An API invoker (an rApp container, or an SMO module acting as one) is onboarded, receives a server-generated id and secret, and trades them, or a JWT signed with its onboarded key, for a bearer token whose scope names only APIs it may use. R1 Termination asks SME whether that token is active on every call. **Roles (PR-SEC-14):** an invoker that presents the enrollment secret at registration (`X-SMO-Enrollment`, the compose secret every SMO module mounts) is recorded as `internal`, any other as `rapp`; a wrong secret is refused (403 `ENROLLMENT_REFUSED`), and with no secret configured registration is 503 `ENROLLMENT_NOT_CONFIGURED` unless `SME_ALLOW_OPEN_ENROLLMENT` is set (development: everyone is internal). The `smo-internal` and `smo-gui` scopes are granted to `internal` invokers only; an rApp asks for `smo-rapp`. Introspection answers with the invoker's `role`, which R1 Termination applies its role policy on. `SMO_ROLE_ENFORCEMENT` (`enforce`, default; `audit` grants and logs instead).
3. **Change events.** Subscribers are notified when a service becomes available, changes or goes away, and when an invoker is onboarded, updated or offboarded.

It also keeps a per-invoker security context (trusted invokers) as CAPIF's security API defines it.

### 1.2 Standards basis

SME is a profile of 3GPP CAPIF (TS 23.222 architecture, TS 29.222 APIs), as R1AP clause 6 requires. Specs in `../../specs/5G_APIs/`:

| CAPIF API | Spec file | Realised as |
|---|---|---|
| Publish Service API | [`TS29222_CAPIF_Publish_Service_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_Publish_Service_API.yaml) | `POST/DELETE/GET /published-apis/v1/{apfId}/service-apis` |
| Discover Service API | [`TS29222_CAPIF_Discover_Service_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_Discover_Service_API.yaml) | `GET /service-apis/v1/allServiceAPIs` with `api_name`, `api_version`, `aef_id`, `protocol`, `data_format`, `comm_type` filters (plus the required `api_invoker_id`) |
| API Provider Management | [`TS29222_CAPIF_API_Provider_Management_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_API_Provider_Management_API.yaml) | `/provider-registrations` (APF enrolment, flattened) |
| API Invoker Management | [`TS29222_CAPIF_API_Invoker_Management_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_API_Invoker_Management_API.yaml) | `/invoker-registrations` (public-key onboarding, server-generated id and secret; `PUT` key update, `DELETE` offboarding) |
| Security API | [`TS29222_CAPIF_Security_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_Security_API.yaml) | `/trusted-invokers/{id}` (`PUT`, `GET`, `DELETE`, `POST .../update`, `POST .../delete` revocation); token issuance at `/oauth2/token`, with the `3gpp#aefId:apiName` scope and RFC 7523 client assertions |
| Events API | [`TS29222_CAPIF_Events_API.yaml`](../../specs/5G_APIs/TS29222_CAPIF_Events_API.yaml) | `/capif-events/v1/{subscriberId}/subscriptions`; events `SERVICE_API_AVAILABLE`, `SERVICE_API_UNAVAILABLE`, `SERVICE_API_UPDATE`, `API_INVOKER_ONBOARDED`, `API_INVOKER_UPDATED`, `API_INVOKER_OFFBOARDED`; `CAPIFEventFilter` on `apiIds`, `apiInvokerIds` and `aefIds` |

Deliberate adaptations and omissions:

- Identity is flattened: `apfId == producerId == rAppId`, and an invoker id is the rAppId or an SMO module's own invoker id. There is no provider-domain / function-id hierarchy, so the domain-level collision detection of CAPIF core does not apply. Enrolment is an idempotent upsert on `apfId`.
- Tokens are opaque and server-tracked, not signed JWTs (no IdP is run). Validity is asked through RFC 7662 `POST /oauth2/introspect`, which R1 Termination calls. The token request/response shape follows CAPIF's security token operation (`client_id`, `client_secret`, `grant_type=client_credentials`, `scope`; `access_token`, `expires_in`, `token_type`, `scope`), with RFC 6749 error bodies.
- `ServiceProfile` keeps only the fields discovery filters need from `AefProfile` (`aefId`, `protocol`, `dataFormat`, `versions[].resources[].commType`), stored as JSON; the rest of `aefProfiles` passes through untouched.
- Not implemented: Access Control Policy, Auditing, Logging, Routing Info and Open Discover APIs; the `apiCat` discovery filter (no published service carries a category).

### 1.3 Position in the platform

```
 rApp / module R1Client            R1 Termination
   | POST /invoker-registrations      | every proxied call:
   | POST /oauth2/token  (direct)     |   POST /oauth2/introspect {token}
   v                                  v
 +--------------------------------------+
 |                 SME                  |  publish / discover / events / trusted invokers
 +--------------------------------------+
        | event callbacks (best effort)
        v
   subscribers (callbackUri)
```

SME calls nothing but subscriber callbacks. It reads no other module's data, and no module reads SME's tables. The token and invoker endpoints are the one place a caller without a token must reach a service: `/bootstrap` on R1 Termination advertises SME's own address as the token endpoint, so clients call it directly (through the gateway those paths need a token like any other).

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| Service registry (`ServiceProfile`) and the discovery authorization gate | Whether a token is presented and enforced on a call → R1 Termination |
| Provider (APF) enrolment, invoker onboarding, secrets, tokens, introspection | Which instance an rApp is, and its `oauth_client_id` → rApp Management |
| Trusted-invoker security contexts | Declaring which services a package publishes → Onboarding (reads the CSAR); registering them per instance → rApp Management |
| Service change events and subscriptions | Per-role rules for GUI users → GUI BFF |
| Its rows of the shared `idempotency_key` table (module `sme`) | The table and the decorator → `smo_shared.idempotency` (PR-ST-3) |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| One authorization gate for discovery and notification. A service with an authz policy and a non-empty `allowedConsumers` is returned to (and notified to) only those listed; an empty list means open to all. | CAPIF leaves discover-versus-invoke authorization open; one gate means an unauthorized consumer is never told the service exists. |
| `serviceName` is globally unique. The same producer re-registering updates in place (`SERVICE_API_UPDATE`); a different producer gets `SERVICE_NAME_CONFLICT`. | A conflict is two producers claiming one name, not one producer repeating itself. Unique on `service_name` alone. |
| Registering a service needs prior provider enrolment (`APF_NOT_REGISTERED`, 403). | CAPIF's `IsPublishingFunctionRegistered` gate. |
| The server mints `apiInvokerId` and the onboarding secret; the client supplies only a public key. Every onboarding creates a new invoker. | CAPIF trust direction: a client does not choose its own identity or secret. |
| Onboarding secrets are stored as salted scrypt hashes (`salt:digest`, n=2^14, r=8, p=1); tokens as SHA-256 hashes. Raw values are returned once and never stored. A DB leak yields no reusable credentials. Token hashing uses a fast hash because the token is 256 bits of randomness. | Security review. |
| Token endpoint rejects with 400 (`unsupported_grant_type`, `invalid_request`, `invalid_client`, `unauthorized_client`, `invalid_scope`) and bodies `{"error", "error_description"}`, not ProblemDetails. A refused client assertion's `error_description` is a fixed message per failure kind (bad signature, expired, wrong audience, missing claim, disallowed alg, malformed), never the JWT library's own text. | RFC 6749 / CAPIF shape; no library internals leak to the caller. |
| A token's `scope` is checked before it is issued. Absent, `smo-internal` or `smo-gui`: granted as-is. Otherwise TS 29.222's `3gpp#aefId:apiName[,apiName][;aefId:...]`, where each API must be published, exposed by that AEF (one of its `aefProfiles`) and discoverable by the invoker; anything else is `invalid_scope`. The granted scope is stored on the token and returned by introspection. | CAPIF core's `IsFunctionRegistered` / `IsAPIPublished` checks, plus the discovery gate, so a token never names an API the invoker could not discover. The two internal scopes are what SMO's own clients request. |
| An invoker onboarded with a PEM public key can authenticate a token request with an RFC 7523 client assertion instead of its secret: a JWT signed with that key (RS/PS/ES/EdDSA), `iss` and `sub` = the invoker, `aud` = the token endpoint, expiring within 300 s, with a `jti` used only once. Any other `apiInvokerPublicKey` is an opaque label and the invoker uses its secret. | Makes the onboarded key a real credential; replay protection is RFC 7523's. SMO's own clients onboard with labels and keep using secrets. |
| Offboarding an invoker deletes its live tokens and trusted-invoker context. A key update takes effect at once. | Nothing an invoker was granted outlives it. |
| `GET /trusted-invokers/{id}` redacts `authenticationInfo` and `authorizationInfo` to `""` unless asked per query flag. Invoker listings never expose a secret or its hash. | As CAPIF core. |
| `selSecurityMethod` of a trusted-invoker entry is the caller's first `prefSecurityMethods`; it is not matched against AEF-side capabilities. | No AEF-side security-method catalogue exists here to match against. |
| Revocation (`POST .../delete`) removes only entries matching the notified `aefId` or `apiIds`, and drops the record once none remain. | Implements CAPIF's stated filter semantics directly. |
| Event delivery goes through the transactional outbox (`smo_shared.outbox`, `PR-MSG-1.6`): a row in the same transaction as the change, sent right after it commits, at least once; a failed first attempt waits for the outbox sweep (MSG-2). | An unreachable subscriber must never fail a publish, and a crash between the commit and the send must not lose the event. |
| Idempotent deletes: deregistering an unknown or someone else's service, provider, trusted invoker, or subscription is a silent 204 (an unmatched owner is a no-op, nothing is notified). | Same shape as CAPIF core. |

Failure behaviour: SME has no outbound dependency whose failure changes a response. If SME is down, R1 Termination fails closed: every proxied call returns 503 `AUTH_SERVICE_UNAVAILABLE` with `Retry-After: 5`, except one whose token answer the gateway still holds in its introspection cache (`R1_INTROSPECTION_CACHE_SECONDS`).

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | All routes; secret and token hashing; discovery filter and gate; event notification; trusted-invoker validation. |
| `app/models.py` | The tables and the event types (`SERVICE_API_EVENTS`, `API_INVOKER_EVENTS`). |
| `../shared/smo_shared/` | `outbox`, `pagination`, `errors`, `timeutil`, `openapi_security` (exempts `/oauth2/token` and `/oauth2/introspect` from the declared bearer scheme), `r1_client` (the client side of onboarding and token fetch). |

### 2.2 Data model

**`service_profile`**: one row per published service.

| Column | Notes |
|---|---|
| `service_id` (PK, UUID) | Returned as `serviceId`; also the `apiId` used by event filters |
| `service_name` | UNIQUE |
| `producer_id` | The publishing `apfId` |
| `endpoint`, `version`, `full_api_versions`, `service_capabilities`, `selection_criteria` | |
| `module_scope` | Required on registration; stored, not used in any query |
| `aef_profiles` (JSON list), `api_supp_feats`, `shareable_info` | CAPIF `ServiceAPIDescription` parts |

**`service_authz_policy`**: PK and FK `service_id` (cascade); `allowed_consumers` (array; JSON on SQLite); `gates_discovery_visibility` (default true). Created with every registration; the flag has no API to change it.

**`provider_registration`**: `apf_id` (PK), `provider_domain_info`.

**`invoker_registration`**: `api_invoker_id` (PK, `api-invoker-<uuid>`), `public_key` (a PEM key verifies client assertions; anything else is a label), `onboarding_secret_hash`, `kind` (`internal` or `rapp`, PR-SEC-14), `authz_scope` (JSON, null: unscoped; PR-SEC-10.3, revision `0032`: the scope claim `{"regions": [...], "tenants": [...]}`, either key optional, which the introspection returns and the modules that own a target enforce; `smo_shared/scope.py`, `docs/adr/0005-tenant-region-authorization.md`).

**`issued_access_token`**: `access_token_hash` (PK), `api_invoker_id`, `expires_at`, `scope` (the granted scope, null if unscoped). Expired rows are not purged; an invoker's rows are deleted when it is offboarded.

**`used_client_assertion`**: `jti` (PK), `api_invoker_id`, `expires_at`. One row per assertion exchanged; expired rows are purged on the next assertion.

**`trusted_invoker`**: `api_invoker_id` (PK), `notification_destination`, `request_test_notification`, `security_info` (JSON list of `{aefId, apiId, authenticationInfo, authorizationInfo, prefSecurityMethods, selSecurityMethod}`).

**`service_event_subscription`**: `subscription_id` (PK), `subscriber_id`, `event_types` (array), `callback_uri`, `api_ids`, `api_invoker_ids`, `aef_ids` (arrays, null = no filter).

No cross-module references.

### 2.3 State machines

None: stateless as to lifecycle. The only time-dependent state is token validity: a token is active until `expires_at` (issuance plus `ACCESS_TOKEN_TTL_SECONDS` = 3600), or until its invoker is offboarded.

### 2.4 API

**Provider enrolment**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/provider-registrations` (201) | `{apfId, providerDomainInfo?}`; idempotent upsert; returns `{apfId}`. An rApp registers only its own invoker id (403 `PROVIDER_IDENTITY_MISMATCH`, call flow 18a) | |
| GET | `/provider-registrations` | Paged; each item has `serviceCount` | |
| DELETE | `/provider-registrations/{apf_id}` (204) | Idempotent. The provider's services go with it, each announced `SERVICE_API_UNAVAILABLE` (call flow 18a); an rApp deletes only its own (403 `PROVIDER_IDENTITY_MISMATCH`) | |

**Invoker onboarding and tokens**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/invoker-registrations` (201) | `{apiInvokerPublicKey, authzScope?}` → `{apiInvokerId, onboardingSecret, role, keyAuthentication, authzScope}`; a **scoped caller** (an rApp whose claim the gateway forwards) gets an invoker that carries its own claim, or a narrower one it asks for, never a wider one; always creates a new invoker (it cannot be idempotent: only a hash of the secret is kept, so a repeat could not return it; modules share one identity through `smo_shared/module_identity.py` instead, where the secret SME issued is encrypted at rest when `SMO_IDENTITY_KEY` is set, `SEC-15.18`); emits `API_INVOKER_ONBOARDED` | 422 `SECURITY_CONTEXT_INVALID` (a malformed PEM key), 422 `AUTHZ_SCOPE_INVALID` (fixed message), 403 `SCOPE_DENIED` (a claim wider than the caller's own, or a caller whose claim permits nothing) |
| PUT | `/invoker-registrations/{id}/authz-scope` | PR-SEC-10.3: `{authzScope}` replaces the claim (`null` or `{}` removes it) → `{apiInvokerId, authzScope}`; emits `API_INVOKER_UPDATED`. Read live by the introspection, so tokens already issued are limited from the next request. Internal-only at the gateway (`INTERNAL_ONLY`; an admin's call in the GUI backend) | 400 `INVOKER_NOT_REGISTERED`; 422 `AUTHZ_SCOPE_INVALID` |
| PUT | `/invoker-registrations/{id}` | Replace the public key (rotation) → `{apiInvokerId, keyAuthentication}`; emits `API_INVOKER_UPDATED` | 400 `INVOKER_NOT_REGISTERED`; 422 |
| POST | `/invoker-registrations/{id}/secret` | Call flow 01e: a new onboarding secret for the same invoker id → `{apiInvokerId, onboardingSecret}` (shown once, stored as its hash); the old secret is refused at once and the invoker's issued tokens are revoked; emits `API_INVOKER_UPDATED`. rApp Management's, to re-send an instance's credentials (call flow 01e). Internal-only at the gateway (`INTERNAL_ONLY`: an rApp could otherwise take an identity over) | 400 `INVOKER_NOT_REGISTERED` |
| DELETE | `/invoker-registrations/{id}` (204) | Offboard: the invoker, its tokens and its trusted-invoker context; idempotent; emits `API_INVOKER_OFFBOARDED` Also removes the invoker's CAPIF event subscriptions and the provider registered under its id with that provider's services (call flow 18b). | |
| POST | `/invoker-registrations/purge-stale` | PR-ST-4 housekeeping (this build's own): query `unused_for_days` (> 0) and `dry_run` (default true). Offboards, like DELETE, every invoker that got no token for that long (or never did and was onboarded that long ago) → `{unusedForDays, dryRun, count, invokerIds}`. Invokers carry `created_at` and `last_token_issued_at`. A module whose identity is purged is onboarded afresh by `R1Client` | 422 `unused_for_days` missing or ≤ 0 |
| GET | `/invoker-registrations` | Paged: `apiInvokerId`, `apiInvokerPublicKey`, `keyAuthentication`, `authzScope`, `trusted` | |
| POST | `/oauth2/token` | `{grant_type: client_credentials, client_id, client_secret? \| client_assertion_type + client_assertion?, scope?}` → `{access_token, expires_in, token_type: Bearer, scope}` | 400 `unsupported_grant_type`, `invalid_request` (secret and assertion together), `invalid_client`, `unauthorized_client`, `invalid_scope` |
| POST | `/oauth2/introspect` | `{token}` → `{active: false}` or `{active: true, client_id, exp, role, scope?, authz_scope?}` (`authz_scope`: PR-SEC-10.3, absent for an unscoped invoker); unauthenticated Not open to an rApp through R1: only R1 Termination introspects, directly (call flow 18d). | |

**Trusted invokers (CAPIF security contexts)**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| PUT | `/trusted-invokers/{id}` (201) | Create or replace `{notificationDestination, requestTestNotification, securityInfo[]}` | 400 `INVOKER_NOT_REGISTERED`; 422 `SECURITY_CONTEXT_INVALID` (blank destination, empty `securityInfo`, an entry without `prefSecurityMethods`) |
| GET | `/trusted-invokers/{id}?authentication_info=&authorization_info=` | Secrets redacted unless the flag is set | 404 `TRUSTED_INVOKER_NOT_FOUND` |
| GET | `/trusted-invokers` | Paged Internal-only at R1, as is `GET /trusted-invokers/{id}`: an rApp reads no trust context (call flow 18c). | |
| POST | `/trusted-invokers/{id}/update` | Update in place; does not re-check invoker onboarding | 404; 422 |
| POST | `/trusted-invokers/{id}/delete` (204) | Revocation by `{aefId?, apiIds[], apiInvokerId, cause}` (`cause`: `OVERLIMIT_USAGE` or `UNEXPECTED_REASON`) | 404; 422 (empty `apiIds`) |
| DELETE | `/trusted-invokers/{id}` (204) | Idempotent | |

**Publish and discover**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/published-apis/v1/{apf_id}/service-apis` (201) | Register or update; body: `serviceName, producerId, endpoint, version, moduleScope, fullApiVersions, serviceCapabilities, selectionCriteria, allowedConsumers, aefProfiles, apiSuppFeats, shareableInfo`. The path `apf_id` is the producer; the body `producerId` is not used for ownership. Returns `{serviceId}`. An rApp publishes only under its own invoker id (403 `PROVIDER_IDENTITY_MISMATCH`, call flow 18a). | 403 `APF_NOT_REGISTERED`; 409 `SERVICE_NAME_CONFLICT` |
| GET | `/published-apis/v1/{apf_id}/service-apis` | The publisher's own services (bare list). A publisher with services always gets them; one with none gets `[]` if enrolled | 404 `PUBLISHING_FUNCTION_NOT_FOUND` (not enrolled and no services) |
| DELETE | `/published-apis/v1/{apf_id}/service-apis/{service_id}` (204) | Owner only; a different `apf_id` is a silent no-op. Notifies `SERVICE_API_UNAVAILABLE`. An rApp names only its own apfId (403 `PROVIDER_IDENTITY_MISMATCH`). | |
| GET | `/service-apis/v1/allServiceAPIs?api_invoker_id=&api_name=&api_version=&aef_id=&protocol=&data_format=&comm_type=` | Discovery (bare list). `api_invoker_id` is required and taken as given. A service is returned if it has no policy or `allowedConsumers` is empty or contains the invoker, and (when any AEF filter is given) at least one `aefProfile` matches all given AEF filters. For an rApp `api_invoker_id` must be its own invoker id (403 `INVOKER_IDENTITY_MISMATCH`, call flow 18e); the console and modules may ask for any. | |

**Events**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/capif-events/v1/{subscriber_id}/subscriptions` (201) | `{subscriberId, eventTypes[], callbackUri, apiIds?, apiInvokerIds?, aefIds?}` → `{subscriptionId}`. Each filter that is set must share a value with the event; an event with no value of that kind (an invoker event for an `apiIds` filter) does not match. An rApp subscribes only as itself (403 `INVOKER_IDENTITY_MISMATCH`); a `callbackUri` the SSRF guard refuses is 422 `SCHEMA_VALIDATION_FAILED` (call flow 18e). | 422 `SUBSCRIPTION_SCOPE_CONFLICT` (an event type outside `EVENT_TYPES`) |
| GET | `/capif-events/v1/{subscriber_id}/subscriptions` | Paged; a build addition (CAPIF defines only `POST`) An rApp lists only its own (403 `INVOKER_IDENTITY_MISMATCH`). | |
| DELETE | `/capif-events/v1/{subscriber_id}/subscriptions/{subscription_id}` (204) | Only if the subscriber matches; otherwise a silent no-op An rApp names only itself (403 `INVOKER_IDENTITY_MISMATCH`). | |

**Other**: `GET /health`.

**Repeats (SDK-RETRY-IDEMPOTENCY).** `POST /capif-events/v1/{subscriber_id}/subscriptions` is `@idempotent("sme", status_code=201)`
(`smo_shared.idempotency`, PR-ST-3): a repeat with the same `Idempotency-Key` (every SDK POST carries one, and repeats it after a transport error, 502 or
504) gets the first `subscriptionId` with `Idempotent-Replayed: true`, and no second subscription that would deliver every event twice. The same key with
another body, or under another subscriber id, is 422 `IDEMPOTENCY_KEY_REUSED`. The role `sme` may write the shared `idempotency_key` table
(`migrations/db_roles.json`). `POST /provider-registrations` and `/published-apis/v1/{apfId}/service-apis` are upserts and not decorated, and neither are
the token-flow routes (`/invoker-registrations` mints an invoker per call and its answer holds the onboarding secret, which must not be stored).

### 2.5 Interactions

| Direction | Call | When | Failure behaviour |
|---|---|---|---|
| in | `POST /oauth2/introspect` from R1 Termination | every proxied request | n/a |
| out, webhook | `POST {callbackUri}` `{serviceId, subscriptionId, eventType, eventDetail: {apiIds, aefIds}}` (2 s) | register (`SERVICE_API_AVAILABLE`), re-register (`SERVICE_API_UPDATE`), deregister (`SERVICE_API_UNAVAILABLE`, sent before the row is deleted) | At least once, through the outbox: committed with the change, sent after it, a crash leaves a pending row that the outbox sweep sends. A subscriber is skipped if its `eventTypes` lacks the event, a filter it sets does not match, or the service's policy lists consumers and the subscriber is not one of them. |
| out, webhook | `POST {callbackUri}` `{apiInvokerId, subscriptionId, eventType, eventDetail: {apiInvokerIds}}` (2 s) | invoker onboarded, key updated, offboarded | Same; no visibility gate. |

Matching rule for the last case: the subscriber id is compared with `allowedConsumers` exactly as the discovery gate compares `api_invoker_id`.

### 2.6 Configuration

`SME_TOKEN_AUDIENCE`: the `aud` a client assertion must carry (default `{SME_URL}/oauth2/token`, with `SME_URL` default `http://sme:8000`: the token endpoint R1 Termination advertises). Through `smo_shared`: `SMO_DATABASE_URL` (required, no default). Constants in code: `ACCESS_TOKEN_TTL_SECONDS = 3600`; `INTERNAL_SCOPES = {smo-internal, smo-gui}`; `MAX_ASSERTION_LIFETIME_SECONDS = 300`; scrypt parameters `n=2**14, r=8, p=1, dklen=32`.

### 2.7 Error codes

| Code | Status | When |
|---|---|---|
| `APF_NOT_REGISTERED` | 403 | Publishing without provider enrolment |
| `SERVICE_NAME_CONFLICT` | 409 | `serviceName` held by a different producer |
| `PUBLISHING_FUNCTION_NOT_FOUND` | 404 | Own-services query for an unenrolled `apf_id` with no services |
| `SUBSCRIPTION_SCOPE_CONFLICT` | 422 | Unknown event type (the name is shared with A1; here it means an invalid `eventTypes`) |
| `AUTHZ_SCOPE_INVALID` | 422 | A scope claim that is not an object of `regions` and/or `tenants`, each a list of 1 to 100 distinct values (PR-SEC-10.3) |
| `SCOPE_DENIED` | 403 | A scoped caller registering an invoker with a wider claim than its own |
| `INVOKER_NOT_REGISTERED` | 400 | `PUT /trusted-invokers` or `PUT /invoker-registrations/{id}` for an invoker never onboarded |
| `TRUSTED_INVOKER_NOT_FOUND` | 404 | Read, update or revoke without a context |
| `SECURITY_CONTEXT_INVALID` | 422 | Malformed `ServiceSecurity` or `SecurityNotification`; a malformed PEM `apiInvokerPublicKey` |
| `unsupported_grant_type`, `invalid_request`, `invalid_client`, `unauthorized_client`, `invalid_scope` | 400 | `/oauth2/token` (body `{"error", "error_description"}`, not ProblemDetails) |
| `IDEMPOTENCY_KEY_REUSED` / `IDEMPOTENCY_KEY_IN_PROGRESS` / `IDEMPOTENCY_KEY_INVALID` | 422 / 409 / 422 | `Idempotency-Key` on a decorated route: used for another path or body, the first request still running, or not 1 to 255 printable characters (`smo_shared.idempotency`) |
| FastAPI request validation | 422 | Missing or mistyped fields |

### 2.8 Limits and open items

- Tokens are opaque, with no IdP. SME checks a scope when it issues the token, and introspection reports it, but no resource server enforces it per call: R1 Termination checks only that the token is active.
- No `category` discovery filter: no published service carries a category.
- Discovery does not verify that `api_invoker_id` is an onboarded invoker or the caller's own identity.
- Expired tokens are not purged; a single token cannot be revoked (offboarding the invoker revokes all of its tokens). R1 Termination can cache introspection answers (`R1_INTROSPECTION_CACHE_SECONDS`, off by default): an offboarded invoker's token is then honoured at a gateway replica for at most that many seconds after the offboarding (not at all after it, at the replica the `DELETE` went through), `r1-termination/README.md`.
- The key-update and offboarding routes do not prove the caller is the invoker; like every other route they sit behind R1 Termination's token check, and the GUI allows them to admins only.
- `gates_discovery_visibility` is always true; `module_scope` is stored only.
- Event delivery is retried by the shared outbox sweep (`smo_shared/worker.py`), which the other modules' workers run over the one `notification_outbox` table: no answer or a 5xx is retried with backoff up to 5 attempts (`outbox.MAX_ATTEMPTS`), then the row is DEAD. SME runs no worker of its own.
- Out of scope: CAPIF Access Control Policy, Auditing, Logging, Routing Info, Open Discover.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/sme && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/test-counts/sme.md`](../docs/test-counts/sme.md).

| Test file | Covers |
|---|---|
| `tests/test_main.py` | Service registry and discovery: register, name conflict, same-producer update, authorization gate (hidden / shown / unrestricted), deregistration ownership, `api_name` / `api_version` / AEF / protocol / data-format / comm-type filters |
| `tests/test_identity.py` | Call flow 18: an rApp registers, publishes and deregisters only as its own provider; discovers only as itself; subscribes, lists and unsubscribes only as itself, and a refused callback is 422; offboarding removes the invoker's subscriptions and its provider with its services |
| | Event subscriptions and notifications: event-type validation, unsubscribe ownership, matching subscribers only, update / unavailable events, `apiIds` filter, visibility respected, unreachable subscriber, the `Idempotency-Key` replay of a subscription (one row, `Idempotent-Replayed`, 422 `IDEMPOTENCY_KEY_REUSED` for another body or subscriber id) |
| | Provider enrolment and the own-services query |
| | Invoker onboarding, token issue and introspection (server-generated id and secret, no cleartext secret or token, bad grant / client / secret, expired and unknown tokens) |
| | Trusted invokers: validation, replace, redaction and reveal, update, revocation (by `aefId`, whole record), 404s |
| | Registry reads (providers with service count, invokers without secrets, trusted invokers, event subscriptions) and health |
| `tests/test_scope.py` | PR-SEC-10.3: an invoker without a claim is unscoped and introspection says nothing of one; the claim at registration is returned sorted by introspection; invalid claims are 422 with a fixed message and register nothing; an operator sets, replaces and removes a claim and it applies to tokens already issued; a scoped caller (or a module acting for a scoped rApp) cannot mint a wider invoker |
| `tests/test_security.py` | Scope: unscoped and internal scopes, a `3gpp#` scope over published APIs (and introspection of it), unpublished API, wrong AEF, malformed entries, unknown scope, an API hidden from the invoker |
| | Client assertions: RSA and EC keys, one token per assertion, wrong key / audience / issuer, expired, too long-lived, no `jti`, label-only invoker, secret and assertion together, malformed PEM at onboarding, key rotation, offboarding revokes tokens |
| | Invoker events and filters: onboarded / updated / offboarded delivery, `apiInvokerIds` filter, `aefIds` filter, a filter of another kind never matches, filters listed, unknown event type |

### 3.3 What is not covered here

- R1 Termination actually introspecting a token issued by SME, and every module's `R1Client` onboarding and refreshing its token: `tests_integration/` (in-process mesh; the gateway mechanics themselves are tested in `r1-termination/tests`).
- rApp Management registering a package's declared providers and service APIs, and RAN Analytics creating a service: `tests_integration/test_cross_service.py` (e.g. `test_ran_analytics_producer_registration_creates_a_real_sme_service`, `test_real_demo_csar_onboards_and_deploys`).
- PostgreSQL array columns and cascades (unit tests use SQLite).

## 4. References

- Call flows: [18 SME CAPIF security lifecycle](../docs/call-flows/18-sme-trusted-invokers-lifecycle.md), [01 onboarding to deployment](../docs/call-flows/01-rapp-onboarding-to-deployment.md)
- OpenAPI: [`../docs/openapi/sme.json`](../docs/openapi/sme.json)
- Specs: [`../../specs/5G_APIs/`](../../specs/5G_APIs/) (`TS29222_CAPIF_*.yaml`)
- Open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); audit history: [`../HISTORY.md`](../HISTORY.md)
- Cross-cutting rules: [ARCHITECTURE.md](../docs/ARCHITECTURE.md)
- Related READMEs: [R1 Termination](../r1-termination/README.md) (the token gate), [rApp Management](../rapp-mgmt/README.md) (per-instance registration), [Onboarding](../onboarding/README.md) (CSAR declarations)
