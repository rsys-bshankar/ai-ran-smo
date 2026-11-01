"""Tests of `sdk.models` (`ModelsClient`): that each method calls its MLMR route with the expected verb, path, query, body and files, and that an error status becomes `SdkError`. `store_model` is in `test_wave10_wrappers.py`.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_models.py -q`; no network and no database. The `r1` fixture (`conftest.py`) is a recording fake of `R1Client`: every call is kept as `{verb, path, params, json, files}` and answered from `r1.script(...)` (default: 200 with `{}`).
"""

import uuid

import pytest

from smo_sdk._common import SdkError
from smo_sdk.models import ModelsClient


@pytest.fixture
def client(r1):
    """A `ModelsClient` over the recording fake."""
    return ModelsClient(r1)


def test_register_model(client, r1):
    """`register_model` posts the model's metadata with empty target environments and the Wave 3 fields null."""
    client.register_model("QoE-predictor", "1.0", description="d", author="a", owner="o")
    call = r1.calls[0]
    assert call["verb"] == "post"
    assert call["path"] == "/aimgf/models"
    assert call["json"] == {
        "modelType": "QoE-predictor", "version": "1.0", "requiredResourceTypeId": None,
        "description": "d", "author": "a", "owner": "o", "inputDataType": None,
        "outputDataType": None, "targetEnvironments": [],
        "domain": None, "customDomain": None, "vendors": None,
    }


def test_register_model_with_domain_and_vendors(client, r1):
    """The domain, custom domain and vendors reach the body."""
    client.register_model("QoE-predictor", "1.0", domain="CUSTOM", custom_domain="qoe", vendors=["acme"])
    call = r1.calls[0]
    assert call["json"]["domain"] == "CUSTOM"
    assert call["json"]["customDomain"] == "qoe"
    assert call["json"]["vendors"] == ["acme"]


def test_discover_models(client, r1):
    """`discover_models` filters by model type."""
    client.discover_models(model_type="QoE-predictor")
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/models", "params": {"model_type": "QoE-predictor"}}


def test_get_model(client, r1):
    """`get_model` reads one model by id."""
    model_id = uuid.uuid4()
    client.get_model(model_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/models/{model_id}", "params": None}


def test_update_model(client, r1):
    """`update_model` puts the immutable model type and version together with the fields given."""
    model_id = uuid.uuid4()
    client.update_model(model_id, "QoE-predictor", "1.0", description="new desc")
    call = r1.calls[0]
    assert call["verb"] == "put"
    assert call["path"] == f"/aimgf/models/{model_id}"
    assert call["json"] == {"modelType": "QoE-predictor", "version": "1.0", "description": "new desc"}


def test_deregister_model(client, r1):
    """`deregister_model` deletes the model by id."""
    model_id = uuid.uuid4()
    client.deregister_model(model_id)
    assert r1.calls[0] == {"verb": "delete", "path": f"/aimgf/models/{model_id}", "params": None}


def test_upload_artifact(client, r1):
    """`upload_artifact` sends the bytes as a multipart `file` of type `application/zip` and no JSON body."""
    model_id = uuid.uuid4()
    client.upload_artifact(model_id, "model.zip", b"binarydata")
    call = r1.calls[0]
    assert call["verb"] == "post"
    assert call["path"] == f"/aimgf/models/{model_id}/artifact"
    assert call["json"] is None
    assert call["files"] == {"file": ("model.zip", b"binarydata", "application/zip")}


def test_download_artifact_returns_raw_response(client, r1):
    """`download_artifact` returns the raw response, whose `.content` holds the bytes."""
    model_id = uuid.uuid4()
    r1.script(200, content=b"zipbytes")
    resp = client.download_artifact(model_id, 1)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/models/{model_id}/artifact/1", "params": None}
    assert resp.content == b"zipbytes"


def test_download_artifact_raises_sdk_error_on_4xx(client, r1):
    """A 404 on download raises `SdkError` with the response text as the body."""
    model_id = uuid.uuid4()
    r1.script(404, text="not found")
    with pytest.raises(SdkError) as exc_info:
        client.download_artifact(model_id, 99)
    assert exc_info.value.status_code == 404
    assert exc_info.value.body == "not found"


def test_create_coordination_group(client, r1):
    """`create_coordination_group` sends member ids as strings and the default retrain propagation."""
    m1, m2 = uuid.uuid4(), uuid.uuid4()
    client.create_coordination_group([m1, m2], member_use_cases=["retrain-together"])
    call = r1.calls[0]
    assert call["path"] == "/aimgf/coordination-groups"
    assert call["json"] == {
        "memberModelIds": [str(m1), str(m2)], "memberUseCases": ["retrain-together"],
        "sharedFeaturePipelineRef": None, "retrainPropagation": "ANY_MEMBER_TRIGGERS",
    }


def test_list_coordination_groups(client, r1):
    """`list_coordination_groups` reads the collection."""
    client.list_coordination_groups()
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/coordination-groups", "params": None}
