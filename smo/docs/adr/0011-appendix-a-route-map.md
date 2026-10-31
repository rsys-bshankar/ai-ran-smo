# ADR 0011, appendix A — every route of today and what replaces it

Part of [ADR 0011](0011-standard-interfaces-only.md). Generated on 31 October 2026 from the 21 committed OpenAPI documents in `docs/openapi/*.json` at
`main` cf95d1e (one row per path and method: **750 operations**), by classifying each against the interface rules of ADR 0011 section 3. The
generator was a one-off script and is not committed; the S0.17 gate will hold the same mapping in a machine-readable list.

**Verdicts.** *keep (standard)*: already a standard operation at its standard path, kept. *replaced*: a standard operation replaces it (the path
usually moves to the ProvMnS form of D5, or to the standard prefix of CAPIF, R1AP or O2), in the stage named. *delete*: the route goes with no
successor route (its job is done in process, by another standard flow, or not at all). *[D] exception*: kept as one of the named exceptions of ADR 0011
section 6 (the ID is given). **Stage** is the PR-CONV row that does it (`OPEN_ITEMS.md` §7); "–" for a probe or a test double.

Specification versions as `specs/README.md` records them: TS 28.532 ProvMnS v19.2.0, TS 28.312 v20.0.0, TS 28.105 v19.5.0, TS 28.104 v20.0.0,
TS 28.111 v19.3.0, TS 28.319 v19.0.0, TS 28.536 v19.1.0, TS 29.222 v20.0.0 (provider management v19.5.0), TS 29.482 v19.1.0; O-RAN R1GAP v14.00,
R1TD v05.00, O2ims v12, O2dms Kubernetes-native profile v7.

## Counts

| Module | Operations | Keep (standard) | Replaced | Delete | [D] exception |
|---|---:|---:|---:|---:|---:|
| `aimgf` | 110 | 0 | 99 | 1 | 10 |
| `coverage-optimization-rapp` | 26 | 0 | 0 | 14 | 12 |
| `dme` | 35 | 0 | 27 | 3 | 5 |
| `energy-saving-rapp` | 28 | 0 | 0 | 13 | 15 |
| `focom` | 94 | 0 | 90 | 0 | 4 |
| `intent-service` | 25 | 0 | 16 | 5 | 4 |
| `mdaf` | 23 | 0 | 19 | 0 | 4 |
| `mllf` | 5 | 0 | 0 | 1 | 4 |
| `mlmr` | 27 | 0 | 22 | 0 | 5 |
| `mobility-optimization-rapp` | 27 | 0 | 0 | 14 | 13 |
| `mock-o1-adaptor` | 24 | 7 | 0 | 0 | 17 |
| `nfo` | 18 | 0 | 14 | 0 | 4 |
| `onboarding` | 18 | 0 | 10 | 4 | 4 |
| `r1-termination` | 10 | 5 | 0 | 1 | 4 |
| `ran-analytics` | 7 | 0 | 3 | 0 | 4 |
| `ran-nf-oam` | 157 | 1 | 72 | 15 | 69 |
| `rapp-mgmt` | 34 | 0 | 21 | 0 | 13 |
| `sa-smos` | 18 | 0 | 10 | 4 | 4 |
| `sme` | 29 | 8 | 16 | 1 | 4 |
| `so-smos` | 8 | 0 | 4 | 0 | 4 |
| `traffic-steering-rapp` | 27 | 0 | 0 | 14 | 13 |
| **total** | **750** | **21** | **423** | **90** | **216** |

## A.1 Every operation of `docs/openapi/*.json`

| # | Module | Method | Path | Verdict | Replaced by / reason | Stage |
|---:|---|---|---|---|---|---|
| 1 | `aimgf` | GET | `/aiml-inference-emulation-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceEmulationFunction` [N] TS 28.105 | S2.7, S2.10 |
| 2 | `aimgf` | POST | `/aiml-inference-emulation-functions` | replaced | TS 28.532 ProvMnS createMOI on `AIMLInferenceEmulationFunction` [N] TS 28.105 | S2.7, S2.10 |
| 3 | `aimgf` | DELETE | `/aiml-inference-emulation-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `AIMLInferenceEmulationFunction` [N] TS 28.105 | S2.7, S2.10 |
| 4 | `aimgf` | GET | `/aiml-inference-emulation-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceEmulationFunction` [N] TS 28.105 | S2.7, S2.10 |
| 5 | `aimgf` | GET | `/aiml-inference-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceFunction` [N] TS 28.105 | S2.7, S2.10 |
| 6 | `aimgf` | POST | `/aiml-inference-functions` | replaced | TS 28.532 ProvMnS createMOI on `AIMLInferenceFunction` [N] TS 28.105 | S2.7, S2.10 |
| 7 | `aimgf` | DELETE | `/aiml-inference-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `AIMLInferenceFunction` [N] TS 28.105 | S2.7, S2.10 |
| 8 | `aimgf` | GET | `/aiml-inference-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceFunction` [N] TS 28.105 | S2.7, S2.10 |
| 9 | `aimgf` | PATCH | `/aiml-inference-functions/{function_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `AIMLInferenceFunction` [N] TS 28.105 | S2.7, S2.10 |
| 10 | `aimgf` | GET | `/aiml-inference-reports` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceReport` [N] TS 28.105 | S2.10 |
| 11 | `aimgf` | POST | `/aiml-inference-reports` | replaced | TS 28.532 ProvMnS createMOI on `AIMLInferenceReport` [N] TS 28.105 | S2.10 |
| 12 | `aimgf` | GET | `/aiml-inference-reports/{report_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `AIMLInferenceReport` [N] TS 28.105 | S2.10 |
| 13 | `aimgf` | GET | `/emulation-jobs` | replaced | TS 28.532 ProvMnS on `AIMLInferenceEmulationFunction` [N] TS 28.105 (v19.5.0 has no separate emulation request class) | S2.10 |
| 14 | `aimgf` | POST | `/emulation-jobs` | replaced | TS 28.532 ProvMnS on `AIMLInferenceEmulationFunction` [N] TS 28.105 (v19.5.0 has no separate emulation request class) | S2.10 |
| 15 | `aimgf` | POST | `/emulation-jobs/{emulation_job_id}/complete` | replaced | TS 28.532 ProvMnS on `AIMLInferenceEmulationFunction` [N] TS 28.105 (v19.5.0 has no separate emulation request class) | S2.10 |
| 16 | `aimgf` | GET | `/emulation-jobs/{emulation_job_id}/status` | replaced | TS 28.532 ProvMnS on `AIMLInferenceEmulationFunction` [N] TS 28.105 (v19.5.0 has no separate emulation request class) | S2.10 |
| 17 | `aimgf` | POST | `/execution-timeouts/sweep` | delete | worker task in process; not an interface | S2.3 |
| 18 | `aimgf` | GET | `/feature-groups` | replaced | DME data types (R1GAP v14 §5.2.2, R1TD v05) named by the training request; lineage per artifact version | S2.12, S3.17 |
| 19 | `aimgf` | POST | `/feature-groups` | replaced | DME data types (R1GAP v14 §5.2.2, R1TD v05) named by the training request; lineage per artifact version | S2.12, S3.17 |
| 20 | `aimgf` | DELETE | `/feature-groups/{feature_group_name}` | replaced | DME data types (R1GAP v14 §5.2.2, R1TD v05) named by the training request; lineage per artifact version | S2.12, S3.17 |
| 21 | `aimgf` | GET | `/feature-groups/{feature_group_name}` | replaced | DME data types (R1GAP v14 §5.2.2, R1TD v05) named by the training request; lineage per artifact version | S2.12, S3.17 |
| 22 | `aimgf` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 23 | `aimgf` | GET | `/inference-jobs` | replaced | R1GAP v14 §5.6.10 AI/ML model inference service | S2.11 |
| 24 | `aimgf` | POST | `/inference-jobs/{inference_job_id}/resolve` | replaced | R1GAP v14 §5.6.10 AI/ML model inference service | S2.11 |
| 25 | `aimgf` | GET | `/inference-jobs/{inference_job_id}/status` | replaced | R1GAP v14 §5.6.10 AI/ML model inference service | S2.11 |
| 26 | `aimgf` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 27 | `aimgf` | GET | `/ml-model-loading-policies` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingPolicy` [N] TS 28.105 | S2.10 |
| 28 | `aimgf` | POST | `/ml-model-loading-policies` | replaced | TS 28.532 ProvMnS createMOI on `MLModelLoadingPolicy` [N] TS 28.105 | S2.10 |
| 29 | `aimgf` | DELETE | `/ml-model-loading-policies/{policy_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MLModelLoadingPolicy` [N] TS 28.105 | S2.10 |
| 30 | `aimgf` | GET | `/ml-model-loading-policies/{policy_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingPolicy` [N] TS 28.105 | S2.10 |
| 31 | `aimgf` | POST | `/ml-model-loading-policies/{policy_id}/trigger` | replaced | TS 28.532 ProvMnS createMOI on `MLModelLoadingRequest` [N] TS 28.105 (a policy triggers itself on its conditions) | S2.10 |
| 32 | `aimgf` | GET | `/ml-model-loading-processes` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingProcess` [N] TS 28.105 | S2.10 |
| 33 | `aimgf` | GET | `/ml-model-loading-processes/{process_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingProcess` [N] TS 28.105 | S2.10 |
| 34 | `aimgf` | GET | `/ml-model-loading-requests` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingRequest` [N] TS 28.105 | S2.10 |
| 35 | `aimgf` | POST | `/ml-model-loading-requests` | replaced | TS 28.532 ProvMnS createMOI on `MLModelLoadingRequest` [N] TS 28.105 | S2.10 |
| 36 | `aimgf` | GET | `/ml-model-loading-requests/{request_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelLoadingRequest` [N] TS 28.105 | S2.10 |
| 37 | `aimgf` | PATCH | `/ml-model-loading-requests/{request_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLModelLoadingRequest` [N] TS 28.105 | S2.10 |
| 38 | `aimgf` | GET | `/ml-models/{model_id}/nrm-refs` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModel`, scope BASE_SUBTREE | S2.7 |
| 39 | `aimgf` | GET | `/ml-testing-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 40 | `aimgf` | POST | `/ml-testing-functions` | replaced | TS 28.532 ProvMnS createMOI on `MLTestingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 41 | `aimgf` | DELETE | `/ml-testing-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MLTestingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 42 | `aimgf` | GET | `/ml-testing-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 43 | `aimgf` | GET | `/ml-testing-reports` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingReport` [N] TS 28.105 | S2.7, S2.10 |
| 44 | `aimgf` | GET | `/ml-testing-reports/{report_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingReport` [N] TS 28.105 | S2.7, S2.10 |
| 45 | `aimgf` | GET | `/ml-testing-requests` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 46 | `aimgf` | POST | `/ml-testing-requests` | replaced | TS 28.532 ProvMnS createMOI on `MLTestingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 47 | `aimgf` | GET | `/ml-testing-requests/{request_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTestingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 48 | `aimgf` | PATCH | `/ml-testing-requests/{request_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLTestingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 49 | `aimgf` | GET | `/ml-training-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 50 | `aimgf` | POST | `/ml-training-functions` | replaced | TS 28.532 ProvMnS createMOI on `MLTrainingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 51 | `aimgf` | DELETE | `/ml-training-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MLTrainingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 52 | `aimgf` | GET | `/ml-training-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 53 | `aimgf` | PUT | `/ml-training-functions/{function_id}` | replaced | TS 28.532 ProvMnS createMOI / modifyMOIAttributes on `MLTrainingFunction` [N] TS 28.105 | S2.7, S2.10 |
| 54 | `aimgf` | GET | `/ml-training-processes` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingProcess` [N] TS 28.105 | S2.7, S2.10 |
| 55 | `aimgf` | GET | `/ml-training-processes/{process_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingProcess` [N] TS 28.105 | S2.7, S2.10 |
| 56 | `aimgf` | PATCH | `/ml-training-processes/{process_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLTrainingProcess` [N] TS 28.105 | S2.7, S2.10 |
| 57 | `aimgf` | POST | `/ml-training-processes/{process_id}/progress` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLTrainingProcess` (`progressStatus`) | S2.10 |
| 58 | `aimgf` | GET | `/ml-training-reports` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingReport` [N] TS 28.105 | S2.7, S2.10 |
| 59 | `aimgf` | GET | `/ml-training-reports/{report_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingReport` [N] TS 28.105 | S2.7, S2.10 |
| 60 | `aimgf` | GET | `/ml-training-requests` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 61 | `aimgf` | POST | `/ml-training-requests` | replaced | TS 28.532 ProvMnS createMOI on `MLTrainingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 62 | `aimgf` | DELETE | `/ml-training-requests/{request_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MLTrainingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 63 | `aimgf` | GET | `/ml-training-requests/{request_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLTrainingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 64 | `aimgf` | PATCH | `/ml-training-requests/{request_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLTrainingRequest` [N] TS 28.105 | S2.7, S2.10 |
| 65 | `aimgf` | GET | `/ml-update-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateFunction` [N] TS 28.105 | S2.10 |
| 66 | `aimgf` | POST | `/ml-update-functions` | replaced | TS 28.532 ProvMnS createMOI on `MLUpdateFunction` [N] TS 28.105 | S2.10 |
| 67 | `aimgf` | DELETE | `/ml-update-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MLUpdateFunction` [N] TS 28.105 | S2.10 |
| 68 | `aimgf` | GET | `/ml-update-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateFunction` [N] TS 28.105 | S2.10 |
| 69 | `aimgf` | GET | `/ml-update-processes` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateProcess` [N] TS 28.105 | S2.10 |
| 70 | `aimgf` | GET | `/ml-update-processes/{process_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateProcess` [N] TS 28.105 | S2.10 |
| 71 | `aimgf` | GET | `/ml-update-reports` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateReport` [N] TS 28.105 | S2.10 |
| 72 | `aimgf` | GET | `/ml-update-reports/{report_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateReport` [N] TS 28.105 | S2.10 |
| 73 | `aimgf` | GET | `/ml-update-requests` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateRequest` [N] TS 28.105 | S2.10 |
| 74 | `aimgf` | POST | `/ml-update-requests` | replaced | TS 28.532 ProvMnS createMOI on `MLUpdateRequest` [N] TS 28.105 | S2.10 |
| 75 | `aimgf` | GET | `/ml-update-requests/{request_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLUpdateRequest` [N] TS 28.105 | S2.10 |
| 76 | `aimgf` | PATCH | `/ml-update-requests/{request_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLUpdateRequest` [N] TS 28.105 | S2.10 |
| 77 | `aimgf` | GET | `/mlmf/reports` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 78 | `aimgf` | GET | `/mlmf/subscriptions` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 79 | `aimgf` | POST | `/mlmf/subscriptions` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 80 | `aimgf` | DELETE | `/mlmf/subscriptions/{subscription_id}` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 81 | `aimgf` | GET | `/mlmf/subscriptions/{subscription_id}/reports` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 82 | `aimgf` | POST | `/mlmf/subscriptions/{subscription_id}/reports` | replaced | R1GAP v14 §5.6.9 AI/ML model performance monitoring service (produced by the consumer rApp) + TS 28.622 `ThresholdMonitor` through the subscription manager | S1.13, S2.11 |
| 83 | `aimgf` | GET | `/model-lifecycles` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 84 | `aimgf` | GET | `/model-lifecycles/counts` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 85 | `aimgf` | POST | `/models/{model_id}/advance` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 86 | `aimgf` | GET | `/models/{model_id}/governance-history` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 87 | `aimgf` | POST | `/models/{model_id}/inference-jobs` | replaced | R1GAP v14 §5.6.10 AI/ML model inference service | S2.11 |
| 88 | `aimgf` | GET | `/models/{model_id}/lifecycle` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 89 | `aimgf` | GET | `/models/{model_id}/lifecycle-history` | [D] exception | D-07 model governance on the 14-state lifecycle (operator gates, certification); TS 28.105 has no governance operation (ADR 0010) | S2.6 |
| 90 | `aimgf` | POST | `/models/{model_id}/runtime/activate` | replaced | R1GAP v14 §5.6.7 AI/ML model deployment request service; inside AIMgF a TS 28.105 `MLModelLoadingRequest` | S2.10, S2.11 |
| 91 | `aimgf` | POST | `/models/{model_id}/runtime/deploy` | replaced | R1GAP v14 §5.6.7 AI/ML model deployment request service; inside AIMgF a TS 28.105 `MLModelLoadingRequest` | S2.10, S2.11 |
| 92 | `aimgf` | PATCH | `/models/{model_id}/runtime/node-groups` | replaced | O2dms Kubernetes-native profile v7 through NFO (placement and scale are NFO's) | S4.4, S4.9 |
| 93 | `aimgf` | POST | `/models/{model_id}/runtime/scale` | replaced | O2dms Kubernetes-native profile v7 through NFO (placement and scale are NFO's) | S4.4, S4.9 |
| 94 | `aimgf` | POST | `/models/{model_id}/runtime/terminate` | replaced | TS 28.532 ProvMnS deleteMOI on the loaded `MLModel` under its `AIMLInferenceFunction` (unload, AIML-UNLOAD) | S2.10 |
| 95 | `aimgf` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 96 | `aimgf` | GET | `/training-jobs` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 97 | `aimgf` | POST | `/training-jobs` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 98 | `aimgf` | DELETE | `/training-jobs/{training_job_id}` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 99 | `aimgf` | POST | `/training-jobs/{training_job_id}/complete` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 100 | `aimgf` | GET | `/training-jobs/{training_job_id}/model-metrics` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 101 | `aimgf` | POST | `/training-jobs/{training_job_id}/model-metrics` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 102 | `aimgf` | POST | `/training-jobs/{training_job_id}/progress` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 103 | `aimgf` | POST | `/training-jobs/{training_job_id}/resume` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 104 | `aimgf` | GET | `/training-jobs/{training_job_id}/status` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 105 | `aimgf` | POST | `/training-jobs/{training_job_id}/suspend` | replaced | R1GAP v14 §5.6.8 AI/ML model training services; inside AIMgF `MLTrainingRequest` / `MLTrainingProcess` [N] TS 28.105 | S2.10, S2.11 |
| 106 | `aimgf` | GET | `/validation-jobs` | replaced | TS 28.532 ProvMnS on `MLTestingRequest` [N] TS 28.105 | S2.10 |
| 107 | `aimgf` | POST | `/validation-jobs` | replaced | TS 28.532 ProvMnS on `MLTestingRequest` [N] TS 28.105 | S2.10 |
| 108 | `aimgf` | POST | `/validation-jobs/{validation_job_id}/complete` | replaced | TS 28.532 ProvMnS on `MLTestingRequest` [N] TS 28.105 | S2.10 |
| 109 | `aimgf` | GET | `/validation-jobs/{validation_job_id}/status` | replaced | TS 28.532 ProvMnS on `MLTestingRequest` [N] TS 28.105 | S2.10 |
| 110 | `aimgf` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 111 | `coverage-optimization-rapp` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 112 | `coverage-optimization-rapp` | GET | `/instances` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 113 | `coverage-optimization-rapp` | GET | `/instances/{instance_id}` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 114 | `coverage-optimization-rapp` | GET | `/instances/{instance_id}/cells` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 115 | `coverage-optimization-rapp` | GET | `/instances/{instance_id}/dashboard` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 116 | `coverage-optimization-rapp` | GET | `/instances/{instance_id}/decisions` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 117 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/demo/emulate` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 118 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/demo/history` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 119 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/demo/kpi-hour` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 120 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/demo/live` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 121 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/demo/prepare` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 122 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/evaluate` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 123 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/deploy` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 124 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/emulate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 125 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/train` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 126 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/validate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 127 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/reconcile` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 128 | `coverage-optimization-rapp` | POST | `/instances/{instance_id}/start` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 129 | `coverage-optimization-rapp` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 130 | `coverage-optimization-rapp` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 131 | `coverage-optimization-rapp` | GET | `/sim-producer/health` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 132 | `coverage-optimization-rapp` | POST | `/sim-producer/jobs` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 133 | `coverage-optimization-rapp` | DELETE | `/sim-producer/jobs/{data_job_id}` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 134 | `coverage-optimization-rapp` | POST | `/sim-producer/publish` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 135 | `coverage-optimization-rapp` | POST | `/sim-producer/register` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 136 | `coverage-optimization-rapp` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 137 | `dme` | GET | `/actions` | delete | DME O1 action mediation retires; network writes are Level-2 intents to SO SMOS | S3.12, S5.9 |
| 138 | `dme` | POST | `/actions` | delete | DME O1 action mediation retires; network writes are Level-2 intents to SO SMOS | S3.12, S5.9 |
| 139 | `dme` | GET | `/actions/{action_id}` | delete | DME O1 action mediation retires; network writes are Level-2 intents to SO SMOS | S3.12, S5.9 |
| 140 | `dme` | DELETE | `/data-jobs` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 141 | `dme` | GET | `/data-jobs` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 142 | `dme` | POST | `/data-jobs` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 143 | `dme` | DELETE | `/data-jobs/{data_job_id}` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 144 | `dme` | GET | `/data-jobs/{data_job_id}` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 145 | `dme` | PUT | `/data-jobs/{data_job_id}` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 146 | `dme` | GET | `/data-jobs/{data_job_id}/records` | replaced | R1GAP v14 §5.2.6 data delivery (pull / push / streaming), R1TD v05 delivery schemas | S3.17 |
| 147 | `dme` | POST | `/data-jobs/{data_job_id}/records` | replaced | R1GAP v14 §5.2.6 data delivery (pull / push / streaming), R1TD v05 delivery schemas | S3.17 |
| 148 | `dme` | GET | `/data-jobs/{data_job_id}/status` | replaced | R1GAP v14 §5.2.4 data request / §5.2.5 data subscription (data job), R1AP path form | S3.17 |
| 149 | `dme` | GET | `/dme-types` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 150 | `dme` | DELETE | `/dme-types/{dme_type_id}` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 151 | `dme` | GET | `/dme-types/{dme_type_id}` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 152 | `dme` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 153 | `dme` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 154 | `dme` | GET | `/offers` | replaced | R1GAP v14 §5.2.7 data offer service | S3.17 |
| 155 | `dme` | POST | `/offers` | replaced | R1GAP v14 §5.2.7 data offer service | S3.17 |
| 156 | `dme` | DELETE | `/offers/{offer_id}` | replaced | R1GAP v14 §5.2.7 data offer service | S3.17 |
| 157 | `dme` | GET | `/offers/{offer_id}` | replaced | R1GAP v14 §5.2.7 data offer service | S3.17 |
| 158 | `dme` | POST | `/offers/{offer_id}/notify` | replaced | R1GAP v14 §5.2.7 data offer service | S3.17 |
| 159 | `dme` | DELETE | `/production-capabilities` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 160 | `dme` | GET | `/production-capabilities` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 161 | `dme` | POST | `/production-capabilities` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 162 | `dme` | GET | `/production-capabilities/{producer_id}` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 163 | `dme` | GET | `/production-capabilities/{producer_id}/status` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 164 | `dme` | DELETE | `/production-capabilities/{producer_id}/types` | replaced | R1GAP v14 §5.2.2 data registration / §5.2.3 data discovery, R1TD v05 type ids `namespace:name:semver` | S3.17 |
| 165 | `dme` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 166 | `dme` | GET | `/smo-events` | [D] exception | D-04 SMO event feed (CloudEvents, internal only): the console's live updates and the Kafka relay's source | S6.10 |
| 167 | `dme` | GET | `/type-subscriptions` | replaced | R1GAP v14 §5.2.3 data discovery (type availability subscription) | S3.17 |
| 168 | `dme` | POST | `/type-subscriptions` | replaced | R1GAP v14 §5.2.3 data discovery (type availability subscription) | S3.17 |
| 169 | `dme` | DELETE | `/type-subscriptions/{subscription_id}` | replaced | R1GAP v14 §5.2.3 data discovery (type availability subscription) | S3.17 |
| 170 | `dme` | GET | `/type-subscriptions/{subscription_id}` | replaced | R1GAP v14 §5.2.3 data discovery (type availability subscription) | S3.17 |
| 171 | `dme` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 172 | `energy-saving-rapp` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 173 | `energy-saving-rapp` | GET | `/instances` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 174 | `energy-saving-rapp` | GET | `/instances/{instance_id}` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 175 | `energy-saving-rapp` | GET | `/instances/{instance_id}/cell-states` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 176 | `energy-saving-rapp` | GET | `/instances/{instance_id}/cells` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 177 | `energy-saving-rapp` | DELETE | `/instances/{instance_id}/cells/{cell_id}/override` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 178 | `energy-saving-rapp` | POST | `/instances/{instance_id}/cells/{cell_id}/override` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 179 | `energy-saving-rapp` | GET | `/instances/{instance_id}/dashboard` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 180 | `energy-saving-rapp` | GET | `/instances/{instance_id}/decisions` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 181 | `energy-saving-rapp` | POST | `/instances/{instance_id}/demo/emulate` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 182 | `energy-saving-rapp` | POST | `/instances/{instance_id}/demo/history` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 183 | `energy-saving-rapp` | POST | `/instances/{instance_id}/demo/live` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 184 | `energy-saving-rapp` | POST | `/instances/{instance_id}/demo/prepare` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 185 | `energy-saving-rapp` | POST | `/instances/{instance_id}/evaluate` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 186 | `energy-saving-rapp` | POST | `/instances/{instance_id}/lifecycle/deploy` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 187 | `energy-saving-rapp` | POST | `/instances/{instance_id}/lifecycle/emulate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 188 | `energy-saving-rapp` | POST | `/instances/{instance_id}/lifecycle/train` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 189 | `energy-saving-rapp` | POST | `/instances/{instance_id}/lifecycle/validate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 190 | `energy-saving-rapp` | POST | `/instances/{instance_id}/reconcile` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 191 | `energy-saving-rapp` | POST | `/instances/{instance_id}/start` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 192 | `energy-saving-rapp` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 193 | `energy-saving-rapp` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 194 | `energy-saving-rapp` | GET | `/sim-producer/health` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 195 | `energy-saving-rapp` | POST | `/sim-producer/jobs` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 196 | `energy-saving-rapp` | DELETE | `/sim-producer/jobs/{data_job_id}` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 197 | `energy-saving-rapp` | POST | `/sim-producer/publish` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 198 | `energy-saving-rapp` | POST | `/sim-producer/register` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 199 | `energy-saving-rapp` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 200 | `focom` | GET | `/alarm-subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 201 | `focom` | POST | `/alarm-subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 202 | `focom` | DELETE | `/alarm-subscriptions/{subscription_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 203 | `focom` | GET | `/alarm-subscriptions/{subscription_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 204 | `focom` | GET | `/alarms` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 205 | `focom` | POST | `/alarms/ingest` | replaced | O2ims v12 alarm / performance notifications to FOCOM's subscriptions (inbound callbacks) | S4.9 |
| 206 | `focom` | GET | `/alarms/{alarm_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 207 | `focom` | PATCH | `/alarms/{alarm_id}/ack` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 208 | `focom` | PATCH | `/alarms/{alarm_id}/clear` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 209 | `focom` | PATCH | `/alarms/{alarm_id}/severity` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 210 | `focom` | GET | `/artifact-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 211 | `focom` | POST | `/artifact-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 212 | `focom` | DELETE | `/artifact-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 213 | `focom` | GET | `/artifact-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 214 | `focom` | GET | `/artifact-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 215 | `focom` | POST | `/artifact-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 216 | `focom` | DELETE | `/artifact-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 217 | `focom` | GET | `/artifact-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 218 | `focom` | GET | `/cluster-resource-groups` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 219 | `focom` | POST | `/cluster-resource-groups` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 220 | `focom` | DELETE | `/cluster-resource-groups/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 221 | `focom` | GET | `/cluster-resource-groups/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 222 | `focom` | GET | `/cluster-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 223 | `focom` | POST | `/cluster-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 224 | `focom` | DELETE | `/cluster-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 225 | `focom` | GET | `/cluster-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 226 | `focom` | GET | `/cluster-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 227 | `focom` | POST | `/cluster-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 228 | `focom` | DELETE | `/cluster-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 229 | `focom` | GET | `/cluster-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 230 | `focom` | GET | `/deployment-managers` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 231 | `focom` | GET | `/deployment-managers/{deployment_manager_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 232 | `focom` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 233 | `focom` | GET | `/infrastructure-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 234 | `focom` | POST | `/infrastructure-resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 235 | `focom` | DELETE | `/infrastructure-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 236 | `focom` | GET | `/infrastructure-resource-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 237 | `focom` | GET | `/infrastructure-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 238 | `focom` | POST | `/infrastructure-resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 239 | `focom` | DELETE | `/infrastructure-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 240 | `focom` | GET | `/infrastructure-resources/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 241 | `focom` | GET | `/inventory` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 242 | `focom` | GET | `/inventory/subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 243 | `focom` | POST | `/inventory/subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 244 | `focom` | DELETE | `/inventory/subscriptions/{subscription_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 245 | `focom` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 246 | `focom` | GET | `/locations` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 247 | `focom` | POST | `/locations` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 248 | `focom` | DELETE | `/locations/{location_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 249 | `focom` | GET | `/locations/{location_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 250 | `focom` | GET | `/node-cluster-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 251 | `focom` | POST | `/node-cluster-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 252 | `focom` | DELETE | `/node-cluster-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 253 | `focom` | GET | `/node-cluster-types/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 254 | `focom` | GET | `/node-clusters` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 255 | `focom` | POST | `/node-clusters` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 256 | `focom` | DELETE | `/node-clusters/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 257 | `focom` | GET | `/node-clusters/{object_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 258 | `focom` | GET | `/o-cloud-sites` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 259 | `focom` | POST | `/o-cloud-sites` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 260 | `focom` | DELETE | `/o-cloud-sites/{site_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 261 | `focom` | GET | `/o-cloud-sites/{site_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 262 | `focom` | GET | `/performance` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 263 | `focom` | GET | `/performance-jobs` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 264 | `focom` | POST | `/performance-jobs` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 265 | `focom` | DELETE | `/performance-jobs/{job_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 266 | `focom` | GET | `/performance-jobs/{job_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 267 | `focom` | PATCH | `/performance-jobs/{job_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 268 | `focom` | GET | `/performance-subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 269 | `focom` | POST | `/performance-subscriptions` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 270 | `focom` | DELETE | `/performance-subscriptions/{subscription_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 271 | `focom` | GET | `/performance-subscriptions/{subscription_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 272 | `focom` | POST | `/performance/ingest` | replaced | O2ims v12 alarm / performance notifications to FOCOM's subscriptions (inbound callbacks) | S4.9 |
| 273 | `focom` | GET | `/provisioning-requests` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 274 | `focom` | POST | `/provisioning-requests` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 275 | `focom` | DELETE | `/provisioning-requests/{request_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 276 | `focom` | GET | `/provisioning-requests/{request_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 277 | `focom` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 278 | `focom` | GET | `/resource-pools` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 279 | `focom` | POST | `/resource-pools` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 280 | `focom` | DELETE | `/resource-pools/{resource_pool_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 281 | `focom` | GET | `/resource-pools/{resource_pool_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 282 | `focom` | GET | `/resource-pools/{resource_pool_id}/resources` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 283 | `focom` | GET | `/resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 284 | `focom` | POST | `/resource-types` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 285 | `focom` | GET | `/resource-types/{resource_type_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 286 | `focom` | POST | `/resources/provision` | replaced | O2ims v12 `ProvisioningRequest` | S4.9 |
| 287 | `focom` | DELETE | `/resources/{resource_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 288 | `focom` | GET | `/resources/{resource_id}` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 289 | `focom` | GET | `/resources/{resource_id}/status` | replaced | O2ims v12 at its standard path (inventory, subscriptions, alarms, performance, provisioning) | S4.9 |
| 290 | `focom` | GET | `/resources/{resource_id}/utilisation` | replaced | O2ims v12 performance (measurement jobs and reports) | S4.9 |
| 291 | `focom` | GET | `/topology` | replaced | WG10 TE&IV R1 topology query (`HOSTED_ON_OCLOUD` edges written by FOCOM) | S4.5 |
| 292 | `focom` | GET | `/utilisation` | replaced | O2ims v12 performance (measurement jobs and reports) | S4.9 |
| 293 | `focom` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 294 | `intent-service` | GET | `/autonomy-dispatches` | delete | autonomy dispatch retires; an rApp submits a Level-2 intent to SO SMOS | S5.4, S5.9 |
| 295 | `intent-service` | POST | `/autonomy-dispatches` | delete | autonomy dispatch retires; an rApp submits a Level-2 intent to SO SMOS | S5.4, S5.9 |
| 296 | `intent-service` | GET | `/autonomy-dispatches/{dispatch_id}` | delete | autonomy dispatch retires; an rApp submits a Level-2 intent to SO SMOS | S5.4, S5.9 |
| 297 | `intent-service` | POST | `/autonomy-dispatches/{dispatch_id}/reject` | delete | autonomy dispatch retires; an rApp submits a Level-2 intent to SO SMOS | S5.4, S5.9 |
| 298 | `intent-service` | POST | `/autonomy-dispatches/{dispatch_id}/resolve` | delete | autonomy dispatch retires; an rApp submits a Level-2 intent to SO SMOS | S5.4, S5.9 |
| 299 | `intent-service` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 300 | `intent-service` | GET | `/intent-handling-functions` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 301 | `intent-service` | POST | `/intent-handling-functions` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS createMOI (in SO SMOS) | S5.1 |
| 302 | `intent-service` | DELETE | `/intent-handling-functions/{rmih_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS deleteMOI (in SO SMOS) | S5.1 |
| 303 | `intent-service` | GET | `/intent-reports` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 304 | `intent-service` | POST | `/intent-reports` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS createMOI (in SO SMOS) | S5.1 |
| 305 | `intent-service` | GET | `/intent-reports/{report_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 306 | `intent-service` | GET | `/intent-utility-formulas` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 307 | `intent-service` | POST | `/intent-utility-formulas` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS createMOI (in SO SMOS) | S5.1 |
| 308 | `intent-service` | DELETE | `/intent-utility-formulas/{formula_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS deleteMOI (in SO SMOS) | S5.1 |
| 309 | `intent-service` | GET | `/intent-utility-formulas/{formula_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 310 | `intent-service` | GET | `/intents` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 311 | `intent-service` | POST | `/intents` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS createMOI (in SO SMOS) | S5.1 |
| 312 | `intent-service` | DELETE | `/intents/{intent_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS deleteMOI (in SO SMOS) | S5.1 |
| 313 | `intent-service` | GET | `/intents/{intent_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS getMOIAttributes (in SO SMOS) | S5.1 |
| 314 | `intent-service` | PATCH | `/intents/{intent_id}/admin-state` | replaced | TS 28.312 v20.0.0 Intent MnS: TS 28.532 ProvMnS modifyMOIAttributes `administrativeState` on `Intent` (in SO SMOS) | S5.1 |
| 315 | `intent-service` | POST | `/intents/{intent_id}/negotiation-feedback` | replaced | TS 28.312 v20.0.0 §4.6.2 negotiation: modifyMOIAttributes on `Intent` with the negotiation context (in SO SMOS) | S5.1, S5.15 |
| 316 | `intent-service` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 317 | `intent-service` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 318 | `intent-service` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 319 | `mdaf` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 320 | `mdaf` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 321 | `mdaf` | GET | `/mda-functions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDAFunction` [N] TS 28.104 | S7.5 |
| 322 | `mdaf` | POST | `/mda-functions` | replaced | TS 28.532 ProvMnS createMOI on `MDAFunction` [N] TS 28.104 | S7.5 |
| 323 | `mdaf` | DELETE | `/mda-functions/{function_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MDAFunction` [N] TS 28.104 | S7.5 |
| 324 | `mdaf` | GET | `/mda-functions/{function_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDAFunction` [N] TS 28.104 | S7.5 |
| 325 | `mdaf` | PUT | `/mda-functions/{function_id}` | replaced | TS 28.532 ProvMnS createMOI / modifyMOIAttributes on `MDAFunction` [N] TS 28.104 | S7.5 |
| 326 | `mdaf` | GET | `/mda-reports` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDAReport` [N] TS 28.104 | S7.5, S7.10 |
| 327 | `mdaf` | POST | `/mda-reports` | replaced | TS 28.532 ProvMnS createMOI on `MDAReport` [N] TS 28.104 | S7.5, S7.10 |
| 328 | `mdaf` | GET | `/mda-reports/{report_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDAReport` [N] TS 28.104 | S7.5, S7.10 |
| 329 | `mdaf` | GET | `/mda-reports/{report_id}/file` | replaced | TS 28.532 file data reporting MnS (MDA report file) | S7.10 |
| 330 | `mdaf` | GET | `/mda-requests` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDARequest` [N] TS 28.104 | S7.5 |
| 331 | `mdaf` | POST | `/mda-requests` | replaced | TS 28.532 ProvMnS createMOI on `MDARequest` [N] TS 28.104 | S7.5 |
| 332 | `mdaf` | DELETE | `/mda-requests/{request_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MDARequest` [N] TS 28.104 | S7.5 |
| 333 | `mdaf` | GET | `/mda-requests/{request_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDARequest` [N] TS 28.104 | S7.5 |
| 334 | `mdaf` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 335 | `mdaf` | GET | `/reports` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 336 | `mdaf` | POST | `/reports` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 337 | `mdaf` | GET | `/reports/{report_id}` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 338 | `mdaf` | GET | `/subscriptions` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 339 | `mdaf` | POST | `/subscriptions` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 340 | `mdaf` | DELETE | `/subscriptions/{subscription_id}` | replaced | TS 28.104 MDA reporting (file, stream, notification) and TS 28.622 `NtfSubscriptionControl` | S7.10, S1.13 |
| 341 | `mdaf` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 342 | `mllf` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 343 | `mllf` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 344 | `mllf` | POST | `/models/{model_id}/deploy` | delete | the deploy gate becomes an in-process call inside AIMgF; callers use R1GAP v14 §5.6.7 | S2.2 |
| 345 | `mllf` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 346 | `mllf` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 347 | `mlmr` | GET | `/coordination-groups` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelCoordinationGroup` [N] TS 28.105 (in AIMgF) | S2.1, S2.10 |
| 348 | `mlmr` | POST | `/coordination-groups` | replaced | TS 28.532 ProvMnS createMOI on `MLModelCoordinationGroup` [N] TS 28.105 (in AIMgF) | S2.1, S2.10 |
| 349 | `mlmr` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 350 | `mlmr` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 351 | `mlmr` | GET | `/ml-model-coordination-groups/{group_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MLModelCoordinationGroup` [N] TS 28.105 (in AIMgF) | S2.1, S2.10 |
| 352 | `mlmr` | GET | `/ml-model-repositories` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 353 | `mlmr` | POST | `/ml-model-repositories` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 354 | `mlmr` | DELETE | `/ml-model-repositories/{repository_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 355 | `mlmr` | GET | `/ml-model-repositories/{repository_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 356 | `mlmr` | GET | `/ml-models/{model_id}` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 357 | `mlmr` | GET | `/models` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 358 | `mlmr` | POST | `/models` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 359 | `mlmr` | DELETE | `/models/{model_id}` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 360 | `mlmr` | GET | `/models/{model_id}` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 361 | `mlmr` | PUT | `/models/{model_id}` | replaced | R1GAP v14 §5.6.2 / §5.6.4 model registration and discovery; TS 28.532 ProvMnS on `MLModel` [N] TS 28.105 (in AIMgF) | S2.1, S2.7, S2.11 |
| 362 | `mlmr` | POST | `/models/{model_id}/artifact` | replaced | R1GAP v14 §5.6.3 AI/ML model storage service; the artifact lives in the object store (D10) | S2.5, S2.11 |
| 363 | `mlmr` | GET | `/models/{model_id}/artifact/{artifact_version}` | replaced | R1GAP v14 §5.6.3 AI/ML model storage service; the artifact lives in the object store (D10) | S2.5, S2.11 |
| 364 | `mlmr` | PUT | `/models/{model_id}/owner` | [D] exception | D-08 model owner (SEC-15.3; package-level owner from S4.7); no TS 28.105 attribute | S2.1, S4.7 |
| 365 | `mlmr` | PATCH | `/models/{model_id}/phase-info` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `MLModel` (in AIMgF) | S2.1, S2.7 |
| 366 | `mlmr` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 367 | `mlmr` | GET | `/storages` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 368 | `mlmr` | POST | `/storages` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 369 | `mlmr` | DELETE | `/storages/{storage_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 370 | `mlmr` | GET | `/storages/{storage_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 371 | `mlmr` | PATCH | `/storages/{storage_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 372 | `mlmr` | PUT | `/storages/{storage_id}` | replaced | TS 29.482 v19.1.0 MLR model management (storage profiles), served by AIMgF over the object store | S2.1, S2.5 |
| 373 | `mlmr` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 374 | `mobility-optimization-rapp` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 375 | `mobility-optimization-rapp` | GET | `/instances` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 376 | `mobility-optimization-rapp` | GET | `/instances/{instance_id}` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 377 | `mobility-optimization-rapp` | GET | `/instances/{instance_id}/dashboard` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 378 | `mobility-optimization-rapp` | GET | `/instances/{instance_id}/decisions` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 379 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/demo/emulate` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 380 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/demo/history` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 381 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/demo/kpi-hour` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 382 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/demo/live` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 383 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/demo/prepare` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 384 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/dmro` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 385 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/evaluate` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 386 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/deploy` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 387 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/emulate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 388 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/train` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 389 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/lifecycle/validate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 390 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/reconcile` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 391 | `mobility-optimization-rapp` | GET | `/instances/{instance_id}/relations` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 392 | `mobility-optimization-rapp` | POST | `/instances/{instance_id}/start` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 393 | `mobility-optimization-rapp` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 394 | `mobility-optimization-rapp` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 395 | `mobility-optimization-rapp` | GET | `/sim-producer/health` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 396 | `mobility-optimization-rapp` | POST | `/sim-producer/jobs` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 397 | `mobility-optimization-rapp` | DELETE | `/sim-producer/jobs/{data_job_id}` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 398 | `mobility-optimization-rapp` | POST | `/sim-producer/publish` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 399 | `mobility-optimization-rapp` | POST | `/sim-producer/register` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 400 | `mobility-optimization-rapp` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 401 | `mock-o1-adaptor` | GET | `/.well-known/host-meta` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 402 | `mock-o1-adaptor` | GET | `/capabilities` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 403 | `mock-o1-adaptor` | POST | `/edit-config` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 404 | `mock-o1-adaptor` | GET | `/edit-config/{managed_object_ref}` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 405 | `mock-o1-adaptor` | POST | `/emit/alarm` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 406 | `mock-o1-adaptor` | POST | `/emit/alarm-clear` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 407 | `mock-o1-adaptor` | POST | `/emit/heartbeat` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 408 | `mock-o1-adaptor` | POST | `/emit/pm-file` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 409 | `mock-o1-adaptor` | POST | `/emit/pm-report` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 410 | `mock-o1-adaptor` | POST | `/emit/software-phase` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 411 | `mock-o1-adaptor` | POST | `/faults` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 412 | `mock-o1-adaptor` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 413 | `mock-o1-adaptor` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 414 | `mock-o1-adaptor` | GET | `/objects/{managed_object_ref}` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 415 | `mock-o1-adaptor` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 416 | `mock-o1-adaptor` | POST | `/restconf/data` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 417 | `mock-o1-adaptor` | DELETE | `/restconf/data/{path}` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 418 | `mock-o1-adaptor` | GET | `/restconf/data/{path}` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 419 | `mock-o1-adaptor` | PATCH | `/restconf/data/{path}` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 420 | `mock-o1-adaptor` | POST | `/restconf/data/{path}` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 421 | `mock-o1-adaptor` | PUT | `/restconf/data/{path}` | keep (standard) | RESTCONF RFC 8040 [N] (test double of an O1 element) | – |
| 422 | `mock-o1-adaptor` | GET | `/software-rpcs` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 423 | `mock-o1-adaptor` | DELETE | `/state` | [D] exception | D-16 test double controls (emit, faults, state); never deployed in production | S0.15 |
| 424 | `mock-o1-adaptor` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 425 | `nfo` | GET | `/deployments` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 426 | `nfo` | POST | `/deployments` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 427 | `nfo` | DELETE | `/deployments/{nf_deployment_id}` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 428 | `nfo` | GET | `/deployments/{nf_deployment_id}` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 429 | `nfo` | POST | `/deployments/{nf_deployment_id}/dms-notifications` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 430 | `nfo` | POST | `/deployments/{nf_deployment_id}/heal` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 431 | `nfo` | GET | `/deployments/{nf_deployment_id}/operations` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 432 | `nfo` | GET | `/deployments/{nf_deployment_id}/placement` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 433 | `nfo` | GET | `/deployments/{nf_deployment_id}/resources` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 434 | `nfo` | POST | `/deployments/{nf_deployment_id}/scale` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 435 | `nfo` | GET | `/descriptors` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 436 | `nfo` | POST | `/descriptors` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 437 | `nfo` | DELETE | `/descriptors/{descriptor_id}` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 438 | `nfo` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 439 | `nfo` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 440 | `nfo` | GET | `/operations/{operation_id}` | replaced | O2dms Kubernetes-native profile v7 (deployment, operations, notifications) | S4.4, S4.9 |
| 441 | `nfo` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 442 | `nfo` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 443 | `onboarding` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 444 | `onboarding` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 445 | `onboarding` | GET | `/packages` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 446 | `onboarding` | POST | `/packages` | replaced | TS 28.532 ProvMnS createMOI on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 447 | `onboarding` | POST | `/packages/upload` | replaced | TS 28.532 ProvMnS createMOI on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 448 | `onboarding` | DELETE | `/packages/{package_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 449 | `onboarding` | GET | `/packages/{package_id}/artifacts` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 450 | `onboarding` | POST | `/packages/{package_id}/cancel-delete` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `RappPackage` (administrative state) | S4.1, S4.2 |
| 451 | `onboarding` | POST | `/packages/{package_id}/deprecate` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `RappPackage` (administrative state) | S4.1, S4.2 |
| 452 | `onboarding` | POST | `/packages/{package_id}/deprime` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `RappPackage` (administrative state) | S4.1, S4.2 |
| 453 | `onboarding` | GET | `/packages/{package_id}/onboarding-status` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappPackage` (WG10 App Packaging v1.00, provisional until S0.14) | S4.1, S4.2 |
| 454 | `onboarding` | POST | `/packages/{package_id}/prime` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on `RappPackage` (administrative state) | S4.1, S4.2 |
| 455 | `onboarding` | GET | `/packages/{package_id}/usage` | delete | a package is in use when `RappInstance` objects name it; read through ProvMnS | S4.2 |
| 456 | `onboarding` | POST | `/packages/{package_id}/usage/start` | delete | a package is in use when `RappInstance` objects name it; read through ProvMnS | S4.2 |
| 457 | `onboarding` | POST | `/packages/{package_id}/usage/{registration_id}/release` | delete | a package is in use when `RappInstance` objects name it; read through ProvMnS | S4.2 |
| 458 | `onboarding` | POST | `/packages/{package_id}/usage/{registration_id}/stop` | delete | a package is in use when `RappInstance` objects name it; read through ProvMnS | S4.2 |
| 459 | `onboarding` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 460 | `onboarding` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 461 | `r1-termination` | GET | `/bootstrap` | delete | peers are found by CAPIF discovery (TS 29.222 CAPIF_Discover_Service_API) | S1.16 |
| 462 | `r1-termination` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 463 | `r1-termination` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 464 | `r1-termination` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 465 | `r1-termination` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 466 | `r1-termination` | DELETE | `/{full_path}` | keep (standard) | the R1 gateway forwards standard operations; it routes by MnS registry and CAPIF from S1.8 / S1.16 | S1.8 |
| 467 | `r1-termination` | GET | `/{full_path}` | keep (standard) | the R1 gateway forwards standard operations; it routes by MnS registry and CAPIF from S1.8 / S1.16 | S1.8 |
| 468 | `r1-termination` | PATCH | `/{full_path}` | keep (standard) | the R1 gateway forwards standard operations; it routes by MnS registry and CAPIF from S1.8 / S1.16 | S1.8 |
| 469 | `r1-termination` | POST | `/{full_path}` | keep (standard) | the R1 gateway forwards standard operations; it routes by MnS registry and CAPIF from S1.8 / S1.16 | S1.8 |
| 470 | `r1-termination` | PUT | `/{full_path}` | keep (standard) | the R1 gateway forwards standard operations; it routes by MnS registry and CAPIF from S1.8 / S1.16 | S1.8 |
| 471 | `ran-analytics` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 472 | `ran-analytics` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 473 | `ran-analytics` | GET | `/producers` | replaced | TS 28.532 ProvMnS getMOIAttributes on `MDAFunction` [N] TS 28.104 (registry absorbed by MDAF) | S7.5 |
| 474 | `ran-analytics` | POST | `/producers` | replaced | TS 28.532 ProvMnS createMOI on `MDAFunction` [N] TS 28.104 (registry absorbed by MDAF) | S7.5 |
| 475 | `ran-analytics` | DELETE | `/producers/{producer_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `MDAFunction` [N] TS 28.104 (registry absorbed by MDAF) | S7.5 |
| 476 | `ran-analytics` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 477 | `ran-analytics` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 478 | `ran-nf-oam` | GET | `/alarms` | replaced | TS 28.111 v19.3.0 getAlarmList on `AlarmList` (comments, correlation and history are AlarmRecord attributes) | S3.15 |
| 479 | `ran-nf-oam` | GET | `/alarms/counts` | replaced | TS 28.111 v19.3.0 getAlarmCount | S3.15 |
| 480 | `ran-nf-oam` | POST | `/alarms/ingest` | replaced | O1 alarm notifications: VES `stndDefined` notifyNewAlarm / notifyClearedAlarm (TS 28.111) | S3.15 |
| 481 | `ran-nf-oam` | GET | `/alarms/stats` | replaced | TS 28.111 v19.3.0 getAlarmCount | S3.15 |
| 482 | `ran-nf-oam` | PATCH | `/alarms/{alarm_id}/ack` | replaced | TS 28.111 v19.3.0 acknowledgeAlarms | S3.15 |
| 483 | `ran-nf-oam` | PATCH | `/alarms/{alarm_id}/clear` | replaced | TS 28.111 v19.3.0 clearAlarms | S3.15 |
| 484 | `ran-nf-oam` | GET | `/alarms/{alarm_id}/comments` | replaced | TS 28.111 v19.3.0 getAlarmList on `AlarmList` (comments, correlation and history are AlarmRecord attributes) | S3.15 |
| 485 | `ran-nf-oam` | POST | `/alarms/{alarm_id}/comments` | replaced | TS 28.111 v19.3.0 getAlarmList on `AlarmList` (comments, correlation and history are AlarmRecord attributes) | S3.15 |
| 486 | `ran-nf-oam` | GET | `/alarms/{alarm_id}/correlated` | replaced | TS 28.111 v19.3.0 getAlarmList on `AlarmList` (comments, correlation and history are AlarmRecord attributes) | S3.15 |
| 487 | `ran-nf-oam` | GET | `/alarms/{alarm_id}/history` | replaced | TS 28.111 v19.3.0 getAlarmList on `AlarmList` (comments, correlation and history are AlarmRecord attributes) | S3.15 |
| 488 | `ran-nf-oam` | GET | `/approval-subscriptions` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 489 | `ran-nf-oam` | POST | `/approval-subscriptions` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 490 | `ran-nf-oam` | DELETE | `/approval-subscriptions/{subscription_id}` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 491 | `ran-nf-oam` | GET | `/capabilities` | replaced | TS 28.622 `MnsInfo` in the MnS registry | S1.12 |
| 492 | `ran-nf-oam` | GET | `/cell-guards` | replaced | TE&IV cell attributes and the cell lock table (one writer, SO SMOS) | S3.1, S3.2 |
| 493 | `ran-nf-oam` | GET | `/cm-schemas` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 494 | `ran-nf-oam` | POST | `/cm-schemas` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 495 | `ran-nf-oam` | GET | `/cm-schemas/{schema_name}` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 496 | `ran-nf-oam` | POST | `/config-history/purge` | delete | worker task in process; not an interface | S3.3 |
| 497 | `ran-nf-oam` | GET | `/config-jobs` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on the TS 28.541 NRM through RAN NF OAM's facade, called by SO SMOS only | S3.3, S5.9 |
| 498 | `ran-nf-oam` | POST | `/config-jobs` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on the TS 28.541 NRM through RAN NF OAM's facade, called by SO SMOS only | S3.3, S5.9 |
| 499 | `ran-nf-oam` | POST | `/config-jobs/advance-due` | delete | worker task in process; not an interface | S3.3 |
| 500 | `ran-nf-oam` | GET | `/config-jobs/{job_id}` | replaced | TS 28.532 ProvMnS modifyMOIAttributes on the TS 28.541 NRM through RAN NF OAM's facade, called by SO SMOS only | S3.3, S5.9 |
| 501 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/abort` | replaced | TS 28.312 operations on the Level-2 intent (admin state, delete); SO SMOS restores from its snapshot | S5.6, S5.7 |
| 502 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/approve` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 503 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/continue` | replaced | TS 28.312 operations on the Level-2 intent (admin state, delete); SO SMOS restores from its snapshot | S5.6, S5.7 |
| 504 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/halt` | replaced | TS 28.312 operations on the Level-2 intent (admin state, delete); SO SMOS restores from its snapshot | S5.6, S5.7 |
| 505 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/kpi-check` | replaced | TS 28.312 operations on the Level-2 intent (admin state, delete); SO SMOS restores from its snapshot | S5.6, S5.7 |
| 506 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/reject` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 507 | `ran-nf-oam` | POST | `/config-jobs/{job_id}/rollback` | replaced | TS 28.312 operations on the Level-2 intent (admin state, delete); SO SMOS restores from its snapshot | S5.6, S5.7 |
| 508 | `ran-nf-oam` | GET | `/decision-records` | [D] exception | D-05 decision records on the hash-chained audit (moves to SO SMOS) | S5.4 |
| 509 | `ran-nf-oam` | GET | `/decision-records/export.csv` | [D] exception | D-05 decision records on the hash-chained audit (moves to SO SMOS) | S5.4 |
| 510 | `ran-nf-oam` | GET | `/decision-records/{decision_id}` | [D] exception | D-05 decision records on the hash-chained audit (moves to SO SMOS) | S5.4 |
| 511 | `ran-nf-oam` | POST | `/dme-jobs` | replaced | R1GAP v14 §5.2.2 data registration: the producer side of a DME data job (R1TD v05) | S3.17 |
| 512 | `ran-nf-oam` | DELETE | `/dme-jobs/{data_job_id}` | replaced | R1GAP v14 §5.2.2 data registration: the producer side of a DME data job (R1TD v05) | S3.17 |
| 513 | `ran-nf-oam` | GET | `/element-onboarding` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 514 | `ran-nf-oam` | GET | `/element-onboarding/{managed_element_ref}` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 515 | `ran-nf-oam` | POST | `/element-onboarding/{managed_element_ref}/apply` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 516 | `ran-nf-oam` | POST | `/element-onboarding/{managed_element_ref}/select` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 517 | `ran-nf-oam` | POST | `/file-subscriptions` | replaced | TS 28.532 v19.1.0 file data reporting MnS (listAvailableFiles, notifyFileReady) | S3.16 |
| 518 | `ran-nf-oam` | DELETE | `/file-subscriptions/{subscription_id}` | replaced | TS 28.532 v19.1.0 file data reporting MnS (listAvailableFiles, notifyFileReady) | S3.16 |
| 519 | `ran-nf-oam` | GET | `/files` | replaced | TS 28.532 v19.1.0 file data reporting MnS (listAvailableFiles, notifyFileReady) | S3.16 |
| 520 | `ran-nf-oam` | GET | `/fm-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 521 | `ran-nf-oam` | POST | `/fm-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 522 | `ran-nf-oam` | DELETE | `/fm-subscriptions/{subscription_id}` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 523 | `ran-nf-oam` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 524 | `ran-nf-oam` | GET | `/kpi-definitions` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 525 | `ran-nf-oam` | GET | `/kpi-definitions/standard` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 526 | `ran-nf-oam` | POST | `/kpi-definitions/standard` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 527 | `ran-nf-oam` | DELETE | `/kpi-definitions/{name}` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 528 | `ran-nf-oam` | GET | `/kpi-definitions/{name}` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 529 | `ran-nf-oam` | PUT | `/kpi-definitions/{name}` | [D] exception | D-10 KPI formula definitions (whitelisted formula language); TS 28.554 names the KPIs, it has no definition operation | S3.8 |
| 530 | `ran-nf-oam` | GET | `/kpi-schedules` | delete | DME computes KPIs per ROP; no schedule or publish call | S3.8 |
| 531 | `ran-nf-oam` | DELETE | `/kpi-schedules/{schedule_id}` | delete | DME computes KPIs per ROP; no schedule or publish call | S3.8 |
| 532 | `ran-nf-oam` | GET | `/kpi-schedules/{schedule_id}` | delete | DME computes KPIs per ROP; no schedule or publish call | S3.8 |
| 533 | `ran-nf-oam` | PUT | `/kpi-schedules/{schedule_id}` | delete | DME computes KPIs per ROP; no schedule or publish call | S3.8 |
| 534 | `ran-nf-oam` | GET | `/kpis/{name}` | replaced | DME R1TD data types of KPI ROP rows (and D-02 matrix query) | S3.8, S3.9 |
| 535 | `ran-nf-oam` | POST | `/kpis/{name}/publish` | delete | DME computes KPIs per ROP; no schedule or publish call | S3.8 |
| 536 | `ran-nf-oam` | GET | `/kpis/{name}/series` | replaced | DME R1TD data types of KPI ROP rows (and D-02 matrix query) | S3.8, S3.9 |
| 537 | `ran-nf-oam` | GET | `/lifecycle-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 538 | `ran-nf-oam` | POST | `/lifecycle-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 539 | `ran-nf-oam` | DELETE | `/lifecycle-subscriptions/{subscription_id}` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 540 | `ran-nf-oam` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 541 | `ran-nf-oam` | GET | `/managed-entities` | replaced | TS 28.532 ProvMnS getMOIAttributes on `ManagedElement` (served by TE&IV) | S3.1, S3.3 |
| 542 | `ran-nf-oam` | GET | `/managed-entities/health` | delete | console aggregate, rebuilt from TS 28.111 alarm counts and TE&IV | S6.2 |
| 543 | `ran-nf-oam` | GET | `/managed-entities/scopes` | replaced | TS 28.532 ProvMnS on `ManagedElement` with the ADR 0005 scope as a [D] attribute (TE&IV), read by TS 28.319 AccessRules | S1.14, S3.1 |
| 544 | `ran-nf-oam` | GET | `/managed-entities/worst` | delete | console aggregate, rebuilt from TS 28.111 alarm counts and TE&IV | S6.2 |
| 545 | `ran-nf-oam` | GET | `/managed-entities/{managed_element_ref}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `ManagedElement` (served by TE&IV) | S3.1, S3.3 |
| 546 | `ran-nf-oam` | DELETE | `/managed-entities/{managed_element_ref}/cells/{cell_id}/guards` | replaced | TE&IV cell attributes and the cell lock table (one writer, SO SMOS) | S3.1, S3.2 |
| 547 | `ran-nf-oam` | PUT | `/managed-entities/{managed_element_ref}/cells/{cell_id}/guards` | replaced | TE&IV cell attributes and the cell lock table (one writer, SO SMOS) | S3.1, S3.2 |
| 548 | `ran-nf-oam` | GET | `/managed-entities/{managed_element_ref}/config` | replaced | TS 28.532 ProvMnS getMOIAttributes on `ManagedElement` (served by TE&IV) | S3.1, S3.3 |
| 549 | `ran-nf-oam` | GET | `/managed-entities/{managed_element_ref}/config-history` | [D] exception | D-13 configuration history and before / after diff (no MnS returns history) | S3.6 |
| 550 | `ran-nf-oam` | GET | `/managed-entities/{managed_element_ref}/config-history/diff` | [D] exception | D-13 configuration history and before / after diff (no MnS returns history) | S3.6 |
| 551 | `ran-nf-oam` | POST | `/managed-entities/{managed_element_ref}/managed-objects/refresh` | replaced | TE&IV discovery: RAN NF OAM writes the discovered NRM through TE&IV's ProvMnS | S3.4 |
| 552 | `ran-nf-oam` | PUT | `/managed-entities/{managed_element_ref}/scope` | replaced | TS 28.532 ProvMnS on `ManagedElement` with the ADR 0005 scope as a [D] attribute (TE&IV), read by TS 28.319 AccessRules | S1.14, S3.1 |
| 553 | `ran-nf-oam` | PUT | `/managed-entities/{managed_element_ref}/site-cluster` | replaced | TS 28.532 ProvMnS on `ManagedElement` with the ADR 0005 scope as a [D] attribute (TE&IV), read by TS 28.319 AccessRules | S1.14, S3.1 |
| 554 | `ran-nf-oam` | GET | `/managed-objects/{dn}` | replaced | TS 28.532 ProvMnS getMOIAttributes with scope BASE_ONLY / BASE_NTH_LEVEL / BASE_SUBTREE (TE&IV) | S1.4, S3.1 |
| 555 | `ran-nf-oam` | GET | `/managed-objects/{dn}/children` | replaced | TS 28.532 ProvMnS getMOIAttributes with scope BASE_ONLY / BASE_NTH_LEVEL / BASE_SUBTREE (TE&IV) | S1.4, S3.1 |
| 556 | `ran-nf-oam` | GET | `/managed-objects/{dn}/subtree` | replaced | TS 28.532 ProvMnS getMOIAttributes with scope BASE_ONLY / BASE_NTH_LEVEL / BASE_SUBTREE (TE&IV) | S1.4, S3.1 |
| 557 | `ran-nf-oam` | GET | `/msac/access-rules` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 558 | `ran-nf-oam` | POST | `/msac/access-rules` | replaced | TS 28.532 ProvMnS createMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 559 | `ran-nf-oam` | DELETE | `/msac/access-rules/{rule_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 560 | `ran-nf-oam` | GET | `/msac/access-rules/{rule_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 561 | `ran-nf-oam` | GET | `/msac/identities` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 562 | `ran-nf-oam` | POST | `/msac/identities` | replaced | TS 28.532 ProvMnS createMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 563 | `ran-nf-oam` | DELETE | `/msac/identities/{identity_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 564 | `ran-nf-oam` | GET | `/msac/identities/{identity_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 565 | `ran-nf-oam` | PUT | `/msac/identities/{identity_id}` | replaced | TS 28.532 ProvMnS createMOI / modifyMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 566 | `ran-nf-oam` | GET | `/msac/roles` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 567 | `ran-nf-oam` | POST | `/msac/roles` | replaced | TS 28.532 ProvMnS createMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 568 | `ran-nf-oam` | DELETE | `/msac/roles/{role_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 569 | `ran-nf-oam` | GET | `/msac/roles/{role_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 570 | `ran-nf-oam` | PUT | `/msac/roles/{role_id}` | replaced | TS 28.532 ProvMnS createMOI / modifyMOIAttributes on `Identity` / `Role` / `AccessRule` [N] TS 28.319 | S1.14 |
| 571 | `ran-nf-oam` | GET | `/o1-adaptor-endpoints` | [D] exception | D-14 O1 southbound endpoint registry (where to reach an element); no northbound standard operation | S3.6 |
| 572 | `ran-nf-oam` | POST | `/o1-adaptor-endpoints` | [D] exception | D-14 O1 southbound endpoint registry (where to reach an element); no northbound standard operation | S3.6 |
| 573 | `ran-nf-oam` | POST | `/o1-adaptor-endpoints/discover` | replaced | pnfRegistration and NETCONF call-home (RFC 8071) | S8.2 |
| 574 | `ran-nf-oam` | POST | `/o1-adaptor-endpoints/{endpoint_id}/heartbeat` | replaced | TS 28.532 heartbeat notification (`HeartbeatControl`, TS 28.622) | S1.13 |
| 575 | `ran-nf-oam` | GET | `/o1-adaptor-endpoints/{endpoint_id}/host-keys` | delete | SSH host-key trust replaced by WG11 PKI certificates (CMPv2 / EST) | S8.1 |
| 576 | `ran-nf-oam` | PUT | `/o1-adaptor-endpoints/{endpoint_id}/host-keys` | delete | SSH host-key trust replaced by WG11 PKI certificates (CMPv2 / EST) | S8.1 |
| 577 | `ran-nf-oam` | DELETE | `/o1-adaptor-endpoints/{endpoint_id}/host-keys/{key_type}` | delete | SSH host-key trust replaced by WG11 PKI certificates (CMPv2 / EST) | S8.1 |
| 578 | `ran-nf-oam` | GET | `/onboarding-templates` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 579 | `ran-nf-oam` | DELETE | `/onboarding-templates/{name}` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 580 | `ran-nf-oam` | GET | `/onboarding-templates/{name}` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 581 | `ran-nf-oam` | PUT | `/onboarding-templates/{name}` | [D] exception | D-12 Day-1 configuration templates for zero-touch onboarding (the triggers are standard: pnfRegistration, NETCONF call-home RFC 8071) | S8.2 |
| 582 | `ran-nf-oam` | POST | `/pm-files` | replaced | O1 file-ready notification and VES `stndDefined` PM from the element | S3.16 |
| 583 | `ran-nf-oam` | GET | `/pm-files/{file_id}/file` | replaced | TS 28.532 v19.1.0 file data reporting MnS (listAvailableFiles, notifyFileReady) | S3.16 |
| 584 | `ran-nf-oam` | POST | `/pm-reports` | replaced | O1 file-ready notification and VES `stndDefined` PM from the element | S3.16 |
| 585 | `ran-nf-oam` | GET | `/pm-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 586 | `ran-nf-oam` | POST | `/pm-subscriptions` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 587 | `ran-nf-oam` | DELETE | `/pm-subscriptions/{subscription_id}` | replaced | TS 28.622 `PerfMetricJob` / `NtfSubscriptionControl` through the subscription manager | S1.13, S3.16 |
| 588 | `ran-nf-oam` | DELETE | `/rapp-approval-policy/{invoker_id_}` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 589 | `ran-nf-oam` | GET | `/rapp-approval-policy/{invoker_id_}` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 590 | `ran-nf-oam` | PUT | `/rapp-approval-policy/{invoker_id_}` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 591 | `ran-nf-oam` | GET | `/rapp-approvals` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 592 | `ran-nf-oam` | POST | `/rapp-approvals/expire-due` | delete | worker task in process; not an interface | S3.3 |
| 593 | `ran-nf-oam` | GET | `/rapp-approvals/{approval_id}` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 594 | `ran-nf-oam` | POST | `/rapp-approvals/{approval_id}/approve` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 595 | `ran-nf-oam` | POST | `/rapp-approvals/{approval_id}/reject` | [D] exception | D-03 operator approval queue (moves to SO SMOS's Level-2 intake) | S5.4 |
| 596 | `ran-nf-oam` | GET | `/rapp-kill` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 597 | `ran-nf-oam` | DELETE | `/rapp-kill/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 598 | `ran-nf-oam` | GET | `/rapp-kill/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 599 | `ran-nf-oam` | PUT | `/rapp-kill/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 600 | `ran-nf-oam` | DELETE | `/rapp-limits/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 601 | `ran-nf-oam` | GET | `/rapp-limits/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 602 | `ran-nf-oam` | PUT | `/rapp-limits/{invoker_id_}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 603 | `ran-nf-oam` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 604 | `ran-nf-oam` | GET | `/safeguard-refusals` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 605 | `ran-nf-oam` | POST | `/safeguard-refusals/purge` | delete | worker task in process; not an interface | S3.3 |
| 606 | `ran-nf-oam` | GET | `/safeguard-subscriptions` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 607 | `ran-nf-oam` | POST | `/safeguard-subscriptions` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 608 | `ran-nf-oam` | DELETE | `/safeguard-subscriptions/{subscription_id}` | [D] exception | D-06 rApp safeguards: kill switch, package limits, refusal log (move to SO SMOS's Level-2 intake) | S5.4, S5.11 |
| 609 | `ran-nf-oam` | GET | `/software-campaigns` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 610 | `ran-nf-oam` | POST | `/software-campaigns` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 611 | `ran-nf-oam` | POST | `/software-campaigns/advance-due` | delete | worker task in process; not an interface | S3.3 |
| 612 | `ran-nf-oam` | GET | `/software-campaigns/{campaign_id}` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 613 | `ran-nf-oam` | POST | `/software-campaigns/{campaign_id}/abort` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 614 | `ran-nf-oam` | POST | `/software-campaigns/{campaign_id}/continue` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 615 | `ran-nf-oam` | POST | `/software-campaigns/{campaign_id}/halt` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 616 | `ran-nf-oam` | GET | `/software-campaigns/{campaign_id}/report` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 617 | `ran-nf-oam` | POST | `/software-campaigns/{campaign_id}/rollback` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 618 | `ran-nf-oam` | GET | `/software-management-jobs` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 619 | `ran-nf-oam` | POST | `/software-management-jobs` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 620 | `ran-nf-oam` | GET | `/software-management-jobs/{job_id}` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 621 | `ran-nf-oam` | POST | `/software-management-jobs/{job_id}/abort` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 622 | `ran-nf-oam` | POST | `/software-management-jobs/{job_id}/advance` | [D] exception | D-15 software campaigns and jobs (waves, health gate); the O1 software-management RPCs underneath are O-RAN [N] | – |
| 623 | `ran-nf-oam` | GET | `/topology` | replaced | WG10 TE&IV R1 topology and inventory services (TE&IV module) | S3.4 |
| 624 | `ran-nf-oam` | GET | `/topology/graph` | replaced | WG10 TE&IV R1 topology and inventory services (TE&IV module) | S3.4 |
| 625 | `ran-nf-oam` | GET | `/topology/links` | replaced | WG10 TE&IV R1 topology and inventory services (TE&IV module) | S3.4 |
| 626 | `ran-nf-oam` | GET | `/topology/links/counts` | replaced | WG10 TE&IV R1 topology and inventory services (TE&IV module) | S3.4 |
| 627 | `ran-nf-oam` | GET | `/topology/relation` | replaced | WG10 TE&IV R1 topology and inventory services (TE&IV module) | S3.4 |
| 628 | `ran-nf-oam` | GET | `/vendor-capabilities` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 629 | `ran-nf-oam` | DELETE | `/vendor-capabilities/{vendor_name}` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 630 | `ran-nf-oam` | GET | `/vendor-capabilities/{vendor_name}` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 631 | `ran-nf-oam` | PUT | `/vendor-capabilities/{vendor_name}` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 632 | `ran-nf-oam` | POST | `/vendor-onboarding` | [D] exception | D-11 vendor capability registry and YANG dialect mapper (O1 has no vendor-registration operation) | S3.5 |
| 633 | `ran-nf-oam` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 634 | `ran-nf-oam` | POST | `/ves/eventListener/v7` | keep (standard) | VES 7.2 event listener, the O1 notification path [N] | S3.15 |
| 635 | `rapp-mgmt` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 636 | `rapp-mgmt` | GET | `/instances` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 637 | `rapp-mgmt` | POST | `/instances` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 638 | `rapp-mgmt` | GET | `/instances/performance/latest` | replaced | TS 28.622 `PerfMetricJob` on `RappInstance` | S1.13, S4.2 |
| 639 | `rapp-mgmt` | DELETE | `/instances/{instance_id}` | replaced | TS 28.532 ProvMnS deleteMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 640 | `rapp-mgmt` | GET | `/instances/{instance_id}` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 641 | `rapp-mgmt` | POST | `/instances/{instance_id}/bootstrap-complete` | replaced | SME `ServiceReady` when the rApp publishes its R1 endpoint (CAPIF publish) | S4.6 |
| 642 | `rapp-mgmt` | GET | `/instances/{instance_id}/config` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 643 | `rapp-mgmt` | PUT | `/instances/{instance_id}/config` | replaced | TS 28.532 ProvMnS createMOI / modifyMOIAttributes on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 644 | `rapp-mgmt` | POST | `/instances/{instance_id}/credentials` | replaced | TS 29.222 CAPIF API invoker onboarding and security (credentials of the invoker) | S1.16, S4.8 |
| 645 | `rapp-mgmt` | POST | `/instances/{instance_id}/credentials/renew` | replaced | TS 29.222 CAPIF API invoker onboarding and security (credentials of the invoker) | S1.16, S4.8 |
| 646 | `rapp-mgmt` | POST | `/instances/{instance_id}/fault` | replaced | TS 28.111 alarms on `RappInstance` | S3.15, S4.2 |
| 647 | `rapp-mgmt` | GET | `/instances/{instance_id}/faults` | replaced | TS 28.111 alarms on `RappInstance` | S3.15, S4.2 |
| 648 | `rapp-mgmt` | DELETE | `/instances/{instance_id}/kill` | [D] exception | D-06 rApp safeguards: kill switch (Global Stop, two-person reset) | S5.11 |
| 649 | `rapp-mgmt` | PUT | `/instances/{instance_id}/kill` | [D] exception | D-06 rApp safeguards: kill switch (Global Stop, two-person reset) | S5.11 |
| 650 | `rapp-mgmt` | DELETE | `/instances/{instance_id}/operator-api` | [D] exception | D-09 rApp operator page declaration (ADR 0004) | S6.8 |
| 651 | `rapp-mgmt` | GET | `/instances/{instance_id}/operator-api` | [D] exception | D-09 rApp operator page declaration (ADR 0004) | S6.8 |
| 652 | `rapp-mgmt` | PUT | `/instances/{instance_id}/operator-api` | [D] exception | D-09 rApp operator page declaration (ADR 0004) | S6.8 |
| 653 | `rapp-mgmt` | GET | `/instances/{instance_id}/performance` | replaced | TS 28.622 `PerfMetricJob` on `RappInstance` | S1.13, S4.2 |
| 654 | `rapp-mgmt` | POST | `/instances/{instance_id}/performance` | replaced | TS 28.622 `PerfMetricJob` on `RappInstance` | S1.13, S4.2 |
| 655 | `rapp-mgmt` | GET | `/instances/{instance_id}/performance/latest` | replaced | TS 28.622 `PerfMetricJob` on `RappInstance` | S1.13, S4.2 |
| 656 | `rapp-mgmt` | POST | `/instances/{instance_id}/recover` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 657 | `rapp-mgmt` | POST | `/instances/{instance_id}/rollback` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 658 | `rapp-mgmt` | GET | `/instances/{instance_id}/safeguards` | [D] exception | D-06 rApp safeguards (read) | S5.4 |
| 659 | `rapp-mgmt` | POST | `/instances/{instance_id}/terminate` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 660 | `rapp-mgmt` | POST | `/instances/{instance_id}/upgrade` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 661 | `rapp-mgmt` | POST | `/instances/{instance_id}/upgrade/resolve` | replaced | TS 28.532 ProvMnS createMOI on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 662 | `rapp-mgmt` | GET | `/instances/{instance_id}/versions` | replaced | TS 28.532 ProvMnS getMOIAttributes on `RappInstance` (R1GAP v14 §5.7, HLD six states) | S4.2 |
| 663 | `rapp-mgmt` | DELETE | `/kill-all` | [D] exception | D-06 rApp safeguards: kill switch (Global Stop, two-person reset) | S5.11 |
| 664 | `rapp-mgmt` | GET | `/kill-all` | [D] exception | D-06 rApp safeguards: kill switch (Global Stop, two-person reset) | S5.11 |
| 665 | `rapp-mgmt` | PUT | `/kill-all` | [D] exception | D-06 rApp safeguards: kill switch (Global Stop, two-person reset) | S5.11 |
| 666 | `rapp-mgmt` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 667 | `rapp-mgmt` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 668 | `rapp-mgmt` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 669 | `sa-smos` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 670 | `sa-smos` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 671 | `sa-smos` | GET | `/monitors` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 672 | `sa-smos` | POST | `/monitors` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 673 | `sa-smos` | GET | `/monitors/{monitor_id}` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 674 | `sa-smos` | PUT | `/monitors/{monitor_id}/automation` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 675 | `sa-smos` | POST | `/monitors/{monitor_id}/escalate` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 676 | `sa-smos` | POST | `/monitors/{monitor_id}/evaluate` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 677 | `sa-smos` | POST | `/monitors/{monitor_id}/remedial-actions` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 678 | `sa-smos` | PUT | `/monitors/{monitor_id}/remedy` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 679 | `sa-smos` | POST | `/monitors/{monitor_id}/reports` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 680 | `sa-smos` | GET | `/o1-cm-handler/enactments` | delete | the O1-CM handler retires; SO SMOS enacts Level-2 intents | S5.9 |
| 681 | `sa-smos` | POST | `/o1-cm-handler/intents` | delete | the O1-CM handler retires; SO SMOS enacts Level-2 intents | S5.9 |
| 682 | `sa-smos` | DELETE | `/o1-cm-handler/registration` | delete | the O1-CM handler retires; SO SMOS enacts Level-2 intents | S5.9 |
| 683 | `sa-smos` | POST | `/o1-cm-handler/registration` | delete | the O1-CM handler retires; SO SMOS enacts Level-2 intents | S5.9 |
| 684 | `sa-smos` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 685 | `sa-smos` | GET | `/remedial-actions` | replaced | TS 28.532 ProvMnS on `AssuranceClosedControlLoop` / `AssuranceGoal` [N] TS 28.536; outcomes as TS 28.312 intent reports to SO SMOS | S5.8, S5.17 |
| 686 | `sa-smos` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 687 | `sme` | GET | `/capif-events/v1/{subscriber_id}/subscriptions` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 688 | `sme` | POST | `/capif-events/v1/{subscriber_id}/subscriptions` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 689 | `sme` | DELETE | `/capif-events/v1/{subscriber_id}/subscriptions/{subscription_id}` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 690 | `sme` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 691 | `sme` | GET | `/invoker-registrations` | replaced | TS 29.222 CAPIF_API_Invoker_Management_API (`/api-invoker-management/v1/onboardedInvokers`) | S1.16 |
| 692 | `sme` | POST | `/invoker-registrations` | replaced | TS 29.222 CAPIF_API_Invoker_Management_API (`/api-invoker-management/v1/onboardedInvokers`) | S1.16 |
| 693 | `sme` | POST | `/invoker-registrations/purge-stale` | delete | worker task in process; not an interface | S1.16 |
| 694 | `sme` | DELETE | `/invoker-registrations/{api_invoker_id}` | replaced | TS 29.222 CAPIF_API_Invoker_Management_API (`/api-invoker-management/v1/onboardedInvokers`) | S1.16 |
| 695 | `sme` | PUT | `/invoker-registrations/{api_invoker_id}` | replaced | TS 29.222 CAPIF_API_Invoker_Management_API (`/api-invoker-management/v1/onboardedInvokers`) | S1.16 |
| 696 | `sme` | PUT | `/invoker-registrations/{api_invoker_id}/authz-scope` | replaced | TS 28.319 `AccessRule` naming the invoker (ADR 0005 scope) | S1.14 |
| 697 | `sme` | POST | `/invoker-registrations/{api_invoker_id}/secret` | replaced | TS 29.222 CAPIF_API_Invoker_Management_API (`/api-invoker-management/v1/onboardedInvokers`) | S1.16 |
| 698 | `sme` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 699 | `sme` | POST | `/oauth2/introspect` | keep (standard) | RFC 7662 token introspection [N, IETF], the gateway's check | S1.15 |
| 700 | `sme` | POST | `/oauth2/token` | replaced | TS 29.222 CAPIF_Security_API token (`/capif-security/v1/securities/{id}/token`) | S1.16 |
| 701 | `sme` | GET | `/provider-registrations` | replaced | TS 29.222 CAPIF_API_Provider_Management_API (`/api-provider-management/v1/registrations`) | S1.16 |
| 702 | `sme` | POST | `/provider-registrations` | replaced | TS 29.222 CAPIF_API_Provider_Management_API (`/api-provider-management/v1/registrations`) | S1.16 |
| 703 | `sme` | DELETE | `/provider-registrations/{apf_id}` | replaced | TS 29.222 CAPIF_API_Provider_Management_API (`/api-provider-management/v1/registrations`) | S1.16 |
| 704 | `sme` | GET | `/published-apis/v1/{apf_id}/service-apis` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 705 | `sme` | POST | `/published-apis/v1/{apf_id}/service-apis` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 706 | `sme` | DELETE | `/published-apis/v1/{apf_id}/service-apis/{service_id}` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 707 | `sme` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 708 | `sme` | GET | `/service-apis/v1/allServiceAPIs` | keep (standard) | TS 29.222 v20.0.0 CAPIF Events / Publish / Discover service API | S1.16 |
| 709 | `sme` | GET | `/trusted-invokers` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 710 | `sme` | DELETE | `/trusted-invokers/{api_invoker_id}` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 711 | `sme` | GET | `/trusted-invokers/{api_invoker_id}` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 712 | `sme` | PUT | `/trusted-invokers/{api_invoker_id}` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 713 | `sme` | POST | `/trusted-invokers/{api_invoker_id}/delete` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 714 | `sme` | POST | `/trusted-invokers/{api_invoker_id}/update` | replaced | TS 29.222 CAPIF_Security_API `trustedInvokers` at its standard path | S1.16 |
| 715 | `sme` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 716 | `so-smos` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 717 | `so-smos` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 718 | `so-smos` | GET | `/orders` | replaced | TS 28.312 v20.0.0 Intent MnS: a Level-1 intent and its playbook replace a service order | S5.3 |
| 719 | `so-smos` | POST | `/orders` | replaced | TS 28.312 v20.0.0 Intent MnS: a Level-1 intent and its playbook replace a service order | S5.3 |
| 720 | `so-smos` | GET | `/orders/{order_id}` | replaced | TS 28.312 v20.0.0 Intent MnS: a Level-1 intent and its playbook replace a service order | S5.3 |
| 721 | `so-smos` | POST | `/orders/{order_id}/cancel` | replaced | TS 28.312 v20.0.0 Intent MnS: a Level-1 intent and its playbook replace a service order | S5.3 |
| 722 | `so-smos` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 723 | `so-smos` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 724 | `traffic-steering-rapp` | GET | `/health` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 725 | `traffic-steering-rapp` | GET | `/instances` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 726 | `traffic-steering-rapp` | GET | `/instances/{instance_id}` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 727 | `traffic-steering-rapp` | GET | `/instances/{instance_id}/cells` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 728 | `traffic-steering-rapp` | GET | `/instances/{instance_id}/dashboard` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 729 | `traffic-steering-rapp` | GET | `/instances/{instance_id}/decisions` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 730 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/demo/emulate` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 731 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/demo/history` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 732 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/demo/kpi-hour` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 733 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/demo/live` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 734 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/demo/prepare` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 735 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/evaluate` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 736 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/lifecycle/deploy` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 737 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/lifecycle/emulate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 738 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/lifecycle/train` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 739 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/lifecycle/validate` | delete | the rApp uses R1GAP v14 §5.6 services itself; the operator acts through intents | S7.13 |
| 740 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/reconcile` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 741 | `traffic-steering-rapp` | GET | `/instances/{instance_id}/relations` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 742 | `traffic-steering-rapp` | POST | `/instances/{instance_id}/start` | [D] exception | D-09 the rApp's own operator API, declared by its package (ADR 0004) | S7.13 |
| 743 | `traffic-steering-rapp` | GET | `/live` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 744 | `traffic-steering-rapp` | GET | `/ready` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |
| 745 | `traffic-steering-rapp` | GET | `/sim-producer/health` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 746 | `traffic-steering-rapp` | POST | `/sim-producer/jobs` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 747 | `traffic-steering-rapp` | DELETE | `/sim-producer/jobs/{data_job_id}` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 748 | `traffic-steering-rapp` | POST | `/sim-producer/publish` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 749 | `traffic-steering-rapp` | POST | `/sim-producer/register` | delete | demo data comes from the O1 simulator and the Tier-3 replay; a producer registers through R1GAP v14 §5.2.2 | S0.15, S7.8 |
| 750 | `traffic-steering-rapp` | GET | `/version` | [D] exception | D-01 platform probe (Kubernetes liveness / readiness, build version); not an R1 operation | – |

## A.2 The GUI BFF's own routes (not in `docs/openapi`)

The BFF serves no OpenAPI document; its 45 routes, from `gui-bff/app/`, grouped by prefix:

| Prefix | Routes | Verdict | Replaced by / reason | Stage |
|---|---:|---|---|---|
| `/api/login`, `/api/login/totp`, `/api/logout`, `/api/oidc/*`, `/api/auth/config`, `/api/token` (RFC 6749 password grant for scripts) | 7 | [D] exception | D-20 console session | S6.1 |
| `/api/me/*` (preferences, pins, KPI layouts, password, TOTP, sign-ins) | 15 | [D] exception | D-20 the user's console settings | S6.1 |
| `/api/admin/*` (console users, the console's audit view and export) | 9 | [D] exception | D-20 console administration; SMO identities and access rules move to TS 28.319 through the generic views (S6.11) | S6.1, S6.11 |
| `/api/exports/*` | 5 | [D] exception | D-20 console exports of what the user already read | S6.1 |
| `/.well-known/jwks.json` | 1 | [D] exception | D-20 the console's signing keys | – |
| `/api/events` | 1 | [D] exception | D-04: the SSE stream of the event feed to the browser | S6.10 |
| `/api/permissions` | 1 | replaced | The role × class × operation matrix generated from SME's MnS registry | S6.1 |
| `/api/search`, `/api/summary/*`, `/api/modules/status`, `/api/rapps`, `/api/rapps/{instance}` | 6 | replaced | Reads through `/api/mns/{class}` (ProvMnS) and `/api/intents` | S6.1, S6.2 |

Totals: 45 routes; 38 [D] (37 D-20, 1 D-04), 7 replaced. The BFF's proxy to module routes is replaced by the two generic families (ADR 0011 section 3.4).
