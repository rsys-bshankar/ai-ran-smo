"""Tests of `sdk.lifecycle` (`LifecycleClient`): that each AIMgF or MLLF method calls its route with the expected verb, path, query and body (a raw unwrapped body where the route takes one body parameter), and that an error status becomes `SdkError`. The Wave 10 job wrappers are in `test_wave10_wrappers.py`.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_lifecycle.py -q`; no network and no database. The `r1` fixture (`conftest.py`) is a recording fake of `R1Client`: every call is kept as `{verb, path, params, json, files}` and answered from `r1.script(...)` (default: 200 with `{}`).
"""

import uuid

import pytest

from smo_sdk._common import SdkError
from smo_sdk.lifecycle import LifecycleClient


@pytest.fixture
def client(r1):
    """A `LifecycleClient` over the recording fake."""
    return LifecycleClient(r1)


def test_request_training(client, r1):
    """`request_training` posts ids as strings and an unset coordination group as null."""
    model_id = uuid.uuid4()
    client.request_training("producer-1", model_id=model_id, run_id="run-1")
    call = r1.calls[0]
    assert call["verb"] == "post"
    assert call["path"] == "/aimgf/training-jobs"
    assert call["json"]["modelId"] == str(model_id)
    assert call["json"]["producerId"] == "producer-1"
    assert call["json"]["runId"] == "run-1"
    assert call["json"]["modelCoordinationGroupId"] is None


def test_get_training_job_status(client, r1):
    """`get_training_job_status` reads the job's status sub-resource."""
    job_id = uuid.uuid4()
    client.get_training_job_status(job_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/training-jobs/{job_id}/status", "params": None}


def test_cancel_training(client, r1):
    """`cancel_training` deletes the job."""
    job_id = uuid.uuid4()
    client.cancel_training(job_id)
    assert r1.calls[0] == {"verb": "delete", "path": f"/aimgf/training-jobs/{job_id}", "params": None}


def test_suspend_and_resume_training(client, r1):
    """`suspend_training` and `resume_training` post to their sub-resources with no body."""
    job_id = uuid.uuid4()
    client.suspend_training(job_id)
    client.resume_training(job_id)
    assert r1.calls[0] == {"verb": "post", "path": f"/aimgf/training-jobs/{job_id}/suspend", "params": None, "files": None, "json": None}
    assert r1.calls[1] == {"verb": "post", "path": f"/aimgf/training-jobs/{job_id}/resume", "params": None, "files": None, "json": None}


def test_update_training_job_model_metrics_sends_raw_unwrapped_body(client, r1):
    """`update_training_job_model_metrics` sends the metrics dict itself as the body, not wrapped, because the route's only body parameter is that dict."""
    job_id = uuid.uuid4()
    client.update_training_job_model_metrics(job_id, {"accuracy": 0.9})
    assert r1.calls[0] == {
        "verb": "post", "path": f"/aimgf/training-jobs/{job_id}/model-metrics",
        "params": None, "files": None, "json": {"accuracy": 0.9},
    }


def test_get_training_job_model_metrics(client, r1):
    """`get_training_job_model_metrics` reads the job's model-metrics sub-resource."""
    job_id = uuid.uuid4()
    client.get_training_job_model_metrics(job_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/training-jobs/{job_id}/model-metrics", "params": None}


def test_list_training_jobs(client, r1):
    """`list_training_jobs` filters by status and sends the unset model id as null."""
    client.list_training_jobs(status="RUNNING")
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/training-jobs", "params": {"model_id": None, "status": "RUNNING"}}


def test_advance_model_lifecycle_is_deprecated_and_fails_before_any_call(client, r1):
    """`advance_model_lifecycle` raises `SdkError` 403 `ROLE_NOT_PERMITTED` for every event and sends nothing: the gateway and AIMgF refuse an rApp the route (SEC-15.1, 15.1b)."""
    model_id = uuid.uuid4()
    for event, decided_by in (("DEPRECATE", None), ("CERTIFY", "operator-1")):
        with pytest.raises(SdkError) as exc_info:
            client.advance_model_lifecycle(model_id, event, decided_by=decided_by, rationale="x")
        assert exc_info.value.status_code == 403
        assert exc_info.value.body["title"] == "ROLE_NOT_PERMITTED"
        assert "deprecated" in exc_info.value.body["detail"]
    assert r1.calls == []


def test_the_gateway_policy_no_longer_lets_an_rapp_advance_a_model():
    """The path the deprecated method used is off the rApp allow-list, while the AIMgF model routes a rApp does use stay on it (a loosened or over-tightened pattern shows here)."""
    from smo_shared import roles
    assert not roles.rapp_may_change("/aimgf", "POST", "/models/m/advance")
    for tail in ("inference-jobs", "runtime/deploy", "runtime/activate", "runtime/scale", "runtime/terminate"):
        assert roles.rapp_may_change("/aimgf", "POST", f"/models/m/{tail}")


def test_request_inference(client, r1):
    """`request_inference` posts to the model's inference-jobs with the notification destination as a query parameter."""
    model_id = uuid.uuid4()
    client.request_inference(model_id, notification_destination="http://x/notify")
    assert r1.calls[0] == {
        "verb": "post", "path": f"/aimgf/models/{model_id}/inference-jobs",
        "params": {"notification_destination": "http://x/notify"}, "files": None, "json": None,
    }


def test_get_inference_job_status(client, r1):
    """`get_inference_job_status` reads the job's status sub-resource."""
    job_id = uuid.uuid4()
    client.get_inference_job_status(job_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/inference-jobs/{job_id}/status", "params": None}


def test_resolve_inference(client, r1):
    """Without outputs `resolve_inference` sends `succeeded` as a query parameter and no body."""
    job_id = uuid.uuid4()
    client.resolve_inference(job_id, True)
    assert r1.calls[0] == {
        "verb": "post", "path": f"/aimgf/inference-jobs/{job_id}/resolve",
        "params": {"succeeded": True}, "files": None, "json": None,
    }


def test_list_inference_jobs(client, r1):
    """`list_inference_jobs` filters by status."""
    client.list_inference_jobs(model_id=None, status="RESOLVED")
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/inference-jobs", "params": {"model_id": None, "status": "RESOLVED"}}


def test_subscribe_performance_monitoring_embeds_body_under_two_params(client, r1):
    """`subscribe_performance_monitoring` sends the metric types and the guard floor as a body under their own keys (two body parameters are embedded by FastAPI) and the ids as query parameters."""
    model_id, dme_type_id = uuid.uuid4(), uuid.uuid4()
    client.subscribe_performance_monitoring(model_id, ["latency"], dme_type_id, guard_kpi_floor={"latency": 100})
    call = r1.calls[0]
    assert call["path"] == "/aimgf/mlmf/subscriptions"
    assert call["params"] == {"model_id": str(model_id), "dme_type_id": str(dme_type_id), "notification_destination": None}
    assert call["json"] == {"metric_types": ["latency"], "guard_kpi_floor": {"latency": 100}}


def test_subscribe_performance_monitoring_with_notification_destination(client, r1):
    """The optional notification destination reaches the query."""
    model_id, dme_type_id = uuid.uuid4(), uuid.uuid4()
    client.subscribe_performance_monitoring(model_id, ["latency"], dme_type_id, notification_destination="http://x/notify")
    assert r1.calls[0]["params"]["notification_destination"] == "http://x/notify"


def test_unsubscribe_performance_monitoring(client, r1):
    """`unsubscribe_performance_monitoring` deletes the subscription."""
    sub_id = uuid.uuid4()
    client.unsubscribe_performance_monitoring(sub_id)
    assert r1.calls[0] == {"verb": "delete", "path": f"/aimgf/mlmf/subscriptions/{sub_id}", "params": None}


def test_report_performance_sends_raw_unwrapped_body(client, r1):
    """`report_performance` sends the metrics dict itself as the body, not wrapped."""
    sub_id = uuid.uuid4()
    client.report_performance(sub_id, {"latency": 50})
    assert r1.calls[0] == {
        "verb": "post", "path": f"/aimgf/mlmf/subscriptions/{sub_id}/reports",
        "params": None, "files": None, "json": {"latency": 50},
    }


def test_list_performance_subscriptions(client, r1):
    """`list_performance_subscriptions` filters by model and sends the unset id as null."""
    client.list_performance_subscriptions()
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/mlmf/subscriptions", "params": {"model_id": None}}


def test_list_performance_reports(client, r1):
    """`list_performance_reports` passes the limit as a query parameter."""
    sub_id = uuid.uuid4()
    client.list_performance_reports(sub_id, limit=10)
    assert r1.calls[0] == {"verb": "get", "path": f"/aimgf/mlmf/subscriptions/{sub_id}/reports", "params": {"limit": 10}}


def test_list_recent_performance_reports(client, r1):
    """`list_recent_performance_reports` passes `breached_only` and the default limit of 50."""
    client.list_recent_performance_reports(breached_only=True)
    assert r1.calls[0] == {
        "verb": "get", "path": "/aimgf/mlmf/reports",
        "params": {"breached_only": True, "limit": 50},
    }


def test_create_feature_group(client, r1):
    """`create_feature_group` posts the group's datalake settings with `tokenRef` (no `token` key) and defaults `enableDme` to false."""
    client.create_feature_group("fg-1", "feat1,feat2", "influx", "host", "8086", "bucket", token_ref="lake-token", db_org="org", measurement="meas")
    call = r1.calls[0]
    assert call["path"] == "/aimgf/feature-groups"
    assert call["json"]["featureGroupName"] == "fg-1"
    assert call["json"]["tokenRef"] == "lake-token"
    assert "token" not in call["json"]
    assert call["json"]["enableDme"] is False


def test_create_feature_group_with_its_dme_type(client, r1):
    """With `enable_dme` the DME type id is sent as a string and the delivery method defaults to PULL_HTTP."""
    type_id = uuid.uuid4()
    client.create_feature_group("fg-1", "feat1", "influx", "host", "8086", "bucket", token_ref="lake-token", db_org="org", measurement="meas",
                                enable_dme=True, dme_type_id=type_id)
    json = r1.calls[0]["json"]
    assert (json["enableDme"], json["dmeTypeId"], json["dataDeliveryMethod"]) == (True, str(type_id), "PULL_HTTP")


def test_create_feature_group_with_the_deprecated_token_still_sends_it_and_warns_without_the_value(client, r1):
    """The deprecated clear-text `token` (old positional form) is still sent as `token`, with a `DeprecationWarning` whose text does not contain the secret."""
    with pytest.warns(DeprecationWarning) as caught:
        client.create_feature_group("fg-1", "feat1", "influx", "host", "8086", "bucket", "s3cret-value", "org", "meas")
    json = r1.calls[0]["json"]
    assert json["token"] == "s3cret-value" and "tokenRef" not in json
    assert "s3cret-value" not in str(caught[0].message)


def test_create_feature_group_needs_exactly_one_credential_and_never_names_it(client, r1):
    """Neither or both of `token_ref` and `token` is a `ValueError` raised before any call, and the message holds neither value."""
    with pytest.raises(ValueError) as both:
        client.create_feature_group("fg-1", "f", "influx", "h", "1", "b", "s3cret-value", "org", "meas", token_ref="lake-token")
    with pytest.raises(ValueError):
        client.create_feature_group("fg-1", "f", "influx", "h", "1", "b", db_org="org", measurement="meas")
    assert "s3cret-value" not in str(both.value) and "lake-token" not in str(both.value)
    assert r1.calls == []


def test_create_feature_group_requires_db_org_and_measurement(client, r1):
    """`db_org` and `measurement` have no default in practice: leaving one out is a `ValueError` before any call."""
    with pytest.raises(ValueError):
        client.create_feature_group("fg-1", "f", "influx", "h", "1", "b", token_ref="lake-token", measurement="meas")
    assert r1.calls == []


def test_list_feature_groups(client, r1):
    """`list_feature_groups` reads the collection."""
    client.list_feature_groups()
    assert r1.calls[0] == {"verb": "get", "path": "/aimgf/feature-groups", "params": None}


def test_delete_feature_group(client, r1):
    """`delete_feature_group` deletes the group by name."""
    client.delete_feature_group("fg-1")
    assert (r1.calls[0]["verb"], r1.calls[0]["path"]) == ("delete", "/aimgf/feature-groups/fg-1")


def test_report_training_progress(client, r1):
    """`report_training_progress` posts the step to the job's progress sub-resource."""
    client.report_training_progress("job-1", "TRAINING")
    assert (r1.calls[0]["path"], r1.calls[0]["json"]) == ("/aimgf/training-jobs/job-1/progress", {"step": "TRAINING"})


def test_deploy_model_sends_raw_unwrapped_body(client, r1):
    """`deploy_model` sends the node-group list itself as the body, not `{"node_groups": [...]}`, because the route's only body parameter is that list."""
    model_id = uuid.uuid4()
    client.deploy_model(model_id, ["ng-1", "ng-2"])
    assert r1.calls[0] == {
        "verb": "post", "path": f"/mllf/models/{model_id}/deploy",
        "params": None, "files": None, "json": ["ng-1", "ng-2"],
    }


def test_raises_sdk_error_on_a_4xx_response(client, r1):
    """A 409 answer is raised as `SdkError` carrying the status code and the body."""
    r1.script(409, {"detail": "invalid transition"})
    with pytest.raises(SdkError) as exc_info:
        client.suspend_training(uuid.uuid4())
    assert exc_info.value.status_code == 409
    assert exc_info.value.body == {"detail": "invalid transition"}


def test_scale_and_terminate_runtime_post_to_the_models_runtime(client, r1):
    """Call flow 17c/17d: `scale_runtime` and `terminate_runtime` are one POST each to the model's runtime routes, which the gateway lets an rApp make."""
    model_id = uuid.uuid4()
    client.scale_runtime(model_id)
    client.terminate_runtime(model_id)
    assert [(c["verb"], c["path"]) for c in r1.calls[-2:]] == [("post", f"/aimgf/models/{model_id}/runtime/scale"),
                                                              ("post", f"/aimgf/models/{model_id}/runtime/terminate")]
