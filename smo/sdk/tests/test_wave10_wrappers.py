"""Tests of the SDK's convenience wrappers (HISTORY.md W10-03, decision D-4): `get_dataset`, `store_model`, the lifecycle job wrappers, `execute_action`, `read_config` and the autonomy dispatch calls.

These are the methods that combine several routes or carry client-side logic, so they are checked against a fake that answers by (verb, path). Run with
`cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_wave10_wrappers.py -q`; the `r1` fixture comes from `conftest.py`; no network.
"""

import uuid

import pytest

from conftest import FakeResponse

from smo_sdk._common import SdkError
from smo_sdk.data import DataClient
from smo_sdk.intent import IntentClient
from smo_sdk.lifecycle import LifecycleClient
from smo_sdk.models import ModelsClient
from smo_sdk.platform import PlatformClient


class RoutedR1:
    """A fake R1 client that answers by (verb, path), from a table of responses or of functions of the call, and records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _send(self, verb, path, **kw):
        self.calls.append((verb, path, kw))
        answer = self.routes.get((verb, path))
        if callable(answer):
            answer = answer(**kw)
        return answer or FakeResponse(200, {})

    def get(self, path, params=None, **kw):
        return self._send("get", path, params=params)

    def post(self, path, json=None, params=None, files=None, **kw):
        return self._send("post", path, json=json, params=params, files=files)


TYPE = {"dmeTypeId": "t-1", "dmeTypeIdStruct": {"namespace": "RAN", "name": "PRB_UTILIZATION", "version": "1.0.0"},
        "typeName": "RAN.PRB_UTILIZATION", "sourceDomain": "LIVE_RAN"}


def test_get_dataset_reuses_the_consumers_job_and_pages_records_oldest_first():
    """`get_dataset` reuses the consumer's existing data job (no POST), reads the records in pages of 500, the second before the oldest record of the first
    (call flow 12b: a cursor, not an offset), and returns all 501 oldest first."""
    pages = [[{"recordId": str(i)} for i in range(500, 0, -1)], [{"recordId": "0"}]]
    r1 = RoutedR1({
        ("get", "/dme/dme-types"): FakeResponse(200, [TYPE]),
        ("get", "/dme/data-jobs"): FakeResponse(200, {"items": [{"dataJobId": "j-1", "lifecycleStage": "INFERENCE"}]}),
        ("get", "/dme/data-jobs/j-1/records"): lambda params: FakeResponse(200, {"items": pages[0 if "before" not in params else 1 if params["before"] == "1" else 2]}),
    })
    out = DataClient(r1).get_dataset("PRB_UTILIZATION", consumer_id="es-rapp", lifecycle_stage="INFERENCE")
    assert (out["dmeTypeId"], out["dataJobId"], out["sourceDomain"]) == ("t-1", "j-1", "LIVE_RAN")
    assert [r["recordId"] for r in out["records"][:2]] == ["0", "1"] and len(out["records"]) == 501
    assert not any(verb == "post" for verb, _, _ in r1.calls)  # the existing job was reused


def test_get_dataset_creates_a_job_and_404s_an_unknown_dataset():
    """`get_dataset` creates a CONTINUOUS PULL_HTTP job for the consumer when none exists, and raises `SdkError(404)` for a dataset no type matches."""
    r1 = RoutedR1({
        ("get", "/dme/dme-types"): FakeResponse(200, [TYPE]),
        ("get", "/dme/data-jobs"): FakeResponse(200, {"items": []}),
        ("post", "/dme/data-jobs"): FakeResponse(202, {"dataJobId": "j-2"}),
        ("get", "/dme/data-jobs/j-2/records"): FakeResponse(200, {"items": []}),
    })
    assert DataClient(r1).get_dataset("PRB_UTILIZATION", "es-rapp", lifecycle_stage="TRAINING")["dataJobId"] == "j-2"
    created = next(kw["json"] for verb, path, kw in r1.calls if verb == "post")
    assert (created["consumerId"], created["lifecycleStage"], created["dataDeliveryMethod"]) == ("es-rapp", "TRAINING", "PULL_HTTP")
    with pytest.raises(SdkError) as err:
        DataClient(r1).get_dataset("NOPE", "es-rapp")
    assert err.value.status_code == 404


def test_store_model_registers_once_then_adds_artifact_versions():
    """Storing the same (type, version) twice registers the model once and uploads two artifact versions to it."""
    models: list[dict] = []

    def register(json, **kw):
        models.append({"modelId": "m-1", **json})
        return FakeResponse(201, {"modelId": "m-1"})

    r1 = RoutedR1({
        ("get", "/mlmr/models"): lambda params: FakeResponse(200, {"items": models}),
        ("post", "/mlmr/models"): register,
        ("post", "/mlmr/models/m-1/artifact"): FakeResponse(201, {"modelId": "m-1", "artifactVersion": 1}),
    })
    client = ModelsClient(r1)
    assert client.store_model("EnergySavingPredictor", "1.0.0", b"zip", description="x")["modelId"] == "m-1"
    client.store_model("EnergySavingPredictor", "1.0.0", b"zip2")
    assert [path for verb, path, _ in r1.calls if verb == "post"] == [
        "/mlmr/models", "/mlmr/models/m-1/artifact", "/mlmr/models/m-1/artifact"]


def test_lifecycle_wrappers(r1):
    """The training, validation, emulation, runtime and inference wrappers call the expected routes in order with the expected bodies and parameters."""
    client, model_id, package_id = LifecycleClient(r1), uuid.uuid4(), uuid.uuid4()
    client.start_training(model_id, "es-rapp", package_id=package_id, dme_data_job_ids=["j-1"])
    client.complete_training("t-1", True, metrics={"rmse": 1.2}, modelConfidenceIndication=90)
    client.start_validation(model_id, "es-rapp", package_id=package_id)
    client.complete_validation("v-1", True)
    client.start_emulation(model_id, "es-rapp", emulation_criteria={"dataset": "PRB_UTILIZATION_SIM"})
    client.complete_emulation("e-1", False)
    client.deploy_runtime(model_id, package_id=package_id)
    client.activate_runtime(model_id)
    client.resolve_inference("i-1", True, inference_outputs=[{"outputResult": {"cellId": "101"}}])
    client.get_inference_report("r-1")
    calls = [(c["verb"], c["path"]) for c in r1.calls]
    assert calls == [
        ("post", "/aimgf/training-jobs"), ("post", "/aimgf/training-jobs/t-1/complete"),
        ("post", "/aimgf/validation-jobs"), ("post", "/aimgf/validation-jobs/v-1/complete"),
        ("post", "/aimgf/emulation-jobs"), ("post", "/aimgf/emulation-jobs/e-1/complete"),
        ("post", f"/aimgf/models/{model_id}/runtime/deploy"), ("post", f"/aimgf/models/{model_id}/runtime/activate"),
        ("post", "/aimgf/inference-jobs/i-1/resolve"), ("get", "/aimgf/aiml-inference-reports/r-1"),
    ]
    assert r1.calls[0]["json"] == {"modelId": str(model_id), "producerId": "es-rapp", "packageId": str(package_id),
                                   "dmeDataJobIds": ["j-1"]}
    assert r1.calls[1]["json"] == {"succeeded": True, "metrics": {"rmse": 1.2}, "modelConfidenceIndication": 90}
    assert r1.calls[6]["params"] == {"package_id": str(package_id)}
    assert r1.calls[8]["json"] == {"inferenceOutputs": [{"outputResult": {"cellId": "101"}}], "potentialImpactInfo": None}


def test_execute_action_read_config_and_autonomy_dispatch(r1):
    """`execute_action` forwards the idempotency `actionId`, `read_config` passes the function ref, and the autonomy-dispatch calls reach their routes."""
    action_id = uuid.uuid4()
    PlatformClient(r1).execute_action("es-rapp", [{"managedElementRef": "me-1"}], action_id=action_id,
                                      source_context={"correlationId": "c"})
    DataClient(r1).read_config("me-1", "NRCellDU=101")
    IntentClient(r1).request_autonomy_dispatch("inst-1", [{"expectationId": "e"}], "sa-smos", user_label="x")
    IntentClient(r1).get_autonomy_dispatch("d-1")
    assert r1.calls[0]["path"] == "/dme/actions" and r1.calls[0]["json"]["actionId"] == str(action_id)
    assert (r1.calls[1]["path"], r1.calls[1]["params"]) == ("/ran-nf-oam/managed-entities/me-1/config",
                                                            {"managed_function_ref": "NRCellDU=101"})
    assert r1.calls[2]["json"]["rmihId"] == "sa-smos" and r1.calls[3]["path"] == "/intent-service/autonomy-dispatches/d-1"


def test_execute_action_carries_the_decision_context_and_the_approval_can_be_followed(r1):
    """PR-AI-13 / PR-AI-11: why the rApp acts rides on the action; an action held for a human is followed by its approval id."""
    approval_id = uuid.uuid4()
    PlatformClient(r1).execute_action("es-rapp", [{"managedElementRef": "me-1"}], decision={"modelVersion": "m 1.0", "rationale": "low load"})
    PlatformClient(r1).execute_action("es-rapp", [{"managedElementRef": "me-1"}])
    PlatformClient(r1).get_approval(approval_id)
    assert r1.calls[0]["json"]["decision"] == {"modelVersion": "m 1.0", "rationale": "low load"} and "decision" not in r1.calls[1]["json"]
    assert (r1.calls[2]["verb"], r1.calls[2]["path"]) == ("get", f"/ran-nf-oam/rapp-approvals/{approval_id}")
