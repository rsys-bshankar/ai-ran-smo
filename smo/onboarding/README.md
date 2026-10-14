# Onboarding (`onboarding/`)

> Software Package Onboarding SMOS: fetches an rApp package (a TOSCA CSAR), validates it, registers its descriptor with NFO, and owns the package lifecycle and the usage registrations that guard it.

| | |
|---|---|
| Standards basis | O-RAN rApp package onboarding (ASD / TOSCA CSAR, O-RAN-SC rApp Manager) + internal manifest.yaml/capabilities.yaml extension |
| R1 route / port | `/onboarding` via R1 Termination (container `:8000`) |
| Depends on (over R1) | NFO (`POST /nfo/descriptors`); the package location itself (plain HTTP GET, not over R1) |
| Called by | rApp Management (`onboarding-status`, `usage/start`, `usage/stop`); AIMgF (`onboarding-status`, for `aiCapabilities.runtimeProfiles`); GUI BFF (operator and admin actions); operators |
| Database tables | `application_package` (versioned), `artifact`, `package_usage_registration` |
| Unit tests | 177 passed (`tests/`, SQLite, standalone) |
| Status | Done. Package signatures are verified against an operator-held trust store when one is configured (`PR-RAPP-1`; off by default, see 1.5, 2.6, 2.8) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

Onboarding turns a package location into a deployable, governed package:

1. **Validate.** Fetch the CSAR, check its structure, read its identity and optional AI-platform declarations, register its artifacts.
2. **Register with NFO.** Create the NF deployment descriptor NFO will later instantiate, so rApp Management has a real descriptor id to deploy.
3. **Govern the lifecycle.** `AVAILABLE`, `PRIMED`, `DEPRECATED`, `DELETING`, `FAILED`, with a delete guard that protects anything still in use.
4. **Track usage.** rApp Management registers one usage per instance; open usages block depriming and deletion.

```mermaid
flowchart TD
    A[Operator or GUI] -->|POST /packages with location| B[Commit package row as ONBOARDING]
    B --> C{Location ends in .csar and passes destination checks?}
    C -- No --> F[Set state to FAILED]
    C -- Yes --> D[Fetch CSAR; verify signature when policy requires]
    D --> E[Open zip; read TOSCA metadata and ASD identity]
    E --> G[Parse optional manifest, capabilities and SME declarations; collect artifacts]
    G --> H{Package content hash already onboarded?}
    H -- Yes --> F
    H -- No --> I[POST /nfo/descriptors]
    I --> J{NFO returns 201 with descriptor ID?}
    J -- No --> F
    J -- Yes --> K[Set state to AVAILABLE and commit]
    F --> L[Return HTTP 202 with packageId and trackingId]
    K --> L
    L --> M[GET /packages/{id}/onboarding-status]
    M --> N[Read final state and descriptor ID when available]
```

Validation and descriptor creation happen within the `POST /packages` request; `202` is returned for either outcome. The caller reads the final state from `onboarding-status`. `AVAILABLE` means the package is ready for rApp Management to create an instance; Onboarding itself does not deploy or start it.

It does not deploy anything (rApp Management and NFO do), does not validate that an rApp works, and does not store the package bytes: the artifacts table records where each file inside the package can be read.

### 1.2 Standards basis

The package format and lifecycle are modelled on the O-RAN-SC rApp Manager (`nonrtric-plt-rappmanager`) and its rApp package (CSAR) with an Application Service Descriptor (ASD).

| Reference | Realised here |
|---|---|
| CSAR layout: `TOSCA-Metadata/TOSCA.meta` with `Entry-Definitions`, `Definitions/`, `Artifacts/` | Same. The entry definitions file is read for identity; every file under `Artifacts/` becomes an `artifact` row |
| ASD node type `tosca.nodes.asd` | `application_name`, `application_version`, `provider` become `name`, `version`, `vendor`; `descriptor_id`, `descriptor_invariant_id`, `descriptor_version`, `schema_version` are stored as given |
| Validator chain: `NamingValidator` | Adopted: a location not ending in `.csar` fails before it is fetched |
| Validator chain: `AsdDescriptorValidator` duplicate check | Adapted: duplicates are detected by SHA-256 of the package bytes (`integrity_hash`), not by `descriptor_id` |
| Validator chain: `FileExistenceValidator` requiring `Files/Acm/definition/compositions.json` | Deliberately not adopted: this build never calls ONAP ACM, so the file is neither required nor read |
| Package priming `COMMISSIONED → PRIMING → PRIMED → DEPRIMING` | `AVAILABLE` plays `COMMISSIONED`. Real resource pre-provisioning behind priming is out of scope, so each step completes inside one request |
| `Files/Sme/providers/*.json`, `Files/Sme/serviceapis/*.json` (CAPIF declarations) | Read at onboarding, stored as `sme_declarations`, registered with SME per instance by rApp Management |

Internal extension (not in any standard): two optional files at the CSAR root, read in addition to the above.

- `capabilities.yaml` declares which of the SDK's six namespaces (data, analytics, models, lifecycle, intent, platform) the rApp consumes or provides.
- `manifest.yaml` declares `executionModes`, `autonomyModes`, `requiredServices` and per-mode `runtimeProfiles` (cpu, memory, gpu), and may declare `limits` (`configJobsPerHour`: CM write jobs the rApp may start in an hour; `maxElementsPerJob`: managed elements one job may touch; `maxChangePercent`: how far a numeric value may move in one write, in percent of its current value; an unknown limit or a bad value fails onboarding). rApp Management puts the limit in force at RAN NF OAM when the instance bootstraps (`AI-10.1`/`10.2`).

The result is stored as `application_package.ai_capabilities` and exposed on the status and package views. A package without either file onboards unchanged. The full layout, every field and what fails onboarding are specified in [`../docs/RAPP_PACKAGING.md`](../docs/RAPP_PACKAGING.md); this README does not repeat it.

### 1.3 Position in the platform

```
 operator / GUI BFF ──POST /packages {location}──▶ Onboarding ──GET location──▶ package server (.csar)
                                                      │  POST /nfo/descriptors
                                                      ▼
                                                     NFO
 rApp Management ──GET onboarding-status, POST usage/start|stop──▶ Onboarding
 AIMgF           ──GET onboarding-status (aiCapabilities.runtimeProfiles)──▶ Onboarding
```

Onboarding never calls rApp Management, AIMgF or SME. The FK from NFO's descriptor table to `application_package` is a cross-module reference declared in the SQL migration, not in the ORM.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `ApplicationPackage` and its lifecycle | NF deployment descriptor, deployment placement → NFO |
| Package validation and the CSAR parsing rules | Instances of a package, their state and credentials → rApp Management |
| `Artifact` records | The package bytes (stay at the location) |
| Usage registrations and the cascade-delete / deprime guards | Registering a package's SME declarations → rApp Management, per instance |
| The package's `ai_capabilities` record | Using `runtimeProfiles` to size a runtime → AIMgF; autonomy mode of an instance → rApp Management |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| `POST /packages` answers `202`, but validation, descriptor creation and the state change all happen inside the request. The response carries the final state implicitly: poll `onboarding-status`. | Keeps the API shape of an async operation without a worker; the pipeline is short. |
| The package row is committed (not just flushed) before NFO is called. | NFO's descriptor row has a real FK on `application_package`; under PostgreSQL's read-committed isolation, NFO's connection cannot see an uncommitted row. |
| `manifest.yaml` may carry `operatorUi`, the operator page the rApp declares (`PR-GUI-8`, `docs/adr/0004-operator-ui-declaration.md`). It is validated by `smo_shared.operator_ui` (kinds, GET sources, routes without `..`, limits, duplicate ids, the `rowDetail` drawer of a table row and its `{row.<field>}` references) and stored in `aiCapabilities.operatorUi`; no new column. A bad one lands the package in `FAILED` like any validation failure, and the message (place and rule) is returned as `failureReason` in the `202` answer and logged, not stored. | The GUI needs the declaration at run time without a GUI build; the manifest and `aiCapabilities` already carry the package's AI part to the readers that need it. A stored reason would need a migration; a failed package is onboarded again once fixed. |
| Any known validation failure (bad zip, missing entry, unreachable location, NFO not answering 201, duplicate, malformed YAML or JSON, invalid runtime profile) lands the package in `FAILED` with a normal `202`, not an HTTP error. | Failing to validate is an expected outcome of onboarding. |
| `FAILED` is terminal and is deleted directly, skipping the cascade check. | Nothing can depend on a package that never became `AVAILABLE`. |
| A byte-identical package already onboarded is rejected, but one whose earlier package is `DELETING` or `FAILED` does not count. | Identity is the content hash; a deleted package may be onboarded again. |
| Signatures (`PR-RAPP-1`, `docs/RAPP_PACKAGING.md` §8). A signed package carries `TOSCA-Metadata/DIGESTS.sha256` (the sha-256 of every other file) and `….sig` (an ed25519 signature over it). With `ONBOARDING_TRUST_STORE` set (a PEM key file or a directory of `<publisher>.pub`, read for each package) a package that carries either must verify: a changed, added or removed file, an unknown publisher and a wrong key are each refused with the rule broken, as `package signature: …` in `failureReason`; `ONBOARDING_REQUIRE_SIGNED_PACKAGES=true` refuses an unsigned package too, and with no trust store refuses every package (a misconfiguration is not "accept all"). The check runs on the fetched bytes before anything is parsed. With neither setting, nothing is checked and a signed package is an unsigned one. | An operator chooses whom to trust, in a file it owns, with no CA to run; the default changes nothing for anyone who sets nothing. cosign and X.509 are not used (`docs/RAPP_PACKAGING.md` §8.4). |
| `signature_verified`: with no trust store it is set true once validation passes, as always (it records "validated"); with one it is true only for a package whose signature verified, false for an unsigned one the policy lets through. The publisher is logged, not stored. | Flipping the default to the truthful value would change what every operator sees (the GUI shows it) for no setting; the publisher would need a column (a migration). |
| Size limits on every package zip (`SEC-15.17`). A package arrives from outside, so the zip is opened through `_open_zip` and read through `_read` in `app/package_validation.py`, signed or not: the central directory is checked against `ZIP_LIMITS` (the `csar_signing.ZipLimits` defaults `verify_zip` applies: 2000 entries, 50 MiB for a file, 200 MiB in all, a ratio of 200 to 1 above 1 MiB) before anything is decompressed, and each file read is bounded. A package over them is `PackageValidationFailed` ("package too large to read: ...") and so `FAILED` with that `failureReason`. | An unsigned package used to be opened and parsed with no limit at all, so a zip bomb exhausted the worker before any check ran (a signed package was already limited by `verify_zip`). |
| `runtimeProfiles.<MODE>.memory` must be a Kubernetes quantity, and the descriptor created for NFO carries `containerResourcesByMode` (requests and limits per mode, `PR-RAPP-2.1`). | The memory becomes a container limit; a value like `16 GB` would be an invalid pod spec the day a deployment manager applies it. |
| Delete from `AVAILABLE` or `DEPRECATED` is blocked while a child package is `AVAILABLE` / `DEPRECATED` or a usage registration has no `stopped_at`; deprime is blocked while a usage registration is open. | Cascade-delete guard; the reference's own deprime guard. |
| `DELETING` is terminal and keeps the row. | Nothing re-enters; the row remains as the record. |
| Instances may be created from `AVAILABLE` or `PRIMED` packages only. Enforced by rApp Management, not here. | Priming is optional. |

Idempotency: usage stop is idempotent (an already stopped registration keeps its first `stoppedAt`). Onboarding the same location twice is refused by the duplicate check, not deduplicated.

Security: the package location is fetched with a direct `httpx.get` (30 s timeout, no redirects). Before the fetch it must end in `.csar` and pass the shared SSRF guard (`smo_shared.webhook.is_safe_webhook_destination`: http/https only; loopback, link-local such as the cloud metadata address, multicast and reserved addresses refused), otherwise the package goes to `FAILED` and nothing is fetched. Any other hostname or private address is allowed, because the package server is usually another container. The caller of `POST /packages` is therefore still trusted to name an appropriate location; the GUI BFF limits this route to the operator role. Onboarding performs no authorization of its own beyond R1 Termination's token check.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | Routes; `_validate_package` (the `.csar` and SSRF checks, the fetch, then `validate_package_bytes`); `_signing_policy` (`ONBOARDING_TRUST_STORE`, `ONBOARDING_REQUIRE_SIGNED_PACKAGES`); `_create_nf_deployment_descriptor` (NFO call, with `containerResourcesByMode`); `_fire` (maps FSM refusals to 409). Re-exports the validation names below, which the tests and `fuzz/fuzz_csar_parsers.py` import from here. |
| `app/package_validation.py` | What needs neither the database nor the web framework, so the offline conformance validator (`conformance/rapp`, PK-V) runs the same code: `validate_package_bytes` (naming, signature, zip, TOSCA.meta, identity, artifacts, hash), `verify_signature`, `_asd_identity`, `_parse_ai_capabilities` (which calls `smo_shared.operator_ui.validate_operator_ui` for the `operatorUi` page declaration), `_validate_runtime_profiles`, `_validate_limits`, `_parse_sme_declarations`, `PackageValidationFailed`, `PARSE_FAILURES`. |
| `../shared/smo_shared/csar_signing.py` | The digest list, the ed25519 signature, the trust store and `verify_csar`; also what `scripts/csar_sign.py` and `samples/build_csar.py` sign with. |
| `../shared/smo_shared/runtime_resources.py` | A runtime profile as Kubernetes requests and limits; the quantity check. |
| `app/statemachine.py` | `PackageState`, `PackageEvent`, `ONBOARDING_FSM`, the two guards `_no_blocking_dependents` and `_no_active_instances`. |
| `app/models.py` | The three tables; `PACKAGE_STATES`. |
| `../shared/smo_shared/statemachine.py` | Generic FSM (`fire`, `legal_events`, `IllegalTransition`). |

### 2.2 Data model

**`application_package`**

| Column | Notes |
|---|---|
| `package_id` (PK, UUID) | Also the `trackingId` |
| `application_type` | request value, default `rApp` |
| `name`, `version`, `vendor` | `unresolved-until-validated` / `0.0.0` / null until the ASD is read; stay so if the ASD lacks the fields |
| `state` | see 2.3 |
| `parent_package_id` (FK to itself, null) | read by the delete guard; no route sets it |
| `manifest_ref` | the location the package was fetched from |
| `tosca_entry_definitions` | the `Entry-Definitions` path |
| `signature_verified` | see 1.5 |
| `integrity_hash` | SHA-256 of the package bytes; the identity used for duplicate detection |
| `descriptor_id`, `descriptor_invariant_id`, `descriptor_version`, `schema_version` | ASD fields, surfaced, not used as a key |
| `sme_declarations` (JSON, null) | `{providers: [...], serviceApis: [...]}` from `Files/Sme/` |
| `ai_capabilities` (JSON, null) | `manifestVersion`, `aiRuntimeSdkVersion`, `executionModes`, `autonomyModes`, `requiredServices`, `runtimeProfiles`, `consumes`, `provides` (only those present) |
| `nf_deployment_descriptor_id` (null) | bare UUID into NFO's descriptor table; the FK exists only in the SQL migration |

**`artifact`**: `artifact_id` (PK), `package_id` (FK, cascade), `path` (inside the CSAR), `access_url` (`<location>#<path>`).

**`package_usage_registration`**: `id` (PK), `package_id` (FK), `consumer_id` (the rApp instance id), `stopped_at` (null while in use).

### 2.3 State machines

`PackageState`: `ONBOARDING`, `AVAILABLE`, `PRIMING`, `PRIMED`, `DEPRIMING`, `DEPRECATED`, `DELETING`, `FAILED`.

| From | Event | To | Guard / note |
|---|---|---|---|
| `ONBOARDING` | `VALIDATE_OK` | `AVAILABLE` | fired by the onboarding pipeline |
| `ONBOARDING` | `VALIDATE_FAILED` | `FAILED` | any known validation failure |
| `AVAILABLE` | `PRIME` | `PRIMING` | |
| `PRIMING` | `PRIME_COMPLETE` | `PRIMED` | fired in the same request as `PRIME` |
| `PRIMED` | `DEPRIME` | `DEPRIMING` | guard `_no_active_instances`: no usage registration without `stopped_at` |
| `DEPRIMING` | `DEPRIME_COMPLETE` | `AVAILABLE` | fired in the same request as `DEPRIME` |
| `AVAILABLE` | `DEPRECATE` | `DEPRECATED` | |
| `DEPRECATED` | `CANCEL_DELETE` | `AVAILABLE` | |
| `AVAILABLE`, `DEPRECATED` | `DELETE` | `DELETING` | guard `_no_blocking_dependents`: no child in `AVAILABLE` / `DEPRECATED`, no open usage |
| `FAILED` | (direct delete) | row removed | handled by the route, not the FSM |

`PRIMING` and `DEPRIMING` are never observable from outside. Forbidden: everything not in the table, for example `PRIMED → DEPRECATE` or `PRIMED → DELETE` (deprime first), `DEPRECATED → PRIME`, `AVAILABLE → DEPRIME`, any event from `FAILED` through the FSM, any event from `DELETING`. `models.PACKAGE_STATES` lists only five of the eight states and is not used by any code.

Event routes and refusals: an event with no edge from the current state gives `409 LIFECYCLE_ILLEGAL_TRANSITION` naming the state and the event. An event whose edge exists but whose guard refuses gives `409 SERVICE_NAME_CONFLICT` with the reason.

### 2.4 API

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/packages` (202) | `{location, applicationType="rApp"}` → `{packageId, trackingId}`, plus `failureReason` when the package ended `FAILED` (the message of a validation failure; the exception name for any other). Runs the validation pipeline. | none for a validation failure (the package is `FAILED`) |
| GET | `/packages?state=` | Paged list of package views | |
| GET | `/packages/{id}/onboarding-status` | `{packageId, state, nfDeploymentDescriptorId, smeDeclarations, aiCapabilities}`; the read rApp Management and AIMgF depend on | 404 `PACKAGE_NOT_FOUND` |
| GET | `/packages/{id}/artifacts` | Paged `{artifactId, path, accessUrl}` | 404 |
| GET | `/packages/{id}/usage` | Paged `{registrationId, consumerId, stoppedAt, active}`: what blocks a delete | 404 |
| POST | `/packages/{id}/prime` | `AVAILABLE → PRIMED` | 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| POST | `/packages/{id}/deprime` | `PRIMED → AVAILABLE` | 409 `SERVICE_NAME_CONFLICT` (open usage); 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| POST | `/packages/{id}/deprecate` | `AVAILABLE → DEPRECATED` | 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| POST | `/packages/{id}/cancel-delete` | `DEPRECATED → AVAILABLE` | 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| DELETE | `/packages/{id}` | `AVAILABLE` / `DEPRECATED → DELETING` (returns the view); `FAILED` → row deleted, `{"status": "deleted"}` | 409 `SERVICE_NAME_CONFLICT` (child or open usage); 409 `LIFECYCLE_ILLEGAL_TRANSITION` (any other state) |
| POST | `/packages/{id}/usage/start?consumer_id=` | Open a usage → `{registrationId}` | 404 |
| POST | `/packages/{id}/usage/{registration_id}/stop` | Close it (idempotent) → `{status: stopped}` | 404 `PACKAGE_NOT_FOUND`; 404 `PACKAGE_USAGE_REGISTRATION_NOT_FOUND` (unknown, or belongs to another package) |

Package view: `packageId, name, version, vendor, applicationType, state, toscaEntryDefinitions, descriptorId, descriptorInvariantId, descriptorVersion, schemaVersion, signatureVerified, nfDeploymentDescriptorId, aiCapabilities, smeDeclarations`. `GET /health` is liveness.

### 2.5 Interactions

The onboarding pipeline, in order, inside `POST /packages`:

| Step | Action | On failure |
|---|---|---|
| 1 | Insert the row in `ONBOARDING`, commit | |
| 2 | Reject a location not ending in `.csar` or not an allowed destination | `FAILED` |
| 2b | `GET {location}` (30 s); read the trust store and the policy; with a trust store (or the policy on) verify the signature of the bytes (`PR-RAPP-1`) | `FAILED` (`package signature: …`, `the trust store cannot be used`) |
| 3 | Open as zip, read `TOSCA-Metadata/TOSCA.meta` and `Entry-Definitions`, read that file, scan it line by line for the ASD identity keys | `FAILED` (unreachable, bad zip, missing file) |
| 4 | Collect `Artifacts/*`; parse `manifest.yaml` / `capabilities.yaml` (YAML) and validate `runtimeProfiles`; collect `Files/Sme/providers/*.json` and `serviceapis/*.json` | `FAILED` (malformed YAML or JSON, invalid profile) |
| 5 | Duplicate check on the SHA-256 against packages not `DELETING` / `FAILED` | `FAILED` |
| 6 | `POST /nfo/descriptors` `{packageId, name: <entry definitions>, workloadTemplate: {toscaEntryDefinitions, containerResourcesByMode?}}`; needs `201` and `nfDeploymentDescriptorId` | `FAILED` (any other status) |
| 7 | Fire `VALIDATE_OK`, commit | |

A `FAILED` package keeps whatever identity fields and artifact rows steps 3 to 5 had already set. The descriptor's `workloadTemplate` is a thin reference to the entry definitions, not a parsed TOSCA node template. An NFO transport failure (as opposed to a non-201 reply) is an `httpx` error and is also treated as a validation failure.

Inbound: rApp Management calls `onboarding-status` on every create, upgrade and bootstrap-complete, and `usage/start` / `usage/stop` around an instance's life; AIMgF reads `aiCapabilities.runtimeProfiles` before it sizes a runtime. There are no callbacks and no background tasks.

### 2.6 Configuration

| Variable | Default | Effect |
|---|---|---|
| `ONBOARDING_TRUST_STORE` | empty | Path of the accepted publishers' public keys: a PEM file, or a directory of `<publisher>.pub` / `.pem` (the file name is the publisher; dot-files skipped, so a mounted ConfigMap works). Read for each package. Empty: no signature is checked. A store that cannot be used fails the package. Chart: `rappSigning.trustStoreConfigMap` |
| `ONBOARDING_REQUIRE_SIGNED_PACKAGES` | `false` | `true`: an unsigned package is refused (and, with no trust store, every package). Chart: `rappSigning.requireSigned` |

Through `smo_shared`: `SMO_DATABASE_URL` (required, no default) and `R1_GATEWAY_URL` (default `http://r1-termination:8000`, for the NFO call). Constant: package fetch timeout 30 s. The signing settings are paths and flags, not secrets: public keys are not secret, so there is no `*_FILE` form. The generated reference is `docs/CONFIGURATION.md`.

**Metrics (PR-OBS-4).** Besides the shared series, `GET /metrics` has `smo_rapp_packages{state}`: the `application_package` rows by `PackageState` (every state present, 0 when empty; `AVAILABLE`, `PRIMED` and so on are the rApps onboarded), read from the database at scrape time (cached 15 s). Aggregate replicas with `max`.

### 2.7 Error codes

| Code | Status | When |
|---|---|---|
| `PACKAGE_NOT_FOUND` | 404 | Unknown package on any `/packages/{id}` route |
| `PACKAGE_USAGE_REGISTRATION_NOT_FOUND` | 404 | Usage stop for an unknown or foreign registration |
| `LIFECYCLE_ILLEGAL_TRANSITION` | 409 | The lifecycle event has no edge from the package's current state |
| `SERVICE_NAME_CONFLICT` | 409 | The edge exists but its guard refused: open usage (deprime, delete) or a dependent child (delete). The code name is borrowed from SME; the detail says what blocked it |

The reasons a package becomes `FAILED` are not HTTP errors; they are listed in 2.5 and, in full, in `RAPP_PACKAGING.md`.

### 2.8 Limits and open items

- Signatures are optional and operator-held (`PR-RAPP-1`): no certificate chain, expiry, revocation list or timestamp; the check is at fetch time only; the publisher is logged, not stored (`signature_verified` means "validated" until a trust store is set, see 1.5; `docs/RAPP_PACKAGING.md` §8.3).
- Package location is fetched as given (suffix check only); no allowlist, no size limit.
- Priming performs no resource pre-provisioning; `PRIMING` and `DEPRIMING` are instantaneous.
- `parent_package_id` is read by the delete guard but nothing sets it, so the child-package guard is only reachable through direct database writes.
- ASD identity is a line scan of flat `key: value` lines, not a YAML parse.
- Package bytes are not stored; `access_url` points back into the original location.
- `models.PACKAGE_STATES` is stale (see 2.3).
- No open item in [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md) names this module.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/onboarding && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_main.py` | Onboarding success (descriptor created via NFO, no ACM file needed, identity and ASD descriptor fields resolved, placeholder kept when absent) | 6 |
| | `operatorUi` (GUI-8.2): the ADR's Energy Saving example onboards and is stored, accepted under `rappManifest` too, absent leaves the package unchanged, `x-` keys dropped, 14 refusals each with the place and rule in `failureReason` (unknown kind, a POST source, a GET action, `..` in a route, `%2e%2e`, `..` in a field path, too many panels, duplicate panel and action ids, version 2, unknown key, over the size limit, `readOnly` with actions, not a mapping), 6 `rowDetail` refusals (unknown block kind, too many blocks, a non-GET per-row source, `..`, a `{row.x}` that names no column, nesting), the stored drawer and its per-row read, the refusal logged, a non-declaration failure reporting only its kind | 27 |
| | Onboarding failures to `FAILED` (NFO refuses, malformed zip, not `.csar`, malformed capabilities YAML, malformed SME JSON, byte-identical duplicate) | 6 |
| | `manifest.yaml` / `capabilities.yaml`: null when absent, parsed when present, capabilities alone, execution modes and runtime profiles, four invalid-profile cases | 8 |
| | SME declarations: null when absent, parsed | 2 |
| | Re-onboarding after delete / failure, live packages still block duplicates | 6 |
| | Lifecycle: deprecate / cancel-delete, delete (failed, plain, blocked by child, blocked by usage, allowed after stop), prime, deprime (plain, blocked, allowed after stop) | 10 |
| | Illegal events are 409 naming state and event; delete without an edge | 9 |
| | Unknown package / registration is 404 on every route | 10 |
| | Usage: stop idempotent, listing shows what blocks a delete | 2 |
| | Package list and state filter, identity fields for the GUI, health | 3 |
| | Package row committed before the NFO call | 1 |
| `tests/test_statemachine.py` | The FSM alone: success and failure onboarding, deprecate round trip, delete guards (child, usage, after stop), no transition from `FAILED`, prime / deprime round trip and guard, no `ONBOARDING → PRIME`, no `PRIMED → DELETE` | 11 |
| `tests/test_business_metrics.py` | `smo_rapp_packages` counts packages by state with every state of `PackageState` present | 1 |
| `tests/test_signing.py` | `PR-RAPP-1` on the real route, real CSAR bytes, a trust store directory: nothing set (signed and unsigned package both onboard, `signatureVerified` true); the policy flag alone refuses every package and says why; a trusted publisher; unsigned accepted but unverified, refused when required; tampered, added and removed file each refused whatever the policy; unknown publisher; wrong key under a trusted key id; digest list without a signature; a refused package does not block the next attempt; a key added to the store is seen without a restart; a missing or empty store fails the package with a reason that does not show the path; the four committed samples verify against the demo publisher | 20 |
| `tests/test_package_validation.py` | The validation module alone: the result of a good package, a `TOSCA.meta` without `Entry-Definitions:` (a validation failure, not a `StopIteration`), the `.csar` name check An unsigned zip bomb (60 MiB from a few kilobytes), too many entries, a file or a total over its limit, a read over the limit, each of the six files the parse reads, the probe for signing entries (`SEC-15.17`) | 14 |
| `tests/test_location_checks.py` | The location checks before any fetch: a name without `.csar`, and a loopback, link-local or non-http(s) address, are refused with their reason and nothing is fetched | 4 |
| `tests/test_main.py` (`PR-RAPP-2.1`) | `memory` that is not a Kubernetes quantity refuses the package (3 cases); the NFO descriptor carries `containerResourcesByMode` (requests = limits, millicores, a GPU-only mode left out) and a package without profiles gets the descriptor it always got | 6 |
| | Total (the listed rows do not add up to the whole: the file also holds the limits and other manifest tests) | 177 |


### 3.3 What is not covered here

- A real CSAR from `samples/` onboarding and deploying through rApp Management and NFO: `tests_integration/test_cross_service.py` (`test_real_demo_csar_onboards_and_deploys`, `test_onboarding_to_rapp_management_full_deploy_creates_real_nf_deployment_descriptor`, `test_runtime_profile_flows_from_rapp_manifest_to_nfo_descriptor`). The same suite fails if a committed `.csar` no longer matches its sources.
- The FK between NFO's descriptor table and `application_package` on PostgreSQL: `tests_integration/` and `scripts/check_migration_matches_models.py`.
- Real network fetch of a package location: unit tests substitute the HTTP call.

## 4. References

- Packaging specification (layout, manifest and capabilities fields, failure reasons): [`../docs/RAPP_PACKAGING.md`](../docs/RAPP_PACKAGING.md)
- Call flows: [01 onboarding to deployment](../docs/call-flows/01-rapp-onboarding-to-deployment.md), [06 package lifecycle](../docs/call-flows/06-package-lifecycle.md)
- OpenAPI: [`../docs/openapi/onboarding.json`](../docs/openapi/onboarding.json)
- Cross-cutting rules: [ARCHITECTURE.md](../docs/ARCHITECTURE.md); history: [`../HISTORY.md`](../HISTORY.md)
- Related READMEs: [rApp Management](../rapp-mgmt/README.md), [SME](../sme/README.md), [R1 Termination](../r1-termination/README.md)
