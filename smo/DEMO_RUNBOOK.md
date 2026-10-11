# Demo runbook — the Energy Saving rApp's full lifecycle

A live walk-through of `smo/docs/call-flows/01-rapp-onboarding-to-deployment.md`
and the rest of the platform against a running `docker compose up` stack:
onboard a real package, deploy it, simulate its bootstrap (SME/DME
registration, OAuth2), watch it go `RUNNING`, exercise every other module,
then retire it. §24–§27 run the four reference rApps. Every request body
is copy-pasteable and matches this build's routes.

Run it in your own environment with a Docker daemon.
`tests_integration/test_demo_runbook.py` replays the same requests (§2–§23)
and the four `demo.py` scripts (§24–§27) through the in-process mesh on
every CI run, and the `compose-e2e` job replays the same test file against
a live `docker compose up` stack (`SMO_E2E_LIVE=1`, `tests_integration/live.py`).

With mutual TLS between the services on (`docker-compose.mtls.yml`, `PR-SEC-2`) the commands below need `https://` and a client
certificate (`scripts/mtls_certs.py client NAME`; `curl --cacert certs/mtls/ca/ca.crt --cert ... --key ...`), the snippets that
call a service by name from inside a container need `verify=` and `cert=` (or `smo_shared.mtls.client_kwargs()`), and a notification
destination inside the stack (`so-smos`, `sa-smos`, the intent-handling steps) is written `https://` instead of `http://`. The
CI job `compose-mtls` replays the test file that way, which is the supported route; receivers outside the stack stay plain HTTP.

Sections §6–§22 are optional and independent of the rApp instance,
except where a step says it reuses an id from an earlier step. There is no
§9.

## What you'll onboard

`smo/samples/energy-saving-rapp.csar` — a valid CSAR package
(`TOSCA-Metadata/TOSCA.meta`, `Definitions/asd.yaml`, and the optional
root-level `manifest.yaml`/`capabilities.yaml` declaring its AI Platform
capabilities) for the EnergySaving_rApp, together with the rApp's own
source. Rebuild it with
`smo/samples/build_csar.py` after editing `smo/samples/energy-saving-rapp/`.
`tests_integration/test_cross_service.py::test_real_demo_csar_onboards_and_deploys`
onboards and deploys it.

## Why every step runs via `docker compose exec`

Only `r1-termination` publishes a host port (`8080:8000`). Every other
service (`onboarding`, `sme`, `dme`, `nfo`, `rapp-mgmt`, ...) is reachable
only inside the compose network, by hostname. So every command below runs
Python (with `httpx`) inside the `r1-termination` container:
`docker compose exec r1-termination python3 -c "..."`.

## 0. Start the stack

```bash
cd smo
scripts/init_secrets.sh   # once: creates the database password and enrollment secrets (no defaults)
docker compose up -d --build
docker compose ps   # confirm all services are healthy/running
```

## 1. Serve the rApp package on the compose network

Onboarding fetches the package over HTTP, so it needs a URL reachable
inside the network:

```bash
docker compose cp samples/energy-saving-rapp.csar r1-termination:/srv/scratch/energy-saving-rapp.csar
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch
```

`http://r1-termination:8899/energy-saving-rapp.csar` is now reachable from
every container.

## 2. Onboard the package

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://onboarding:8000/packages', json={
    'location': 'http://r1-termination:8899/energy-saving-rapp.csar',
})
print(r.status_code, r.json())
"
```

Note the `packageId`. Check its status (onboarding resolves synchronously):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://onboarding:8000/packages/<packageId>/onboarding-status')
print(r.json())
"
```

`state` is `AVAILABLE` and `nfDeploymentDescriptorId` is a UUID (NFO's
`CreateDescriptor`, called once validation passes). If `state` is `FAILED`,
the CSAR is malformed: re-run `python3 smo/samples/build_csar.py`, re-copy
it (step 1), and check that `TOSCA-Metadata/TOSCA.meta`/`Entry-Definitions` are present.

**A validation failure.** `_validate_package` rejects a byte-identical
package that is already onboarded (same content hash). Onboard the same
CSAR again:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://onboarding:8000/packages', json={
    'location': 'http://r1-termination:8899/energy-saving-rapp.csar',
})
print(r.status_code, r.json())
"
```

This still returns `202`: `OnboardPackage` never rejects synchronously; the
outcome is only visible via `onboarding-status`. Poll the new `packageId`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://onboarding:8000/packages/<secondPackageId>/onboarding-status')
print(r.json())
"
```

`state` is `FAILED` (the same `integrity_hash` already exists). None of the
second package's name, version, artifacts or `nfDeploymentDescriptorId` is
populated — `VALIDATE_FAILED` fires first. The first package is untouched
and still `AVAILABLE`.

## 3. Deploy it (CreateInstance)

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://rapp-mgmt:8000/instances', json={
    'packageId': '<packageId>', 'config': {},
})
print(r.status_code, r.json())
"
```

Note the `instanceId` and `oauthClientId` (the rAppId). Creating the
instance called NFO's `Instantiate`, which queried FOCOM's `/inventory` for
a cluster:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://focom:8000/inventory')
print(r.json())
"
```

The instance is now `DEPLOYING`, waiting for the (simulated) container to
bootstrap and call back.

## 4. Simulate the deployed container's bootstrap

This is the one call reachable from your host — the one R1 Termination
call an rApp container makes before it has a token:

```bash
curl -s http://localhost:8080/bootstrap | python3 -m json.tool
```

The response's `apiEndpoints` name `service-apis` and `published-apis`,
each with a `tokenEndPoint`/`apiEndPoint` pointing directly at SME (R1
requires a bearer token on every other route). From here on, continue with
`docker compose exec r1-termination` — those URIs are container-internal.

This package ships no `Files/Sme/` declarations, so `bootstrap-complete`
registers nothing with SME. (A package that does ship them has them
registered under the instance's own `oauthClientId`.) The manual
registrations below create the `energy-saving-rapp` producer/requester
identity that later sections use.

**Register as a provider (APF):**

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/provider-registrations', json={
    'apfId': 'energy-saving-rapp', 'providerDomainInfo': 'Energy Saving rApp — demo provider domain',
})
print(r.status_code, r.json())
"
```

**Register as an invoker.** As
in CAPIF, the client submits only its `apiInvokerPublicKey`; SME generates
`apiInvokerId` and `onboardingSecret`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/invoker-registrations', json={'apiInvokerPublicKey': 'demo-rapp-public-key'})
print(r.status_code, r.json())
"
```

Note the returned `apiInvokerId` and `onboardingSecret` — later SME calls
(and §12) use them.

**Obtain an OAuth2 token:**

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/oauth2/token', json={
    'grant_type': 'client_credentials', 'client_id': '<apiInvokerId>',
    'client_secret': '<onboardingSecret>',
})
print(r.status_code, r.json())
"
```

**Publish the service API:**

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/published-apis/v1/energy-saving-rapp/service-apis', json={
    'serviceName': 'energy-saving-api', 'producerId': 'energy-saving-rapp',
    'endpoint': 'http://energy-saving-rapp:8080/energy-saving/v1', 'version': 'v1',
    'fullApiVersions': ['v1'], 'moduleScope': 'energy-saving-rapp',
})
print(r.status_code, r.json())
"
```

Note the returned `serviceId` (§21 uses it as `<energySavingServiceId>`).

**Register as a DME producer (optional):**

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://dme:8000/production-capabilities', json={
    'namespace': 'demo', 'name': 'energy-saving-metrics', 'version': '1.0',
    'typeName': 'energy-saving-metrics-v1', 'producerId': 'energy-saving-rapp',
    'dataProductionSchema': {'type': 'object', 'properties': {'greeting': {'type': 'string'}}},
    'producerHealthCallbackUrl': 'http://energy-saving-rapp:8080/health',
    'jobCallbackUrl': 'http://energy-saving-rapp:8080/dme-jobs',
})
print(r.status_code, r.json())
"
```

## 5. Complete bootstrap — DEPLOYING → RUNNING

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://rapp-mgmt:8000/instances/<instanceId>/bootstrap-complete')
print(r.status_code, r.json())
"
```

`state` is now `RUNNING`. Confirm it; `smeServiceIds` is empty, because this
package bundles no `Files/Sme/` declarations for bootstrap-complete to
register:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://rapp-mgmt:8000/instances/<instanceId>')
print(r.json())
"
```

## 6. Operate (optional) — a performance/fault report

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://rapp-mgmt:8000/instances/<instanceId>/performance', json={'cellsAsleep': 2, 'prbSavedPercent': 12.5})
print(r.status_code, r.json())
"
```

## 7. RAN NF OAM closed-loop (optional) — a real CM write and fault lifecycle

A managed RAN function being reconfigured and reporting a fault. Register a
managed element behind the mock O1 Adaptor (`mock-o1-adaptor:8000/edit-config`,
this build's NETCONF-shaped test double for an O1 network element):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-nf-oam:8000/o1-adaptor-endpoints', json={
    'managedElementRef': 'demo-o-du-1', 'adaptorUri': 'http://mock-o1-adaptor:8000/edit-config',
    'protocolSupport': ['NETCONF'], 'o1Protocol': 'NETCONF', 'entityType': 'O-DU',
})
print(r.status_code, r.json())
"
```

Note the `endpointId`; health starts `DISCOVERED`. Heartbeat it to `ACTIVE`
(a real O1 Adaptor would do this on its own timer):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-nf-oam:8000/o1-adaptor-endpoints/<endpointId>/heartbeat')
print(r.status_code, r.json())
"
```

**Dispatch a CM write.** `WriteConfigurationChanges` sends a NETCONF
`<edit-config>` RPC to the mock O1 Adaptor (`netconf_client.py`):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-nf-oam:8000/config-jobs', json={
    'requestedBy': 'energy-saving-rapp', 'scope': 'cell',
    'changes': [{'managedElementRef': 'demo-o-du-1', 'attributeChanges': {'adminState': 'UNLOCKED'}}],
})
print(r.status_code, r.json())
"
```

Note the `jobId`. Confirm the sub-change reached the adaptor and applied:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://ran-nf-oam:8000/config-jobs/<jobId>')
print(r.status_code, r.json())
r2 = httpx.get('http://mock-o1-adaptor:8000/edit-config/demo-o-du-1')
print(r2.status_code, r2.json())
"
```

`status` is `COMPLETED`, the sub-change `APPLIED`, and the mock adaptor's
record shows the applied attribute change.

**A partial failure.** A multi-ME request is decomposed into per-ME
sub-changes whose outcomes are aggregated (RAN NF OAM LLD section 5.1). A
batch touching one registered ME and one never-registered ME:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-nf-oam:8000/config-jobs', json={
    'requestedBy': 'energy-saving-rapp', 'scope': 'cell',
    'changes': [
        {'managedElementRef': 'demo-o-du-1', 'attributeChanges': {'adminState': 'LOCKED'}},
        {'managedElementRef': 'demo-o-du-2-never-registered', 'attributeChanges': {'adminState': 'LOCKED'}},
    ],
})
print(r.status_code, r.json())
r2 = httpx.get(f'http://ran-nf-oam:8000/config-jobs/{r.json()[\"jobId\"]}')
print(r2.status_code, r2.json())
"
```

The job's `status` is `PARTIAL_SUCCESS`. `subChanges` shows `demo-o-du-1`
`APPLIED` (the RPC fired again, setting `adminState` back to `LOCKED`) and
`demo-o-du-2-never-registered` `REJECTED` with
`rejectionReason: ENDPOINT_UNREACHABLE` — no `O1AdaptorEndpoint` exists for
it.

**Raise, acknowledge, and clear a fault alarm** on the same ME:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-nf-oam:8000/alarms/ingest', params={
    'source_alarm_id': 'demo-alarm-1', 'managed_element_ref': 'demo-o-du-1',
    'severity': 'major', 'alarm_type': 'EQUIPMENT_ALARM',
})
print(r.status_code, r.json())
"
```

Note the `alarmId`, then acknowledge and clear it:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.patch('http://ran-nf-oam:8000/alarms/<alarmId>/ack', params={'new_state': 'ACKNOWLEDGED', 'ack_user_id': 'demo-operator'})
print(r.status_code, r.json())
r2 = httpx.patch('http://ran-nf-oam:8000/alarms/<alarmId>/clear', params={'clear_user_id': 'demo-operator'})
print(r2.status_code, r2.json())
"
```

The cleared alarm's `severity` is `cleared`, with
`ackUserId`/`clearUserId`/`changedAt` populated.

## 8. FOCOM resource management (optional) — provision, subscribe, observe a real notification

FOCOM's O2IMS inventory subscription: subscribe to changes for a resource
type, then provision and deprovision a resource of that type. Subscribe:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://focom:8000/inventory/subscriptions', json={
    'callback': 'http://demo-consumer:9000/inventory-events',
    'resourceTypeId': 'gpu-l40', 'consumerSubscriptionId': 'demo-sub-1',
})
print(r.status_code, r.json())
"
```

Note the `subscriptionId`. Provision a matching resource:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://focom:8000/resources/provision', json={'resourceTypeId': 'gpu-l40', 'description': 'demo GPU node'})
print(r.status_code, r.json())
"
```

`resourceId` is a persisted `Resource` row; confirm with the pool drill-down:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://focom:8000/resource-pools/pool-0/resources')
print(r.status_code, r.json())
"
```

Provisioning fired a `CREATE` notification (`_notify_inventory_subscribers`).
`focom`'s logs show the delivery attempt to
`http://demo-consumer:9000/inventory-events`. No listener exists at that
address in this stack, so the attempt fails DNS resolution and is dropped;
delivery is best-effort and never surfaces as a 500 to the caller.
`tests_integration/test_demo_runbook.py` intercepts the outbound
`httpx.post` and asserts its URL and body. The same pattern applies to
every `demo-consumer:9000` callback in this runbook: watch the named
service's logs for the attempt.

Deprovision it — this fires a matching `DELETE` notification:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://focom:8000/resources/<resourceId>')
print(r.status_code, r.json())
"
```

**FOCOM FCAPS** — infrastructure/O-Cloud alarms and performance, distinct
from RAN NF OAM's RAN-function alarms (NFO+FOCOM LLD section 1). Ingest an
infrastructure alarm against the Phase 1 cluster:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://focom:8000/alarms/ingest', params={'resource_ref': 'phase1-degenerate-cluster', 'severity': 'critical'})
print(r.status_code, r.json())
"
```

Confirm it's queryable:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://focom:8000/alarms')
print(r.status_code, r.json())
"
```

Performance metrics are queryable too, filterable by `resource_ref`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://focom:8000/performance')
print(r.status_code, r.json())
"
```

This returns `[]` in a fresh stack: FOCOM collects nothing, records are
ingested (`POST /performance/ingest`, an O2-IMS
`PerformanceMeasurementRecord`). Play the O-Cloud's collector for the node
utilisation the console shows (PR-GUI-9.8b, the convention is in
`focom/README.md` 1.2): a `CPU_UTILIZATION` and a `MEMORY_UTILIZATION`
record, each a percentage, for the Phase 1 cluster:

```bash
docker compose exec r1-termination python3 -c "
import httpx
for name, value in (('CPU_UTILIZATION', 63.5), ('MEMORY_UTILIZATION', 41.0)):
    r = httpx.post('http://focom:8000/performance/ingest', json={
        'resourceId': 'phase1-degenerate-cluster', 'performanceMeasurementDefinitionId': name, 'measurementValue': value})
    print(r.status_code, r.json())
r = httpx.get('http://focom:8000/resources/phase1-degenerate-cluster/utilisation')
print(r.status_code, r.json())
"
```

The read answers `cpuPercent` 63.5 and `memoryPercent` 41.0 with the
collection time; `GET /focom/utilisation?resource_ids=a,b` reads up to 100
nodes at once (nulls for a node nothing reported).

## 10. Intent Service automation (optional) — register, address, dispatch, retract

Intent Service's Intent-to-RMIH dispatch with consumer-side selection
(`docs/ARCHITECTURE.md (Intent Service)`): an SMO-internal RAN
Management Intent Handler (RMIH) declares what it can fulfil, and an rApp
addresses its Intent at one registered RMIH by `rmihId`
(`TS28312_IntentNrm.yaml`: `IntentHandlingFunction` contains `Intent`).

Register an RMIH. Per D-SEC-POLICY-1 only an SMO-internal module may hold
an `rmihId` (an rApp UUID is rejected), so this uses `so-smos`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://intent-service:8000/intent-handling-functions', json={
    'rmihId': 'so-smos', 'smeServiceId': 'so-smos-svc',
    'intentHandlingCapabilityList': [{'intentHandlingCapabilityId': 'ran-energy', 'supportedExpectationObjectType': 'RAN_SUBNETWORK', 'supportedExpectationTargetInfoList': [{'supportedTargetName': 'RANEnergyConsumption'}]}],
    'notificationDestination': 'http://so-smos:8000/intents/notify',
    'intentHandlingScope': ['RAN'],
})
print(r.status_code, r.json())
"
```

Create an Intent addressed at `so-smos`, whose
`expectationObject.objectType` matches that RMIH's declared capability:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://intent-service:8000/intents', json={
    'userLabel': 'demo energy intent', 'intentReportControl': [{'observationPeriod': 60}], 'intentExpectations': [{'expectationId': 'e1', 'expectationVerb': 'DELIVER', 'expectationObject': {'objectType': 'RAN_SUBNETWORK'}, 'expectationTargets': [{'targetName': 'RANEnergyConsumption', 'targetCondition': 'IS_LESS_THAN', 'targetValueRange': 500}]}],
    'rmioId': 'energy-saving-rapp', 'rmihId': 'so-smos', 'intentHandlingScope': 'RAN',
})
print(r.status_code, r.json())
"
```

`CreateIntent` checks that `so-smos` declares a matching capability and
covers the requested `intentHandlingScope` (otherwise 422
`RMIH_CAPABILITY_MISMATCH`), then dispatches a notification to
`so-smos:8000/intents/notify`. Nothing accepts it there yet (best-effort),
so watch `intent-service`'s logs. The integration test asserts the
dispatch's `intentId`/`expectationObjectTypes` payload.

Note the `intentId`, then confirm the persisted Intent:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://intent-service:8000/intents/<intentId>')
print(r.status_code, r.json())
"
```

**A negative case.** Register a second RMIH with the same capability
(`RAN_SUBNETWORK`) but a `CN`-only scope:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://intent-service:8000/intent-handling-functions', json={
    'rmihId': 'sa-smos', 'smeServiceId': 'sa-smos-svc',
    'intentHandlingCapabilityList': [{'intentHandlingCapabilityId': 'ran-energy', 'supportedExpectationObjectType': 'RAN_SUBNETWORK', 'supportedExpectationTargetInfoList': [{'supportedTargetName': 'RANEnergyConsumption'}]}],
    'notificationDestination': 'http://sa-smos:8000/intents/notify',
    'intentHandlingScope': ['CN'],
})
print(r.status_code, r.json())
"
```

Address a `RAN`-scoped Intent at `sa-smos`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://intent-service:8000/intents', json={
    'userLabel': 'demo energy intent', 'intentReportControl': [{'observationPeriod': 60}], 'intentExpectations': [{'expectationId': 'e1', 'expectationVerb': 'DELIVER', 'expectationObject': {'objectType': 'RAN_SUBNETWORK'}, 'expectationTargets': [{'targetName': 'RANEnergyConsumption', 'targetCondition': 'IS_LESS_THAN', 'targetValueRange': 500}]}],
    'rmioId': 'energy-saving-rapp', 'rmihId': 'sa-smos', 'intentHandlingScope': 'RAN',
})
print(r.status_code, r.json())
"
```

Rejected with `422 RMIH_CAPABILITY_MISMATCH` before any Intent row is
created or any dispatch attempted: the matching capability is not enough
when the scope does not match. Create a second Intent addressed at
`so-smos`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://intent-service:8000/intents', json={
    'userLabel': 'demo energy intent', 'intentReportControl': [{'observationPeriod': 60}], 'intentExpectations': [{'expectationId': 'e1', 'expectationVerb': 'DELIVER', 'expectationObject': {'objectType': 'RAN_SUBNETWORK'}, 'expectationTargets': [{'targetName': 'RANEnergyConsumption', 'targetCondition': 'IS_LESS_THAN', 'targetValueRange': 500}]}],
    'rmioId': 'energy-saving-rapp', 'rmihId': 'so-smos', 'intentHandlingScope': 'RAN',
})
print(r.status_code, r.json())
"
```

`intent-service`'s logs show exactly one further delivery attempt, to
`so-smos:8000/intents/notify`, and none to `sa-smos:8000/intents/notify`.

Retract both Intents, then deregister both RMIHs:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://intent-service:8000/intents/<intentId>')
print(r.status_code)
r2 = httpx.delete('http://intent-service:8000/intents/<secondIntentId>')
print(r2.status_code)
r3 = httpx.delete('http://intent-service:8000/intent-handling-functions/so-smos')
print(r3.status_code)
r4 = httpx.delete('http://intent-service:8000/intent-handling-functions/sa-smos')
print(r4.status_code)
"
```

## 11. (removed) A1 Policy Management

This step registered an A1 service, created a policy at the mock Near-RT RIC and observed its status notification. A1, the Near-RT RIC and E2 are out of scope for this build and the module and its mock were removed (`CHANGELOG.md`, release 0.5.0); the other steps keep their numbers.

## 12. SME Trusted Invokers (optional) — register, query, revoke a real security context

CAPIF's per-AEF security context (`capifcore/internal/securityservice/security.go`),
separate from OAuth2 token issuance: an AEF consults it directly.

Reuse the `apiInvokerId` from step 4 (the invoker must already be
onboarded). Register a security context:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.put('http://sme:8000/trusted-invokers/<apiInvokerId>', json={
    'notificationDestination': 'http://demo-consumer:9000/security-notify',
    'securityInfo': [{'aefId': 'energy-saving-rapp', 'apiId': 'energy-saving-api', 'authenticationInfo': 'demo-auth-info',
                       'authorizationInfo': 'demo-authz-info', 'prefSecurityMethods': ['OAUTH']}],
})
print(r.status_code, r.json())
"
```

`201`; `selSecurityMethod` is the invoker's first preferred method (no
per-AEF security-method catalog exists to cross-check). Query it back —
`authenticationInfo`/`authorizationInfo` are redacted by default:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://sme:8000/trusted-invokers/<apiInvokerId>')
print(r.status_code, r.json())
"
```

Both fields are empty strings. Ask for them explicitly:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://sme:8000/trusted-invokers/<apiInvokerId>', params={'authentication_info': True, 'authorization_info': True})
print(r.status_code, r.json())
"
```

The real values come back. **Revoke** the context for this one AEF (a
partial removal):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/trusted-invokers/<apiInvokerId>/delete', json={
    'aefId': 'energy-saving-rapp', 'apiIds': ['energy-saving-api'], 'apiInvokerId': '<apiInvokerId>', 'cause': 'UNEXPECTED_REASON',
})
print(r.status_code)
"
```

That was the only `securityInfo` entry, so the whole trusted-invoker
record is gone — confirm with a 404:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://sme:8000/trusted-invokers/<apiInvokerId>')
print(r.status_code, r.json())
"
```

## 13. AI Platform: MLMR + AIMgF + MLLF (optional) — register, train, upload/download a real artifact, advance to ACTIVE, deploy

Three services, calling each other through R1 Termination
(`docs/ARCHITECTURE.md`): MLMR (model repository),
AIMgF (lifecycle orchestration, owns the MLModel FSM), MLLF
(loading/deployment). Artifact bytes round-trip through Postgres.

Register a model (MLMR):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mlmr:8000/models', json={
    'modelType': 'energy-saving-anomaly-detector', 'version': '1.0.0',
    'description': 'Demo anomaly-detection model for the Energy Saving rApp',
    'author': 'energy-saving-rapp', 'owner': 'energy-saving-rapp',
    'inputDataType': 'application/json', 'outputDataType': 'application/json',
})
print(r.status_code, r.json())
"
```

Note the `modelId`; `state` is `REGISTERED`. Request training (AIMgF) — an
FSM transition (`REGISTERED -> TRAINING`, `TRAIN`), written to MLMR's row
over R1:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://aimgf:8000/training-jobs', json={
    'modelId': '<modelId>', 'producerId': 'energy-saving-rapp',
    'runId': 'demo-run-1', 'trainingDataset': 's3://demo/energy-saving-train',
    'validationDataset': 's3://demo/energy-saving-val',
})
print(r.status_code, r.json())
"
```

Note the `trainingJobId`. Confirm the model moved to `TRAINING` (MLMR):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://mlmr:8000/models/<modelId>')
print(r.status_code, r.json())
"
```

**Upload a model artifact** (MLMR) — stored in a Postgres-backed
`ModelArtifact` row (S3 storage is not included):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mlmr:8000/models/<modelId>/artifact',
                files={'file': ('energy-saving-model.zip', b'demo-model-weights-bytes', 'application/zip')})
print(r.status_code, r.json())
"
```

Note `artifactVersion` (1). Write training metrics (AIMgF, whole-body
replace):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://aimgf:8000/training-jobs/<trainingJobId>/model-metrics', json={'accuracy': 0.94, 'f1Score': 0.91})
print(r.status_code, r.json())
"
```

**Drive the model through its governed lifecycle** (AIMgF). Each stage
completes through its job route, and the operator gates and governance
decisions name who decided (call flow 26):

```bash
docker compose exec r1-termination python3 -c "
import httpx
A, M, who = 'http://aimgf:8000', '<modelId>', {'decided_by': 'noc-operator'}
def ok(r): assert r.status_code < 300, (r.status_code, r.text); return r.json()
ok(httpx.post(f'{A}/training-jobs/<trainingJobId>/complete', json={'succeeded': True, 'metrics': {'accuracy': 0.94}}))
ok(httpx.post(f'{A}/models/{M}/advance', params={'event': 'APPROVE_TRAINING', **who}))
v = ok(httpx.post(f'{A}/validation-jobs', json={'modelId': M, 'producerId': 'energy-saving-rapp'}))
ok(httpx.post(f'{A}/validation-jobs/{v[\"validationJobId\"]}/complete', json={'succeeded': True, 'metrics': {}}))
ok(httpx.post(f'{A}/models/{M}/advance', params={'event': 'APPROVE_VALIDATION', **who}))
e = ok(httpx.post(f'{A}/emulation-jobs', json={'modelId': M, 'producerId': 'energy-saving-rapp'}))
ok(httpx.post(f'{A}/emulation-jobs/{e[\"emulationJobId\"]}/complete', json={'succeeded': True, 'metrics': {}}))
for event in ['SUBMIT_FOR_APPROVAL', 'APPROVE', 'CERTIFY', 'PROMOTE']:
    ok(httpx.post(f'{A}/models/{M}/advance', params={'event': event, **who}))
print(ok(httpx.get(f'{A}/models/{M}/lifecycle'))['modelLifecycleState'])
"
```

The ending state is `PROMOTED`. A job-driven event posted to `advance`
(for example `TRAINING_COMPLETE`) is refused with 422 and names the job
route to use. **Deploy the model** (MLLF) — stamps
`clearedNodeGroups` onto MLMR's row (LLD section 5):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mllf:8000/models/<modelId>/deploy', json=['edge-gpu-a', 'edge-gpu-b'])
print(r.status_code, r.json())
"
```

Download the artifact (MLMR) and confirm the bytes match the upload:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://mlmr:8000/models/<modelId>/artifact/1')
print(r.status_code, r.content == b'demo-model-weights-bytes')
"
```

Deregister (MLMR). Postgres `ON DELETE CASCADE` removes the artifact row
and AIMgF's training-job/inference-job/subscription rows:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://mlmr:8000/models/<modelId>')
print(r.status_code)
"
```

## 14. RAN Analytics + MDAF (optional) — register a producer, subscribe, publish a real report

Producer registration stays in `ran-analytics`; report publishing and
subscriptions live in **MDAF**. Producer registration does the same
two-step CAPIF sequence as step 4 (SME provider enrolment, then service
publish).

Register an analytics producer (RAN Analytics):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://ran-analytics:8000/producers',
                params={'producer_id': 'energy-saving-rapp', 'analytics_type': 'coverage-issue-analysis'},
                json={'dme_input_types': [], 'output_schema': {'type': 'object', 'properties': {'issue': {'type': 'string'}}}})
print(r.status_code, r.json())
"
```

`energy-saving-rapp` was SME-enrolled in step 4; this re-registers the same
provider (idempotent) and publishes a second service
(`mdaf.coverage-issue-analysis`). Confirm the registration:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://ran-analytics:8000/producers', params={'analytics_type': 'coverage-issue-analysis'})
print(r.status_code, r.json())
"
```

Subscribe, with a notification destination (MDAF):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mdaf:8000/subscriptions', params={
    'analytics_type': 'coverage-issue-analysis', 'requested_by': 'sa-smos',
}, json={'notificationDestination': 'http://demo-consumer:9000/analytics-reports'})
print(r.status_code, r.json())
"
```

Note the `subscriptionId`, then publish a report (MDAF):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mdaf:8000/reports', params={'analytics_type': 'coverage-issue-analysis'},
                json={'output': {'issue': 'demo-cell-1 coverage hole detected'}, 'input_sources': []})
print(r.status_code, r.json())
"
```

`publish_report` notifies `http://demo-consumer:9000/analytics-reports` —
watch `mdaf`'s logs. The integration test asserts the `reportId`/`output`
payload. Confirm the report is queryable:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://mdaf:8000/reports', params={'analytics_type': 'coverage-issue-analysis'})
print(r.status_code, r.json())
"
```

Unsubscribe:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://mdaf:8000/subscriptions/<subscriptionId>')
print(r.status_code)
"
```

## 15. SA SMOS (optional) — a real assurance monitor, a genuine `RECONNECT` heal, and `ROLLBACK` scoping

SA SMOS's remedial-action dispatch (SO/SA SMOS LLD section 2.1).
`RECONNECT` needs a `RUNNING` `NFDeployment`. The Energy Saving rApp's deployment
can't be reused (NFO deploys a descriptor only once), so this creates a
second deployment of the same package through SO SMOS.

Create a second `NFDeploymentDescriptor` for the package from step 2
(descriptors are not unique per package; only deploying one twice is
rejected):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://nfo:8000/descriptors', json={
    'packageId': '<packageId>', 'name': 'sa-smos-demo-descriptor',
})
print(r.status_code, r.json())
"
```

Note the new `nfDeploymentDescriptorId`, then submit an SO SMOS order with
a single `DEPLOY` step targeting it (reaches NFO's `Instantiate`):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://so-smos:8000/orders', json={
    'scope': 'sa-smos-demo-deploy',
    'steps': [
        {'stepType': 'DEPLOY', 'targetModule': 'NFO', 'nfDeploymentDescriptorId': '<newDescriptorId>', 'name': 'sa-smos-demo-deployment'},
    ],
})
print(r.status_code, r.json())
"
```

The step is `COMPLETED`; its `result` carries an `nfDeploymentId` in state
`RUNNING`. Note the `orderId`.

Register an `AssuranceMonitor` scoped to that order:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors', params={'target_order_id': '<orderId>'}, json={'latency': 100})
print(r.status_code, r.json())
"
```

Note the `monitorId`. Evaluate it against a metrics sample that breaches
the threshold:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors/<monitorId>/evaluate', json={'latency': 80})
print(r.status_code, r.json())
"
```

`breaches` shows `{'latency': 100}`.

**`RECONNECT`.** `ExecuteRemedialAction` resolves the monitor's
`target_order_id` to an `nfDeploymentId` by reading the SO SMOS order
(`_resolve_deployed_nf`, the `DEPLOY` step's `COMPLETED` result) and calls
NFO's `Heal`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors/<monitorId>/remedial-actions', params={'action_type': 'RECONNECT'})
print(r.status_code, r.json())
"
```

`outcome` is `RESOLVED`; NFO's `Heal` fired (idempotent from `RUNNING` —
see `heal()` in `nfo/app/main.py`) and recorded an `LCMOperation` row.

**`ROLLBACK` needs a rApp instance.** Only rApp Management keeps a version
history (its committed upgrades); NFO keeps none for a bare NF deployment
like this order's. On this order-scoped monitor `ROLLBACK` is refused:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors/<monitorId>/remedial-actions', params={'action_type': 'ROLLBACK'})
print(r.status_code, r.json())
"
```

`409`, with `detail.title` = `ROLLBACK_HISTORY_UNAVAILABLE` and an
explanation. A monitor registered with `target_rapp_instance_id` instead
rolls that rApp back to the version it ran before its last upgrade: SA SMOS
calls rApp Management's `POST /instances/{id}/rollback`, an upgrade back to
the previous package and configuration, resolved like any upgrade
(`upgrade/resolve`). It needs an instance that has been upgraded at least
once, so it is not repeated here; call flows 04 and 07 show it, and
`GET /rapp-mgmt/instances/{id}/versions` lists an instance's history.

Retire the second deployment — NFO's `Terminate` (completes synchronously
in Phase 1):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://nfo:8000/deployments/<nfDeploymentId>')
print(r.status_code)
"
```

## 16. SO SMOS (optional) — a real multi-step order, fail-fast, cancel

SO SMOS dispatches an order's steps in sequence to the module each
`stepType`/`targetModule` pair maps to, over R1 (SO/SA SMOS LLD section 1).
It is **fail-fast** (section 1.1): the first failed step halts the order,
later steps stay `PENDING`, and completed steps are not rolled back.

Submit a 3-step order: a `FOCOM` provision, a `DEPLOY` step naming an NF
deployment descriptor NFO doesn't know, and a `TRAINING` step that is never
reached:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://so-smos:8000/orders', json={
    'scope': 'demo-multi-step-order',
    'steps': [
        {'stepType': 'INFRA', 'targetModule': 'FOCOM', 'spec': {'resourceTypeId': 'gpu-l40', 'description': 'SO SMOS provisioned node'}},
        {'stepType': 'DEPLOY', 'targetModule': 'NFO', 'nfDeploymentDescriptorId': '00000000-0000-0000-0000-000000000000', 'name': 'no-such-descriptor'},
        {'stepType': 'TRAINING', 'targetModule': 'AI_ML_WORKFLOW', 'producerId': 'energy-saving-rapp'},
    ],
})
print(r.status_code, r.json())
"
```

The response shows all three outcomes: step 1 `COMPLETED` (a new FOCOM
`Resource` row), step 2 `FAILED` (NFO's `NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND`,
surfaced as a `DownstreamError`, distinct from a transport failure), and
step 3 `PENDING` — `AI_ML_WORKFLOW` was never dispatched to. Note the
`orderId`, then confirm the persisted state:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://so-smos:8000/orders/<orderId>')
print(r.status_code, r.json())
"
```

**Cancel** the order. The `PENDING` step becomes `CANCELLED`; the
`COMPLETED`/`FAILED` steps are unchanged:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://so-smos:8000/orders/<orderId>/cancel')
print(r.status_code, r.json())
"
```

## 17. DME type subscriptions (optional) — notify a consumer when a type is registered or removed

DME's type subscriptions (ICS's `/info-type-subscription`): a consumer is
notified whenever any `DmeType` is registered or removed, unfiltered (as
in ICS).

Subscribe:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://dme:8000/type-subscriptions', json={
    'notificationDestination': 'http://demo-consumer:9000/dme-type-events', 'owner': 'energy-saving-rapp',
})
print(r.status_code, r.json())
"
```

Note the `subscriptionId`. Register a new DME type; `register_dme_type`
sends a `REGISTERED` notification to every subscriber
(`_notify_type_subscribers`):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://dme:8000/production-capabilities', json={
    'namespace': 'demo', 'name': 'dme-type-sub-demo', 'version': '1.0',
    'typeName': 'dme-type-sub-demo-v1', 'producerId': 'energy-saving-rapp',
    'dataProductionSchema': {'type': 'object', 'properties': {'reading': {'type': 'number'}}},
    'producerHealthCallbackUrl': 'http://energy-saving-rapp:8080/health',
    'jobCallbackUrl': 'http://energy-saving-rapp:8080/dme-jobs',
})
print(r.status_code, r.json())
"
```

Watch `dme`'s logs for a POST to `http://demo-consumer:9000/dme-type-events`
with `{infoTypeId, jobDataSchema, status: "REGISTERED"}`.

Deregister the producer. Producer and Type are separate entities (as in
ICS), so `deregister_producer` removes only `energy-saving-rapp`; its types
stay registered but `DISABLED`, and no notification fires (ICS's
`deleteInfoProducer` never touches info-types):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://dme:8000/production-capabilities', params={'producer_id': 'energy-saving-rapp'})
print(r.status_code)
r = httpx.get('http://dme:8000/dme-types')
print([(t['typeName'], t['producerIds'], t['typeStatus']) for t in r.json()])
"
```

`dme-type-sub-demo` is still listed (`producerIds: []`,
`typeStatus: DISABLED`), as is `energy-saving-metrics` from step 4. Only
`delete_dme_type` (ICS's `DELETE /info-types/{id}`) removes a type, and
only once no producer remains (409 otherwise); it fires the
`DEREGISTERED` notification:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://dme:8000/dme-types/<dmeTypeId>')  # the dme-type-sub-demo one, from its own registrationId above
print(r.status_code)
"
```

Watch `dme`'s logs for a POST with
`{infoTypeId, jobDataSchema, status: "DEREGISTERED"}`. Unsubscribe:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://dme:8000/type-subscriptions/<subscriptionId>')
print(r.status_code)
"
```

## 18. FOCOM topology export (optional) — TEIV entities and relationships from real inventory rows

`GET /topology` exports FOCOM's `ResourceType`/`ResourcePool`/
`DeploymentManager`/`Resource` rows in the TEIV wire shape
(`o-ran-smo-teiv-cloud:<EntityType>` keys, `{id, attributes}` for entities,
`{id, aSide, bSide, sourceIds}` for relationships). It is a pull-based
substitute for a Kafka-fed `focom-to-teiv-adapter`; this build has no
message broker.

By now FOCOM holds the `gpu-l40` `Resource` from §16's `INFRA` step (never
deprovisioned):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://focom:8000/topology')
print(r.status_code, r.json())
"
```

`entities` include a `ResourceType` for `generic` (the seeded type) and
`gpu-l40` (auto-registered when §16 provisioned against it), a
`ResourcePool`, a `DeploymentManager`, and at least one `Resource`.
`relationships` come only from real foreign keys:
`RESOURCE_IS_OF_TYPE_RESOURCETYPE` and `RESOURCE_CONTAINED_IN_RESOURCEPOOL`
for every `Resource`, plus `RESOURCE_CHILD_OF_RESOURCE` for any with a
`parentId` (none in this topology, so that key is absent).

## 19. AIMgF feature groups (optional) — register, list, a real duplicate-name rejection

The reference's `CreateFeatureGroup` (`featuregroup_controller.py`):
registration, listing, name validation and duplicate-name rejection.
Feature-store queries are not included. With `enableDme` and a `dmeTypeId`,
creating a group also creates its DME data job (`dmeDataJobId` in the
answer), and `DELETE /feature-groups/{name}` terminates it.

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://aimgf:8000/feature-groups', json={
    'featureGroupName': 'demo_coverage_features', 'featureList': 'rsrp,rsrq,sinr',
    'datalakeSource': 'INFLUX', 'host': 'influx.demo', 'port': '8086', 'bucket': 'demo-bucket',
    'tokenRef': 'demo-lake-token', 'dbOrg': 'demo-org', 'measurement': 'coverage_metrics',
})
print(r.status_code, r.json())
"
```

The data-lake token is given by reference (`tokenRef`, the name of a secret the AIMgF service resolves itself, `docs/SECRETS.md`); the answer shows `tokenSet: true` and `tokenRef`, never a token (`SEC-15.2`). The clear-text `token` field is deprecated.

Note the `featureGroupId`. Confirm it's listed:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://aimgf:8000/feature-groups')
print(r.status_code, r.json())
"
```

**A duplicate-name rejection.** Register the same `featureGroupName` again
(a `UniqueConstraint`, matching the reference's "already exist"
`DBException`):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://aimgf:8000/feature-groups', json={
    'featureGroupName': 'demo_coverage_features', 'featureList': 'rsrp,rsrq,sinr',
    'datalakeSource': 'INFLUX', 'host': 'influx.demo', 'port': '8086', 'bucket': 'demo-bucket',
    'tokenRef': 'demo-lake-token', 'dbOrg': 'demo-org', 'measurement': 'coverage_metrics',
})
print(r.status_code, r.json())
"
```

`409`, `detail.title` = `FEATURE_GROUP_ALREADY_REGISTERED`. An invalid name
is rejected too (the reference's `\w+`, 3–63 character rule, shared with
`TrainingJob` names):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://aimgf:8000/feature-groups', json={
    'featureGroupName': 'no spaces allowed', 'featureList': 'rsrp', 'datalakeSource': 'INFLUX',
    'host': 'influx.demo', 'port': '8086', 'bucket': 'demo-bucket', 'tokenRef': 'demo-lake-token',
    'dbOrg': 'demo-org', 'measurement': 'coverage_metrics',
})
print(r.status_code, r.json())
"
```

`400`, `detail.title` = `FEATURE_GROUP_NAME_INVALID`.

## 20. SA SMOS coordination-group remedial action (optional) — a real group retrain, `actionType`-independent

A coordination-group-scoped `AssuranceMonitor` ignores the NF-deployment
meanings of `CONFIG_CHANGE`/`SCALE`/`RECONNECT`/`ROLLBACK` and always
dispatches a group retrain via AIMgF's `RequestTraining`, whatever
`actionType` is requested. (§15's monitor was `targetOrderId`-scoped.)

Register two models as the group's members (MLMR). A group needs at least
two members; the migration's `member_model_ids` CHECK constraint
(`array_length >= 2`) enforces this:

```bash
docker compose exec r1-termination python3 -c "
import httpx
for i in range(2):
    r = httpx.post('http://mlmr:8000/models', json={
        'modelType': f'demo-coordination-group-model-{i}', 'version': '1.0.0',
        'author': 'energy-saving-rapp', 'owner': 'energy-saving-rapp',
    })
    print(r.status_code, r.json())
"
```

Note both `modelId`s, then create a coordination group (MLMR):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://mlmr:8000/coordination-groups', json={'memberModelIds': ['<modelId1>', '<modelId2>']})
print(r.status_code, r.json())
"
```

A single-member `memberModelIds` returns 422 `COORDINATION_GROUP_TOO_SMALL`.

Note the `groupId`, then register an `AssuranceMonitor` scoped to it
(`targetCoordinationGroupId`, not `targetOrderId`):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors', params={'target_coordination_group_id': '<groupId>'}, json={})
print(r.status_code, r.json())
"
```

**Execute a remedial action.** For an order-scoped monitor, `SCALE` always
ends `ESCALATED` (NFO's Phase 1 stub); this monitor is group-scoped, so
that branch is never reached:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sa-smos:8000/monitors/<monitorId>/remedial-actions', params={'action_type': 'SCALE'})
print(r.status_code, r.json())
"
```

`outcome` is `RESOLVED`: SA SMOS sent `POST /aimgf/training-jobs` with the
group's `modelCoordinationGroupId` (AIMgF's `groupRetrainTriggered`
mechanism). Confirm the `TrainingJob` row:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.get('http://aimgf:8000/training-jobs', params={'status': 'IN_PROGRESS'})
print(r.status_code, [j for j in r.json() if j['modelCoordinationGroupId'] == '<groupId>'])
"
```

A `TrainingJob` with `producerId: sa-smos` and this
`modelCoordinationGroupId`.

## 21. SME event-subscription `apiId` filtering (optional) — a subscriber scoped to one service, not every service

SME's `SubscribeEvents` (the reference's `CAPIFEventFilter`,
`eventservice.go`'s `getMatchingSubs`) filters by `eventTypes` and by
`apiIds`, which map onto this build's `serviceId`, as well as by
`aefIds` and `apiInvokerIds` (the last for the `API_INVOKER_*` events).

Subscribe two consumers: `consumer-unscoped` gets every
`SERVICE_API_UPDATE`; `consumer-scoped` only `energy-saving-api`'s (its
`serviceId` from step 4):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/capif-events/v1/consumer-unscoped/subscriptions', json={
    'subscriberId': 'consumer-unscoped', 'eventTypes': ['SERVICE_API_UPDATE'],
    'callbackUri': 'http://demo-consumer:9000/sme-events-unscoped',
})
print(r.status_code, r.json())
r = httpx.post('http://sme:8000/capif-events/v1/consumer-scoped/subscriptions', json={
    'subscriberId': 'consumer-scoped', 'eventTypes': ['SERVICE_API_UPDATE'],
    'callbackUri': 'http://demo-consumer:9000/sme-events-scoped', 'apiIds': ['<energySavingServiceId>'],
})
print(r.status_code, r.json())
"
```

Register an unrelated second service, then re-register it (`UPDATE`). The
update reaches only `consumer-unscoped`; `consumer-scoped`'s `apiIds`
filter excludes it (`notify_service_change`'s
`sub.api_ids and str(service.service_id) not in sub.api_ids` check):

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/published-apis/v1/energy-saving-rapp/service-apis', json={
    'serviceName': 'other-api', 'producerId': 'energy-saving-rapp',
    'endpoint': 'http://energy-saving-rapp:8080/other/v1', 'version': '1.0', 'moduleScope': 'energy-saving-rapp',
})
print(r.status_code, r.json())
r = httpx.post('http://sme:8000/published-apis/v1/energy-saving-rapp/service-apis', json={
    'serviceName': 'other-api', 'producerId': 'energy-saving-rapp',
    'endpoint': 'http://energy-saving-rapp:8080/other/v1', 'version': '2.0', 'moduleScope': 'energy-saving-rapp',
})
print(r.status_code, r.json())
"
```

`sme`'s logs show exactly one delivery attempt, to `consumer-unscoped`'s
callback.

Re-register `energy-saving-api` itself (`UPDATE`). This matches
`consumer-scoped`'s filter too, so **both** subscribers are notified:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://sme:8000/published-apis/v1/energy-saving-rapp/service-apis', json={
    'serviceName': 'energy-saving-api', 'producerId': 'energy-saving-rapp',
    'endpoint': 'http://energy-saving-rapp:8080/energy-saving/v1', 'version': 'v2',
    'fullApiVersions': ['v1'], 'moduleScope': 'energy-saving-rapp',
})
print(r.status_code, r.json())
"
```

`sme`'s logs show two delivery attempts, one per callback. Unsubscribe
both:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://sme:8000/capif-events/v1/consumer-unscoped/subscriptions/<unscopedSubscriptionId>')
print(r.status_code)
r = httpx.delete('http://sme:8000/capif-events/v1/consumer-scoped/subscriptions/<scopedSubscriptionId>')
print(r.status_code)
"
```

## 22. (removed) A1 Related's service supervision sweep

Removed with the A1 module (see step 11); the other steps keep their numbers.

## 23. Retire it — package priming lifecycle, Terminate, then Delete

**Prime the package** — the reference's `COMMISSIONED -> PRIMING -> PRIMED`
lifecycle (`AVAILABLE` plays the `COMMISSIONED` role). ACM/DME/SME
pre-provisioning is not included, so both transitions happen within this
one request:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://onboarding:8000/packages/<packageId>/prime')
print(r.status_code, r.json())
"
```

`state` is now `PRIMED`.

**A deprime refusal.** Try to deprime while the Energy Saving rApp's instance
(step 3) is still deployed. This is the reference's `deprimeRapp` guard
("Unable to deprime as there are active rapp instances"), backed by a
query against `PackageUsageRegistration`:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://onboarding:8000/packages/<packageId>/deprime')
print(r.status_code, r.json())
"
```

`409`, with `detail.title` = `SERVICE_NAME_CONFLICT`; the package stays
`PRIMED`.

Terminate the instance:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://rapp-mgmt:8000/instances/<instanceId>/terminate')
print(r.status_code, r.json())
"
```

`state` is `UNDEPLOYED`. `TerminateInstance` also calls Onboarding's
`usage/stop`, closing the usage registration the deprime guard was reading.

**Deprime again** — the guard now passes:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.post('http://onboarding:8000/packages/<packageId>/deprime')
print(r.status_code, r.json())
"
```

`state` is back to `AVAILABLE`. Then delete the instance:

```bash
docker compose exec r1-termination python3 -c "
import httpx
r = httpx.delete('http://rapp-mgmt:8000/instances/<instanceId>')
print(r.status_code)
"
```

`204` with an empty body — the instance row is gone. This completes the
rApp lifecycle.

## 24. Wave 10.1 — the EnergySaving rApp (Demo 00–11)

Continues from the package used above. The EnergySaving reference
rApp (`samples/energy-saving-rapp/`) runs beside the platform as the
`energy-saving-rapp` service. Using only O1 PM data, it:

* predicts sustained low PRB utilisation;
* runs its model through the governed TS 28.105 lifecycle;
* puts cells to sleep through O1, verifies every write, and rolls back
  when one doesn't take;
* wakes cells before load returns.

There is no A1, Near-RT RIC, xApp or E2. Design:
`docs/call-flows/22-energy-saving-closed-loop.md`; scope and tests:
`docs/STANDARDS.md` §9.

The demo is a script, `samples/energy-saving-rapp/demo.py`, one step per
Demo number; it keeps the ids it needs between steps. Timestamps are
simulation time (history 2026-09-01..03, live PM from midnight on the 4th),
so "midnight behaviour" reproduces on any day.

```bash
python3 samples/build_csar.py energy-saving-rapp      # only after editing the sample
docker compose cp samples/energy-saving-rapp.csar r1-termination:/srv/scratch/energy-saving-rapp.csar
docker compose cp samples/energy-saving-rapp r1-termination:/srv/scratch/energy-saving-rapp
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch   # if §1 isn't already serving
```

Then run one step at a time and look at what each prints:

| Step | Command (`docker compose exec r1-termination …`) | What to observe |
|------|------|------|
| Demo 00 — prepare the RAN | `python3 /srv/scratch/energy-saving-rapp/demo.py 00` | `gnb-du-demo-01` registered behind `mock-o1-adaptor`, PRB PM subscribed, cell 103 marked EMERGENCY, the Digital Twin dataset and the O1-CM intent handler registered |
| Demo 01 — onboard | `… demo.py 01` | package `AVAILABLE`; 4 execution modes, 3 autonomy modes; an AUTONOMOUS instance started |
| Demo 02 — dataset | `… demo.py 02` | 288 PM measurements → DME; datasets TRAINING/INFERENCE = `PRB_UTILIZATION`, EMULATION = `PRB_UTILIZATION_SIM` |
| Demo 03 — train | `… demo.py 03` | `TRAINING → TRAINED` on the MLTF runtime; RMSE and confidence; artifact stored in MLMR |
| Demo 04 — validate | `… demo.py 04` | `VALIDATING → VALIDATED`, held-out score |
| Demo 05 — emulate | `… demo.py 05` | Digital Twin trend in, midnight recommendation `LOCKED`, estimated kWh saved |
| Demo 06 — promote | `… demo.py 06` | `CERTIFIED → PROMOTED` (operator governance decisions) |
| Demo 07 — deploy runtime | `… demo.py 07` | RuntimeLifecycle `ACTIVE` |
| Demo 08 — live inference | `… demo.py 08` | cell 101 at PRB 2 % for an hour → `LOCK`; cell 103 blocked `EMERGENCY_CELL` |
| Demo 09 — DME action | `… demo.py 09` | the action record, its source (`sa-smos:o1-cm-intent-handler`, intent/expectation ids) and the correlation chain execution → dispatch → intent → action |
| Demo 10 — O1 update | `… demo.py 10` | `NRCellDU=101` read back over NETCONF get-config: `administrativeState: LOCKED`, verification `VERIFIED` |
| Demo 11 — dashboard | `… demo.py 11` | per cell: state, PRB, predicted PRB, decision, outcome. In the GUI: **rApps** → the instance (the demo passes `operatorApiBase` when it creates the instance, so its declared page has data) |

`python3 /srv/scratch/energy-saving-rapp/demo.py all` runs every step. Things to
try afterwards:

* **Wake the cell.** Report a load spike and run the loop:
  `POST http://energy-saving-rapp:8000/instances/<id>/evaluate`.
* **Operator override.** Press **Override: unlock** on the Energy Saving
  page.
* **ASSIST mode.** Deploy a second instance with `autonomyMode: "ASSIST"`.
  Its LOCK waits in **Policy & Intents → Autonomy dispatches** until you
  Resolve or Reject it; then press **Reconcile approvals**.

The integration suite covers every Wave 10 test case (TC01–TC33) the same
way: `tests_integration/test_energy_saving_rapp.py`.

## 25. Wave 10.2 — the Mobility Optimization rApp (Demo 00–11)

This section is independent of §24. The Mobility Optimization reference
rApp (`samples/mobility-optimization-rapp/`) is deployed beside the
platform as the `mobility-optimization-rapp` service. Using only O1
handover PM data, it:

* classifies handover failures per neighbour relation (too late, too
  early, wrong cell, ping-pong);
* predicts the next hour's failure rate;
* moves `NRCellRelation.cellIndividualOffset` in 2 dB steps, within ± 6 dB
  of the baseline and inside the gNB's DMRO bounds;
* verifies every write, and reverts a change that made the KPI worse.

It never tunes towards a cell that is asleep, about to sleep or just woken
(it reads the EnergySaving rApp's cell states when given its instance id),
and it leaves alone relations with `isHOAllowed=false` and EMERGENCY or
incident-zone cells. There is no A1, Near-RT RIC, xApp or E2. Design:
`docs/call-flows/23-mobility-optimization-closed-loop.md`; scope and tests:
`docs/STANDARDS.md` §10.

The demo is a script, `samples/mobility-optimization-rapp/demo.py`, one step
per Demo number. Timestamps are simulation time (handover history
2026-09-01..03, live PM from midnight on the 4th).

```bash
python3 samples/build_csar.py mobility-optimization-rapp      # only after editing the sample
docker compose cp samples/mobility-optimization-rapp.csar r1-termination:/srv/scratch/mobility-optimization-rapp.csar
docker compose cp samples/mobility-optimization-rapp r1-termination:/srv/scratch/mobility-optimization-rapp
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch   # if not already serving
```

Then run one step at a time:

| Step | Command (`docker compose exec r1-termination …`) | What to observe |
|------|------|------|
| Demo 00 — prepare the RAN | `python3 /srv/scratch/mobility-optimization-rapp/demo.py 00` | `gnb-du-mro-demo-01` registered behind `mock-o1-adaptor`, HO_PERFORMANCE PM subscribed, cell 204 marked EMERGENCY, the Digital Twin dataset and the O1-CM intent handler registered |
| Demo 01 — onboard | `… demo.py 01` | package `AVAILABLE`; an AUTONOMOUS instance over four relations started |
| Demo 02 — dataset | `… demo.py 02` | 288 hourly per-relation counter sets → DME; datasets TRAINING/INFERENCE = `HO_PERFORMANCE`, EMULATION = `HO_PERFORMANCE_SIM` |
| Demo 03 — train | `… demo.py 03` | `TRAINING → TRAINED`; the regression's weights and RMSE; artifact stored in MLMR |
| Demo 04 — validate | `… demo.py 04` | `VALIDATING → VALIDATED`, held-out band score |
| Demo 05 — emulate | `… demo.py 05` | the Digital Twin injects one fault per relation; direction accuracy and false actions |
| Demo 06 — promote | `… demo.py 06` | `CERTIFIED → PROMOTED` (operator governance decisions) |
| Demo 07 — deploy | `… demo.py 07` | RuntimeLifecycle `ACTIVE`; `DMROFunction` bounds −6/+6 dB written and read back `VERIFIED` |
| Demo 08 — live inference | `… demo.py 08` | 201→203 `RAISE_CIO` 0 → 2 dB (too late), 202→203 `LOWER_CIO` 0 → −2 dB (too early), 203→204 blocked `PROTECTED_CELL` |
| Demo 09 — DME action and O1 | `… demo.py 09` | the action record and the execution → dispatch → intent → action chain; `NRCellRelation=201-203` read back as `[2, 2, 2, 2, 2, 2]` |
| Demo 10 — KPI check | `… demo.py 10` | an hour later the failure rate has dropped: `CONFIRMED` |
| Demo 11 — dashboard | `… demo.py 11` | per relation: state, CIO, failure rate, prediction, decision, outcome. In the GUI: **Mobility** |

Things to try afterwards:

* **A change that backfires.** Report worse ping-pong after a LOWER and
  evaluate: the rApp reverts the CIO straight through DME (`REVERTED`).
* **Coordinate with EnergySaving.** Start the instance with
  `energySavingInstanceId` set to a §24 instance: relations towards a SLEEP
  or PRE_SLEEP cell are blocked `TARGET_ASLEEP`, and for 30 minutes after a
  wake `TARGET_RECENTLY_WOKEN`.
* **ASSIST mode.** A CIO change waits in **Policy & Intents → Autonomy
  dispatches**; Resolve or Reject it, then press **Reconcile approvals**.

The integration suite covers MRO-01..MRO-20 the same way:
`tests_integration/test_mobility_optimization_rapp.py`.

## 26. Wave 10.3 — the Coverage Optimization rApp (Demo 00–11)

This section is independent of §24 and §25. The Coverage Optimization
reference rApp (`samples/coverage-optimization-rapp/`) is deployed beside
the platform as the `coverage-optimization-rapp` service. Using only O1 PM
data, it:

* measures each cell's weak-coverage, overshoot and pilot-pollution shares
  and its overlap with each neighbour;
* learns how those shares respond to a cell's own tilt and power steps, and
  to its neighbours' steps;
* picks, jointly for the cluster, the tilt or power steps (at most two
  cells per pass) that most reduce the problems. So an overshooting cell
  is downtilted, rather than the neighbours it pollutes;
* verifies every write, and reverts a change set that left the cluster
  worse.

It holds a cell while the cell or a neighbour is asleep, about to sleep or
just woken (EnergySaving), and while the Mobility rApp is observing one of
its relations. It also leaves EMERGENCY and incident-zone cells alone, and
holds everything under a critical alarm. There is no A1, Near-RT RIC, xApp
or E2. Design: `docs/call-flows/24-coverage-optimization-closed-loop.md`;
scope and tests: `docs/STANDARDS.md` §10a.

The demo is a script, `samples/coverage-optimization-rapp/demo.py`, one step
per Demo number. Live PM is produced from each cell's tilt and power as read
back over O1, so the rApp's own changes show up in the next hour's PM.
Timestamps are simulation time (history 2026-09-01..03, live PM from
midnight on the 4th).

```bash
python3 samples/build_csar.py coverage-optimization-rapp      # only after editing the sample
docker compose cp samples/coverage-optimization-rapp.csar r1-termination:/srv/scratch/coverage-optimization-rapp.csar
docker compose cp samples/coverage-optimization-rapp r1-termination:/srv/scratch/coverage-optimization-rapp
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch   # if not already serving
```

Then run one step at a time:

| Step | Command (`docker compose exec r1-termination …`) | What to observe |
|------|------|------|
| Demo 00 — prepare the RAN | `python3 /srv/scratch/coverage-optimization-rapp/demo.py 00` | `gnb-cco-demo-01` registered behind `mock-o1-adaptor`, COVERAGE_PERFORMANCE PM subscribed, the four-cell cluster at 6.0° / 43 dBm |
| Demo 01 — onboard | `… demo.py 01` | package `AVAILABLE`; an AUTONOMOUS instance over cells 301–304 started |
| Demo 02 — dataset | `… demo.py 02` | 288 hourly per-cell windows → DME, with each cell's tilt or power stepped in turn; datasets TRAINING/INFERENCE = `COVERAGE_PERFORMANCE`, EMULATION = `COVERAGE_PERFORMANCE_SIM` |
| Demo 03 — train | `… demo.py 03` | `TRAINING → TRAINED`; the 12 learned sensitivities (uptilt raises overshoot, power cuts weak coverage, neighbours reaching in raise pollution) |
| Demo 04 — validate | `… demo.py 04` | `VALIDATING → VALIDATED`, held-out RMSE and direction accuracy |
| Demo 05 — emulate | `… demo.py 05` | the Digital Twin injects one fault per cluster; move accuracy 1.0, no false actions |
| Demo 06 — promote | `… demo.py 06` | `CERTIFIED → PROMOTED` (operator governance decisions) |
| Demo 07 — deploy | `… demo.py 07` | RuntimeLifecycle `ACTIVE` |
| Demo 08 — live inference | `… demo.py 08` | 301 overshoots; the joint plan includes `301 DOWNTILT`, and 302 is `HELPED_BY` it; the predicted cluster objective drops |
| Demo 09 — DME action and O1 | `… demo.py 09` | the action record and the execution → dispatch → intent → action chain; `CommonBeamformingFunction=301` read back as `digitalTilt: 70` |
| Demo 10 — KPI check | `… demo.py 10` | an hour later the measured objective has dropped: `CONFIRMED` |
| Demo 11 — dashboard | `… demo.py 11` | per cell: tilt, power, shares, decision, outcome. In the GUI: **Coverage** |

Things to try afterwards:

* **Weak coverage.** Report a `WEAK_COVERAGE` fault on 302 for an hour and
  evaluate: the rApp raises 302's power by 1 dB.
* **A change set that backfires.** Open coverage holes in 302 and 303 the
  hour after a change: the whole change set is reverted straight through
  DME (`REVERTED`).
* **ASSIST mode.** A change set waits in **Policy & Intents → Autonomy
  dispatches**; Resolve or Reject it, then press **Reconcile approvals**.

The integration suite covers CCO-01..CCO-20 the same way:
`tests_integration/test_coverage_optimization_rapp.py`.

## 27. Wave 10.4 — the Traffic Steering rApp (Demo 00–11)

This section is independent of §24–§26. The Traffic Steering reference rApp
(`samples/traffic-steering-rapp/`) is deployed beside the platform as the
`traffic-steering-rapp` service. Using only O1 PM data, it:

* scores each cell's congestion from PRB utilisation, connected UEs and UE
  throughput;
* forecasts the next hour's score;
* moves load off a cell forecast congested towards its least-loaded
  neighbour, one step at a time:
  - idle UEs by reselection priority towards another frequency layer;
  - connected UEs by the relation's CIO;
* never pushes a neighbour above its own limit;
* verifies every write;
* reverts a step that congested its target, didn't help, or raised
  handover failures;
* releases its steering when the load falls.

The rApp shares the CIO with the Mobility rApp, within one ± 6 dB envelope.
Neither touches a relation the other is observing. It holds steering around
cells that are asleep or just woken (EnergySaving) and around cells in a
Coverage change set. It also leaves protected cells alone and never reverses
a steering direction within 6 hours. There is no A1, Near-RT RIC, xApp or E2.
Design: `docs/call-flows/25-traffic-steering-closed-loop.md`; scope and tests:
`docs/STANDARDS.md` §10b.

The demo is a script, `samples/traffic-steering-rapp/demo.py`, one step per
Demo number. Live PM is produced from each cell's CIO and reselection
priority as read back over O1, so the rApp's own steps show up in the next
hour's PM. Timestamps are simulation time (history 2026-09-01..03, live PM
from noon on the 4th).

```bash
python3 samples/build_csar.py traffic-steering-rapp      # only after editing the sample
docker compose cp samples/traffic-steering-rapp.csar r1-termination:/srv/scratch/traffic-steering-rapp.csar
docker compose cp samples/traffic-steering-rapp r1-termination:/srv/scratch/traffic-steering-rapp
docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch   # if not already serving
```

Then run one step at a time:

| Step | Command (`docker compose exec r1-termination …`) | What to observe |
|------|------|------|
| Demo 00 — prepare the RAN | `python3 /srv/scratch/traffic-steering-rapp/demo.py 00` | `gnb-mlb-demo-01` registered behind `mock-o1-adaptor`, LOAD_PERFORMANCE PM subscribed; layers F3500 (401, 402) and F2100 (411, 412) |
| Demo 01 — onboard | `… demo.py 01` | package `AVAILABLE`; an AUTONOMOUS instance whose region scope names every relation and frequency relation it may write |
| Demo 02 — dataset | `… demo.py 02` | 288 hourly per-cell windows → DME, with each cell's CIO or priority stepped in turn; datasets TRAINING/INFERENCE = `LOAD_PERFORMANCE`, EMULATION = `LOAD_PERFORMANCE_SIM` |
| Demo 03 — train | `… demo.py 03` | `TRAINING → TRAINED`; the learned transfer: about 3 % of the source's score per CIO dB, 6 % per priority step |
| Demo 04 — validate | `… demo.py 04` | `VALIDATING → VALIDATED`, held-out band score and RMSE |
| Demo 05 — emulate | `… demo.py 05` | the Digital Twin injects hotspots; steering accuracy 1.0, no false actions |
| Demo 06 — promote | `… demo.py 06` | `CERTIFIED → PROMOTED` (operator governance decisions) |
| Demo 07 — deploy | `… demo.py 07` | RuntimeLifecycle `ACTIVE` |
| Demo 08 — live inference | `… demo.py 08` | 401 forecast ≈ 81 (`CONGESTED`); one step towards its least-loaded neighbour, which stays ≤ 55 after the transfer |
| Demo 09 — DME action and O1 | `… demo.py 09` | the action record and the execution → dispatch → intent → action chain; the steered attribute read back |
| Demo 10 — KPI check | `… demo.py 10` | an hour later 401 is below its no-steering forecast and the target is fine: `CONFIRMED` |
| Demo 11 — dashboard | `… demo.py 11` | per cell: layer, score, steering in force, decision, outcome. In the GUI: **Traffic Steering** |

Things to try afterwards:

* **More steps.** Keep reporting the hotspot each hour and evaluate. The
  next step goes to the other neighbour, in idle mode towards the other
  layer. Once both neighbours are near 55, the result is
  `NO_ELIGIBLE_TARGET`.
* **Release.** Report hours without the hotspot into the evening: the
  steering is released step by step (`RELEASE_CONNECTED`, `RELEASE_IDLE`).
* **ASSIST mode.** A step waits in **Policy & Intents → Autonomy
  dispatches**; Resolve or Reject it, then press **Reconcile approvals**.

The integration suite covers TS-01..TS-20 the same way:
`tests_integration/test_traffic_steering_rapp.py`.

## Known rough edges for a live walkthrough

- `smo/docs/call-flows/01-rapp-onboarding-to-deployment.md`'s diagram
  doesn't show the provider/invoker registration steps in §4; this runbook
  is the more current sequence.
- Callbacks to `http://demo-consumer:9000/...` have no listener in the
  compose stack; each delivery attempt shows up only in the sending
  service's logs.
- If a live run hits one of the spec-conformance gaps listed in
  `OPEN_ITEMS.md`, record it there.
