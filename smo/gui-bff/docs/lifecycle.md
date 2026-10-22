# GUI backend (gui-bff): lifecycle

The GUI backend (BFF) is the only API the browser talks to: it keeps the console's users, roles and audit log in its own small database, issues the
session, checks every call against its permission table and forwards the allowed ones to R1 Termination with its own SME token. Its design is in
[`../README.md`](../README.md); this document follows one BFF process from start to stop. Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the container (uvicorn) | users seeded, the signing key agreed, the R1 gateway built; the SME identity made on the first call that needs it |
| b | Local sign-in, the one-time code and the session check | a person at the sign-in page, a script (`/api/token`) | the session cookies (or a Bearer token), a challenge, or a refusal |
| c | OIDC sign-in and sign-out | a person ("Sign in with ...") | the session cookies after the provider's callback; a revoked session at logout |
| d | A call through the proxy | any page of the console | the module's answer, or the BFF's own 403 / 502, a change audited |
| e | Counts, the event stream and preferences | the shell of an open tab | counts pushed when they change; per-user settings stored |
| f | Stopping and recovering | SIGTERM, a restart, an SME reset | export jobs ended `FAILED`, sessions and identity read back from the database |

**How to read the diagrams.** As in [call flow 01](../../docs/call-flows/01-rapp-onboarding-to-deployment.md): an arrow is one call with the inputs that
matter, a box on the receiver says what it does with them, an `alt` block is the outcome (success first, then each failure and what the caller gets).
*Browser* is the console's code (or a script); the console's nginx in front of the BFF forwards `/api/...` unchanged and is not drawn. Every audit row
is one row of `gui_audit_log`, written in its own commit.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant Up as uvicorn
    participant B as GUI BFF
    participant DB as BFF database
    participant R1 as R1 Termination
    participant SME as SME

    Up->>B: import app.main, create_app(Settings from the environment)
    Note over B: refuses to start (ValueError) for GUI_LOGIN_MODE=oidc without OIDC,<br/>local login off without OIDC, a GUI_TOTP_KEY under 32 characters,<br/>GUI_ADMIN_MFA_REQUIRED without a key, a bad GUI_ACCOUNT_* set,<br/>a bad signing key or OIDC setting, an unreadable *_FILE
    Up->>B: lifespan start
    B->>DB: create_all, missing columns and indexes added (retried while instances race)
    alt the user table is empty (first boot)
        B->>DB: admin (GUI_ADMIN_PASSWORD, else a generated one written to GUI_INITIAL_PASSWORD_FILE, mode 0600),<br/>operator and viewer when their passwords are set
    else users exist, or another instance seeded a moment earlier
        Note over B: nothing written, the loser of the race deletes its password file
    end
    opt GUI_JWT_SECRET not set
        B->>DB: shared setting jwt_secret (the first instance stores its key, every other adopts it)
    end
    Note over B: R1 gateway built: R1_URL, SME_URL, GUI_UPSTREAM_TIMEOUT_SECONDS,<br/>client certificate when SMO_MTLS=on<br/>serving now: nothing has been asked of R1 or SME yet
    B->>R1: first call that needs a token: GET /bootstrap (skipped when SME_URL is set)
    R1-->>B: tokenEndPoint
    opt no SME identity stored
        B->>SME: POST /invoker-registrations {apiInvokerPublicKey smo-gui-bff:random}, X-SMO-Enrollment
        SME-->>B: 201 {apiInvokerId, onboardingSecret}
        B->>DB: the identity stored (another instance stored first: ours offboarded, theirs adopted)
    end
    B->>SME: POST /oauth2/token {client_credentials, client_id, client_secret, scope smo-gui}
    alt 200
        SME-->>B: access_token, kept until expires_in less 30 s
    else 400 (SME no longer knows the invoker)
        Note over B: registers again once, replaces the stored identity, asks again
    else any other answer, or SME unreachable
        Note over B: SmoAuthError: the call that needed it answers 502 SMO_AUTH_FAILED
    end
```

The BFF has no `/live` or `/ready` of its own. Compose gives it no healthcheck, and the Helm chart runs it as `kind: static` with a TCP probe on its
port: it is "ready" as soon as uvicorn listens. Everything that touches the SMO is lazy, so a BFF whose R1 or SME is down still starts, signs people in
and writes its audit log; only the pages that read the SMO fail (502). The SME identity is one row in the BFF database, shared by every instance on that
database (PR-ST-5); the token is cached per process, and refreshes are serialised by a lock.

**Phase 1:** the default `GUI_DATABASE_URL` is a SQLite file on one volume, so the chart runs one replica. Several instances need one shared database
(and `GUI_ADMIN_PASSWORD` set, so seeding does not race on generated passwords).

## b — Local sign-in, the one-time code and the session check

```mermaid
sequenceDiagram
    participant Br as Browser / script
    participant B as GUI BFF
    participant DB as BFF database

    Br->>B: POST /api/login {username, password}
    alt GUI_LOCAL_LOGIN_ENABLED=false
        B-->>Br: 403 LOCAL_LOGIN_DISABLED
    else the name locked for this source, the source throttled, or the name cooling down
        B-->>Br: 429 TOO_MANY_ATTEMPTS (audit LOGIN_LOCKED)
    else unknown, inactive or wrong password (an unknown name is checked against a dummy hash)
        B->>DB: failure counted per (source, name), per source and per name
        B-->>Br: 401 INVALID_CREDENTIALS (audit LOGIN_FAILED)
    else GUI_LOGIN_MODE=oidc and not break-glass, or break-glass with no code enrolled
        B-->>Br: 403 LOGIN_MODE_OIDC_ONLY / BREAK_GLASS_NEEDS_TOTP (audit LOGIN_REFUSED)
    else a one-time code is enrolled
        B->>DB: challenge row, 5 minutes
        B-->>Br: 200 {mfaRequired, challenge, expiresIn 300} (503 TOTP_KEY_UNAVAILABLE without a key)
        Br->>B: POST /api/login/totp {challenge, code}
        Note over B: challenge signature and purpose, the lockout, the user still active<br/>with the same token version, the challenge unspent, then the code<br/>(a TOTP step used once, or a recovery code), the challenge spent last
        B-->>Br: 200 as below, or 401 CHALLENGE_INVALID / INVALID_CODE
    else
        Note over B: the pair counter cleared, a JWT {sub, ver, csrf, jti} signed for GUI_SESSION_TTL_SECONDS
        B-->>Br: 200 {username, role, csrfToken, mfa state}, cookies smo_session<br/>(HttpOnly, Path=/api, SameSite=Strict) and smo_csrf (readable), audit LOGIN
    end
    Br->>B: any later call, with the cookie (or Authorization Bearer from POST /api/token)
    Note over B: signature and exp, user active, token version equal, jti not revoked,<br/>on a cookie POST/PUT/PATCH/DELETE the X-CSRF-Token equal to the csrf claim,<br/>role and scope claim read from gui_user on this request
    alt any of these fails
        B-->>Br: 401 UNAUTHENTICATED / SESSION_REVOKED, 403 CSRF_TOKEN_INVALID
    else a local admin without a code while GUI_ADMIN_MFA_REQUIRED=true
        B-->>Br: 403 MFA_ENROLMENT_REQUIRED (only /api/me and the enrolment routes open)
    end
```

`POST /api/token` (form `grant_type=password`, `username`, `password`, `otp`) runs the same checks and lockout and answers a Bearer token of the same
lifetime, which needs no CSRF header; an account with a one-time code must send `otp`. A session ends without a session list: a password change, a
password reset, a deactivation and `POST /api/admin/users/{name}/revoke-sessions` bump the user's `token_version`, which every session token carries; a
role or scope change needs nothing, because both are read from the table on every request.

## c — OIDC sign-in and sign-out

```mermaid
sequenceDiagram
    participant Br as Browser
    participant B as GUI BFF
    participant DB as BFF database
    participant IdP as OIDC provider

    Br->>B: GET /api/auth/config
    B-->>Br: {localLogin, loginMode, breakGlass, oidc {enabled, providerName, loginUrl}}
    Br->>B: GET /api/oidc/login
    alt OIDC not configured (or GUI_LOGIN_MODE=local)
        B-->>Br: 404 OIDC_DISABLED
    else discovery failed, or 5000 sign-ins already pending
        B-->>Br: 303 /login?oidc_error=code (audit OIDC_LOGIN_FAILED)
    else
        B->>DB: pending sign-in {state, nonce, PKCE verifier, hash of a binding value}, 10 minutes
        B-->>Br: 302 to the provider, cookie smo_oidc (the binding, Path=/api/oidc, SameSite=Lax)
    end
    Br->>IdP: sign in (the provider asks for its own second factor)
    IdP-->>Br: redirect to GUI_OIDC_REDIRECT_URI with code and state
    Br->>B: GET /api/oidc/callback?code&state
    Note over B: the pending row consumed first, the binding cookie compared,<br/>the provider's error, the code exchanged with the verifier, the ID token<br/>checked (key by kid, iss, aud, nonce), groups mapped to a role,<br/>the scope claim read when GUI_OIDC_SCOPE_CLAIM is set
    alt a check fails, no group maps to a role, a broken scope claim, the user disabled
        B-->>Br: 303 /login?oidc_error=code (audit OIDC_LOGIN_FAILED)
    else
        B->>DB: user oidc:subject created, or its role (and scope) set again
        B-->>Br: 303 / with smo_session and smo_csrf (audit OIDC_LOGIN)
    end
    Br->>B: POST /api/logout (no valid session needed)
    B->>DB: the token's jti revoked until it would expire (when the token decodes), audit LOGOUT
    B-->>Br: 200 {status, endSessionUrl for an OIDC user}, both cookies cleared
```

A person removed from every mapped group, or whose scope claim no longer reads, also loses the sessions already open: the callback bumps the user's
token version before it refuses. The provider's discovery document and signing keys are cached per process (keys for an hour, refetched at most every
30 s for an unknown `kid`). An OIDC user has no password (`UNUSABLE_HASH`) and enrols no one-time code here.

## d — A call through the proxy

```mermaid
sequenceDiagram
    participant Br as Browser
    participant B as GUI BFF
    participant DB as BFF database
    participant R1 as R1 Termination
    participant M as Module

    Br->>B: GET/POST/PUT/PATCH/DELETE /api/smo/{module}/{path}?query, body, X-CSRF-Token
    Note over B: session as in b, then decide(method, path, every query value, role):<br/>the first rule that matches (rbac.py RULES)
    alt no rule, or the role below the rule's
        B->>DB: audit DENIED
        B-->>Br: 403 FORBIDDEN
    else allowed
        Note over B: the rule's forced values replace the browser's: query_overrides<br/>(ack_user_id, decided_by, requested_by, as_admin) and json_overrides<br/>(requestedBy smo-gui:user, decidedBy, msacRole, rmioId smo-gui ...),<br/>a body to rewrite that is not a JSON object is 400 INVALID_BODY
    end
    Note over B: forwards content-type and accept only, adds X-R1-Acting-User smo-gui:user,<br/>X-R1-Acting-User-Scope for a scoped user and a new X-Correlation-ID
    B->>R1: same method /{module}/{path}, Authorization Bearer (the BFF's token)
    opt R1 answers 401
        B->>R1: the call again with a fresh token (once)
    end
    R1->>M: the call, role internal, the acting user and scope passed on
    M-->>R1: answer
    R1-->>B: answer
    alt no SME token
        B-->>Br: 502 SMO_AUTH_FAILED
    else R1 did not answer within GUI_UPSTREAM_TIMEOUT_SECONDS
        B-->>Br: 502 R1_UNREACHABLE
    else
        B-->>Br: the module's status and body, without hop-by-hop, length, encoding,<br/>Set-Cookie, server and date headers, X-Correlation-ID echoed
    end
    Note over B,DB: a change writes audit PROXY with the status and the correlation id, a read writes nothing
```

The permission table is an allow-list: a route with no rule is unreachable from the console, and machine-to-machine routes are left out on purpose.
The correlation id is minted here, never taken from the browser ([call flow 14d](../../docs/call-flows/14-correlation-id-propagation.md)). A rApp's
own page takes a second road, `/api/rapps/{instance}/operator/{route}`: only a route its package's `operatorUi` declares is allowed (403
`UNDECLARED_ROUTE`, `RAPP_READ_ONLY`, `FORBIDDEN`, 413, 422), a change is audited `RAPP_ACTION` before and after it is sent, and the call goes to R1's
`/rapps/{instanceId}/operator/...` with a 65 s timeout. `GET /api/modules/status` probes R1 and the fifteen modules in parallel and always answers 200.

## e — Counts, the event stream and preferences

```mermaid
sequenceDiagram
    participant Br as Browser tab
    participant B as GUI BFF
    participant H as Event hub (one per process)
    participant R1 as R1 Termination
    participant DB as BFF database

    Br->>B: GET /api/summary/{page}?region&site_cluster
    Note over B: cache key page, scope and the user's claim, kept 5 s and shared:<br/>one parallel fan-out of limit=1 reads (5 s each), a module that fails named in partial
    B-->>Br: {counts, partial, panels} or 404 NO_SUCH_SUMMARY, 400 INVALID_SCOPE, 403
    Br->>B: GET /api/events?topics=summary:nav,summary:alarms
    alt an unknown topic, more than 4, a count the role cannot read, 5 streams of this user open here
        B-->>Br: 400 UNKNOWN_TOPIC / TOO_MANY_TOPICS, 403 FORBIDDEN, 429 TOO_MANY_STREAMS
    else
        B-->>Br: 200 text/event-stream, retry 3000, then the full counts of each topic
        Note over H: the first stream starts the poller and the feed follower
        loop every 5 s, or within a second after a feed event
            H->>B: summary of each subscribed topic (the shared cache)
            H-->>Br: event summary or attention, only when a count or a panel changed
        end
        H->>R1: GET /dme/smo-events?after&wait=25 (long poll, own loop, retried after 30 s when it fails)
        Note over H: every 15 s the session is checked again and a ping sent,<br/>the stream ends after 30 min, at a failed check or a changed scope claim,<br/>the last stream to close stops the poller and the follower
    end
    Br->>B: GET/PUT /api/me/preferences, GET/PUT/DELETE /api/me/pins and /api/me/kpi-layouts
    B->>DB: one row per user, every field from a closed set (an unknown field 422), pins at most 5
```

A tile reads a total, never a list, and fifty open tabs on one page cost one fan-out per 5 s. The event feed is only a cue to recompute sooner; a
feed that cannot be read leaves the 5 s poll carrying the streams. Exports (`POST /api/exports`) are the one other background work: an asyncio task in
the accepting instance pages its source into `gui_export_chunk` rows with a heartbeat per page (at most 3 active jobs per user, files kept 24 h).

**Phase 1:** the stream cap (5 per user) and the poller are per process; several instances each count their own.

## f — Stopping and recovering

```mermaid
sequenceDiagram
    participant K as Container runtime
    participant Up as uvicorn
    participant B as GUI BFF
    participant X as Export tasks
    participant DB as BFF database
    participant SME as SME

    K->>Up: SIGTERM (compose stop_grace_period 30 s)
    Note over Up: stops accepting, lets running requests finish for<br/>UVICORN_GRACEFUL_SHUTDOWN_SECONDS (20), then cuts the rest,<br/>an open event stream with them (the browser reconnects)
    Up->>B: lifespan end
    B->>X: cancel this process's export tasks
    X->>DB: each running job FAILED interrupted
    Note over B: gateway client closed, the cached SME token is lost
    K->>Up: start again
    Note over B: lifespan as in a: users not seeded again, jwt_secret read back,<br/>open sessions stay valid (same key, same token versions)
    B->>SME: first call: a token from the stored identity
    alt SME forgot the invoker (its database was reset)
        Note over B: 400, so a new registration once, the stored identity replaced
    end
    Note over B,DB: a crashed instance's export is marked FAILED interrupted when it is next read<br/>after 90 s without a heartbeat, never resumed elsewhere
```

Users, revoked sessions, challenges, pending OIDC sign-ins, the SME identity, the generated signing key, the audit log, exports and preferences are in
the database and survive; the SME token, the summary cache, the streams and the provider's keys are per process and do not. Audit retention is a
command, not a task: `python -m app.retention` (cron, or `kubectl exec`) deletes rows older than `GUI_AUDIT_RETENTION_DAYS`.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `R1_URL` | `http://r1-termination:8000` | Where every SMO call, the module health probe and `/bootstrap` go |
| `SME_URL` | empty: discovered from R1's `/bootstrap` | The SME base for registration and the token endpoint |
| `SMO_ENROLLMENT_SECRET[_FILE]` | empty | Sent at registration so SME records the BFF as `internal` (compose mounts it as a secret) |
| `SMO_MTLS`, `SMO_MTLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE` | `off`, `/run/mtls/...` | The BFF's calls to R1 and SME present its certificate; `http://` peers become `https://`; read once at start |
| `GUI_DATABASE_URL` | `sqlite:///./gui-bff.db` (compose: `/data/gui-bff.db`) | The BFF's own store; one shared database for several instances |
| `GUI_JWT_SECRET` | empty: generated once and stored in the database | The HS256 session key; changing it signs everyone out |
| `GUI_JWT_ALGORITHM`, `GUI_JWT_PRIVATE_KEY_FILE`, `GUI_JWT_PREVIOUS_KEY_FILES` | `HS256`, empty, empty | RS256 / ES256 sessions with a published key set; a bad combination stops the start |
| `GUI_SESSION_TTL_SECONDS` | `28800` | How long a session (and a `/api/token` token) lasts |
| `GUI_COOKIE_SECURE` | `true` | The Secure flag of the two cookies |
| `GUI_ADMIN_PASSWORD`, `GUI_OPERATOR_PASSWORD`, `GUI_VIEWER_PASSWORD`, `GUI_INITIAL_PASSWORD_FILE` | empty, empty, empty, `./initial-admin-password` | First-boot seeding only |
| `GUI_LOCAL_LOGIN_ENABLED`, `GUI_LOGIN_MODE` | `true`, `both` | Who may use the password form; checked against the OIDC settings at start |
| `GUI_OIDC_ENABLED` and the other `GUI_OIDC_*` | `false` | OIDC sign-in: issuer, client, redirect URI, group-to-role map, default role, scope claim, timeout |
| `GUI_TOTP_KEY[_FILE]`, `GUI_ADMIN_MFA_REQUIRED` | empty, `false` | One-time codes for local accounts; admins forced to enrol |
| `GUI_TRUSTED_PROXY_HOPS` | `1` | Which `X-Forwarded-For` entry is the client, for the lockout and the source throttle |
| `GUI_ACCOUNT_MAX_FAILURES`, `_WINDOW_SECONDS`, `_COOLDOWN_SECONDS`, `_COOLDOWN_MAX_SECONDS` | `20`, `600`, `60`, `300` | The per-name cool-down; a nonsense set stops the start |
| `GUI_UPSTREAM_TIMEOUT_SECONDS` | `30` | How long a proxied call waits for R1 |
| `GUI_HEALTH_TIMEOUT_SECONDS` | `3` | How long each module probe of `/api/modules/status` waits |
| `GUI_AUDIT_RETENTION_DAYS`, `GUI_AUDIT_EXPORT_DIR` | `0`, empty | What `python -m app.retention` deletes, and where it writes it first |

## Decisions this module depends on

- **The BFF is the authority, the browser never reaches R1** (gui-bff README 1.5): an allow-list of rules, first match wins, role read from the table
  on every request; identity fields forced from the session (`query_overrides`, `json_overrides`).
- **The BFF is an SMO module at SME** (PR-SEC-14): it registers with the enrollment secret and asks for `smo-gui`; R1 sees the BFF, and the person
  travels in `X-R1-Acting-User` (SEC-15.8) with their scope claim in `X-R1-Acting-User-Scope` (GUI-5).
- **Sessions are signed tokens, ended by version or by `jti`** (PR-SEC-5 for the keys; logout records the `jti`, PR-SEC-7.5 for revoke-sessions).
- **Two layers of second factor** (PR-SEC-7): the provider's for OIDC users (PR-SEC-6), a one-time code for local accounts and the break-glass account.
- **Sign-in throttling by pair, source and name** (SEC-15.11, SEC-15.11b).
- **One shared database for every instance** (PR-ST-5): seeding, the generated key and the SME identity are written once and adopted by the others.
- **Counts, not lists, and pushed** (GUI-9.1, GUI-1.5 for the event feed cue); **exports as jobs in the database** (GUI-9.5b).
- **One correlation id per console action** ([call flow 14d](../../docs/call-flows/14-correlation-id-propagation.md)); **a rApp's page only on its
  declared routes** (PR-GUI-8).
- **Retention by command, not by worker** (PR-DB-3.6): the BFF does not install `smo_shared` and has no worker.
