# rApp conformance pack (`conformance/rapp`)

What the platform relies on in an rApp package and in the rApp's life on a running stack, as checks. Written for a rApp developer who wants to know, before handing a package to an operator, whether Onboarding will take it and whether the platform treats the rApp's lifecycle as it should; and for this repository, which runs it against its own sample packages (CI job `compose-e2e`, step "rApp conformance pack"). The O1 adaptor kit (`conformance/o1`) is the model: a registry of checks, `pass`/`fail`/`skip`, a report in JSON and Markdown, exit status 1 when a check fails. One addition: `warn`, for advice that Onboarding does not refuse.

```bash
# offline: nothing running, no network
python -m conformance.rapp package my-rapp.csar --out report                       # report.json and report.md
python -m conformance.rapp package samples --trust samples/demo-signing --require-signed   # every .csar in a directory, signatures checked
python -m conformance.rapp package my-rapp.csar --only PK-4 --only SIGNATURE

# against a running stack
python -m conformance.rapp runtime --package-url http://packages:9100/my-rapp.csar --direct --out report     # the modules by name (compose network, a lab)
python -m conformance.rapp runtime --package-url https://packages.example/my-rapp.csar --r1 https://r1.example --header "Authorization: Bearer $TOKEN"
python -m conformance.rapp --list
```

Needs `httpx`, `pyyaml` and `cryptography` (the SMO's runtime requirements) and `shared/` on `PYTHONPATH` (or `pip install -e shared`). Exit status 0: no check failed (warnings and skips do not fail); 1: a check failed; 2: a usage or file problem (nothing was checked).

## Offline: `package`

`PK-V` is the verdict. It calls `onboarding/app/package_validation.py`, the code `POST /packages` runs after it has fetched the file, so a package that passes it is one Onboarding accepts (the NFO call that follows is the one step it does not exercise). The others take the same parsers apart, so you are told **which file** is wrong, and add advice Onboarding does not give.

| Check | Group | What | Fails or warns |
|---|---|---|---|
| PK-1 | STRUCTURE | a `.csar` zip file within Onboarding's zip limits (entries, size of a file and of all, compression ratio), no entry listed twice, no entry path that is absolute or climbs out with `..`, no corrupt entry | fail |
| PK-2 | STRUCTURE | `TOSCA-Metadata/TOSCA.meta` names an `Entry-Definitions:` file that is in the package | fail |
| PK-3 | STRUCTURE | the ASD sets `application_name`, `application_version`, `provider` | warn (Onboarding accepts a package without them) |
| PK-4 | MANIFEST | `manifest.yaml`, when present, is valid YAML and `runtimeProfiles` (including `memory` as a Kubernetes quantity), `limits` and `operatorUi` pass Onboarding's rules | fail |
| PK-5 | MANIFEST | the manifest declares `executionModes` and a profile for each, so the platform can size its runtimes | warn |
| PK-6 | MANIFEST | `capabilities.yaml`, when present, parses; `consumes` and `provides` are `{namespace, description}` entries naming one of the six SDK namespaces | fail (parse), warn (unknown namespace: Onboarding stores it unchecked) |
| PK-7 | MANIFEST | `Files/Sme/…` declarations are valid JSON | fail |
| PK-8 | HYGIENE | no test suite, cache, `.env`, key file shipped | warn |
| PK-9 | HYGIENE | no file holds a private key | fail |
| PK-S | SIGNATURE | with `--trust`: signed by a publisher of the trust store and unchanged since (tampered, added or removed file, unknown publisher, wrong key); without a signature: warn, or fail with `--require-signed`; without `--trust`: skipped (fail with `--require-signed`) | |
| PK-V | ONBOARDING | Onboarding's validation accepts the package, with the same trust store and policy | fail |

The zip limits are Onboarding's own (`ZIP_LIMITS`, `csar_signing.DEFAULT_LIMITS`: 2000 entries, 50 MiB a file, 200 MiB in all, 200 to 1 above 1 MiB), and every file is read bounded, so a zip bomb fails PK-1 (and PK-V) and is never decompressed in full. An unsafe entry path fails PK-1 although Onboarding refuses it only for a signed package.

A check that needs an earlier one to have passed is **skipped** with the reason, not failed a second time (a file that is not a zip fails PK-1 and skips the rest of the structure checks).

## Against a running stack: `runtime`

The kit does the operator's part (onboard, create, terminate) and plays the calls a running rApp makes to the platform (bootstrap-complete, configuration, performance and fault reports), reading the platform back after each step: a step answered 2xx that did nothing fails. **It does not run the rApp's code.** A package's workload is started by the deployment (compose, Kubernetes), not by the platform; whether the workload makes these calls is for the rApp's own tests (`sdk/README.md`).

Onboarding refuses a package whose bytes it already holds, so the package must be new to the stack: a committed sample that the demo runbook has onboarded fails RT-1 as a duplicate. `scripts/unique_sample_csar.py NAME OUT.csar` writes a signed copy of a sample with one extra file, new to any stack (CI uses it). Onboarding fetches the package itself, so `--package-url` must be reachable **from Onboarding**, not from where the kit runs. `--direct` addresses the modules by name (`http://onboarding:8000`, `--direct-template` to change that), as the demo runbook replay does: for a lab or a CI network, where there is no token. `--r1 URL` goes through the R1 gateway (`/onboarding/...`, `/rapp-mgmt/...`) and needs `--header "Authorization: Bearer …"` for a token the gateway accepts.

| Check | Group | What |
|---|---|---|
| RT-1 | ONBOARD | Onboarding fetches the package and it reaches `AVAILABLE` with an NFO deployment descriptor (a refusal shows its `failureReason`) |
| RT-2 | REGISTER | rApp Management creates an instance (`DEPLOYING`, an identity, an NFO workload, the requested autonomy mode) and `bootstrap-complete` makes it `RUNNING`, read back |
| RT-3 | REGISTER | Onboarding holds one **active** usage registration for the package, consumed by the instance (the guard that blocks deleting a package in use) |
| RT-4 | HEARTBEAT | two performance reports (the rApp's periodic heartbeat; there is no dedicated heartbeat route) are recorded and listed newest first; the instance stays `RUNNING` |
| RT-5 | R1-USAGE | a configuration written over R1 is read back as written; the package's declarations (`aiCapabilities`, `smeDeclarations`) are readable |
| RT-6 | R1-USAGE | a `warning` fault is recorded and listed and does not take the instance out of `RUNNING` (only `critical` may) |
| RT-7 | TERMINATE | terminate lands in `UNDEPLOYED`, the NFO workload and the usage registration are released (`lastTeardown`), the registration is no longer active |
| RT-8 | TERMINATE | the terminated instance is deleted (204) and then answers 404 |

When RT-1 fails the rest are skipped, each naming what it needed. **Clean-up:** whatever the run created is removed at the end, also after a failure: the instance is terminated and deleted, and the package deleted (it ends `DELETING`, which frees its hash, so the same package can be run again). `--keep` leaves the package onboarded (a second run of it then fails RT-1: an identical package is already onboarded). A clean-up step that fails is reported as a warning. The run adds rows (the audit trail, the package in `DELETING`, performance and fault reports until the instance row is deleted); do not point it at a production stack.

## The report

`--out report` writes `report.json` (`kit`, `kind`, `subject`, the platform or packages, `summary` counts, `results[]` with `id`, `group`, `title`, `status`, `detail`, `seconds`, `subject`) and `report.md` (the same as a table, also printed). In CI the Markdown goes to the job summary.

## Not covered

* **The rApp's own behaviour**: that the workload bootstraps, authenticates with its credentials, calls R1 as the manifest says, keeps within its `limits`. The checks play the calls; they do not start the workload or hold it to its manifest.
* **Credentials through SME and the gateway's roles**: the kit does not obtain or use an instance's OAuth credentials (`POST /instances/{id}/credentials`), so a role or scope problem of the rApp's real token is not found here (`tests_integration/test_role_policy.py` and `compose_e2e_roles.py` test the roles).
* **Autonomy enforcement** (`SHADOW`, `ASSIST`, `AUTONOMOUS`): the mode is set and read back, what Intent Service does with it is not exercised.
* **Upgrade, rollback, the operator page** (`operatorUi` is validated offline; whether its routes answer is `samples/<name>/tests/test_operator_page.py`).
* **The egress policy** (`PR-RAPP-2.3`): a NetworkPolicy needs a cluster with a CNI that enforces it; the chart tests check what is rendered, nothing here connects from a pod.

## Tests

`tests_integration/test_rapp_conformance.py` runs the pack in process on the mesh (`tests_integration/mesh.py`): the sample packages pass every offline check; each of 13 crafted bad packages fails the check that names its problem **and** the real Onboarding app in the mesh ends `FAILED` exactly when PK-V fails; the runtime checks pass a sample on the real modules, twice in a row, and fakes in front of one module, each breaking one thing (a bootstrap that does not reach `RUNNING`, a missing usage registration, reports acknowledged and not kept, a stale configuration, a warning that faults the instance, a terminate that leaves the usage, a delete that does not delete, a platform that does not answer), each fail the check that reads it back. In CI the compose job runs both commands against the real stack, with none failed, no warning and none skipped as the condition.
