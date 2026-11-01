"""Tests of the TS 28.105 NRM resources in `app/nrm.py`: training, testing and update requests with their functions, processes and reports; the inference function and its
loading policy, request and process; inference and emulation reports; and the `nrm-refs` view MLMR reads.

An NRM request is the same row as the job it starts, so most assertions check both the spec-shaped view (`{id, attributes}`) and the job or lifecycle state behind it.
Fixtures and `_set_lifecycle` come from `test_main.py` (`client`, `mlmr`, `db_session_factory`): MLMR model existence and NFO are in-memory doubles, the database is
SQLite, nothing is sent over the network. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_nrm.py -q`.
"""

import uuid

from sqlalchemy import select

from smo_shared.mns import PROVMNS_VERSION

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from provmns_reads import create, find, ids, register

from app.governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
from app.models import MLModelLoadingProcess, MLTestingReport


def _process_of(client, request_id: str) -> str:
    """The id of the MLTrainingProcess of the training request `request_id` (from the process list)."""
    return next(p["id"] for p in client.get("/ml-training-processes", params={"limit": 500}).json()["items"]
                if p["attributes"]["trainingRequestRef"] == [request_id])


def _attrs(resp):
    """Asserts the response is 200 or 201 and returns its `attributes` object."""
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["attributes"]


# ---------------------------------------------------------------- MLTrainingFunction / Request / Process / Report

def test_training_request_is_a_real_training_job_with_spec_attributes(client, mlmr, db_session_factory):
    """An MLTrainingRequest starts a real training job: the spec attributes round-trip, the same row answers `/training-jobs/{id}/status`, the model enters TRAINING, the function records the
    training type, and a process exists for it.
    """
    model_id = mlmr.add_model()
    function = {"id": create(client, db_session_factory, "MLTrainingFunction", {   # a function is a ProvMnS class since S2.7
        "userLabel": "mltf-1",
        "supportedLearningTechnology": {"learningTechnologyName": ["DL", "FL"], "supportedFLRole": ["FL_SERVER"]},
        "fLParticipationInfo": {"fLRole": "FL_SERVER", "isAvailableForFLTraining": True},
        "mLKnowledge": {"mLKnowledgeName": "prb-trend", "knowledgeType": "REGRESSION"},
    })}
    resp = client.post("/ml-training-requests", json={
        "mLModelRef": str(model_id), "mLTrainingFunctionRef": function["id"], "trainingRequestSource": "es-rapp",
        "aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING", "candidateTrainingDataSource": ["PRB_UTILIZATION"],
        "trainingDataQualityScore": 0.9,
        "performanceRequirements": [{"performanceMetric": "MAE", "performanceScore": 2.5}],
        "fLRequirement": {"fLClientSelectionCriteria": {"minimumAvailableDataSamples": 1000}},
        "rLRequirement": {"rLEnvironmentType": ["SIMULATION_ENVIRONMENTS"]},
        "clusteringInfo": [{"performanceMetric": "MAE", "taskType": "REGRESSION"}],
    })
    attrs = _attrs(resp)
    assert attrs["requestStatus"] == "IN_PROGRESS"
    assert attrs["mLTrainingType"] == "INITIAL_TRAINING"
    assert attrs["aIMLInferenceName"] == "NG_RAN_NETWORK_ENERGY_SAVING"
    assert attrs["fLRequirement"] == {"fLClientSelectionCriteria": {"minimumAvailableDataSamples": 1000}}
    request_id = resp.json()["id"]
    # the same row is the job the original route reads
    assert client.get(f"/training-jobs/{request_id}/status").json()["status"] == "IN_PROGRESS"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING
    # the function reflects the training type it ran
    assert find(client, "MLTrainingFunction", function["id"])["mLTrainingType"] == "INITIAL_TRAINING"
    # a process exists for it
    processes = client.get("/ml-training-processes").json()["items"]
    assert [p["attributes"]["trainingRequestRef"] for p in processes] == [[request_id]]
    assert processes[0]["attributes"]["progressStatus"]["status"] == "RUNNING"


def test_training_request_rejects_values_outside_spec_enums(client, mlmr):
    """A value outside a closed spec enum, and an attribute the spec does not define, are both 422 rather than stored."""
    model_id = mlmr.add_model()
    resp = client.post("/ml-training-requests", json={
        "mLModelRef": str(model_id), "trainingRequestSource": "x",
        "rLRequirement": {"rLEnvironmentType": ["MADE_UP"]},
    })
    assert resp.status_code == 422
    resp = client.post("/ml-training-requests", json={
        "mLModelRef": str(model_id), "trainingRequestSource": "x", "notASpecAttribute": 1,
    })
    assert resp.status_code == 422


def test_initial_training_only_for_a_never_trained_model(client, mlmr, db_session_factory):
    """INITIAL_TRAINING is refused (409) for a model that has been trained before, while another type such as PRE_SPECIALISED_TRAINING is accepted and echoed."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    resp = client.post("/ml-training-requests", json={
        "mLModelRef": str(model_id), "trainingRequestSource": "x", "mLTrainingType": "INITIAL_TRAINING"})
    assert resp.status_code == 409
    resp = client.post("/ml-training-requests", json={
        "mLModelRef": str(model_id), "trainingRequestSource": "x", "mLTrainingType": "PRE_SPECIALISED_TRAINING"})
    assert _attrs(resp)["mLTrainingType"] == "PRE_SPECIALISED_TRAINING"


def test_suspend_resume_cancel_flags_drive_job_and_process(client, mlmr):
    """The request and process flags suspend, resume and cancel the one run: both views follow, `priority` is written alongside, and cancelling twice is 409."""
    model_id = mlmr.add_model()
    request_id = client.post("/ml-training-requests", json={"mLModelRef": str(model_id), "trainingRequestSource": "x"}).json()["id"]
    process_id = client.get("/ml-training-processes").json()["items"][0]["id"]

    attrs = _attrs(client.patch(f"/ml-training-requests/{request_id}", json={"suspendRequest": True}))
    assert (attrs["requestStatus"], attrs["suspendRequest"]) == ("SUSPENDED", True)
    assert _attrs(client.get(f"/ml-training-processes/{process_id}"))["progressStatus"]["status"] == "SUSPENDED"

    attrs = _attrs(client.patch(f"/ml-training-processes/{process_id}", json={"suspendProcess": False, "priority": 5}))
    assert attrs["priority"] == 5 and attrs["progressStatus"]["status"] == "RUNNING"

    attrs = _attrs(client.patch(f"/ml-training-requests/{request_id}", json={"cancelRequest": True}))
    assert (attrs["requestStatus"], attrs["cancelRequest"]) == ("CANCELLED", True)
    assert _attrs(client.get(f"/ml-training-processes/{process_id}"))["cancelProcess"] is True
    assert client.patch(f"/ml-training-requests/{request_id}", json={"cancelRequest": True}).status_code == 409


def test_progress_then_completion_writes_a_chained_training_report(client, mlmr, db_session_factory):
    """A progress write-back shows in the process; completion finishes it at 100 % and writes an MLTrainingReport that references the model and the process; a retrain's report points at the
    previous one through `lastTrainingRef`; and progress on a finished process is 409.
    """
    model_id = mlmr.add_model()
    register(db_session_factory, model_id)
    request_id = client.post("/ml-training-requests", json={"mLModelRef": str(model_id), "trainingRequestSource": "x"}).json()["id"]
    process_id = client.get("/ml-training-processes").json()["items"][0]["id"]
    progress = _attrs(client.post(f"/ml-training-processes/{process_id}/progress",
                                  json={"progressPercentage": 40, "progressStateInfo": "epoch 4/10"}))["progressStatus"]
    assert progress["progressPercentage"] == 40 and progress["progressStateInfo"] == "epoch 4/10"

    resp = client.post(f"/training-jobs/{request_id}/complete", json={
        "succeeded": True, "metrics": {"MAE": 1.9},
        "modelPerformanceTraining": [{"performanceMetric": "MAE", "performanceScore": 1.9}],
        "modelConfidenceIndication": 87, "areNewTrainingDataUsed": True,
    })
    assert resp.status_code == 200
    process = _attrs(client.get(f"/ml-training-processes/{process_id}"))
    assert process["progressStatus"]["status"] == "FINISHED" and process["progressStatus"]["progressPercentage"] == 100
    report = find(client, "MLTrainingReport", process["trainingReportRef"])        # a report is a ProvMnS class since S2.10
    assert ids(report["mLModelGeneratedRef"]) == str(model_id)
    assert report["modelConfidenceIndication"] == 87
    assert ids(report["trainingProcessRef"]) == process_id
    assert "lastTrainingRef" not in report
    assert client.post(f"/ml-training-processes/{process_id}/progress", json={"progressPercentage": 10}).status_code == 409

    # a retrain chains to the previous report
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    second = client.post("/ml-training-requests", json={"mLModelRef": str(model_id), "trainingRequestSource": "x"}).json()["id"]
    client.post(f"/training-jobs/{second}/complete", json={"succeeded": True})
    second_report = find(client, "MLTrainingReport", _attrs(client.get(f"/ml-training-processes/{_process_of(client, second)}"))["trainingReportRef"])
    assert ids(second_report["lastTrainingRef"]) == process["trainingReportRef"]


def test_legacy_training_route_also_gets_a_process(client, mlmr):
    """A run started through POST /training-jobs also gets an MLTrainingProcess, so the request/process split exists for every run."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["trainingJobId"]
    processes = client.get("/ml-training-processes").json()["items"]
    assert processes[0]["attributes"]["trainingRequestRef"] == [job_id]


# ---------------------------------------------------------------- MLTestingFunction / Request / Report

def test_testing_request_drives_validation_and_writes_testing_report(client, mlmr, db_session_factory):
    """An MLTestingRequest starts a real validation (the model enters VALIDATING); completing it writes a PASSED MLTestingReport and the function lists the model."""
    model_id = mlmr.add_model()
    register(db_session_factory, model_id)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    function_id = create(client, db_session_factory, "MLTestingFunction", {"userLabel": "mlvf"})
    resp = client.post("/ml-testing-requests", json={"mLModelRef": str(model_id), "mLTestingFunctionRef": function_id})
    attrs = _attrs(resp)
    assert attrs["requestStatus"] == "IN_PROGRESS"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.VALIDATING
    request_id = resp.json()["id"]

    client.post(f"/validation-jobs/{request_id}/complete", json={
        "succeeded": True, "modelPerformanceTesting": [{"performanceMetric": "MAE", "performanceScore": 2.0}]})
    assert _attrs(client.get(f"/ml-testing-requests/{request_id}"))["requestStatus"] == "FINISHED"
    with db_session_factory() as db:
        report_id = db.scalar(select(MLTestingReport.ml_testing_report_id).where(MLTestingReport.validation_job_id == uuid.UUID(request_id)))
    report = find(client, "MLTestingReport", report_id)
    assert report["mLTestingResult"] == "PASSED" and ids(report["testingRequestRef"]) == request_id
    assert report["modelPerformanceTesting"][0]["performanceScore"] == 2.0
    assert ids(find(client, "MLTestingFunction", function_id)["mLModelRef"]) == [str(model_id)]


def test_testing_request_still_honours_the_operator_gate(client, mlmr, db_session_factory):
    """The NRM testing request cannot skip the APPROVE_TRAINING gate: an unapproved TRAINED model is refused with 409."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    assert client.post("/ml-testing-requests", json={"mLModelRef": str(model_id)}).status_code == 409


def test_group_targeted_testing_request(client, mlmr):
    """A testing request can target a coordination group instead of a model, and a body with neither target is 422."""
    group_id = uuid.uuid4()
    attrs = _attrs(client.post("/ml-testing-requests", json={"mLModelCoordinationGroupRef": str(group_id)}))
    assert attrs["mLModelCoordinationGroupRef"] == str(group_id) and attrs["mLModelRef"] is None
    assert client.post("/ml-testing-requests", json={}).status_code == 422


def test_cancelled_testing_request_fails_the_model_and_suspend_resumes(client, mlmr, db_session_factory):
    """Suspend and resume move a testing request SUSPENDED and back to IN_PROGRESS; cancel marks it CANCELLED, fails the model's VALIDATING stage and makes the run uncompletable (409)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    request_id = client.post("/ml-testing-requests", json={"mLModelRef": str(model_id)}).json()["id"]
    assert _attrs(client.patch(f"/ml-testing-requests/{request_id}", json={"suspendRequest": True}))["requestStatus"] == "SUSPENDED"
    assert _attrs(client.patch(f"/ml-testing-requests/{request_id}", json={"suspendRequest": False}))["requestStatus"] == "IN_PROGRESS"
    assert _attrs(client.patch(f"/ml-testing-requests/{request_id}", json={"cancelRequest": True}))["requestStatus"] == "CANCELLED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED
    # a cancelled run can no longer be completed
    assert client.post(f"/validation-jobs/{request_id}/complete", json={"succeeded": True}).status_code == 409


# ---------------------------------------------------------------- AIMLInferenceFunction / loading / reports

def _certified_model(mlmr, db_session_factory):
    """Registers a model in the MLMR double and puts its lifecycle row at CERTIFIED, the state loading and deploying require."""
    model_id = mlmr.add_model()
    register(db_session_factory, model_id)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    return model_id


def test_loading_request_brings_runtime_up_and_gates_inference(client, mlmr, db_session_factory):
    """A loading request deploys and activates the model's runtime, records the model on the function and the process, and inference on that function is then gated by `activationStatus`
    (refused while DEACTIVATED, accepted once ACTIVATED); the inference report, the function's consumers and `nrm-refs` all show the result.
    """
    model_id = _certified_model(mlmr, db_session_factory)
    function_id = client.post("/aiml-inference-functions", json={
        "aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING", "managedActivationScope": {"dNList": ["cell-101"]}}).json()["id"]

    request = client.post("/ml-model-loading-requests", json={
        "aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(model_id)]})
    assert _attrs(request)["requestStatus"] == "FINISHED"
    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["runtimeLifecycleState"] == RuntimeLifecycleState.ACTIVE
    with db_session_factory() as db:
        process_id = db.scalar(select(MLModelLoadingProcess.ml_model_loading_process_id))
    process = find(client, "MLModelLoadingProcess", process_id)                  # a loading process is a ProvMnS class since S2.10
    assert ids(process["loadedMLModelRef"]) == [str(model_id)]
    assert ids(process["mLModelLoadingRequestRef"]) == [request.json()["id"]]
    assert _attrs(client.get(f"/aiml-inference-functions/{function_id}"))["mLModelRefList"] == [str(model_id)]

    # DEACTIVATED (the default) refuses inference on the function
    resp = client.post(f"/models/{model_id}/inference-jobs", params={"aiml_inference_function_id": function_id})
    assert resp.status_code == 409
    client.patch(f"/aiml-inference-functions/{function_id}", json={"activationStatus": "ACTIVATED"})
    job_id = client.post(f"/models/{model_id}/inference-jobs",
                         params={"aiml_inference_function_id": function_id, "consumer_ref": "es-rapp"}).json()["inferenceJobId"]
    resolved = client.post(f"/inference-jobs/{job_id}/resolve", params={"succeeded": True}, json={
        "inferenceOutputs": [{"aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING",
                              "outputResult": {"recommendedState": "LOCKED"}}],
        "potentialImpactInfo": {"impactedScope": {"dNList": ["cell-101"]}, "impactedPM": [{"pMIdentifier": "PrbUtil"}]},
    }).json()
    report = _attrs(client.get(f"/aiml-inference-reports/{resolved['aIMLInferenceReportId']}"))
    assert report["inferenceOutputs"][0]["outputResult"] == {"recommendedState": "LOCKED"}
    assert report["aIMLInferenceFunctionRef"] == function_id
    assert _attrs(client.get(f"/aiml-inference-functions/{function_id}"))["usedByFunctionRefList"] == ["es-rapp"]
    refs = find(client, "MLModel", model_id)                                       # nrm-refs is the MLModel instance since S2.7
    assert ids(refs["usedByFunctionRefList"]) == [function_id]
    assert ids(refs["aIMLInferenceReportRefList"]) == [resolved["aIMLInferenceReportId"]]


def test_inference_on_a_function_without_the_model_loaded(client, mlmr, db_session_factory):
    """Inference that names an ACTIVATED function which has not loaded the model is refused with 409 `MODEL_NOT_LOADED`."""
    model_id = _certified_model(mlmr, db_session_factory)
    _set_lifecycle(db_session_factory, model_id, runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    function_id = client.post("/aiml-inference-functions", json={"activationStatus": "ACTIVATED"}).json()["id"]
    resp = client.post(f"/models/{model_id}/inference-jobs", params={"aiml_inference_function_id": function_id})
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "MODEL_NOT_LOADED"


def test_loading_rejects_uncertified_model_and_supports_suspend(client, mlmr, db_session_factory):
    """Loading an uncertified model is 409; a request created with `suspendRequest` waits SUSPENDED and deploys nothing until PATCHed back, which runs the load and activates the runtime."""
    function_id = client.post("/aiml-inference-functions", json={}).json()["id"]
    trained = mlmr.add_model()
    _set_lifecycle(db_session_factory, trained, model_lifecycle_state=ModelLifecycleState.TRAINED)
    resp = client.post("/ml-model-loading-requests", json={"aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(trained)]})
    assert resp.status_code == 409

    model_id = _certified_model(mlmr, db_session_factory)
    request = client.post("/ml-model-loading-requests", json={
        "aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(model_id)], "suspendRequest": True})
    assert _attrs(request)["requestStatus"] == "SUSPENDED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == RuntimeLifecycleState.NOT_DEPLOYED
    resumed = client.patch(f"/ml-model-loading-requests/{request.json()['id']}", json={"suspendRequest": False})
    assert _attrs(resumed)["requestStatus"] == "FINISHED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == RuntimeLifecycleState.ACTIVE


def test_loading_policy_trigger(client, mlmr, db_session_factory):
    """Triggering a loading policy runs a loading process that names the policy and finishes, with no loading request behind it."""
    model_id = _certified_model(mlmr, db_session_factory)
    function_id = client.post("/aiml-inference-functions", json={}).json()["id"]
    policy = client.post("/ml-model-loading-policies", json={
        "aIMLInferenceFunctionRef": function_id, "mLModelRef": [str(model_id)],
        "policyForLoading": {"thresholdList": [{"monitoredMDAOutputIE": "PrbUtil", "thresholdValue": 5}]}}).json()
    process = _attrs(client.post(f"/ml-model-loading-policies/{policy['id']}/trigger"))
    assert process["mLModelLoadingPolicyRef"] == [policy["id"]]
    assert process["progressStatus"]["status"] == "FINISHED"


def test_emulation_on_emulation_function_writes_inference_report(client, mlmr, db_session_factory):
    """Completing an emulation run that named an emulation function writes an AIMLInferenceReport under it, carrying the outputs and the job reference."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    emu_fn = client.post("/aiml-inference-emulation-functions", json={"userLabel": "mlef"}).json()["id"]
    job_id = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "p",
                                                   "aIMLInferenceEmulationFunctionRef": emu_fn}).json()["emulationJobId"]
    client.post(f"/emulation-jobs/{job_id}/complete", json={
        "succeeded": True, "inferenceOutputs": [{"outputResult": {"energySavingPct": 18}}]})
    reports = client.get("/aiml-inference-reports", params={"aiml_inference_emulation_function_id": emu_fn}).json()["items"]
    assert reports[0]["attributes"]["inferenceOutputs"] == [{"outputResult": {"energySavingPct": 18}}]
    assert reports[0]["attributes"]["emulationJobRef"] == job_id


def test_direct_inference_report_needs_exactly_one_function(client, mlmr):
    """A report posted directly needs exactly one function reference (none is 422); one with a function is stored and found by its model id."""
    function_id = client.post("/aiml-inference-functions", json={}).json()["id"]
    model_id = str(uuid.uuid4())
    assert client.post("/aiml-inference-reports", json={"mLModelRefList": [model_id]}).status_code == 422
    resp = client.post("/aiml-inference-reports", json={"aIMLInferenceFunctionRef": function_id, "mLModelRefList": [model_id]})
    assert resp.status_code == 201
    assert client.get("/aiml-inference-reports", params={"model_id": model_id}).json()["total"] == 1


# ---------------------------------------------------------------- MLUpdateFunction / Request / Process / Report

def _promoted_models(mlmr, db_session_factory, n=2):
    """Registers `n` models in the MLMR double and puts each at PROMOTED, the state an update request can fine-tune from; returns their ids."""
    made = []
    for _ in range(n):
        model_id = mlmr.add_model()
        register(db_session_factory, model_id)
        _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
        made.append(model_id)
    return made


def test_update_request_fine_tunes_every_model_and_reports_when_all_done(client, mlmr, db_session_factory):
    """An update request starts one FINE_TUNING run per model under one process; progress is 50 % after the first run completes and the report appears only after the second; the process
    and request are then FINISHED and the update function takes on the capability version.
    """
    models = _promoted_models(mlmr, db_session_factory)
    function_id = create(client, db_session_factory, "MLUpdateFunction", {"userLabel": "mluf"})
    request = client.post("/ml-update-requests", json={
        "mLUpdateFunctionRef": function_id, "mLModelRefList": [str(m) for m in models],
        "newCapabilityVersionId": ["v2"]}).json()
    process_id = request["attributes"]["mLUpdateProcessRef"]
    process = _attrs(client.get(f"/ml-update-processes/{process_id}"))
    jobs = process["trainingRequestRefList"]
    assert len(jobs) == 2
    for job_id in jobs:
        assert _attrs(client.get(f"/ml-training-requests/{job_id}"))["mLTrainingType"] == "FINE_TUNING"

    client.post(f"/training-jobs/{jobs[0]}/complete", json={"succeeded": True, "metrics": {"MAE": 1.5}})
    process = _attrs(client.get(f"/ml-update-processes/{process_id}"))
    assert process["progressStatus"]["progressPercentage"] == 50 and process["mLUpdateReportRef"] is None

    client.post(f"/training-jobs/{jobs[1]}/complete", json={"succeeded": True, "metrics": {"MAE": 1.7}})
    process = _attrs(client.get(f"/ml-update-processes/{process_id}"))
    assert process["progressStatus"]["status"] == "FINISHED"
    report = find(client, "MLUpdateReport", process["mLUpdateReportRef"])            # an update report is a ProvMnS class since S2.10
    assert report["updatedMLCapability"]["mLCapabilityVersionId"] == ["v2"]
    assert sorted(ids(report["mLModelRefList"])) == sorted(str(m) for m in models)
    assert _attrs(client.get(f"/ml-update-requests/{request['id']}"))["requestStatus"] == "FINISHED"
    assert find(client, "MLUpdateFunction", function_id)["availMLCapabilityReport"]["mLCapabilityVersionId"] == ["v2"]


def test_update_request_rejects_untrainable_model_and_cancel_stops_runs(client, mlmr, db_session_factory):
    """An update naming a DEPRECATED model is refused (409 naming the state); cancelling a running update cancels its runs and releases the model from TRAINING."""
    deprecated = mlmr.add_model()
    _set_lifecycle(db_session_factory, deprecated, model_lifecycle_state=ModelLifecycleState.DEPRECATED)
    resp = client.post("/ml-update-requests", json={"mLModelRefList": [str(deprecated)]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert "DEPRECATED" in resp.json()["detail"]["detail"]

    models = _promoted_models(mlmr, db_session_factory, n=1)
    request = client.post("/ml-update-requests", json={"mLModelRefList": [str(models[0])]}).json()
    cancelled = _attrs(client.patch(f"/ml-update-requests/{request['id']}", json={"cancelRequest": True}))
    assert cancelled["requestStatus"] == "CANCELLED"
    job_id = _attrs(client.get(f"/ml-update-processes/{cancelled['mLUpdateProcessRef']}"))["trainingRequestRefList"][0]
    assert _attrs(client.get(f"/ml-training-requests/{job_id}"))["requestStatus"] == "CANCELLED"
    # the cancelled run releases its model from TRAINING (OI-2-training-lifecycle-edges)
    assert client.get(f"/models/{models[0]}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED


def test_update_request_accepts_a_certified_model(client, mlmr, db_session_factory):
    """A CERTIFIED (rolled-back) model can be updated: its run starts and the model enters TRAINING."""
    certified = mlmr.add_model()
    _set_lifecycle(db_session_factory, certified, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    assert client.post("/ml-update-requests", json={"mLModelRefList": [str(certified)]}).status_code == 201
    assert client.get(f"/models/{certified}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING


def test_unknown_nrm_object_is_a_clean_404(client):
    """GET by an unknown id on each of the 10 NRM collections that still have a REST route answers 404 `NRM_OBJECT_NOT_FOUND`, never a 500; an
    unknown instance of a class read over ProvMnS only (S2.7, S2.10) is the router's 404 MOI_NOT_FOUND."""
    for path in ("ml-training-requests", "ml-training-processes", "ml-testing-requests", "aiml-inference-functions",
                 "aiml-inference-emulation-functions", "aiml-inference-reports", "ml-model-loading-policies",
                 "ml-model-loading-requests", "ml-update-requests", "ml-update-processes"):
        resp = client.get(f"/{path}/{uuid.uuid4()}")
        assert resp.status_code == 404, path
        assert resp.json()["detail"]["title"] == "NRM_OBJECT_NOT_FOUND"
    for dn in ("MLTrainingFunction=default/MLTrainingReport", "MLTestingFunction", "MLUpdateFunction", "AIMLInferenceFunction=default/MLModelLoadingProcess"):
        resp = client.get(f"/ProvMnS/{PROVMNS_VERSION}/ManagedElement=AIMgF/{dn}={uuid.uuid4()}")
        assert (resp.status_code, resp.json()["type"]) == (404, "MOI_NOT_FOUND"), dn


def test_nrm_cancel_of_a_training_request_releases_the_model(client, mlmr):
    """Cancelling a training request through its flag, or through the process flag, uses the same cancel path as DELETE /training-jobs/{id} (the model leaves TRAINING), and DELETE of an ended request is a no-op 204."""
    model_id = mlmr.add_model()
    request_id = client.post("/ml-training-requests", json={"mLModelRef": str(model_id), "trainingRequestSource": "x"}).json()["id"]
    assert _attrs(client.patch(f"/ml-training-requests/{request_id}", json={"cancelRequest": True}))["requestStatus"] == "CANCELLED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED

    other = mlmr.add_model()
    request_id = client.post("/ml-training-requests", json={"mLModelRef": str(other), "trainingRequestSource": "x"}).json()["id"]
    process_id = [p for p in client.get("/ml-training-processes").json()["items"]
                  if p["attributes"]["trainingRequestRef"] == [request_id]][0]["id"]
    client.patch(f"/ml-training-processes/{process_id}", json={"cancelProcess": True})
    assert client.get(f"/models/{other}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED
    # DELETE of a finished/cancelled request stays a no-op
    assert client.delete(f"/ml-training-requests/{request_id}").status_code == 204


def test_a_list_route_accepts_total_false(client):
    """A list route honours `total=false`: no `total` key, one item for `limit=1`, and `hasMore`; this also holds for the in-memory inference-report list."""
    client.post("/aiml-inference-emulation-functions", json={"userLabel": "a"})
    client.post("/aiml-inference-emulation-functions", json={"userLabel": "b"})
    assert client.get("/aiml-inference-emulation-functions", params={"limit": 1}).json()["total"] == 2
    page = client.get("/aiml-inference-emulation-functions", params={"limit": 1, "total": "false"}).json()
    assert "total" not in page and len(page["items"]) == 1 and page["hasMore"] is True
    reports = client.get("/aiml-inference-reports", params={"total": "false"}).json()
    assert reports["items"] == [] and reports["hasMore"] is False
