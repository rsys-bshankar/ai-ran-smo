# Call Flow 18: SME security — providers and publishing, invokers, trust contexts, tokens and introspection, discovery and events

How SME, the CAPIF core of this SMO (TS 29.222; `HISTORY.md` OI-2-oauth2, OI-5-sme-provider-enrolment, SA-SME-1, SA-SME-2), keeps who is who: a provider
publishes services, an invoker is onboarded and given tokens, a trust context records how an invoker is secured, a token is introspected by R1 Termination on
every call, and an invoker discovers services and subscribes to their events. Call flow 01 abbreviates this as "obtain an OAuth2 token". Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 18a | Providers and publishing | an rApp (SDK), RAN Analytics or rApp Management for an rApp, Admin (GUI) | a provider and its services, each change announced, or refused when an rApp names another |
| 18b | Invokers: onboarding, key, secret, offboarding | every module's `R1Client`, an rApp, rApp Management, Admin (GUI) | an invoker with a server-minted id and secret, or one removed with everything it made |
| 18c | Trust contexts | Admin (GUI), an SMO module | an invoker's security methods recorded, read redacted, never by an rApp |
| 18d | Tokens and introspection | any invoker; R1 Termination on every call | a token for a checked scope, and its role, scope and claim at every call |
| 18e | Discovery and events | an rApp (SDK), a module, Operator (GUI) | the services an invoker may see, and their events, for itself only |

**As images:** [18a](images/18-sme-trusted-invokers-lifecycle/18a-providers-and-publishing.png) ·
[18b](images/18-sme-trusted-invokers-lifecycle/18b-invokers-onboarding-key-secret-offboarding.png) ·
[18c](images/18-sme-trusted-invokers-lifecycle/18c-trust-contexts.png) ·
[18d](images/18-sme-trusted-invokers-lifecycle/18d-tokens-and-introspection.png) ·
[18e](images/18-sme-trusted-invokers-lifecycle/18e-discovery-and-events.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach SME through R1 Termination, which stamps the caller's
role (`X-R1-Role`: `rapp` or `internal`) and invoker id, except the token endpoint (reached without a token) and R1's own introspection. An rApp acts at SME
only as itself, its invoker id; the console and the SMO modules act for others. Every event SME sends is an outbox row committed with the change.

## 18a — Providers and publishing

```mermaid
sequenceDiagram
    participant P as Provider: rApp (SDK) / RAN Analytics or rApp Management for an rApp / Admin (GUI)
    participant SME as SME
    participant DB as SME database
    participant S as CAPIF event subscribers

    P->>SME: POST /provider-registrations {apfId, providerDomainInfo?}
    alt an rApp naming an apfId that is not its own invoker id
        SME-->>P: 403 PROVIDER_IDENTITY_MISMATCH
    else
        SME->>DB: provider upserted
        SME-->>P: 201 {apfId}
    end
    P->>SME: POST /published-apis/v1/{apfId}/service-apis {serviceName, endpoint, version, aefProfiles, allowedConsumers?}
    alt an rApp under another apfId
        SME-->>P: 403 PROVIDER_IDENTITY_MISMATCH, nothing changed
    else apfId not a registered provider
        SME-->>P: 403 APF_NOT_REGISTERED
    else serviceName held by another provider
        SME-->>P: 409 SERVICE_NAME_CONFLICT
    else
        SME->>DB: service and its consumer policy, events SERVICE_API_AVAILABLE or _UPDATE, one commit
        SME-->>P: 201 {serviceId}
        SME->>S: POST callbackUri {eventType, eventDetail {apiIds, aefIds}} (subscribers allowed to see the service)
    end
    P->>SME: DELETE /published-apis/v1/{apfId}/service-apis/{id}, or DELETE /provider-registrations/{apfId}
    alt an rApp naming another apfId
        SME-->>P: 403 PROVIDER_IDENTITY_MISMATCH
    else
        SME->>DB: the service (or the provider and every service it published) deleted,<br/>each announced SERVICE_API_UNAVAILABLE, one commit
        SME-->>P: 204 (also for an unknown id)
    end
```

An rApp could register, publish under, replace and delete any provider's services, a service name included; a deleted provider's services stayed
discoverable. RAN Analytics (call flow 08) and rApp Management (call flow 01) register providers for an rApp as SMO modules, so they are not held to it.

## 18b — Invokers: onboarding, key, secret, offboarding

```mermaid
sequenceDiagram
    participant C as Module R1Client / rApp / rApp Management / Admin (GUI)
    participant SME as SME
    participant DB as SME database

    C->>SME: POST /invoker-registrations {apiInvokerPublicKey, authzScope?}, X-SMO-Enrollment?
    alt a PEM key that does not parse, or a malformed claim
        SME-->>C: 422 SECURITY_CONTEXT_INVALID / AUTHZ_SCOPE_INVALID
    else a scoped caller asking for more than its own claim
        SME-->>C: 403 SCOPE_DENIED
    else an enrollment secret that is wrong, or none configured
        SME-->>C: 403 ENROLLMENT_REFUSED / 503 ENROLLMENT_NOT_CONFIGURED
    else
        Note over SME: the id (api-invoker-uuid) and the secret are minted here, only the secret's hash kept,<br/>role internal with the enrollment secret, rapp without, the caller's claim inherited
        SME->>DB: invoker, event API_INVOKER_ONBOARDED, one commit
        SME-->>C: 201 {apiInvokerId, onboardingSecret, role, keyAuthentication, authzScope}
    end
    C->>SME: PUT /invoker-registrations/{id} (new key), POST .../secret (new secret), PUT .../authz-scope
    Note over SME: internal only at R1 (an rApp would take an identity over), and a new secret revokes the invoker's tokens
    C->>SME: DELETE /invoker-registrations/{id} (offboarding, Admin), or POST /invoker-registrations/purge-stale
    SME->>DB: its tokens, trust context, CAPIF subscriptions, and the provider under its id with its services<br/>(each SERVICE_API_UNAVAILABLE) deleted, event API_INVOKER_OFFBOARDED, one commit
    SME-->>C: 204 (also for an unknown id)
```

An offboarded invoker's subscriptions and published services used to outlive it. **Phase 1:** the number of invokers one rApp may onboard is not limited.

## 18c — Trust contexts

```mermaid
sequenceDiagram
    participant A as Admin (GUI) / SMO module
    participant SME as SME
    participant DB as SME database

    A->>SME: PUT /trusted-invokers/{apiInvokerId} {notificationDestination, securityInfo [{aefId?, apiId?, prefSecurityMethods}]}
    alt the invoker is not onboarded
        SME-->>A: 400 INVOKER_NOT_REGISTERED
    else
        Note over SME: selSecurityMethod is the first preferred method (no AEF publishes what it supports)
        SME->>DB: trust context
        SME-->>A: 201 TrustedInvoker
    end
    A->>SME: GET /trusted-invokers/{id}?authentication_info&authorization_info
    SME-->>A: securityInfo, authenticationInfo and authorizationInfo blank unless asked for
    A->>SME: POST /trusted-invokers/{id}/update, POST .../delete {aefId, apiIds}, DELETE /trusted-invokers/{id}
    SME-->>A: the updated context / 204
    Note over A,SME: an rApp is refused every trust-context route at R1, reads included: the list returns<br/>every invoker's authentication and authorization info unredacted
```

An rApp could read every trust context, including the list that returns each invoker's authentication and authorization info unredacted.

## 18d — Tokens and introspection

```mermaid
sequenceDiagram
    participant C as Invoker (module, rApp, GUI BFF)
    participant SME as SME
    participant R1 as R1 Termination
    participant M as The module called

    C->>SME: POST /oauth2/token {client_credentials, client_id, client_secret or client_assertion (RFC 7523), scope?}
    alt unknown client, or an assertion that fails (key, iss/sub, aud, exp within 300 s, jti reused)
        SME-->>C: 400 invalid_client
    else wrong secret
        SME-->>C: 400 unauthorized_client
    else an rApp asking smo-internal / smo-gui, or a 3gpp# scope naming an API not published,<br/>not exposed by that AEF, or not discoverable by the invoker
        SME-->>C: 400 invalid_scope
    else
        SME-->>C: 200 {access_token (opaque, its hash kept), expires_in 3600, token_type Bearer, scope}
    end
    C->>R1: any call, Authorization Bearer token
    R1->>SME: POST /oauth2/introspect {token} (direct, not through the gateway)
    SME-->>R1: {active, client_id, exp, role, scope?, authz_scope?} read at call time
    alt not active
        R1-->>C: 401
    else
        R1->>M: the call with X-R1-Role, X-R1-Invoker-Id, the scope stamp
    end
```

An rApp could introspect any token it held through the gateway and learn its client, role and scope; only R1 needs the route, so it is no longer open to
an rApp at R1. **Phase 1:** tokens are opaque and server-tracked (no IdP signs JWTs), and introspection is unauthenticated on the internal network, as
`/bootstrap` is.

## 18e — Discovery and events

```mermaid
sequenceDiagram
    participant I as Invoker: rApp (SDK) / module / Operator (GUI)
    participant SME as SME
    participant DB as SME database
    participant CB as The subscriber's callback

    I->>SME: GET /service-apis/v1/allServiceAPIs?api_invoker_id&api_name&aef_id&protocol&data_format&comm_type
    alt an rApp naming another invoker
        SME-->>I: 403 INVOKER_IDENTITY_MISMATCH
    else
        SME-->>I: the services matching the filters that this invoker may see (allowedConsumers)
    end
    I->>SME: POST /capif-events/v1/{subscriberId}/subscriptions {eventTypes, callbackUri, apiIds?, apiInvokerIds?, aefIds?}
    alt an rApp subscribing as another invoker (also for list and delete)
        SME-->>I: 403 INVOKER_IDENTITY_MISMATCH
    else an unknown event type, or a callback the SSRF guard refuses
        SME-->>I: 422 SUBSCRIPTION_SCOPE_CONFLICT / SCHEMA_VALIDATION_FAILED
    else
        SME->>DB: subscription
        SME-->>I: 201 {subscriptionId}
        SME->>CB: POST {eventType, eventDetail} for each matching event, a service's events only if the subscriber may see it
    end
```

Both trusted the id the caller named, so an rApp could see a service restricted to another invoker, or its events, by naming that invoker.

## Decisions this flow depends on

- **An rApp acts at SME only as itself** (DOC-CF-1.8): the apfId it registers, publishes and deregisters under, the invoker it discovers for and the
  subscriber it subscribes as must be its invoker id (403 `PROVIDER_IDENTITY_MISMATCH` / `INVOKER_IDENTITY_MISMATCH`); the console and the SMO modules act for
  others, as RAN Analytics and rApp Management do for an rApp.
- **Nothing outlives what it belongs to** (DOC-CF-1.8): a deleted provider takes its services (announced `SERVICE_API_UNAVAILABLE`), an offboarded invoker
  its tokens, trust context, subscriptions and provider.
- **What only the platform needs, an rApp cannot reach** (DOC-CF-1.8): introspection (R1 calls SME directly) and the trust contexts; as before, an invoker's
  key, secret and claim and the trust-context changes.
- **The server mints identity** (TS 29.222): the invoker id and the secret come from SME; the enrollment secret alone makes an invoker `internal`.
- **A scope is checked before a token is issued**, and an rApp never gets an internal scope (PR-SEC-14).
- **Opaque tokens, introspected**: no IdP signs JWTs in this build.
- **Not taken:** a real AEF security-method catalog (`selSecurityMethod` stays the first preference), signed JWT tokens, authenticating introspection
  itself, and a limit on how many invokers one rApp onboards.
