"""MLLF (ML Loading Function): the deploy-request gate for a model, as a single route.

Where it sits: one FastAPI app behind R1 Termination at `/mllf`. It has no tables. It reads a model's lifecycle from AIMgF and
writes the cleared node groups back to AIMgF's `model_lifecycle` row, both over R1 (`R1Client`); it never calls MLMR, NFO
or DME and deploys nothing. The design record is `docs/ARCHITECTURE.md` (MLLF); the load / unload / activate / deactivate
state machine is AIMgF's `RuntimeLifecycleState`, not this module's.

What it owns: the rule that only a `CERTIFIED` or `PROMOTED` model may be placed, and the choice of node groups. What it does
not own: lifecycle state and the `clearedNodeGroups` column (AIMgF), the model record (MLMR).

Ownership: MLLF judges no owner itself. Its calls to AIMgF carry the deploying rApp in `X-R1-On-Behalf-Of` (`R1Client` adds it), so AIMgF applies
its owner rule to that rApp (MLLF-NODEGROUP-OWNER), and MLLF passes AIMgF's 403 `MODEL_ACCESS_DENIED` on unchanged. The console's call carries no
rApp, so it is not asked.

Before editing: the one route's docstring is published in `docs/openapi/mllf.json`. The read of the lifecycle and the write of the
node groups are two separate calls, so the state can change between them.
"""

import uuid

from fastapi import FastAPI, HTTPException
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import install_health, sme_token_check
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id

app = FastAPI(title="MLLF")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)

# Client for the calls to AIMgF; a module-level instance, which the unit tests replace by patching `R1Client.get` and `R1Client.patch`.
_aimgf = R1Client()


install_health(app, checks=[sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


def _refuse_on_aimgf_answer(resp) -> None:
    """Raises the refusal that an AIMgF answer means for the deploy request: 404 `MODEL_NOT_FOUND`, or 403 `MODEL_ACCESS_DENIED` with AIMgF's own text.

    A 403 is AIMgF's owner rule refusing the rApp MLLF acts for; it goes to the rApp as 403, not as a gateway error. Any other answer returns, so the caller
    reads it as before.
    """
    if resp.status_code == 404:
        raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail="no such model")
    if resp.status_code == 403:
        # AIMgF's ProblemDetails sits under `detail` (FastAPI's HTTPException body); its own text names the model and the rule.
        try:
            problem = resp.json().get("detail")
        except ValueError:
            problem = None
        text = problem.get("detail") if isinstance(problem, dict) else None
        raise framework_error(FrameworkError.MODEL_ACCESS_DENIED, detail=text or "the model belongs to another rApp: only its owner may place it")


@app.post("/models/{model_id}/deploy")
def request_model_deployment(model_id: uuid.UUID, node_groups: list[str]):
    """RequestModelDeployment. Requires CERTIFIED-or-PROMOTED
    ModelLifecycleState (the AIMgF gate, unchanged from v1.3, now read
    cross-service from AIMgF's own `model_lifecycle` row) and stamps
    clearedNodeGroups (LLD section 5, MultiNode Q2's targeting gap) back
    onto that same row — see `PATCH /aimgf/models/{id}/runtime/node-groups`.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): `clearedNodeGroups` is replaced wholesale in AIMgF, so a repeat writes the same list; MLLF has no
    # database to store an answer in.
    # Gate and record, in this order: read AIMgF's lifecycle of the model (404 MODEL_NOT_FOUND if AIMgF has none; 403 MODEL_ACCESS_DENIED
    # when AIMgF's owner rule refuses the rApp this call is for, MLLF-NODEGROUP-OWNER), refuse any state other than CERTIFIED and PROMOTED
    # (409 MODEL_NOT_CERTIFIED, which includes DEPRECATED and RETIRED), then replace the model's `clearedNodeGroups` in AIMgF with
    # `node_groups` (a whole-list replace, no merge; the same 404 and 403 from AIMgF) and echo what AIMgF stored. Nothing is deployed.
    # The state is read from AIMgF's `model_lifecycle` row, not from MLMR, because lifecycle is not the repository's. Any other answer
    # from AIMgF than 200, 403 or 404 is not inspected.
    resp = _aimgf.get(f"/aimgf/models/{model_id}/lifecycle")
    _refuse_on_aimgf_answer(resp)
    lifecycle = resp.json()
    if lifecycle["modelLifecycleState"] not in ("CERTIFIED", "PROMOTED"):
        raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED,
                               detail=f"cannot place a model in state {lifecycle['modelLifecycleState']}")
    written = _aimgf.patch(f"/aimgf/models/{model_id}/runtime/node-groups", json={"clearedNodeGroups": node_groups})
    _refuse_on_aimgf_answer(written)
    updated = written.json()
    return {"modelId": str(model_id), "clearedNodeGroups": updated["clearedNodeGroups"]}
