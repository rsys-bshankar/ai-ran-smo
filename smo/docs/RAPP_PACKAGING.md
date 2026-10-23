# rApp packaging

How an rApp is packaged for this SMO, what Onboarding validates, and which
part of each package file the platform actually uses. The reference packages
are the four directories under [`../samples/`](../samples/):

| Short name | Directory | What it demonstrates |
|---|---|---|
| ES | [`samples/energy-saving-rapp/`](../samples/energy-saving-rapp/README.md) | Energy Saving: cell sleep/wake from PRB forecasts |
| MO | [`samples/mobility-optimization-rapp/`](../samples/mobility-optimization-rapp/README.md) | Mobility Robustness Optimization: per-relation CIO |
| CO | [`samples/coverage-optimization-rapp/`](../samples/coverage-optimization-rapp/README.md) | Coverage Optimization: joint tilt / power |
| TS | [`samples/traffic-steering-rapp/`](../samples/traffic-steering-rapp/README.md) | Traffic Steering: idle priority and connected CIO |

Onboarding is [`../onboarding/README.md`](../onboarding/README.md); the code is
`onboarding/app/main.py` (`_validate_package`, `_parse_ai_capabilities`,
`_validate_runtime_profiles`, `_parse_sme_declarations`; the `operatorUi` check is `shared/smo_shared/operator_ui.py`).

A rApp need not be Python. The Java SDK's example rApp ([`../sdk-java/examples/hello-rapp/`](../sdk-java/README.md)) has a package of the same layout (§1) and
runs as its own container; §7.1 and §7.2 say what a non-Python rApp needs.

## 1. Package layout

An rApp package is a CSAR (a zip). `python3 samples/build_csar.py <name>`
zips `samples/<name>/` into `samples/<name>.csar` with fixed timestamps, so an
unchanged source rebuilds byte-identically (the integration suite fails if a
committed `.csar` is stale). Tests, caches and `__pycache__` are left out.

```
<name>.csar
├── TOSCA-Metadata/TOSCA.meta        required   entry point: Entry-Definitions: Definitions/asd.yaml
├── Definitions/asd.yaml             required   the Application Service Descriptor (ASD)
├── manifest.yaml                    optional   AI platform manifest (this build's extension)
├── capabilities.yaml                optional   SDK namespaces consumed / provided (this build's extension)
├── Artifacts/…                      optional   deployment items, e.g. Artifacts/Deployment/HELM/*.tgz
├── Files/Sme/{providers,serviceapis,invokers}/*.json   optional   CAPIF registrations
├── Files/Dme/{infoproducers,infoconsumers}/*.json      optional   DME type / job declarations
├── Files/Acm/definition/compositions.json              optional   ONAP ACM composition (not used)
├── TOSCA-Metadata/DIGESTS.sha256    optional   signed packages: the sha-256 of every other file (§8)
├── TOSCA-Metadata/DIGESTS.sha256.sig optional  signed packages: a detached ed25519 signature over DIGESTS.sha256 (§8)
└── app/, demo.py, …                 free       the rApp's own source (the rApps ship it here)
```

| Part | Read by | Effect |
|---|---|---|
| `TOSCA-Metadata/TOSCA.meta` | Onboarding | Missing file or no `Entry-Definitions:` line fails validation (`ONBOARDING` → `FAILED`). |
| `Definitions/asd.yaml` | Onboarding (line scan) | Must exist. Eight flat properties become the package identity (§2). Onboarding then creates an NFO `NFDeploymentDescriptor` that references it. |
| `manifest.yaml`, `capabilities.yaml` | Onboarding (YAML parse) | Stored as the package's `aiCapabilities` JSON (the manifest's `operatorUi` page declaration included, §3.1) and returned by `GET /onboarding/packages` and `…/onboarding-status` (§3, §4). |
| `Artifacts/…` | Onboarding | Every file under `Artifacts/` is recorded as a package artifact. |
| `Files/Sme/…` | Onboarding → rApp Management | Providers and service APIs are stored raw at onboarding and registered with SME per instance at `bootstrap-complete`. |
| `Files/Dme/…` | The rApp itself | Declarations the rApp may post to DME when it starts (no reference rApp ships any). Onboarding does not read them. |
| `Files/Acm/…` | Nothing | An ONAP ACM composition. A package without it onboards identically. |
| `TOSCA-Metadata/DIGESTS.sha256`, `….sig` | Onboarding, when `ONBOARDING_TRUST_STORE` is set | The package's signature, checked before anything else is read (§8). Without a trust store they are ignored: a signed package onboards as an unsigned one. |

Both extension files are **optional and additive**: a package with neither
onboards unchanged and its `aiCapabilities` is `null`.

## 2. `Definitions/asd.yaml`

Onboarding reads these `applicationServiceDescriptor` properties by scanning
for `key: value` lines, not by parsing the YAML. A missing property is simply
absent from the result; none is enforced as mandatory.

| Property | Description | Stored as | ES / MO / CO / TS |
|---|---|---|---|
| `application_name` | rApp name | package `name` | `EnergySaving_rApp`, `MobilityOptimization_rApp`, `CoverageOptimization_rApp`, `TrafficSteering_rApp` |
| `application_version` | rApp version | package `version` | `"1.0.0"` |
| `provider` | Vendor | package `vendor` | `Radisys` |
| `descriptor_id` | ASD identity (UUID) | `descriptor_id` | set (distinct UUID each) |
| `descriptor_invariant_id` | Identity across versions | `descriptor_invariant_id` | set |
| `descriptor_version` | ASD version | `descriptor_version` | `"1.0.0"` |
| `schema_version` | ASD schema version | `schema_version` | `"2.0"` |
| `function_description` | Free text | not read | set |
| `artifacts:` (deployment items) | What to deploy: `type: tosca.artifacts.asd.deploymentItem`, `file`, `artifact_type`, `target_server`, `item_id` | not read as such; files under `Artifacts/` are recorded | **none**; see the note below |

The AI rApps declare no deployment items because they are not deployed from a
Helm chart: their services run as `docker-compose.yml` services and are reached
through the instance's operator API base (`operatorApiBase`, registered at rApp Management and reached through R1 Termination's `/rapps/{instanceId}/operator/...`). The CSAR is what
Onboarding validates and what carries the manifest, not what starts the
process. Package uniqueness is the SHA-256 of the whole CSAR, not the ASD ids.

## 3. `manifest.yaml`

`rappManifest` is the root key. `executionModes`, `autonomyModes`,
`requiredServices` and `runtimeProfiles` are accepted either under
`rappManifest` or at the top level of the file (the layout the rApps use).

"Used by" says what the platform does with the value today. "Descriptive"
means Onboarding ignores it: it travels in the CSAR (and its hash) for people
and tools, and nothing branches on it.

| Parameter | Description | Used by | ES | MO | CO | TS |
|---|---|---|---|---|---|---|
| `operatorApi.baseUrl` | Where the rApp's operator API is reached (call flow 01b). Under `rappManifest` or at the top level; one key, `baseUrl` | **Validated** by Onboarding with the rule of an operator-given `operatorApiBase` (http or https, no credentials, query or fragment, not a loopback, link-local or metadata address; a bad one fails the package) and stored in `aiCapabilities.operatorApi`; rApp Management uses it at CreateInstance when the operator gives no `operatorApiBase` (pushes the instance's credentials there, and R1 Termination forwards the operator page to it) | `http://energy-saving-rapp:8000` | `http://mobility-optimization-rapp:8000` | `http://coverage-optimization-rapp:8000` | `http://traffic-steering-rapp:8000` |
| `operatorUi` | The operator page the rApp declares (§3.1). Under `rappManifest` or at the top level | **Validated** by Onboarding and stored in `aiCapabilities.operatorUi`; drawn by the GUI's generic renderer; the GUI backend calls exactly the routes it lists on the instance's `operatorApiBase` | declared (the ADR's worked example, verbatim) | declared | declared | declared |
| `rappManifest.manifestVersion` | Version of the manifest format | Stored in `aiCapabilities.manifestVersion` | `"1.0"` | `"1.0"` | `"1.0"` | `"1.0"` |
| `rappManifest.aiRuntimeSdkVersion` | `sdk/` contract version the rApp was built against | Stored in `aiCapabilities.aiRuntimeSdkVersion` | `"1.0"` | `"1.0"` | `"1.0"` | `"1.0"` |
| `executionModes` | Which of `TRAINING`, `VALIDATION`, `EMULATION`, `INFERENCE` the package supports | Stored; every `runtimeProfiles` key must be one of them | all four | all four | all four | all four |
| `autonomyModes` | Which of `SHADOW`, `ASSIST`, `AUTONOMOUS` the rApp supports | Stored and exposed only. The mode in force is the rApp *instance's* `autonomyMode`; nothing checks it against this list. | all three | all three | all three | all three |
| `requiredServices` | Platform services the rApp needs | Stored and exposed only; no deploy-time check | DME, AIMgF, MLMR, MLLF, RAN-NF-OAM | same | same | same |
| `runtimeProfiles.<MODE>.cpu` | CPU cores for that execution runtime | **Consumed by AIMgF**: sizes the transient NFO runtime for a Training / Validation / Emulation request that names the package (`packageId`); an explicit `runtimeProfile` in the request wins. It reaches the NFO descriptor twice: as written (`workloadTemplate.resources`) and as Kubernetes requests and limits (`workloadTemplate.containerResources`, §3.2) | 8 / 4 / 4 / 2 | 4 / 2 / 2 / 1 | 4 / 2 / 2 / 1 | 4 / 2 / 2 / 1 |
| `runtimeProfiles.<MODE>.memory` | Memory (string, e.g. `16Gi`) | as `cpu` | 16Gi / 8Gi / 8Gi / 4Gi | 8Gi / 4Gi / 4Gi / 2Gi | same as MO | same as MO |
| `runtimeProfiles.<MODE>.gpu` | GPUs | as `cpu` | 0 in every mode | 0 | 0 | 0 |
| `limits` | The most one instance of the rApp may do on the network (§3.4): `configJobsPerHour`, `maxElementsPerJob`, `maxChangePercent`. Under `rappManifest` or at the top level | **Validated** by Onboarding (an unknown name or a bad value fails the package) and stored in `aiCapabilities.limits`; rApp Management puts them in force at RAN NF OAM when an instance bootstraps, which refuses a job past them | `configJobsPerHour: 600`, `maxElementsPerJob: 1` | `1500`, `1` | `720`, `1` | `1440`, `1` |
| `name`, `version`, `vendor`, `ownerTeam`, `description`, `useCase`, `domain`, `deploymentModel` | Package description from the Wave 10 package proposal | Descriptive. Identity is taken from the ASD (§2), not from here. | set; `useCase: Energy Saving`, `deploymentModel: NonRT-RIC` | set | set | set |

The profile values in the `runtimeProfiles` row are listed in the order
TRAINING / VALIDATION / EMULATION / INFERENCE.

Validation of `runtimeProfiles` (`_validate_runtime_profiles`), each a
package failure (`FAILED`):

- it must be a mapping of mode → profile, and each profile a mapping;
- a mode must be one of the four execution modes;
- when `executionModes` is declared, every profile mode must appear in it;
- `cpu` and `gpu` must be non-negative numbers (booleans are refused);
- `memory` is kept as a string and must be a Kubernetes quantity (`16Gi`, `512Mi`, `4G`, `1.5Gi`, `129e6`): it becomes a container limit (§3.2), and a value such as `16 GB` would be an invalid pod spec. A package whose memory is not one fails onboarding. (Until release 0.7.0 `memory` was not checked.)

### 3.1 `operatorUi`: the page a rApp declares

Optional. A rApp that declares nothing still has the generic page (lifecycle, faults, history, KPIs). The decision record is
[`adr/0004-operator-ui-declaration.md`](adr/0004-operator-ui-declaration.md) (read it for the meaning of every field);
the JSON Schema is [`schemas/operator-ui-1.schema.json`](schemas/operator-ui-1.schema.json), and a complete page, the Energy Saving
one, is [`schemas/operator-ui.energy-saving.example.yaml`](schemas/operator-ui.energy-saving.example.yaml). The smallest example is
`sdk/examples/hello_operator_ui.py`, and `smo_sdk.operator_ui` writes and checks a declaration (`sdk/README.md`).

```yaml
operatorUi:
  version: 1
  panels:
    - id: cells
      title: Cells
      kind: table
      source:
        path: "/instances/{instanceId}/cells"     # quote a route that has {…}: a bare { starts a YAML mapping
        refreshSeconds: 15
      rows: items
      rowKey: cellId
      columns:
        - {path: cellId, label: Cell}
        - {path: state, label: State, format: badge}
      rowDetail:                       # the drawer a click on a row opens: up to 6 blocks
        title: "Cell {row.cellId}"
        blocks:
          - {kind: json, title: Latest execution, path: latestDecision, empty: No decision yet.}
      rowActions:
        - id: unlock
          label: Unlock
          method: POST
          path: "/instances/{instanceId}/cells/{row.cellId}/unlock"
          confirm: Unlock this cell?
          success: Cell unlocked
          body: {operator: "{user}"}
```

| Rule | Value |
|---|---|
| Panel kinds | `table`, `keyValues`, `kpis`, `chart`, `actions`; anything else is refused |
| Source | a GET route relative to the rApp's operator API base; `{instanceId}` is the open instance; in a row action also `{row.<field>}` |
| Row drawer | `rowDetail` of a table: a `title` and 1 to 6 blocks of kind `json`, `keyValues`, `table` or `chart`; a `table` or `chart` block reads a list field of the row or a per-row GET `source` whose route and query may use `{row.<field>}` (the table's `rowKey` or a column); no nesting. The per-row sources are declared routes (reads) |
| Field paths | dotted names, at most one `[]`; no `..`, `$`, index, wildcard or filter |
| Permission | the routes the GUI backend may call for the rApp are exactly the panels' `source` routes, the `rowDetail` per-row sources and the action routes; reads need viewer, changes operator, an action declared `role: admin` (a demo or simulation button) admin; `readOnly: true` allows no change |
| Text | always drawn as text, never HTML or markdown |
| Limits | 64 KiB as JSON, 4 000 values, 20 panels, 20 columns, 30 key-value items, 12 tiles, 5 row actions, 6 row-detail blocks, 10 actions per panel, 8 inputs per action, route 200 and field path 100 characters, `refreshSeconds` 5 to 3600 |
| Extensions | keys starting `x-` are ignored (and not stored); any other unknown key is refused |
| Version | `1`; another value is refused |

Onboarding refuses a bad declaration with the place and the rule (`operatorUi.panels[2].columns[1].path: must not contain '..'`);

**Where the page's routes are served.** The routes are relative to the rApp's *operator API base*, which the instance registers at rApp Management: `operatorApiBase` in `POST /rapp-mgmt/instances` (an operator's choice, also in the GUI's Deploy dialog) or `PUT /rapp-mgmt/instances/<id>/operator-api` from the instance itself (a workload running with the instance's own credentials) or an operator. It must be an `http` or `https` URL without credentials, query or fragment and not a loopback, link-local or metadata address (checked when stored and before every call). The package carries no address: where the rApp runs is a fact of the deployment. A rApp with no registered base still has its page's `kpi` tiles and the platform overview, and the page says its operator API is not registered. The four sample rApps run in compose as one container each, under their own identity, so the demo scripts pass `operatorApiBase` when they create the instance.

**The samples.** Each sample's `manifest.yaml` declares its page at the end (an instance block, the Evaluate and Reconcile buttons, and the cells or relations table with a drawer); Energy Saving's starts with the ADR's example verbatim and adds a cell-state history chart (`seriesBy` the cell) and a table of state changes, both read from `GET /instances/{id}/cell-states` (GUI-9.8b), and `samples/<name>/tests/test_operator_page.py` checks that every declared route, query parameter and field exists in the rApp's own answers. What a declared page cannot show is at the end of the ADR.
see §6.

Why a package leaves a parameter out:

- The four AI rApps declare all four execution modes and all three autonomy
  modes: each runs its decisions through `AutonomyDispatch`, which implements
  SHADOW (record only), ASSIST (operator confirms) and AUTONOMOUS (applies at
  once).
- ES declares larger TRAINING and VALIDATION profiles than the other three.
  The manifests do not record why.
- `gpu: 0` everywhere: the reference models are small (threshold / regression),
  see each rApp's `app/model/`.

### 3.2 From the profile to CPU and memory limits (PR-RAPP-2.1)

What was there before: AIMgF copied the profile as written, `{cpu, memory, gpu}`, into `workloadTemplate.resources` of the NFO descriptor it creates for a Training, Validation, Emulation or Inference runtime; NFO stores a descriptor's `workloadTemplate` and nothing reads `resources` (there is no deployment manager behind NFO). The descriptor Onboarding creates for the package itself carried only `toscaEntryDefinitions`, so the manifest's profiles never reached it.

What there is now (`shared/smo_shared/runtime_resources.py`): the same descriptors also carry the Kubernetes `resources:` block of a container, ready to apply.

| Descriptor | Key | Value |
|---|---|---|
| AIMgF's, per runtime | `workloadTemplate.containerResources` | `{requests: {cpu, memory}, limits: {cpu, memory}}` for that runtime's profile |
| Onboarding's, per package | `workloadTemplate.containerResourcesByMode` | `{TRAINING: {requests, limits}, …}` for each mode of the manifest's `runtimeProfiles` that sets a CPU or memory |

The profile `{cpu: 4, memory: 8Gi, gpu: 0}` becomes `{requests: {cpu: "4", memory: 8Gi}, limits: {cpu: "4", memory: 8Gi}}`. Requests equal limits (the pod is `Guaranteed`: a runtime that asks for less than it may use is the one a busy node squeezes). A fractional CPU is written in millicores (`0.5` is `500m`); a zero or an absent value sets nothing for that resource, and a descriptor with nothing to set has no such key. **GPUs are not mapped**: the resource name is vendor specific and where a GPU runs is NFO's decision through `requiredResourceTypeId`. `workloadTemplate.resources` is unchanged. AIMgF's explicit `runtimeProfile.memory` must be a quantity too (422 otherwise). Nothing applies the block yet: that is what a real deployment manager behind NFO would do (`nfo/README.md`, "No real runtime").

### 3.3 The regions and tenants an rApp may touch are not in the package

A scope claim (`{"regions": [...], "tenants": [...]}`, `PR-SEC-10`, `docs/adr/0005-tenant-region-authorization.md`) says which managed elements an rApp instance may touch. It is **not** a key of
`manifest.yaml` or `capabilities.yaml`, and nothing reads one if a package carries it: reach is granted by the operator who creates the instance (`POST /rapp-mgmt/instances`,
field `authzScope`), not claimed by the author of the package. (The `limits` of the manifest are different: they are the most the author says the rApp needs, and the platform holds it to
them.) So the package does not change; the same package can be run unscoped in one deployment and scoped to a tenant in another. A rApp that runs scoped should expect, from
RAN NF OAM, `403 SCOPE_DENIED` for a write that names an element outside its scope (the whole job is refused), the same for a read of such an element's configuration, an empty or shorter
list where it used to see every element, and `404` for an alarm, job or file by id that belongs to another region or tenant.

### 3.4 `limits`: what the rApp may do on the network (AI-10.1, AI-10.3)

Optional; every sample declares it. A mapping of limit name to a positive number, checked by `_validate_limits` (`onboarding/app/package_validation.py`): an unknown name,
a value that is not a number (a boolean is not one), one out of range, or a fraction where a whole number is needed fails the package, because a limit the platform cannot
enforce must not look as if it were. rApp Management pushes the limits to RAN NF OAM when an instance bootstraps (`PUT /rapp-limits/{invokerId}`, under the instance's own
identity, before it goes RUNNING; the push fails closed), so they hold per instance, and they are removed when the instance is terminated.

| Limit | Meaning | Range | What RAN NF OAM does |
|---|---|---|---|
| `configJobsPerHour` | config (CM write) jobs the instance may start in any hour, counted on a rolling hour in the database | whole, 1 to 100 000 | 429 `RAPP_RATE_LIMITED` with `Retry-After` past it |
| `maxElementsPerJob` | distinct managed elements one job may name (blast radius) | whole, 1 to 10 000 | 403 `RAPP_BLAST_RADIUS_EXCEEDED` |
| `maxChangePercent` | how far a numeric value may move in one write, in percent of its current value (magnitude) | above 0, at most 10 000 | 403 `RAPP_MAGNITUDE_EXCEEDED`; a current value of 0 allows only 0, and a value that cannot be read is refused |

A job the rApp asks DME for counts (`POST /dme/actions`; DME passes on who it acts for, `X-R1-On-Behalf-Of`), and so does a rollback or revert the rApp sends itself as a
new action; RAN NF OAM's own rollback of a job and its automatic reverts are not limited. Whether a write enacted through an Intent counts depends on every module on the way
passing the rApp on; the samples' arithmetic counts those writes anyway, so their limits hold either way. A dry run is not counted, but the blast-radius and magnitude
checks apply to it.

**Choosing the numbers: the samples are the worked example.** Take the most config jobs one pass of the rApp can start, times the most passes an hour, and double it.
[`samples/energy-saving-rapp/manifest.yaml`](../samples/energy-saving-rapp/manifest.yaml) declares `configJobsPerHour: 600` and `maxElementsPerJob: 1`, and its README
([`samples/energy-saving-rapp/README.md`](../samples/energy-saving-rapp/README.md), "Limits the package declares") shows the arithmetic: a pass runs at most every 60 s
(`evaluateEverySeconds` has no default and is at least 60) and starts at most 5 jobs (one LOCK of all its cells, two rollback sends, two wake sends), so 300 an hour,
doubled. Coverage Optimization moves at most 2 cells a pass (720). Mobility Optimization and Traffic Steering act per relation or per cell, so their worst case grows with
the network: they state the topology the number assumes (the demo's 4 relations or cells) and the rule for a bigger one (360 per relation or cell), which an operator
applies by raising the number, rebuilding and re-signing the package. Every sample instance names one managed element, so `maxElementsPerJob` is 1 for all four. None
declares `maxChangePercent`: Energy Saving writes state names, not numbers, and the CIO of the other three starts at 0 dB by default (a tilt may too), which could then
never move. The Go and Java SDK examples write no configuration and declare the smallest limits Onboarding accepts (`configJobsPerHour: 1`, `maxElementsPerJob: 1`; 0 is
not a limit).

## 4. `capabilities.yaml`

`capabilities.consumes` and `capabilities.provides` are lists of
`{namespace, description}`, where `namespace` names one of the six
[AI Runtime SDK](../sdk/README.md) clients. Onboarding stores both lists as
declared and does not check that `namespace` is one of the six or that the
rApp really calls it. Everything else in the file is descriptive.

| Namespace | Meaning | ES | MO | CO | TS |
|---|---|---|---|---|---|
| `data` (consumes) | `sdk.data`: DME datasets, cell guards, O1 config read-back | PRB_UTILIZATION, PRB_UTILIZATION_SIM | HO_PERFORMANCE, HO_PERFORMANCE_SIM | COVERAGE_PERFORMANCE, COVERAGE_PERFORMANCE_SIM | LOAD_PERFORMANCE, LOAD_PERFORMANCE_SIM |
| `analytics` (consumes) | `sdk.analytics`: MDAF predictions and reports | MDAF PRB predictions (`get_prediction`) | not used (d) | not used (d) | not used (d) |
| `models` (consumes) | `sdk.models`: register the model, store its artifact in MLMR | EnergySavingPredictor | MobilityRobustnessPredictor | CoverageSensitivityModel | CongestionSteeringModel |
| `lifecycle` (consumes) | `sdk.lifecycle`: AIMgF jobs, MLLF deploy, inference | yes | yes | yes | yes |
| `intent` (consumes) | `sdk.intent`: `AutonomyDispatch` of the decision | LOCK recommendations | CIO changes | tilt / power change sets | steering and release steps |
| `platform` (consumes) | `sdk.platform`: SME registration, direct DME O1 actions | wake, rollback, operator override | KPI reverts, rollbacks, DMRO bounds | KPI reverts, rollbacks | KPI reverts, rollbacks |
| `data` (provides) | A dataset the rApp publishes for others | `PRB_UTILIZATION_SIM` (Digital Twin sample producer) | `HO_PERFORMANCE_SIM` | `COVERAGE_PERFORMANCE_SIM` | `LOAD_PERFORMANCE_SIM` |
| other namespaces (provides) | | none (e) | none (e) | none (e) | none (e) |

Why a namespace is not declared:

- **(d) `analytics` in MO, CO and TS.** These rApps derive their features
  from the DME datasets themselves and read no MDAF prediction or report.
  Only ES asks MDAF for a PRB forecast. The declaration matches each rApp's
  code (`sdk.analytics` is called only in `energy-saving-rapp/app/main.py`).
- **(e) `provides`.** The platform defines no SDK surface an rApp can serve
  to others, so the only thing an rApp provides is data, through its own
  Digital Twin producer.

Descriptive-only keys, present in all four rApps. Onboarding ignores all of them:

| Key | Describes | ES | MO | CO | TS |
|---|---|---|---|---|---|
| `datasets` | DME types used | PRB_UTILIZATION, _SIM | HO_PERFORMANCE, _SIM | COVERAGE_PERFORMANCE, _SIM | LOAD_PERFORMANCE, _SIM |
| `supportedStages` | Lifecycle stages | four | four | four | four |
| `producedModel` | `{name, type}` of the model | EnergySavingPredictor, REGRESSION | MobilityRobustnessPredictor, CLASSIFICATION_REGRESSION | CoverageSensitivityModel, REGRESSION_OPTIMISATION | CongestionSteeringModel, REGRESSION |
| `inputs` | Counters / attributes read | absent (f) | `MM.Ho*` counters | `MR.*`, `CM.*` | `RRU.PrbTotDl`, `RRC.ConnMean`, `DRB.UEThpDl`, `HO.*`, `CM.*` |
| `outputs` | O1 attributes written | `administrativeState` | `cellIndividualOffset` | `digitalTilt`, `configuredMaxTxPower` | `cellReselectionPriority`, `cellIndividualOffset` |
| `supportedActions` | Actions the rApp can take | `LOCK_CELL`, `UNLOCK_CELL` | `RAISE_CIO`, `LOWER_CIO`, `REVERT_CIO` | `DOWNTILT`, `UPTILT`, `POWER_UP`, `POWER_DOWN`, `REVERT` | `STEER_*`, `RELEASE_*`, `REVERT` |
| `o1Targets` / `o1Attributes` | TS 28.541 classes and attributes it changes | `NRCellDU`, `CESManagementFunction` | `NRCellRelation`, `DMROFunction` | `CommonBeamformingFunction`, `NRSectorCarrier` | `NRFreqRelation`, `NRCellRelation` |
| `supportedVendorModes` | O1 transports | `O1_NETCONF`, `O1_RESTCONF` | same | same | same |
| `supportedAnalytics` | Analytics kinds used | `TRAFFIC_FORECAST` | absent (d) | absent (d) | absent (d) |

- **(f)** ES does not list `inputs`; its manifest names the dataset only
  (`datasets: PRB_UTILIZATION`). The other three enumerate the counters.
- `supportedVendorModes` names the O1 transports the rApp's writes may travel
  over: RAN NF OAM dispatches RFC 6241 `edit-config` (NETCONF) and RFC 8040
  RESTCONF, both over plain HTTP (see
  [`../ran-nf-oam/README.md`](../ran-nf-oam/README.md)).

## 5. Files under `Files/`

| Path | Shape | Used by | Reference rApps |
|---|---|---|---|
| `Files/Sme/providers/*.json` | CAPIF `APIProviderEnrolmentDetails` | rApp Management registers it with SME at `bootstrap-complete` | none (g) |
| `Files/Sme/serviceapis/*.json` | CAPIF `ServiceAPIDescription` | as above | none (g) |
| `Files/Sme/invokers/*.json` | CAPIF invoker onboarding body | not read by Onboarding | none (g) |
| `Files/Dme/infoproducers/*.json` | DME type registration (`POST /dme/production-capabilities`) | the rApp at start | none (h) |
| `Files/Dme/infoconsumers/*.json` | DME data job | the rApp at start | none (h) |
| `Files/Acm/definition/compositions.json` | ONAP ACM composition | nothing: the build never calls ONAP ACM | none |

- **(g)** The four rApps ship no SME declarations, so rApp Management has
  nothing to register for them. A package that does ship them is still
  registered with SME at `bootstrap-complete`.
- **(h)** The AI rApps register their DME types from code (for example
  `energy-saving-rapp/app/producer.py` calls `sdk.data.register_type`) instead
  of shipping JSON declarations.

## 6. What fails onboarding

All of these end in `ApplicationPackage.state = FAILED` rather than an HTTP
error from the create call (the answer to `POST /packages` carries the reason as `failureReason`; it is not stored):

| Cause | Source |
|---|---|
| Location does not end in `.csar` | `NamingValidator` check |
| Not a zip, or `TOSCA-Metadata/TOSCA.meta` or the entry definitions file is missing, or no `Entry-Definitions:` line | `_validate_package` |
| Invalid YAML in `manifest.yaml` or `capabilities.yaml` | `_parse_ai_capabilities` |
| Invalid `runtimeProfiles` (§3), a `memory` that is not a Kubernetes quantity included | `_validate_runtime_profiles` |
| `TOSCA.meta` without an `Entry-Definitions:` line | `validate_package_bytes` (a bare `next()` raised `StopIteration` here before 0.7.0, which nothing caught) |
| A signature or digest list that does not verify against `ONBOARDING_TRUST_STORE`, no signature while `ONBOARDING_REQUIRE_SIGNED_PACKAGES` is on, or a trust store that cannot be used (§8). `failureReason` begins `package signature: ` or `the trust store cannot be used` | `verify_signature`, `_signing_policy` |
| Invalid `operatorUi` (§3.1): unknown kind, key or version, a `source` that is not a GET, a route with `..`, a limit exceeded, a duplicate id | `smo_shared.operator_ui.validate_operator_ui`, called by `_parse_ai_capabilities`. The message is in `failureReason` of the `202` answer and in the log |
| Malformed JSON under `Files/Sme/` | `_parse_sme_declarations` (`JSONDecodeError`) |
| Location unreachable (HTTP error fetching the CSAR) | `_validate_package` |
| NFO refuses the descriptor | `_create_nf_deployment_descriptor` |

The code is `onboarding/app/package_validation.py` (the part that needs no database or web framework, which the conformance pack runs offline) and
`onboarding/app/main.py` (the fetch, the policy, NFO). Signatures: §8.

## 7. Authoring checklist

1. Copy the closest sample. Start from `energy-saving-rapp/`.
2. Give the ASD a new `descriptor_id` / `descriptor_invariant_id`, and set
   `application_name`, `application_version`, `provider`.
3. In `manifest.yaml` list only the modes you implement, and keep every
   `runtimeProfiles` key inside `executionModes`.
4. In `capabilities.yaml` declare exactly the SDK namespaces your code calls. To give the rApp its own operator page, add `operatorUi` to the manifest (§3.1; `smo_sdk.operator_ui` builds and checks it).
5. If the rApp will run scoped, handle `403 SCOPE_DENIED` and filtered lists (§3.3); nothing in the package declares the scope.
6. Rebuild with `python3 samples/build_csar.py <name>` and run
   `PYTHONPATH=shared python -m pytest tests_integration/ -q`.
6. Check the package the way Onboarding will, without a stack: `python -m conformance.rapp package my-rapp.csar` (§9). Sign it for an operator who requires signatures: §8.

### 7.1 An rApp that is not written in Python

The package is language-neutral: Onboarding reads the CSAR files in §1 and never the rApp's source, and the executable is not in the CSAR for any of the reference rApps (it runs as a service beside the stack). A rApp in Go follows §7 with two differences. The package directory holds only the package files (no `app/`), and the container is a static binary in an empty image. [`../sdk-go/examples/hello-rapp/`](../sdk-go/examples/hello-rapp/) is the worked example: `package/` (manifest with an `operatorUi` page, `capabilities.yaml`, ASD, `TOSCA.meta`), `build_csar.py` (calls `samples/build_csar.py`'s `build_bytes` on that directory, so the same fixed timestamps and exclusions apply), a `Dockerfile` and a compose override; [`../sdk-go/README.md`](../sdk-go/README.md) §4 says how to run it and package it. In `capabilities.yaml`, `namespace` names the platform area the rApp calls (`data`, `models`, `platform`, ...) whichever SDK it uses. Which routes an rApp may *change* through R1 is the role policy (`shared/smo_shared/roles.py`), the same for every language.

### 7.2 A rApp written in Java (the Java SDK example)

The CSAR does not start the process (§2), so the language of the workload is outside the package; what the platform needs from any rApp is the same.

| Needed | Python sample rApps | Java example (`sdk-java/examples/hello-rapp/`) |
|---|---|---|
| The package | `samples/<name>/`, `python3 samples/build_csar.py <name>` | `package/`, `python3 samples/build_csar.py --source-dir sdk-java/examples/hello-rapp/package --name hello-java-rapp` writes `./hello-java-rapp.csar` (same fixed timestamps, so a rebuild is byte-identical; the CSAR is not committed) |
| The runtime image | one `Dockerfile` for all services, `MODULE=samples/<name>` | `examples/hello-rapp/Dockerfile`: multi-stage, the digest-pinned Maven 3.9.11 / Temurin 21 image builds, Temurin 21 JRE (digest-pinned) runs `java -jar hello-rapp.jar` as uid 10001 |
| The service in compose | an entry in `docker-compose.yml` (and the chart) | `examples/hello-rapp/docker-compose.java-rapp.yml`, an override file: `docker compose -f docker-compose.yml -f sdk-java/examples/hello-rapp/docker-compose.java-rapp.yml up -d --build hello-java-rapp`; hardening as the stack's (`cap_drop: ALL`, read-only root, `no-new-privileges`) |
| Identity | `SMO_IDENTITY_KIND=rapp`; enrols on first use | the same variables; or the pair `POST /rapp-mgmt/instances/{id}/credentials` issues, as `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` |
| Where it is reached | operator API base registered at rApp Management | `PUT /rapp-mgmt/instances/{id}/operator-api` made by the rApp at start; `HELLO_OPERATOR_API_BASE` is the address the gateway reaches the container at |
| Health | `/live`, `/ready` | the same two routes, on the JDK's built-in HTTP server |

The manifest of the Java package has `executionModes: [INFERENCE]`, a small `runtimeProfiles.INFERENCE` (the example trains nothing) and the smallest `limits` (it writes no configuration, §3.4);
its `capabilities.yaml` consumes `data` and `models`, the two namespaces the example reads; its `operatorUi` is the
three-panel page `sdk-java/README.md` describes, and `sdk/tests/test_java_example_package.py` checks it with the code Onboarding runs. The example is not in the Helm chart.

## 8. Signing a package (PR-RAPP-1)

A signed package lets an operator accept only packages from publishers it has chosen, and notice a package that was changed after the publisher signed it. It is off unless the operator configures it: with `ONBOARDING_TRUST_STORE` unset nothing about onboarding changes, signed packages included.

**What is signed.** Two extra entries in the zip:

| Entry | Content |
|---|---|
| `TOSCA-Metadata/DIGESTS.sha256` | One line per file of the package other than these two: `<sha-256 hex>  <path>`, sorted by path. `build_csar.py` and `scripts/csar_sign.py` write it |
| `TOSCA-Metadata/DIGESTS.sha256.sig` | `{"version": 1, "algorithm": "ed25519", "keyId": "<sha-256 of the public key's 32 raw bytes, hex>", "signature": "<base64>"}`: an ed25519 signature over the exact bytes of the digest list |

Signing is deterministic, so the same sources and key give the same bytes (the integration suite relies on it). Algorithm: ed25519 from `cryptography`, already in the hashed lock through `pyjwt[crypto]`: no dependency was added and the lock was not recompiled. cosign is not used (§8.4).

**Trust store.** `ONBOARDING_TRUST_STORE` is a path inside the Onboarding container: one PEM public-key file, or a directory of `<publisher>.pub` / `<publisher>.pem`. The publisher is the **file name** (`acme.pub` is `acme`; several keys in one file are `acme`, `acme#2`), never anything the package says about itself. The store is read for each package: add a key and the next package sees it. An unusable store (missing, empty, a file that is not an ed25519 public key) fails the package with the reason; it never falls back to checking nothing. Compose: set `ONBOARDING_TRUST_STORE=/run/rapp-trust` and mount your directory there with a `docker-compose.override.yml` (`volumes: ["./rapp-trust:/run/rapp-trust:ro"]` under `onboarding`). Kubernetes: `rappSigning.trustStoreConfigMap` (`deploy/helm/smo/README.md`).

**What Onboarding does** (`verify_signature`, before the package is parsed):

| Package | No trust store, policy off (default) | Trust store set | Trust store set and `ONBOARDING_REQUIRE_SIGNED_PACKAGES=true` |
|---|---|---|---|
| Unsigned | onboards, `signatureVerified: true` (the old meaning: validated) | onboards, `signatureVerified: false` | refused: `package signature: the package is not signed` |
| Signed, verifies | onboards, `signatureVerified: true`, nothing checked | onboards, `signatureVerified: true`, the publisher is logged | onboards, as before |
| Signed, does not verify | onboards (nothing is checked) | refused | refused |

With the policy on and no trust store, every package is refused (`signed packages are required ... but no trusted publisher keys are configured`): a misconfiguration must not mean "accept everything".

A package that does not verify is refused with the rule it broke (`failureReason` of the `202` answer, `package signature: …`):

| Case | Message (shortened) |
|---|---|
| A file changed after signing | `file modified after signing: manifest.yaml does not match its signed digest` |
| A file added | `file added to the package: payload.py is not covered by the signed digest list` |
| A file removed | `file removed from the package: manifest.yaml is in the signed digest list but not in the package` |
| The digest list rewritten | `the signature does not verify under the key of publisher acme (the digest list was altered, or it was signed with another key)` |
| Signed with another key but naming a trusted key id | the same |
| Signed by a key not in the store | `unknown publisher: the package is signed with key 598ee6466e59f4c7..., which is not in the trust store` |
| A digest list without a signature, a signature without a list, a malformed one, an entry listed twice, a path with `..` | `no signature`, `malformed`, … |

`signatureVerified` is true only when a signature verified against the trust store, **once a trust store is configured**. With none it keeps its old meaning (the package validated), so nothing changes for an operator who sets nothing; the value will mean "verified against a key" only at the release that makes the trust store mandatory. The publisher is not stored (that would be a column, a migration); it is in Onboarding's log.

### 8.1 Signing your own package

```bash
python scripts/csar_sign.py keygen --out my-publisher      # my-publisher.key.pem (private: a secret store) and my-publisher.pub (give it to the operators)
python scripts/csar_sign.py sign my-rapp.csar --key my-publisher.key.pem
python scripts/csar_sign.py verify my-rapp.csar --trust my-publisher.pub     # exactly Onboarding's check
python scripts/csar_sign.py digests my-rapp.csar                             # what the files hash to, and whether it is signed
```

`samples/build_csar.py` builds and signs a sample (`--key`, `--unsigned`). Re-signing a signed package replaces its signature. Key rotation: put the new `.pub` in the store beside the old one, publish with the new key, remove the old file when no package signed with it should be accepted.

### 8.2 The demo key: never trust it in production

The four committed sample packages are signed with a **demo publisher** key whose **private half is committed** under `samples/demo-signing/` (`demo-publisher.seed`), so that the signing path can be shown and tested without generating a key. Anyone who reads the repository can sign a package that verifies against `demo-publisher.pub`. Trust it in a lab, a demo or a test; **never put `demo-publisher.pub` in the trust store of a deployment that matters**, and never reuse the key. It is the only private key in the repository (`tests_integration/test_rapp_signing.py` fails if another appears) and is kept as a one-line seed rather than a PEM block so that secret scanners do not mistake it for a leak. See `samples/demo-signing/README.md`.

### 8.3 What a signature does not give

* **No identity beyond a file name.** There is no certificate chain, no expiry, no revocation list: removing the key file is the revocation, and it applies to the next package, not to ones already onboarded.
* **No transparency log, no timestamp**: a signature does not say when a package was signed, and an old signed package can be replayed (onboarding it again is refused only when an identical one is already onboarded).
* **It covers the files, not their meaning.** A signed package can still be a bad rApp; the signature says who vouches for it and that it is unchanged.
* **Not checked after onboarding.** The check happens when the package is fetched. A package already `AVAILABLE` stays so if the trust store later loses its key.

### 8.4 Not taken

* **cosign** (keyless or key-based): not available in the session that built this, and the keyless form needs a transparency log and an identity provider that an air-gapped operator does not have. A cosign signature could be added beside the ed25519 one later; the digest list does not change.
* **X.509 / CMS signatures as in the TOSCA CSAR specification** (a certificate and a signature block in `TOSCA.meta`): a certificate chain is the right answer for a public marketplace of rApps, which this is not; an operator-held list of publisher keys is what the rest of this build does (invokers, enrolment).
* **Storing the publisher** on the package row (a migration, and the GUI to show it).

## 9. Checking a package before you ship it (PR-RAPP-3)

`python -m conformance.rapp package my-rapp.csar [--trust DIR] [--require-signed]` runs Onboarding's own validation code (`onboarding/app/package_validation.py`) on the file, file by file, and adds advice (a missing version in the ASD, a test suite shipped, a private key inside, an execution mode with no runtime profile). `python -m conformance.rapp runtime …` takes the package through onboard, register, heartbeat, R1 usage and terminate on a running stack. Both write `report.json` and `report.md`. The checks and the limits: [`../conformance/rapp/README.md`](../conformance/rapp/README.md).
