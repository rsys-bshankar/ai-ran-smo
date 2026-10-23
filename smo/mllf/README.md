# ML Loading Function (`mllf/`)

> MLLF is the deploy-request gate and node-group targeting surface for a model: it refuses placement unless AIMgF says the model is certified, then records which node groups the model is cleared for.

| | |
|---|---|
| Standards basis | Internal logic informed by 3GPP TS 28.105 (deploy-request gate, node-group targeting) |
| R1 route / port | `/mllf` via R1 Termination (container :8000) |
| Depends on (over R1) | AIMgF (`GET /aimgf/models/{id}/lifecycle`, `PATCH /aimgf/models/{id}/runtime/node-groups`) |
| Called by | rApps through the SDK (`sdk/smo_sdk/lifecycle.py`), GUI via the BFF |
| Database tables | none (stateless) |
| Unit tests | `tests/`, standalone, AIMgF faked (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done. Deliberately thin; test coverage is the shallowest of the AI modules (`OI-4` in [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

MLLF answers one question: may this model be placed on these node groups. It has one business route, `POST /models/{id}/deploy`. It has no lifecycle logic and no table of its own. The load / unload / activate / deactivate state machine is AIMgF's `RuntimeLifecycleState`, built jointly with NFO; MLLF is only the gate in front of placement.

### 1.2 Standards basis

No spec clause maps one to one onto this route. It is informed by TS 28.105 ([`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)): the TS 28.105 loading resources (MLModelLoadingRequest / Policy / Process) are realised by AIMgF, not MLLF ([`../aimgf/README.md`](../aimgf/README.md#24-api)). MLLF's gate (only `CERTIFIED` or `PROMOTED` models) and its `clearedNodeGroups` list are this build's own logic. Not realised: any real load / unload / activate / deactivate surface.

### 1.3 Position in the platform

```
 rApp / GUI ──POST /models/{id}/deploy──▶ MLLF ──GET lifecycle──────────────▶ AIMgF
                                              └──PATCH runtime/node-groups──▶ AIMgF (writes the row)
```

MLLF never calls MLMR, NFO or DME, and never fires a lifecycle event. It deploys nothing: after a successful call the caller still asks AIMgF to deploy the runtime (`POST /aimgf/models/{id}/runtime/deploy`).

### 1.4 Ownership

The cross-module AI/ML responsibility matrix is in [`../aimgf/README.md`](../aimgf/README.md#14-ownership). MLLF's row: deploy-request gate and node-group targeting only.

| Owns | Does not own → owner |
|---|---|
| `POST /models/{id}/deploy`: refuses unless AIMgF's `ModelLifecycleState` is `CERTIFIED` or `PROMOTED` (`MODEL_NOT_CERTIFIED`, 409) | Training, validation, emulation, certification → AIMgF |
| `clearedNodeGroups` targeting, written onto AIMgF's row via `PATCH /aimgf/models/{id}/runtime/node-groups` | Repository, versioning, coordination groups → MLMR |
| | The load / unload / activate / deactivate state machine (`RuntimeLifecycleState`) → AIMgF + NFO |
| | The `clearedNodeGroups` column itself → AIMgF (`model_lifecycle`) |

### 1.5 Design decisions

- **Gate against AIMgF's truth.** The state is read from AIMgF's `model_lifecycle` row, not from MLMR, because lifecycle never belonged to MLMR.
- **Decision here, storage there.** MLLF decides the node groups; AIMgF owns the row, so there is one lifecycle record per model.
- **Stateless.** No table; every call re-reads the lifecycle. The `PATCH` replaces the whole list (re-calling with a different list overwrites it; there is no merge).
- **Failure behaviour.** A 404 from AIMgF becomes `404 MODEL_NOT_FOUND` and a 403 becomes `403 MODEL_ACCESS_DENIED` with AIMgF's text, on the read and on the `PATCH`. Any other non-200 is not inspected and surfaces as an unhandled error (see 2.8). The read and write are not atomic: the state can change between them.
- **Idempotency.** Safe to repeat; the result is the list last written.
- **Security.** Authentication is R1 Termination's bearer introspection. The gateway's change allow-list lets an rApp call this route (`smo_shared/roles.py`), and the GUI BFF requires the operator role for it (`gui-bff/app/rbac.py`). The module itself has no RBAC and judges no owner: its two calls to AIMgF go through `R1Client`, which names the calling rApp in `X-R1-On-Behalf-Of`, so AIMgF's owner rule judges that rApp and another rApp's model is refused (`MLLF-NODEGROUP-OWNER`). The console's call names no rApp and is not asked.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app with `/health` and `POST /models/{model_id}/deploy`; one `R1Client` instance |

### 2.2 Data model

None: no tables. State is AIMgF's `model_lifecycle.cleared_node_groups`.

### 2.3 State machines

None: stateless. MLLF reads `modelLifecycleState` and passes only `CERTIFIED` and `PROMOTED`; the FSMs are AIMgF's ([`../aimgf/README.md`](../aimgf/README.md#23-state-machines)).

### 2.4 API

Paths relative to `/mllf`.

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models/{id}/deploy` | Body is a bare JSON array of node-group names (`["ng-a", "ng-b"]`). Returns `{modelId, clearedNodeGroups}` as stored by AIMgF. 404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the model (AIMgF's refusal, passed on); 409 `MODEL_NOT_CERTIFIED` for any other state (including `DEPRECATED`, `RETIRED`, `FAILED`) |
| GET | `/health` | Liveness (used by the GUI BFF module status fan-out) |

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| AIMgF `GET /aimgf/models/{id}/lifecycle` (with `X-R1-On-Behalf-Of` the calling rApp) | every deploy request | 404 → `MODEL_NOT_FOUND`; 403 → `MODEL_ACCESS_DENIED`; other statuses are not handled |
| AIMgF `PATCH /aimgf/models/{id}/runtime/node-groups` body `{clearedNodeGroups}` (same header) | after the gate passes | 404 → `MODEL_NOT_FOUND`; 403 → `MODEL_ACCESS_DENIED`; otherwise the body is read as the new lifecycle view |

No callbacks, no background tasks.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | base URL of AIMgF calls |
| `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` | unset | pre-provisioned OAuth2 identity (otherwise self-onboards at SME) |

No database variable is needed.

### 2.7 Error codes

RFC 7807 ProblemDetails; the code is in `title`.

| Code | Status | Raised when |
|---|---|---|
| `MODEL_NOT_FOUND` | 404 | AIMgF answers 404 for the model |
| `MODEL_ACCESS_DENIED` | 403 | AIMgF's owner rule refuses the calling rApp (another rApp's model); the detail is AIMgF's |
| `MODEL_NOT_CERTIFIED` | 409 | lifecycle state is not `CERTIFIED` / `PROMOTED`; the detail names the state |
| (FastAPI validation) | 422 | body is not a list of strings |

### 2.8 Limits and open items

- No load / unload / activate / deactivate surface here by design; that is AIMgF's RuntimeLifecycle.
- `MODEL_NOT_CERTIFIED` is the only authorisation of a placement; whether the RuntimeLifecycle transitions themselves need an operator gate is open (`OI-6.1-runtime-gate`).
- The node-group list is not validated against any node-group inventory, and `NODE_GROUP_NOT_CLEARED` (declared in `smo_shared`) is not raised by any module.
- An AIMgF error other than 403 or 404 is not translated (an AIMgF 5xx or an empty body would raise inside the handler).
- Route-level tests only, AIMgF faked (`OI-4`; counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)).

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/mllf && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

AIMgF is replaced by a fake R1 client.

| Test file | Covers | Count |
|---|---|---|
| `tests/test_main.py` | refusal unless `CERTIFIED` / `PROMOTED`, refusal for `DEPRECATED`, node groups stamped and returned, `PROMOTED` accepted, unknown model 404, health; ownership (`MLLF-NODEGROUP-OWNER`): MLLF calls AIMgF for the calling rApp, a second rApp is 403 `MODEL_ACCESS_DENIED` with nothing written (on the read and on the write), the owner and the console pass | [counts](../docs/TEST_COUNTS.md) |

### 3.3 What is not covered here

- The real AIMgF round trip (the written `clearedNodeGroups` visible on `GET /aimgf/models/{id}/lifecycle`): `tests_integration/test_cross_service.py` and the rApp closed-loop suites, where the deploy gate runs in every rApp deployment.
- AIMgF's real owner rule behind MLLF: `tests_integration/test_model_ownership.py` (a second rApp's deploy request is 403 through the mesh).
- Error paths other than 403 and 404 from AIMgF: not covered.
- GUI RBAC for the route: `gui-bff` tests.

## 4. References

- Call flows: [02 model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md), [17 runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [26 governance and end of life](../docs/call-flows/26-model-governance-and-end-of-life.md), [27 TS 28.105 resources](../docs/call-flows/27-ts28105-provisioning-resources.md)
- OpenAPI: [`../docs/openapi/mllf.json`](../docs/openapi/mllf.json)
- Spec: [`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)
- Platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
- Related READMEs: [`../aimgf/README.md`](../aimgf/README.md) (responsibility matrix, FSMs), [`../mlmr/README.md`](../mlmr/README.md)
