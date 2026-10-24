# Java AI Runtime SDK (`sdk-java/`)

> A thin Java 21 client over the R1 interface for rApps written in Java: it gets and renews the SME access token, enrols the rApp's invoker, repeats transient failures, and has typed clients for the routes a rApp uses most. One example rApp shows the whole life of an instance.

| | |
|---|---|
| Standards basis | Internal logic (a thin client over the R1 interface, as [`../sdk/`](../sdk/README.md)); CAPIF API-invoker onboarding and OAuth2 `client_credentials` (TS 29.222, RFC 6749) for the token |
| R1 route / port | None: a library (`io.smo:smo-sdk`). The example rApp listens on `8000` (operator API, `/live`, `/ready`) |
| Depends on (over R1) | R1 Termination (`/bootstrap` and the proxied routes of SME, DME, MLMR, rApp Management) |
| Called by | Any Java rApp; the example `examples/hello-rapp`. No SMO module uses it |
| Database tables | None |
| Retries and idempotency | 4 attempts with exponential backoff on 429/502/503/504 and transport errors; every POST carries an `Idempotency-Key` its repeats reuse; the invoker registration is repeated only on 429/503; one repeat on `409 CONCURRENT_MODIFICATION` |
| Unit tests | `smo-sdk` and `hello-rapp`, JUnit 6, no network beyond loopback; CI job `sdk-java` (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | RAPP-4.2 to 4.5 done for Java. Open: a run against the live compose stack, mTLS from the environment, the analytics / lifecycle / intent namespaces (`OPEN_ITEMS.md`, PR-RAPP-4) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

Golden rule 6 of [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md): R1 owns service exposure; an SDK is a thin client over the same R1 Termination path, not a second one. A rApp author using Java gets, without writing HTTP code: the OAuth2 handshake with SME, the invoker identity, retry, and one method per route. Like the Python SDK it adds no business logic and no client-side validation; the error the module answers is raised as `SdkException(status, body)`.

Scope today: the routes the example needs and a rApp typically needs first. Not in scope yet: the Python SDK's `analytics`, `lifecycle` and `intent` namespaces, `SMO_MTLS` from the environment, a typed model of responses.

### 1.2 Position in the platform

```
rApp code --> SmoSdk --> R1Client --(Bearer)--> R1 Termination --> module
                |              ^
                |              | token
                +--> TokenProvider --> GET /bootstrap, POST /invoker-registrations, POST /oauth2/token (SME)
```

### 1.3 Ownership

| Owns | Does not own (owner) |
|---|---|
| Method names and Java signatures; `SdkException`; the `{items,...}` unwrapping; retry policy | Route behaviour, validation, state machines (each module) |
| The token cache and the rApp's SME enrolment as a client | The token endpoint, invoker registry, scopes (SME); role policy (R1 Termination) |
| The list of routes it calls (`Routes`) and the test that keeps it equal to the contract | `docs/openapi/*.json` (generated from each module by `scripts/generate_openapi_specs.py`) |

### 1.4 Behaviour

| Concern | Behaviour |
|---|---|
| Identity | `SMO_INVOKER_ID` + `SMO_INVOKER_SECRET` pin an identity (the pair `POST /rapp-mgmt/instances/{id}/credentials` issues, the right choice for a deployed instance: the gateway lets a rApp register the operator API of its own instance only, by invoker id). Unset, the SDK enrols a new invoker (`smo-rapp:<MODULE>:<random>`) on first use and removes it on `close()` |
| Token | Cached until 30 s before `expires_in`; one fresh grant and one repeat of the call on a 401; a second 401 is the answer. Failing to get a token is an `SdkException` |
| Stale invoker | Self-enrolled and SME says `invalid_client`: enrol again, once. Pinned: error (the SDK never swaps an identity the operator issued) |
| Retry | `RetryPolicy.defaults()`: 4 attempts, 200 ms, doubling, capped at 5 s, full jitter; `Retry-After` (seconds) wins, capped at 30 s. Retried: 429, 502, 503, 504 and I/O errors (including timeouts). Not retried: every other status (a 4xx is final). The enrolment POST is the exception to the exception: it is repeated only on 429 and 503 (SME did not run it), never after an I/O error, 502 or 504, because a repeat after a lost answer would register a second invoker (`SEC-15.13`) |
| POST | `Idempotency-Key` (generated, or the caller's) on every POST, reused by every repeat. The platform answers such a repeat from the stored first answer only on a route with `@idempotent` (PR-ST-3), with `Idempotent-Replayed: true`. `POST /dme/data-jobs`, `/dme/actions`, `/rapp-mgmt/instances/{id}/performance` and `/fault` have it, so a repeat after a lost answer writes nothing twice (for `/dme/actions` the replayed answer may be `202 UNCONFIRMED`: the outcome is still unknown). `POST /rapp-mgmt/instances/{id}/bootstrap-complete` and `/terminate` are safe by state (a repeat is refused `409`, raised although the first attempt worked). The list for all three SDKs is `tests_integration/test_sdk_post_routes_idempotent.py` (`../sdk/README.md` 2.5). The invoker registration (`POST /invoker-registrations`) carries one key generated per registration (a re-enrolment gets a new one); SME does not store an answer for that route (it would hold the onboarding secret), so the limit on repeats above is what keeps it to one registration |
| Lost write race | A mutating call answered `409` with ProblemDetails title `CONCURRENT_MODIFICATION` is sent once more (as the Python SDK's `_RetryOnConflict`). Reads are not |
| Responses | `>= 400` raises `SdkException` (status, raw body); 204 or an empty body is a missing JSON node; an object with a list `items` is unwrapped to the list (`total`, `limit`, `offset` are dropped, as in Python) |
| Secrets | None in code or logs: `SmoConfig.toString()` omits the secret and the bootstrap key; `SMO_BOOTSTRAP_KEY_FILE` / `SMO_ENROLLMENT_SECRET_FILE` read compose secrets |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| **Hand-written thin clients over `java.net.http`, not generated from `docs/openapi/`** (RAPP-4.2) | The committed specs model almost no responses (`{}`), and only some request bodies. A generator (openapi-generator's `java` native library) would add its jar and dependency tree to pin, a template choice, and a client the width of all 25 specs for four modules a rApp calls, and still return untyped bodies. The contract is held by `RoutesContractTest` instead: every route the SDK calls (method, path, query parameters, body fields) is compared with the committed JSON, offline, so drift fails the build. Adding a route is one `Route` constant plus a method. If the specs gain response schemas, generated models for them can be added without changing the clients |
| Jackson `JsonNode` as the value type | The Python SDK returns `dict`; the response shapes are not in the spec |
| Maven with the checked-in wrapper (`mvnw`), the distribution pinned by SHA-256 | Reproducible with only a JDK and network access to Maven Central; no tool to install; the same command locally and in CI |
| JDK 21 (LTS), `--release 21`, `-Xlint:all -Werror` | The current LTS; warnings are failures |
| Dependencies: `jackson-databind` 2.22.4 (runtime; the newest release, patched for the five high advisories the dependency review flagged in 2.18.2: GHSA-j3rv-43j4-c7qm, -rmj7-2vxq-3g9f, -q4xh-88c3-wmh7, -wv8q-qhhj-9h54, -cxp5-3px4-pw24), JUnit Jupiter 6.1.3 (test; JUnit 6 needs Java 17 or newer, the build is on 21) | Nothing else. The JDK's `HttpClient` and `com.sun.net.httpserver` cover the transport and the tests' fake server |
| `dependencies.sha256` and `scripts/verify-dependencies.sh` | Maven has no lock file. The SHA-256 of each of the 12 jars of the test classpath is committed and checked in CI, so a changed artifact at the same version fails; `-C` makes Maven fail on a checksum mismatch with Central. `--write` regenerates it after a deliberate version change (review the diff) |
| No framework, no logging dependency in the SDK | Fewer dependencies to patch; the SDK does not log, it throws |
| `close()` deregisters only a self-enrolled invoker | A pinned invoker belongs to rApp Management (revoked on terminate) |

## 2. Low-level design (LLD)

### 2.1 Code map

| File (`smo-sdk/src/main/java/io/smo/sdk/`) | Responsibility |
|---|---|
| `SmoSdk` | Entry point; the namespace clients over one `R1Client`; `AutoCloseable` |
| `SmoConfig` | Gateway URL, identity, bootstrap key, kind, timeout, retry; `fromEnv()` |
| `R1Client` | A call through R1 Termination: bearer, 401 refresh, idempotency key, conflict repeat, query encoding |
| `TokenProvider` | Discover, enrol, grant, cache, offboard |
| `HttpCaller` | The one place that touches `HttpClient`; retry loop and `Retry-After` |
| `RetryPolicy` | Attempts, backoff, jitter, retryable statuses |
| `R1Response`, `SdkException` | The answer; the failure |
| `MsacCredential` | The credential of an MSAC Identity, for `DataClient.mediateAction` (`SEC-15.6b`); its `toString` never shows it |
| `Route`, `Routes` | The routes called, as data (checked against `docs/openapi`) |
| `DataClient` | `listTypes`, `listDataJobs`, `createDataJob`, `getDataJob`, `queryDataJobStatus`, `terminateDataJob`, `fetchDataRecords`, `mediateAction` (DME; the last takes an optional `MsacCredential`, see below) |
| `ModelsClient` | `listModels`, `getModel` (MLMR) |
| `PlatformClient` | `discoverServices` (SME CAPIF discovery) |
| `InstancesClient` | `get`, `config`, `reportPerformance`, `reportFault`, `registerOperatorApi`, `clearOperatorApi`, `bootstrapComplete`, `terminate` (rApp Management, the instance's own routes) |

`examples/hello-rapp/` is the example (§3); `scripts/verify-dependencies.sh` and `dependencies.sha256` the jar pin.

### 2.2 Configuration (environment)

The names are the Python client's, so a Java rApp is configured as the sample rApps are in `docker-compose.yml`.

| Variable | Meaning |
|---|---|
| `R1_GATEWAY_URL` | R1 Termination, default `http://r1-termination:8000` |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | A pinned identity (both or neither) |
| `SMO_BOOTSTRAP_KEY`, `SMO_BOOTSTRAP_KEY_FILE` | `X-Bootstrap-Key` for `GET /bootstrap`, when the gateway asks for one |
| `SMO_IDENTITY_KIND` | `rapp` (default) or `module` |
| `SMO_ENROLLMENT_SECRET`, `_FILE` | Only for `module` |
| `MODULE` | Label in the enrolled invoker's name, default `java-rapp` |

For mTLS, build the `HttpClient` with your `SSLContext` and use `new SmoSdk(config, httpClient)`.

### 2.3 Using it

```java
SmoConfig config = SmoConfig.fromEnv();
try (SmoSdk sdk = new SmoSdk(config)) {
    JsonNode types = sdk.data().listTypes(null);                    // GET /dme/dme-types
    sdk.instances().reportPerformance(instanceId, Map.of("ok", 1)); // POST /rapp-mgmt/instances/{id}/performance
} catch (SdkException e) {
    // e.status() == 0: no answer (transport) or no token; else the platform's status, e.body() its ProblemDetails
}
```

Build and test: `cd smo/sdk-java && ./mvnw -B -ntp -C verify` (JDK 21). Add it to a project: `io.smo:smo-sdk:0.1.0`; it is not published to a registry, install it with `./mvnw install`.

#### MSAC credential (`SEC-15.6b`)

An O1 configuration action goes through DME (`POST /dme/actions`) and RAN NF OAM asks an rApp for the credential of the MSAC Identity named by `requestedBy` when that Identity has one (`SEC-15.6b`). Pass it per call:

```java
JsonNode action = sdk.data().mediateAction("my-identity", changes, null, null, null, null, MsacCredential.of(secret));
```

The header `X-MSAC-Credential` is sent only when a credential is given; the overload without the last argument sends none. The value is in that header and nowhere else: not in the body, an `SdkException`, a `toString` (`MsacCredential` prints `MsacCredential[redacted]`) or a log, and the SDK reads no environment variable for it, so the rApp passes in what it holds from wherever it keeps secrets. A repeat of the call (a 503, a lost write race) sends the same credential and the same `Idempotency-Key`.

## 3. The example rApp (`examples/hello-rapp/`)

What it does, with the platform route in brackets:

1. Gets a token (`/bootstrap`, `POST /sme/invoker-registrations` unless pinned, `POST /sme/oauth2/token`).
2. Registers its operator API (`PUT /rapp-mgmt/instances/{id}/operator-api`).
3. Every `HELLO_HEARTBEAT_SECONDS` (30) reports a heartbeat (`POST /rapp-mgmt/instances/{id}/performance`; the platform has no separate heartbeat route for a rApp, and the newest performance report is what the instance shows).
4. On "Run now" in the GUI reads a data route (`GET /dme/dme-types`) and an AI route (`GET /mlmr/models`) and records the counts.
5. Serves `/live`, `/ready` and the three operator routes of its `operatorUi` (`GET /instances/{id}/status`, `GET /instances/{id}/runs`, `POST /instances/{id}/run`).
6. On SIGTERM: last heartbeat, `DELETE .../operator-api`, deregister a self-enrolled invoker. It takes no action on the network.

Files: `src/main/java/io/smo/example/{HelloRapp,OperatorServer}.java`; `package/` (`TOSCA-Metadata/TOSCA.meta`, `Definitions/asd.yaml`, `manifest.yaml` with the `operatorUi` block, generated with `smo_sdk.operator_ui`, and its `limits`; `capabilities.yaml`; all checked by `sdk/tests/test_java_example_package.py`); `Dockerfile`; `docker-compose.java-rapp.yml`.

Environment of the example: `SMO_INSTANCE_ID` (required), `HELLO_OPERATOR_API_BASE` (required: the address the gateway reaches the container at, not loopback), `PORT` (8000), `HELLO_HEARTBEAT_SECONDS` (30), plus the SDK's.

### Run the example against docker compose

All commands from `smo/`. The flow is the runbook's (`DEMO_RUNBOOK.md` §1 to §5) with a Java workload in place of the simulated container.

```bash
docker compose up -d                                                        # the stack
python3 samples/build_csar.py --source-dir sdk-java/examples/hello-rapp/package --name hello-java-rapp   # ./hello-java-rapp.csar
docker compose cp hello-java-rapp.csar r1-termination:/srv/scratch/hello-java-rapp.csar
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch
# onboard it, note packageId, then create the instance, note instanceId (runbook §2, §3)
docker compose exec r1-termination python3 -c "
import httpx
print(httpx.post('http://onboarding:8000/packages', json={'location': 'http://r1-termination:8899/hello-java-rapp.csar'}).json())"
docker compose exec r1-termination python3 -c "
import httpx
print(httpx.post('http://rapp-mgmt:8000/instances', json={'packageId': '<packageId>', 'config': {}}).json())"
# issue the instance's own OAuth client (once, before it is RUNNING); the answer holds the id and secret exactly once
docker compose exec r1-termination python3 -c "
import httpx
print(httpx.post('http://rapp-mgmt:8000/instances/<instanceId>/credentials').json())"
```

Like the samples, the package is signed with the demo publisher key by default (`--key my.pem` signs with yours, `--unsigned` writes no signature; `docs/RAPP_PACKAGING.md` §8).

Put the values in `smo/.env` (not committed) and start the rApp:

```bash
cat >> .env <<EOF
HELLO_JAVA_INSTANCE_ID=<instanceId>
HELLO_JAVA_INVOKER_ID=<id from the credentials answer>
HELLO_JAVA_INVOKER_SECRET=<secret from the credentials answer>
EOF
docker compose -f docker-compose.yml -f sdk-java/examples/hello-rapp/docker-compose.java-rapp.yml up -d --build hello-java-rapp
docker compose exec r1-termination python3 -c "
import httpx
print(httpx.post('http://rapp-mgmt:8000/instances/<instanceId>/bootstrap-complete').json())"   # DEPLOYING -> RUNNING
```

Then `GET /rapp-mgmt/instances/<instanceId>/performance` shows the heartbeats, the GUI's rApps directory shows the declared page, and `docker compose stop hello-java-rapp` runs the clean shutdown. Without the credentials step, leave `HELLO_JAVA_INVOKER_*` unset: the rApp then enrols itself, and the gateway refuses its operator-API registration (403, `NOT_THIS_INSTANCE`) because that invoker is not the instance's. **This flow has not been run end to end** (no Docker daemon where the SDK was written); the pieces are tested against stand-ins (§4).

### Packaging a Java rApp as a CSAR, and what a Java rApp needs in the manifest and compose

The CSAR is the same as any other (`../docs/RAPP_PACKAGING.md`, §7.2 for the Java differences): `TOSCA-Metadata/TOSCA.meta`, `Definitions/asd.yaml`, `manifest.yaml`, optional `capabilities.yaml`. The example's `capabilities.yaml` consumes `data` and `models` (the two namespaces `HelloRapp.run` reads), and its manifest declares `limits: {configJobsPerHour: 1, maxElementsPerJob: 1}`: the example writes no configuration, so it needs zero config jobs, and 1 is the smallest limit Onboarding accepts (`../docs/RAPP_PACKAGING.md` §3.4). It carries no code and does not start the process, so nothing in it is Java-specific; the runtime profile is whatever the workload needs (`runtimeProfiles`, JVM heap is sized from the container limit with `-XX:MaxRAMPercentage=75`). A Java rApp needs a container image (the example's `Dockerfile` is a template: multi-stage, digest-pinned Maven and Temurin base images (the image build uses the Maven image, not the wrapper), uid 10001) and a compose service (or Helm values) with `SMO_IDENTITY_KIND=rapp`, `R1_GATEWAY_URL`, the bootstrap key if the gateway has one, and the instance's identity. The example's compose service is an override file, so the stack's own service list, and the Helm chart that must match it, are untouched.

## 4. Tests

`cd smo/sdk-java && ./mvnw -B -ntp -C verify`; CI job `sdk-java` runs it, then `scripts/verify-dependencies.sh`, then builds the example image. A fake platform (`FakePlatform`, on the JDK's `com.sun.net.httpserver`) answers scripted replies and records requests; time and sleeping are injected, so no test waits.

| Test class | Covers |
|---|---|
| `TokenProviderTest` | enrol then grant (body, scope, label, no enrollment header for a rApp), module scope and `X-SMO-Enrollment`, cache until lifetime minus 30 s, refresh, pinned identity never enrolled or offboarded, re-enrolment on `invalid_client` (self-enrolled only), a refused grant keeps status and body, bootstrap without token endpoint, retry during enrolment, the enrolment's one Idempotency-Key reused by repeats and new per re-enrolment, a lost answer or a 504 on the enrolment not repeated, offboard |
| `R1ClientTest` | bearer and `Accept`, `{items}` unwrapping, 401 refresh once and a second 401 final, retry with backoff and `Retry-After`, attempts exhausted, a 4xx not retried and mapped with its body, an unreachable gateway (status 0), `Idempotency-Key` on POST reused by repeats and a caller's own wins, repeat on `CONCURRENT_MODIFICATION` only, reads not repeated, 204, query encoding, `close()` offboards |
| `RetryPolicyTest` | backoff doubling and cap, jitter bound, `Retry-After` cap, retryable statuses |
| `SmoConfigTest` | defaults equal the Python client's, variable names, `_FILE` secrets, module vs rApp, id and secret together, `toString` hides secrets |
| `MsacCredentialTest` | `SEC-15.6b`: `mediateAction` sends `X-MSAC-Credential` on `POST /dme/actions` only when given a credential; not in the body (unset optional fields left out); not in an `SdkException` or a `toString`; the same credential and `Idempotency-Key` on the repeat after a 503 and after `CONCURRENT_MODIFICATION`; a null value refused; the header name is declared by `docs/openapi/dme.json` |
| `RoutesContractTest` | every route exists in `docs/openapi`, query parameters declared, required body properties sent and none invented, a missing route is caught, path expansion and encoding |
| `HelloRappTest` (example) | start registers the operator API and heartbeats, a run reads DME and MLMR and the page sees it, other instances and wrong methods refused, platform failure is a 502, close clears the operator API and deregisters, settings |
| `../sdk/tests/test_java_example_package.py` | the package builds byte-identically, its manifest and `operatorUi` pass Onboarding's check and name exactly the routes the Java server serves, `capabilities.yaml` names the two namespaces the example calls and the manifest the smallest `limits`, the sample CSARs are unchanged by the new build option |

### Not tested

A run against the real SME and R1 Termination (the fake follows `docs/openapi` and the Python client's behaviour, not the services' code); mTLS; concurrency of `TokenProvider` beyond its lock; the example image (built in CI, started in no test).
