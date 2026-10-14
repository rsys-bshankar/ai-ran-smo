"""The dispatch table and the order executor of SO SMOS (`app/dispatch.py`): sequential, fail-fast execution without compensation (SO SMOS LLD section 1.1) and the request each dispatcher sends.

No fixtures: `R1Client` is replaced by a MagicMock whose `post` returns canned responses (`_ok_response`). Run: `cd smo/so-smos && PYTHONPATH=.:../shared python -m pytest tests/test_dispatch.py -q`.
"""

from unittest.mock import MagicMock

import pytest

from app.dispatch import (
    DownstreamError, _ensure_ok, dispatch_config, dispatch_deploy,
    dispatch_emulation, dispatch_inference, dispatch_infra,
    dispatch_model_runtime_deploy, dispatch_training,
    dispatch_validation, execute_order, run_step, Submitter,
)


def _ok_response(payload):
    """A fake HTTP response with status 200 and `payload` as its JSON body."""
    resp = MagicMock()
    resp.json.return_value = payload
    resp.status_code = 200
    return resp


def test_all_steps_succeed_in_order():
    """Two valid steps are both COMPLETED and each makes exactly one R1 call."""
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"jobId": "abc"})

    steps = [
        {"stepType": "CONFIG", "targetModule": "RAN_NF_OAM", "scope": "cell", "changes": []},
        {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": "d1"},
    ]
    results = execute_order(r1, steps)

    assert [s["status"] for s in results] == ["COMPLETED", "COMPLETED"]
    assert r1.post.call_count == 2


def test_first_failure_halts_remaining_steps_as_pending():
    """Fail-fast without compensation: after a failed step the later steps are PENDING (not skipped or failed) and their dispatchers are never called.
    """
    r1 = MagicMock()
    r1.post.side_effect = [Exception("endpoint unreachable"), _ok_response({})]

    steps = [
        {"stepType": "CONFIG", "targetModule": "RAN_NF_OAM", "scope": "cell", "changes": []},
        {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": "d1"},
    ]
    results = execute_order(r1, steps)

    assert results[0]["status"] == "FAILED"
    assert results[1]["status"] == "PENDING"
    # the second step's dispatcher was never actually called
    assert r1.post.call_count == 1


def test_unknown_step_target_pair_fails_without_crashing():
    """A (stepType, targetModule) pair with no dispatcher fails that step with 'no dispatcher' instead of raising."""
    r1 = MagicMock()
    steps = [{"stepType": "CONFIG", "targetModule": "SOME_UNMAPPED_MODULE"}]
    results = execute_order(r1, steps)
    assert results[0]["status"] == "FAILED"
    assert "no dispatcher" in results[0]["error"]


def test_ensure_ok_passes_through_a_successful_response():
    """A 2xx response is returned as its JSON body."""
    assert _ensure_ok(_ok_response({"jobId": "abc"})) == {"jobId": "abc"}


def test_ensure_ok_raises_downstream_error_on_4xx():
    """A downstream error response raises DownstreamError, so the order halts instead of recording the step COMPLETED with the error body as its result.
    """
    resp = MagicMock()
    resp.status_code = 422
    resp.json.return_value = {"title": "UNPROCESSABLE"}
    with pytest.raises(DownstreamError):
        _ensure_ok(resp)


# One row per dispatcher: the step fields it needs and the R1 path it must post to.
@pytest.mark.parametrize("dispatcher,step,expected_path", [
    (dispatch_config, {"scope": "cell-1", "changes": []}, "/ran-nf-oam/config-jobs"),
    (dispatch_deploy, {"nfDeploymentDescriptorId": "d1"}, "/nfo/deployments"),
    (dispatch_infra, {"spec": {"cpu": 4}}, "/focom/resources/provision"),
    (dispatch_training, {"modelId": "m1"}, "/aimgf/training-jobs"),
    (dispatch_validation, {"modelId": "m1"}, "/aimgf/validation-jobs"),
    (dispatch_emulation, {"modelId": "m1"}, "/aimgf/emulation-jobs"),
    (dispatch_model_runtime_deploy, {"modelId": "m1"}, "/aimgf/models/m1/runtime/deploy"),
    (dispatch_inference, {"modelId": "m1"}, "/aimgf/models/m1/inference-jobs"),
])
def test_each_dispatcher_posts_to_its_own_target_module(dispatcher, step, expected_path):
    """Each dispatcher posts to the path of the module its table entry names; a typo would route a step to the wrong service."""
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"ok": True})
    dispatcher(r1, step)
    assert r1.post.call_args.args[0] == expected_path


def test_dispatch_inference_forwards_notification_destination_as_a_query_param():
    """AIMgF's RequestInference takes notification_destination as a query parameter, so the step's notificationDestination is sent as one and not in a JSON body.
    """
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"inferenceJobId": "i1"})
    dispatch_inference(r1, {"modelId": "m1", "notificationDestination": "http://consumer/cb"})
    assert r1.post.call_args.kwargs["params"] == {"notification_destination": "http://consumer/cb"}


def test_dispatch_inference_without_notification_destination_sends_no_params():
    """Without a notificationDestination the call carries no query parameters."""
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"inferenceJobId": "i1"})
    dispatch_inference(r1, {"modelId": "m1"})
    assert r1.post.call_args.kwargs["params"] is None


def test_every_ai_ml_step_dispatches_to_its_own_aimgf_path():
    """Training, validation, emulation, model-runtime deploy and inference each post to their own AIMgF path, in order (HISTORY.md OI-6.6). With AIMgF stubbed
    they all complete; against AIMgF one order cannot run them all, since each only starts its job (call flow 10d).
    """
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"ok": True})

    steps = [
        {"stepType": "TRAINING", "targetModule": "AI_ML_WORKFLOW", "modelId": "m1"},
        {"stepType": "VALIDATION", "targetModule": "AI_ML_WORKFLOW", "modelId": "m1"},
        {"stepType": "EMULATION", "targetModule": "AI_ML_WORKFLOW", "modelId": "m1"},
        {"stepType": "DEPLOY", "targetModule": "AIMGF", "modelId": "m1"},
        {"stepType": "INFERENCE", "targetModule": "AI_ML_WORKFLOW", "modelId": "m1"},
    ]
    results = execute_order(r1, steps)

    assert [s["status"] for s in results] == ["COMPLETED"] * 5
    assert [c.args[0] for c in r1.post.call_args_list] == [
        "/aimgf/training-jobs", "/aimgf/validation-jobs", "/aimgf/emulation-jobs",
        "/aimgf/models/m1/runtime/deploy", "/aimgf/models/m1/inference-jobs",
    ]


def test_model_runtime_deploy_and_workload_deploy_are_distinct_dispatch_entries():
    """("DEPLOY", "NFO") and ("DEPLOY", "AIMGF") share a stepType but go to different services (/nfo/deployments and the model runtime deploy path).
    """
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"ok": True})

    steps = [
        {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": "d1"},
        {"stepType": "DEPLOY", "targetModule": "AIMGF", "modelId": "m1"},
    ]
    results = execute_order(r1, steps)

    assert [s["status"] for s in results] == ["COMPLETED", "COMPLETED"]
    assert [c.args[0] for c in r1.post.call_args_list] == ["/nfo/deployments", "/aimgf/models/m1/runtime/deploy"]


def test_run_step_sends_the_submitter_whatever_the_step_says():
    """Call flow 10a: `run_step` puts the order's submitter on the request a CONFIG step sends, over anything the step carries, and returns the step without the role."""
    r1 = MagicMock()
    r1.post.return_value = _ok_response({"jobId": "j"})
    step = {"stepType": "CONFIG", "targetModule": "RAN_NF_OAM", "scope": "cell", "changes": [], "requestedBy": "someone-else", "msacRole": "admin"}

    result = run_step(r1, step, Submitter(requested_by="smo-gui:bob"))

    sent = r1.post.call_args.kwargs["json"]
    assert (sent["requestedBy"], sent["msacRole"]) == ("smo-gui:bob", None)
    assert result["status"] == "COMPLETED" and result["requestedBy"] == "someone-else"
