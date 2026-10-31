# Call flows

One markdown file per flow: numbered sub-flows (`<nn>a`, `<nn>b`, ...), prose before each mermaid `sequenceDiagram`, and the same diagrams as PNG under
`images/<flow>/` with `images/manifest.json` (drawn by `scripts/render_call_flows.py`; `tests_integration/test_call_flow_images.py` fails on an image
older than its diagram). Every diagram is parsed by `gui/scripts/validate-call-flow-diagrams.mjs` (`npm run validate:diagrams`, CI job
`call-flow-diagrams`). Each module's own lifecycle is in `<module>/docs/lifecycle.md`, in the same format.

- **01–27** describe the code as it runs today (`OPEN_ITEMS.md` DOC-CF-1, closed into `HISTORY.md`).
- **28–32** describe deploying and operating it (DOC-CF-3).
- **34–37** are the **target design** of the converged rewrite (`docs/plan/CONVERGED_REWRITE_PLAN.md`, plan task S0.9; ADRs 0008–0011), not yet built.
  Every message in them is an operation of the schema bundle (`specs/smo-schema-bundle/`) or a listed ADR 0011 [D] step, checked by
  `tests_integration/test_call_flow_operations.py`. `docs/plan/S0.10-use-cases-on-paper.md` traces the four sample rApps through them.

| # | Flow | What it covers |
|---:|---|---|
| 01 | [rApp onboarding → running instance](01-rapp-onboarding-to-deployment.md) | onboard a package, create an instance, its credentials, its bootstrap |
| 02 | [AI/ML model](02-aiml-model-train-to-inference.md) | register, train, validate, emulate, certify, deploy, infer, monitor |
| 03 | [Configuration write](03-config-write-with-schema-check.md) | checks, approval, dispatch per element, waves, rollback, and the DME path |
| 04 | [Closed-loop assurance](04-closed-loop-assurance.md) | a monitor, MDAF's reports, the remedial action and the escalation |
| 05 | – | Unused: it was A1 EI registration; A1 is out of scope and its modules were removed in release 0.5.0 (`OPEN_ITEMS.md`, "Out of scope at every stage") |
| 06 | [rApp package lifecycle](06-package-lifecycle.md) | prime, deprecate, delete |
| 07 | [rApp instance lifecycle](07-rapp-instance-lifecycle.md) | run, fault, upgrade, roll back, stop, terminate |
| 08 | [RAN analytics](08-ran-analytics-data-production.md) | producer registration, subscriptions, reports and their delivery |
| 09 | [Intents](09-intent-service-intent-flow.md) | creating, managing, handling and dispatching them |
| 10 | [SO SMOS multi-step order](10-so-smos-multi-step-infra-training-deploy.md) | submit, dispatch, fail fast, cancel |
| 11 | [DME producers and types](11-dme-producer-type-lifecycle.md) | registering and joining, jobs reaching producers, health, leaving and deleting |
| 12 | [DME data jobs, records and offers](12-dme-data-record-lifecycle-eligibility.md) | whose they are, how data moves, and which data may feed what |
| 13 | [Model performance monitoring (MLMF)](13-mlmf-subscription-lifecycle.md) | subscribe, report, breach, notify, unsubscribe |
| 14 | [Correlation ids](14-correlation-id-propagation.md) | one id per action, from the gateway through every module, the work that outlives the request, and the console |
| 15 | [NFO workload lifecycle](15-nfo-workload-lifecycle.md) | descriptor, instantiate, scale, heal, terminate |
| 16 | [FOCOM inventory resources](16-focom-resource-inventory-lifecycle.md) | subscribing, provisioning and notifying, reading and status, deprovisioning |
| 17 | [Model runtime lifecycle](17-model-runtime-lifecycle.md) | deploy, activate, scale, terminate, deploy again |
| 18 | [SME security](18-sme-trusted-invokers-lifecycle.md) | providers and publishing, invokers, trust contexts, tokens and introspection, discovery and events |
| 19 | [Software management job](19-software-management-job-lifecycle.md) | starting, sending each phase to the element, failure and abort, campaigns |
| 20 | [Alarms and PM / FM subscriptions](20-alarm-pm-subscription-lifecycle.md) | raising, acknowledging and clearing, and subscribing an element's measurements and alarms |
| 21 | [O1 vendor onboarding](21-o1-vendor-onboarding.md) | elements, schemas, the vendor's capability, and the two checks on every O1 operation |
| 22 | [EnergySaving rApp](22-energy-saving-closed-loop.md) | the closed loop: predict, guard, lock or wake, verify, roll back |
| 23 | [Mobility Optimization rApp](23-mobility-optimization-closed-loop.md) | the closed loop: classify, predict, guard, step the CIO, verify, check the KPI |
| 24 | [Coverage Optimization rApp](24-coverage-optimization-closed-loop.md) | the closed loop: guard, plan jointly, move tilt and power, verify, check the KPI |
| 25 | [Traffic Steering rApp](25-traffic-steering-closed-loop.md) | the closed loop: score, forecast, guard, steer idle or connected UEs, verify, check the KPI |
| 26 | [Model governance and end of life](26-model-governance-and-end-of-life.md) | submit, approve, certify, promote, roll back, deprecate, retire |
| 27 | [TS 28.105 provisioning resources](27-ts28105-provisioning-resources.md) | training, update and loading requests, and execution timeouts |
| 28 | [Docker compose](28-docker-compose-deployment.md) | start-up order, the `migrate` service, secrets, readiness, and the profiles and overlays |
| 29 | [Kubernetes with the Helm chart](29-kubernetes-helm-deployment.md) | install, readiness, secrets, exposure, and the GitOps overlays |
| 30 | [Upgrade and rollback](30-upgrade-and-rollback.md) | a rolling upgrade, expand and contract, the previous release's code on the new schema, and going back |
| 31 | [Schema migration on its own](31-schema-migration.md) | Alembic to head, the wait for the schema, roles and grants, proving a revision, undoing one |
| 32 | [Backup, restore and disaster recovery](32-backup-restore-and-disaster-recovery.md) | the off-site copy, the restore, WAL archiving and recovery to a minute, the drill, a primary failover |
| 33 | – | **Unused.** It was [ADR 0007](../adr/0007-intent-driven-ai-ran-tier-1-intent.md)'s flow, which was never adopted: ADR 0009 supersedes ADR 0007, and its tiering is drawn in 34–37. The number is not reused |
| 34 | [Level-1 intake and an on-demand rApp](34-level1-intake-and-on-demand-rapp.md) | strict intake, pre-evaluation, selection, instantiation, CAPIF, model, data, assurance, the Level-2 and the reports |
| 35 | [Level-2 parallel split and rollback](35-level2-parallel-split-and-rollback.md) | intake safeguards, conflict check, twin what-if, the split per O-DU/O-CU, one failure, all rolled back |
| 36 | [NES vs MRO arbitration on cluster 12](36-nes-vs-mro-arbitration.md) | an indirect conflict through the Impact Map, MRO deferred, the conflict over R1, SA SMOS decides what happens next |
| 37 | [Lease expiry and demotion](37-lease-expiry-and-demotion.md) | the rApp stops renewing, the watchdog reverts the cell, SA SMOS reports, SO SMOS demotes, the operator sees why |
