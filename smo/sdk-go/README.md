# Go AI Runtime SDK (`sdk-go/`)

> A thin, standard-library-only Go client over the R1 interface for an rApp written in Go: token acquisition and renewal against SME, retry with backoff, the platform's errors mapped to one Go type, and typed helpers for the routes an rApp starts with. It is the Go counterpart of the Python SDK (`../sdk/`); the Java SDK is `../sdk-java/`.

| | |
|---|---|
| Standards basis | Internal logic (AI Runtime SDK: a thin client over the R1 interface). OAuth2 `client_credentials` (RFC 6749) against SME's token endpoint; CAPIF API-invoker onboarding (TS 29.222) |
| R1 route / port | None: a library (`github.com/rsys-bshankar/ai-ran-ref/smo/sdk-go`, package `smosdk`). The example rApp serves its operator API on `:8000` |
| Depends on (over R1) | R1 Termination `/bootstrap` (token endpoint), SME (`/invoker-registrations`, `/oauth2/token`), and through the gateway DME, MLMR, SME discovery and provider registration, rApp Management (the instance's own routes) |
| Called by | Any rApp written in Go; the example `examples/hello-rapp`. No SMO module imports it |
| Database tables | None |
| Retries and idempotency | 4 attempts, 200 ms base doubling to a 5 s cap, equal jitter, `Retry-After` honoured, context-aware; every POST carries a generated `Idempotency-Key` reused by every repeat; a `409 CONCURRENT_MODIFICATION` is repeated once (as `../sdk/smo_sdk/_common.py` does) |
| Unit tests | `go test -race`, the library and the example; 92 % of the library's statements; no network beyond `httptest` (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done for RAPP-4.2 to 4.5 (Go); not run against a live compose stack (see 3.3). The Java SDK is a separate change |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

Golden rule 6 of [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md): R1 owns service exposure, and an SDK is a thin client over the same R1 Termination path every other cross-module call uses. The Go SDK keeps that rule: it adds no business logic and no client-side re-validation, and it surfaces the platform's own error as `*smosdk.Error`.

It is smaller than the Python SDK on purpose. The Python SDK has six namespaces with a method per route; this one has the authenticated, retrying transport (`Client.Do`, usable for **any** route) and typed helpers only for the routes the example rApp and a typical rApp's start-up use (`Data`, `Models`, `Platform`, `RApp`). A route without a helper is one `Do` call; helpers are added when an rApp needs them, each with its route listed in `routes.go` so that the drift test (3.1) covers it.

### 1.2 Standards basis

| Piece | Reference |
|---|---|
| Token | RFC 6749 section 4.4 `client_credentials`; SME's `POST /oauth2/token` (JSON body, `scope=smo-rapp`) |
| Invoker onboarding | CAPIF API invoker management, SME `POST /invoker-registrations` (TS 29.222); the request carries an opaque label, not a PEM key |
| Errors | The platform's ProblemDetails (RFC 7807) under `detail`, RFC 6749 section 5.2 for the token endpoint |
| Lists | The platform envelope `{items, total, limit, offset}` (`smo_shared/pagination.py`) |

### 1.3 Position in the platform

```
rApp code --> smosdk.Client --Bearer--> R1 Termination --> module
                 |  \__ Do: retry, 401 renew, 409 repeat, Idempotency-Key
                 |__ tokenSource: GET /bootstrap -> POST SME /invoker-registrations -> POST SME /oauth2/token
```

It never calls a module directly (only SME's own two token routes and `/bootstrap`, exactly as `smo_shared.r1_client` does) and never touches a database.

### 1.4 Ownership

| Owns | Does not own (owner) |
|---|---|
| The Go method names, `Error`, the retry policy, the token cache | Route behaviour, validation, state machines (each backend module) |
| The check that every route it calls is in `docs/openapi/` and agrees with the rApp role policy (3.1) | The role policy itself (`../shared/smo_shared/roles.py`) and the OpenAPI documents (generated from the modules) |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| **Hand-written thin client over `net/http`, not generated (RAPP-4.2)** | See 1.6. |
| **Standard library only; no `go.sum`** | The supply chain is the Go toolchain and nothing else: no module to pin, scan or update, nothing for Dependency review to flag, and a reproducible build is `go build` with the Go version in `go.mod`. A test (`tests_integration/test_sdk_go_example.py`) fails if a `require` or a `go.sum` appears, so adding a dependency is a decision someone reviews, not a side effect. (A module with no dependencies has no `go.sum`; the file would be empty.) |
| One `Client` per process: one invoker identity, one cached token, a mutex around renewal | Same as `R1Client`: many goroutines rejected with the same token renew it once. |
| Token renewed 30 s before `expires_in`, and once on a 401 (the rejected token is compared, so ten concurrent 401s renew once) | Same margin and same one-shot as the Python client. |
| An enrolled invoker SME has forgotten (`400 invalid_client`) is enrolled afresh once; **a pinned invoker is never replaced** | Differs from the Python client, which re-enrolls on any 400. A pinned invoker is the identity rApp Management issued for the instance (`oauthClientId`, which `PUT .../operator-api` checks against); a replacement would silently not be the instance. It is an error the operator must see. |
| Always an rApp: scope `smo-rapp`, no enrollment secret, `SMO_IDENTITY_KIND` not read | An SMO module's identity needs the enrollment secret (PR-SEC-14); a Go rApp must not have it. |
| Transport errors are returned as the wrapped `net/http` error, not as `*Error`; context cancellation as the context's error | Only an HTTP answer is an `*Error`, as in the Python SDK (`httpx.HTTPError` propagates). `errors.Is(err, context.Canceled)` and `errors.As(err, &net.Error)` work. |
| `Page[T]` keeps `total`, `limit`, `offset`, `hasMore` | The Python SDK drops them in `ensure_ok`, so a caller cannot see a list cut at the default limit of 100 (its README says so). Go keeps them. |
| Results are `Object` (`map[string]any`) with `ObjectAs` into the caller's own struct | The SDK does not re-model the platform's schemas: that would be client-side validation and a second copy of every model to keep in step. |
| Retries: only what is safe to repeat | See `RetryPolicy`: after a transport error or 502/504 the server may have acted, so only GET, PUT, DELETE and POST-with-`Idempotency-Key` are repeated; the SME invoker registration (every call mints a new invoker) is repeated only on 429/503. |
| mTLS (`SMO_MTLS=on`) through `ConfigFromEnv` / `MutualTLSClient` | The same variables as the Python client (`SMO_MTLS_CA_FILE`, `SMO_MTLS_CERT_FILE`, `SMO_MTLS_KEY_FILE`, `/run/mtls/` defaults), `http://` becomes `https://`. |

### 1.6 Why hand-written, not generated (RAPP-4.2)

Chosen deliberately, and written here because the OPEN_ITEMS step said "generate":

- **The inputs are not good generator input.** `docs/openapi/*.json` are FastAPI's OpenAPI 3.1 output. Every optional field is `anyOf: [T, null]`, the 4xx/5xx responses of every route are the same `ErrorEnvelope`, and R1 Termination's document is a single catch-all `/{full_path}` proxy route, so there is no typed gateway to generate. A generator would produce hundreds of types for 20 modules of which an rApp uses a dozen routes, with pointer-everywhere optional fields.
- **The tool would be the supply chain.** `oapi-codegen` needs a pinned tool version, its runtime module in `go.sum` and, for OpenAPI 3.1, support that was not complete when this was written; `openapi-generator` needs a JVM. Either puts a dependency (or a build step that can drift from the committed code) where the standard library does the job.
- **What generation would buy is the drift check, and that is done differently.** `routes.go` lists every route the SDK calls; `routes_test.go` checks each against the committed `docs/openapi/<module>.json` (path and method). The documents are themselves kept equal to the live schemas by the platform's integration suite, so a renamed or removed route fails this module's CI. The request and response *bodies* are not checked (they are `Object`s or small literals): a field renamed in the platform is caught by the platform's own contract tests, not here.
- **Not taken, revisit when**: the Go SDK grows to cover the six namespaces, at which point generating types from the documents (with the generator vendored and pinned) is worth a second look.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `client.go` | Package doc, `Client`, `New`, `Request`, `Client.Do` (auth, 401 renew, 409 repeat, `Idempotency-Key`, correlation id, JSON in and out), `roundTrip` (one request with retry) |
| `auth.go` | `tokenSource`: `/bootstrap` discovery, invoker enrollment, the grant, the cache, single-flight renewal |
| `retry.go` | `RetryPolicy`: attempts, backoff with jitter, `Retry-After`, which statuses and errors are repeated, context-aware `sleep` |
| `errors.go` | `Error`, the four answer shapes, `StatusOf`, `IsNotFound`, `IsConflict`, `IsForbidden` |
| `config.go` | `Config`, `ConfigFromEnv` (the Python client's variables), `MutualTLSClient` |
| `routes.go` | The route table: spec, prefix, method, template; `route.path` fills and escapes placeholders |
| `namespaces.go` | `Object`, `Page[T]`, and the helpers `Data()` (the DME reads and `MediateAction`, with the `WithMSACCredential` option), `Models()`, `Platform()`, `RApp()` |
| `examples/hello-rapp/` | The example rApp (`main.go`), its package (`package/`), `build_csar.py`, `Dockerfile`, `docker-compose.hello-go.yml` |

### 2.2 The token flow (RAPP-4.3)

`tokenSource` is `smo_shared.r1_client._ModuleIdentity` for an rApp, step for step:

1. `GET {gateway}/bootstrap` (with `X-Bootstrap-Key` when `Config.BootstrapKey` / `SMO_BOOTSTRAP_KEY[_FILE]` is set): the first `apiEndpoints[].tokenEndPoint.uri`, which must end in `/oauth2/token`. SME's base is that URI without the suffix. Cached for the life of the client.
2. Unless `InvokerID`/`InvokerSecret` pin an identity: `POST {sme}/invoker-registrations` with `{"apiInvokerPublicKey": "smo-rapp:<Name>:<random>"}` and **no** `X-SMO-Enrollment` header, so SME records the invoker as `rapp` and refuses it the internal scopes. The answer's `apiInvokerId` and `onboardingSecret` are the identity.
3. `POST {token endpoint}` with `{"grant_type": "client_credentials", "client_id", "client_secret", "scope": "smo-rapp"}`. The token is cached until `expires_in - 30 s` (at least 1 s; 60 s is assumed when the answer has none).

A failure at any step is returned (wrapped, with the step named, `errors.As` finds the `*Error`): the call is **not** sent unauthenticated, unlike `R1Client`, which sends it and lets R1 answer 401. Steps 1 and 3 retry per the policy; step 2 only on 429/503.

An identity enrolled by this process is not deregistered on exit: an rApp may not delete invoker registrations (`smo_shared/roles.py` `RAPP_MAY_CHANGE`), and SME's stale-invoker purge removes orphans. Run a long-lived rApp with the instance's pinned invoker to avoid creating one per start.

### 2.3 `Client.Do`

```
marshal body; POST gets an Idempotency-Key (once per call); correlation id from the context
token := Token(ctx)
loop:
  roundTrip(...)                     retry: transport error / 429 / 502 / 503 / 504, backoff, Retry-After, ctx
  401 and not yet renewed            -> renew (compare-and-swap on the rejected token), repeat once
  >= 400                             -> *Error (409 CONCURRENT_MODIFICATION on a write: repeat once, same key)
  else                               -> decode JSON into out (204 / empty: nothing)
```

### 2.4 Errors

`*Error{Method, Path, StatusCode, Title, Detail, Body}`; `Title` is the ProblemDetails title (for example `ROLE_NOT_PERMITTED`, `CONCURRENT_MODIFICATION`) or the OAuth error code; `Body` is the first 4 KiB. `StatusOf(err)` is 0 for anything that is not a platform answer.

### 2.5 The routes and the role policy

An rApp token is held to `smo_shared/roles.py`: reads are open, **changes** only on an allow-list. Every route the SDK calls is on it; the two lifecycle reports (`bootstrap-complete`, `performance`) were not until `PR-SEC-10` opened them for the rApp's own instance (`rapp-mgmt/README.md`, "Called by"):

| Route | `RApp()` method | Through R1 with an rApp token |
|---|---|---|
| `PUT/DELETE /rapp-mgmt/instances/{id}/operator-api` | `RegisterOperatorAPI`, `ClearOperatorAPI` | allowed (the caller's own instance) |
| `GET /rapp-mgmt/instances/{id}`, `.../operator-api` | `Instance`, `OperatorAPI` | open (reads) |
| `POST .../bootstrap-complete`, `POST .../performance` | `BootstrapComplete`, `ReportPerformance` | allowed (the caller's own instance; rApp Management answers 403 `NOT_THIS_INSTANCE` for another's). Before `PR-SEC-10` they were 403 `ROLE_NOT_PERMITTED` and the runbook called them from inside the compose network, directly at `rapp-mgmt:8000` |

`POST /dme/actions` (`Data().MediateAction`, `SEC-15.6b`) is an rApp write too: DME mediates it to RAN NF OAM, which asks for the credential of the MSAC Identity named by `RequestedBy` when it has one. Pass it per call with `WithMSACCredential` (section 4); it is sent in the `X-MSAC-Credential` header only, never in the body, an `*Error`, a `String`/`%v` of any SDK type, or a log, and the SDK reads no environment variable for it.

`tests_integration/test_sdk_go_example.py` checks that no route the SDK calls is refused to an rApp. (The finding that the policy lacked these two, which this SDK's author raised, is closed by `PR-SEC-10`: `HISTORY.md`.)

## 3. Tests

```bash
cd smo/sdk-go && gofmt -l . && go vet ./... && go test -race -count=1 -cover ./...
```

### 3.1 What is tested (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md))

| File | Tests |
|---|---|
| `auth_test.go` | The first call discovers, enrolls (no enrollment header) and asks for `smo-rapp`; token cached; renewed before expiry (fake clock); a 5 s token still lasts 1 s; a 401 renews once and repeats; a persistent 401 is returned after one renewal; 20 concurrent callers share one grant; 10 concurrent 401s renew once; a pinned invoker is not enrolled and not replaced; an enrolled one SME forgot is enrolled afresh; the bootstrap key; a failed token flow is an error, never an unauthenticated call; a fixed `BearerToken` bypasses SME and is not renewed; a token endpoint without `/oauth2/token` is refused |
| `client_test.go` | 429/502/503/504 retried then success; gives up after 4 with the platform's error; `MaxAttempts: 1`; 400/403/404/422 are final; a dropped connection retried; a persistent transport error is a net error not an `*Error`; a POST repeated with the same `Idempotency-Key`; a caller's own key wins; only POSTs get one; the registration not repeated after a 502 but repeated after a 503; `CONCURRENT_MODIFICATION` repeated once with the same key, twice returned, other 409s and reads not repeated; context cancelled during backoff returns promptly, cancelled before the call never reaches the server; `Retry-After` and its cap; backoff doubling, cap and jitter bounds; correlation id, User-Agent, query; `New` validation; undecodable and 204 answers |
| `errors_test.go` | The four answer shapes, a non-JSON body, an empty body, the 4 KiB cut, the status helpers through wrapping |
| `namespaces_test.go` | `Page` (envelope, `?total=false` envelope, bare array), `ObjectAs`, every helper's method, path, query and body (the metrics body is unwrapped; a null operator API base), a 403 `ROLE_NOT_PERMITTED` |
| `routes_test.go` | **Every route the SDK calls is in `../docs/openapi/<module>.json` with that method** (the drift check that stands in for generated clients); path filling, escaping and the two programming-error panics |
| `msac_credential_test.go` | `SEC-15.6b` (6): `WithMSACCredential` sends `X-MSAC-Credential` on `POST /dme/actions` only when used; the header is declared by `docs/openapi/dme.json`; not in the body (unset optional fields left out); not in an `*Error` or any `%v`/`%+v`/`%#v`/`%s` of the error, client, option or request; the same credential and `Idempotency-Key` on the repeat after a 503 and after `CONCURRENT_MODIFICATION`; no environment variable read |
| `config_test.go` | Defaults, the Python client's variables, `_FILE` secrets, value-and-file conflict, unreadable file, mTLS files |
| `examples/hello-rapp/main_test.go` | The operator routes serve the fields the manifest's page reads; a failed heartbeat is recorded not fatal; the container probe |
| `../tests_integration/test_sdk_go_example.py` | The example package passes Onboarding's checks, declares the smallest `limits` and rebuilds byte-identically; its declared page is valid and every route it names is served by `main.go`; the SDK's routes against `roles.py`; no third-party Go module, no `go.sum`; the Dockerfile pinned and non-root; the CI job present and the action pinned by SHA |

The `httptest` fake platform (`stack_test.go`) answers `/bootstrap`, SME's registration and token routes and an API handler, and counts every call, so each test states how many enrollments and grants it expects. Mutating the code (margin to 0, the 401 renewal off) fails the tests that should.

### 3.2 CI

Job `sdk-go` in `.github/workflows/smo-tests.yml`: `actions/setup-go` pinned by SHA with the version from `go.mod` (no cache: nothing to cache), `gofmt` (fails if it lists a file), `go vet ./...`, `go test -race -count=1 -cover ./...`, `go build ./...` and the example's static binary built as its Dockerfile builds it. The workflow has no path filter (its jobs are required checks), so the job runs on every PR; it takes under a minute. Dependabot watches the `gomod` ecosystem here and the example's Dockerfile digest (`.github/dependabot.yml`).

### 3.3 What is not tested

- **Not run against a live compose stack.** The unit tests use a fake platform whose shapes were read from the real code (`sme/app/main.py`, `r1-termination/app/main.py`, `rapp-mgmt/app/main.py`) and the committed OpenAPI documents; the example binary was run against a small scripted stand-in (enrollment, grant, operator-api `PUT`, heartbeats, `DELETE` on SIGTERM). The steps below are what a person with a stack should run; RAPP-4.3's "test against the stack" is therefore met in the unit sense only.
- The example's `Dockerfile` was not built (no Docker daemon where it was written); the same `go build` command was run directly, and the compose override passes `docker compose config`.
- Mutual TLS beyond building the client from files.

## 4. Use

```go
cfg, err := smosdk.ConfigFromEnv()           // R1_GATEWAY_URL, SMO_INVOKER_ID/SECRET, SMO_BOOTSTRAP_KEY, SMO_MTLS...
c, err := smosdk.New(cfg)                    // no network yet
types, err := c.Data().DiscoverTypes(ctx, "") // token enrolled, cached and renewed behind this call
for _, t := range types.Items { _ = t["typeName"] }

var out smosdk.Object                         // any route, with the same auth, retry and error mapping
err = c.Do(ctx, smosdk.Request{Method: "POST", Path: "/mlmr/models", Body: myModel}, &out)
if smosdk.IsConflict(err) { ... }

// an O1 configuration action through DME; the credential of the MSAC Identity named by RequestedBy, only if it has one
act, err := c.Data().MediateAction(ctx,
	smosdk.ActionRequest{RequestedBy: "my-identity", Changes: changes},
	smosdk.WithMSACCredential(secret)) // secret: from wherever the rApp keeps secrets; leave the option out for no header
```

Go 1.22 or later at run time (the example uses the method-and-wildcard `ServeMux` patterns); `go.mod` says 1.27 (1.24 is out of security support; the CI job and the example image build with the same release).

### 4.1 Run the example against `docker compose`

The example is `examples/hello-rapp`. It needs a stack (`cd smo && docker compose up -d`, see `../README.md`), then:

```bash
# 1. onboard the package like the samples (../DEMO_RUNBOOK.md sections 1 and 2): build it, serve it on the compose network, POST /onboarding/packages
python3 sdk-go/examples/hello-rapp/build_csar.py /tmp            # writes /tmp/hello-go-rapp.csar
# 2. create the instance (section 3: POST /rapp-mgmt/instances {"packageId": ...}); note its instanceId, then, while it is DEPLOYING,
#    issue its credentials: POST /rapp-mgmt/instances/{instanceId}/credentials -> oauthClientId and oauthClientSecret (shown once)
# 3. start the container beside the stack with the instance's identity
export HELLO_INSTANCE_ID=<instanceId> HELLO_INVOKER_ID=<oauthClientId> HELLO_INVOKER_SECRET=<oauthClientSecret>
docker compose -f docker-compose.yml -f sdk-go/examples/hello-rapp/docker-compose.hello-go.yml up -d --build hello-go-rapp
docker compose logs -f hello-go-rapp          # "registered with SME...", "operator API registered", a heartbeat every 30 s
# 4. complete the bootstrap as the runbook does (section 5, DEPLOYING -> RUNNING); the GUI's rApp directory then lists the
#    instance and its page shows Status and a "Heartbeat now" button
# 5. terminate: docker compose stop hello-go-rapp (SIGTERM: the operator API is withdrawn, exit 0), then POST
#    /rapp-mgmt/instances/{instanceId}/terminate as the operator to retire the instance
```

Leave `HELLO_INVOKER_*` empty to let the example enroll its own invoker (the credentials of step 2 are then not needed, but `PUT .../operator-api` is refused for it: only the instance's own invoker may register for the instance, so leave `HELLO_INSTANCE_ID` empty too and no page is drawn). The example does not send `bootstrap-complete` itself, so step 4 is done as the runbook does it. An rApp may send it through R1 for its own instance (`RAppClient.BootstrapComplete`, allowed since `PR-SEC-10`, 2.5); with a self-enrolled invoker rApp Management refuses it (403 `NOT_THIS_INSTANCE`), as it does `PUT .../operator-api`.

### 4.2 Package it as a CSAR

`python3 sdk-go/examples/hello-rapp/build_csar.py [dir]` zips `examples/hello-rapp/package/` with `samples/build_csar.py`'s own `build_bytes` (sorted entries, fixed timestamps, byte-identical rebuilds, `tests/` and `README.md` left out). The package is the manifest (with the `operatorUi` page), `capabilities.yaml`, the ASD and `TOSCA.meta`; the layout and fields are `../docs/RAPP_PACKAGING.md`. Like the sample rApps, the CSAR does not contain the executable: the process runs as a container beside the stack.

**Limits.** The manifest declares `limits: {configJobsPerHour: 1, maxElementsPerJob: 1}`. The example writes no configuration (its only network call is the DME type catalogue read), so it needs zero config jobs; Onboarding accepts no limit of 0, so it declares the smallest it accepts. rApp Management puts them in force at RAN NF OAM when the instance bootstraps. A rApp built from this example that writes configuration raises them to what its own code can do (`../docs/RAPP_PACKAGING.md` §3.4).

A Go rApp's container is a static binary in an empty image (`examples/hello-rapp/Dockerfile`, build context `sdk-go/`): `CGO_ENABLED=0 go build -trimpath -ldflags="-s -w"`, `FROM scratch`, `USER 65532:65532`, the golang builder pinned by digest, and a `HEALTHCHECK` that runs the binary itself (`hello-rapp -probe`) because the image has no shell. About 6 MB. The compose service for it is `examples/hello-rapp/docker-compose.hello-go.yml` (an override file: `cap_drop: [ALL]`, `no-new-privileges`, `read_only`, no database, `R1_GATEWAY_URL`, the optional bootstrap key and invoker identity). For a Helm deployment the same image is an ordinary workload; the chart does not template rApps.

## 5. Versioning and limits

`smosdk.Version` is `0.1.0`, sent in the User-Agent; the contract with the platform is the route table. Limits: no helpers for `analytics`, `lifecycle` and `intent` (use `Do`); responses are `Object`s; no streaming; response bodies above 16 MiB are cut; the gateway's HTTP-date form of `Retry-After` is ignored.
