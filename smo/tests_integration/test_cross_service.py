"""Integration tests across service boundaries — the real cross-module
calls, not mocked, running against the in-process service mesh
(conftest.py, mesh.py). Two real bugs were caught and fixed while
building this harness, before a single test ran: R1 Termination's proxy
never stripped the module prefix before forwarding (would 404 against
every real backend), and NFO queried FOCOM's inventory with the wrong
query parameter name (resourceType vs. resource_type — a silent
behavioral bug, not a crash, since FOCOM just silently fell back to its
default). Both are fixed in their respective modules; these tests now
guard against regressing them.

Run with: pytest smo/tests_integration -q
"""

import uuid
from pathlib import Path


def test_real_demo_csar_onboards_and_deploys(mesh, loaded_apps, shared_engine, monkeypatch):
    """smo/samples/energy-saving-rapp.csar — the real, spec-shaped sample
    package smo/DEMO_RUNBOOK.md walks a live docker-compose deployment
    through — must keep onboarding and deploying for real, through
    _validate_package's actual (unstubbed) zip-parsing, not a mocked
    stand-in. Regression guard for the real bug this exposed: the
    reference's own FileExistenceValidator requires
    Files/Acm/definition/compositions.json (RappCsarPathProvider.
    ACM_COMPOSITION_JSON_LOCATION), not Definitions/acm_composition.json
    — the path this build's own validator and test fixture both
    originally guessed wrong, which would have rejected every real CSAR
    the reference itself produces.
    """
    csar_bytes = (Path(__file__).resolve().parent.parent / "samples" / "energy-saving-rapp.csar").read_bytes()

    class FakeResp:
        content = csar_bytes
        def raise_for_status(self):
            pass

    import httpx
    real_get = httpx.get  # the mesh's own installed dispatcher — must still handle every other call

    def fake_get(location, timeout=None, **kwargs):
        if location == "http://example/energy-saving-rapp.csar":
            return FakeResp()
        return real_get(location, timeout=timeout, **kwargs)

    monkeypatch.setattr(loaded_apps["onboarding"].httpx, "get", fake_get)

    onboard = mesh["onboarding"].post("/packages", json={"location": "http://example/energy-saving-rapp.csar"})
    package_id = onboard.json()["packageId"]

    status = mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"
    assert status.json()["nfDeploymentDescriptorId"] is not None
    pkg = next(p for p in mesh["onboarding"].get("/packages").json()["items"] if p["packageId"] == package_id)
    assert (pkg["name"], pkg["version"], pkg["vendor"]) == ("EnergySaving_rApp", "1.0.0", "Radisys")

    create = mesh["rapp-mgmt"].post("/instances", json={"packageId": package_id, "config": {}})
    assert create.status_code == 202
    assert create.json()["instanceId"]


def test_nfo_instantiate_actually_resolves_cluster_through_focom(mesh):
    """NFO+FOCOM LLD section 4: NFO's Instantiate queries FOCOM's real
    inventory endpoint — not a mock — before placing a workload.
    """
    descriptor_id = mesh["nfo"].post("/descriptors", json={
        "packageId": str(uuid.uuid4()), "name": "Definitions/main.yaml",
    }).json()["nfDeploymentDescriptorId"]

    resp = mesh["nfo"].post("/deployments", json={
        "nfDeploymentDescriptorId": descriptor_id, "name": "integration-test-deployment", "requiredResourceTypeId": "gpu-l40",
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["clusterId"] == "phase1-degenerate-cluster"  # FOCOM's real (Phase 1) answer, reached over the mesh
    assert body["state"] == "RUNNING"


def test_ran_analytics_producer_registration_creates_a_real_sme_service(mesh):
    """RAN Analytics LLD section 1: RegisterAnalyticsProducer registers
    via SME — proving the SME ServiceProfile actually gets created.
    """
    resp = mesh["ran-analytics"].post("/producers", params={
        "producer_id": "rapp-mdaf-1", "analytics_type": "coverage-issue-analysis",
    }, json={"dme_input_types": [str(uuid.uuid4())], "output_schema": {"type": "object"}})
    assert resp.status_code == 201

    services = mesh["sme"].get("/published-apis/v1/rapp-mdaf-1/service-apis").json()
    assert len(services) == 1
    assert services[0]["serviceCapabilities"]["analyticsType"] == "coverage-issue-analysis"


def test_so_smos_fail_fast_halts_on_a_real_downstream_rejection(mesh):
    """SO SMOS LLD section 1.1: fail-fast, no auto-compensation — proven
    here against a REAL downstream failure (an unknown NF deployment
    descriptor, NFO's own validation), not a mocked exception.
    """
    resp = mesh["so-smos"].post("/orders", json={
        "scope": "deploy-rollout",
        "steps": [
            {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": str(uuid.uuid4()), "name": "first"},
            {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": str(uuid.uuid4()), "name": "second"},
        ],
    })
    steps = resp.json()["steps"]
    assert steps[0]["status"] == "FAILED"
    assert steps[1]["status"] == "PENDING"  # never attempted


def test_onboarding_to_rapp_management_status_check(mesh):
    """rApp Management LLD section 5: CreateInstance checks Onboarding's
    real onboarding-status before proceeding — proven by onboarding a
    package with a deliberately-broken location (a 404, not a real
    .csar), which OnboardPackage's own validation routes to FAILED, and
    confirming CreateInstance refuses to deploy it. The full happy-path
    deploy (a real NFDeploymentDescriptor created and consumed) is
    covered separately below.
    """
    # Points at a real, mesh-reachable endpoint that returns 200 JSON —
    # reachable, but neither a real .csar filename nor valid zip bytes, so
    # OnboardPackage's own validation (the filename check now, same as the
    # zipfile.BadZipFile handling it would otherwise fall through to)
    # routes it to FAILED. (An unreachable host is also handled —
    # onboarding/app/main.py's ONBOARD_VALIDATION_FAILURES now catches
    # httpx.HTTPError too — but the mesh only resolves known service
    # hosts, so that path isn't exercisable through this harness; not a
    # gap in the fix itself.)
    onboard = mesh["onboarding"].post("/packages", json={"location": "http://focom:8000/inventory"})
    package_id = onboard.json()["packageId"]

    status = mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "FAILED"

    create = mesh["rapp-mgmt"].post("/instances", json={"packageId": package_id, "config": {}})
    assert create.status_code == 409  # rApp Mgmt correctly refuses — the package never reached AVAILABLE


def test_onboarding_to_rapp_management_full_deploy_creates_real_nf_deployment_descriptor(mesh, loaded_apps, db_connection, monkeypatch):
    """The actual fix for HISTORY.md's top item: NFO's CreateDescriptor
    (NFO+FOCOM LLD section 2) is now called from OnboardPackage once
    validation succeeds, and rApp Management's CreateInstance now passes
    that REAL nfDeploymentDescriptorId to NFO instead of packageId. Proven
    end to end — not just per module — by reaching into both modules'
    real DB rows through the shared engine, not just trusting the HTTP
    responses.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    # _validate_package's own zip-parsing is exercised by onboarding's unit
    # tests; stubbing it here keeps this test's focus on the cross-module
    # wiring this pass actually changed, not TOSCA zip mechanics.
    monkeypatch.setattr(
        loaded_apps["onboarding"], "_validate_package",
        lambda location: ("Definitions/main.yaml", [], "deadbeef", {}),
    )

    onboard = mesh["onboarding"].post("/packages", json={"location": "http://example/pkg.csar"})
    package_id = onboard.json()["packageId"]

    status = mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"
    nf_deployment_descriptor_id = status.json()["nfDeploymentDescriptorId"]
    assert nf_deployment_descriptor_id is not None
    assert nf_deployment_descriptor_id != package_id  # a real, distinct descriptor row — not packageId reused

    NFDeploymentDescriptor = loaded_apps["nfo"].NFDeploymentDescriptor
    NFDeployment = loaded_apps["nfo"].NFDeployment
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        descriptor = session.get(NFDeploymentDescriptor, uuid.UUID(nf_deployment_descriptor_id))
        assert descriptor is not None
        assert str(descriptor.package_id) == package_id

    create = mesh["rapp-mgmt"].post("/instances", json={"packageId": package_id, "config": {}})
    assert create.status_code == 202

    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        deployment = session.scalar(
            select(NFDeployment).where(NFDeployment.nf_deployment_descriptor_id == uuid.UUID(nf_deployment_descriptor_id))
        )
        assert deployment is not None
        assert deployment.state == "RUNNING"


def test_ran_nf_oam_config_write_reaches_a_real_mock_o1_adaptor(mesh, loaded_apps, db_connection):
    """HISTORY.md §2: "no real southbound integrations" — RAN NF OAM LLD section 5.1's PATCH step
    (netconf_client.py) always dispatched a real RFC 6241 <edit-config>
    RPC, but nothing in this build's own topology ever answered it for
    real before mock-o1-adaptor existed. Proven end to end here, no
    ManagedElement registration route exists (a separate, undocumented
    gap, not this item's own scope), so the ManagedEntity/O1AdaptorEndpoint
    rows are seeded directly through the shared engine — the real HTTP
    call this test proves is write_configuration_changes's own dispatch
    to mock-o1-adaptor, not this setup step.
    """
    from sqlalchemy.orm import Session

    ManagedEntity = loaded_apps["ran-nf-oam"].ManagedEntity
    O1AdaptorEndpoint = loaded_apps["ran-nf-oam"].O1AdaptorEndpoint

    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        endpoint = O1AdaptorEndpoint(
            managed_element_ref="ME-integration-1", adaptor_uri="http://mock-o1-adaptor:8000/edit-config",
            protocol_support=["NETCONF"],
        )
        session.add(endpoint)
        session.flush()
        session.add(ManagedEntity(
            managed_element_ref="ME-integration-1", entity_type="O-DU", o1_protocol="NETCONF",
            o1_adaptor_endpoint_id=endpoint.endpoint_id,
        ))
        session.commit()

    resp = mesh["ran-nf-oam"].post("/config-jobs", json={
        "requestedBy": "test", "scope": "single-NF",
        "changes": [{"managedElementRef": "ME-integration-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    assert resp.status_code == 202
    job = resp.json()
    assert job["status"] == "COMPLETED"  # every sub-change APPLIED

    status = mesh["ran-nf-oam"].get(f"/config-jobs/{job['jobId']}")
    assert status.json()["subChanges"][0]["status"] == "APPLIED"

    # mock-o1-adaptor's own real, parsed record of what it received —
    # proves the RPC round trip actually carried the real attribute
    # changes, not just that RAN NF OAM's own status flipped to APPLIED.
    applied = mesh["mock-o1-adaptor"].get("/edit-config/ME-integration-1")
    assert applied.json()["attributeChanges"] == {"adminState": "UNLOCKED"}


def test_ran_nf_oam_config_write_rejected_by_mock_o1_adaptor_is_recorded(mesh, loaded_apps, db_connection):
    """The other real outcome: mock-o1-adaptor's own rejection (an empty
    attributeChanges payload — the same real, testable trigger its own
    unit tests use) must surface as a genuine REJECTED sub_change, not a
    silently-swallowed failure.
    """
    from sqlalchemy.orm import Session

    ManagedEntity = loaded_apps["ran-nf-oam"].ManagedEntity
    O1AdaptorEndpoint = loaded_apps["ran-nf-oam"].O1AdaptorEndpoint

    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        endpoint = O1AdaptorEndpoint(
            managed_element_ref="ME-integration-2", adaptor_uri="http://mock-o1-adaptor:8000/edit-config",
            protocol_support=["NETCONF"],
        )
        session.add(endpoint)
        session.flush()
        session.add(ManagedEntity(
            managed_element_ref="ME-integration-2", entity_type="O-DU", o1_protocol="NETCONF",
            o1_adaptor_endpoint_id=endpoint.endpoint_id,
        ))
        session.commit()

    resp = mesh["ran-nf-oam"].post("/config-jobs", json={
        "requestedBy": "test", "scope": "single-NF",
        "changes": [{"managedElementRef": "ME-integration-2", "attributeChanges": {}}],
    })
    job = resp.json()
    assert job["status"] == "FAILED"  # every sub-change REJECTED

    status = mesh["ran-nf-oam"].get(f"/config-jobs/{job['jobId']}")
    sub_change = status.json()["subChanges"][0]
    assert sub_change["status"] == "REJECTED"
    assert sub_change["rejectionReason"] == "NETCONF_RPC_FAILED"


def test_ts28105_nrm_pipeline_across_aimgf_mlmr_and_nfo(mesh):
    """Wave 4 (TS 28.105 at REST level): an MLTrainingRequest through real
    AIMgF -> MLMR (model existence) -> NFO (MLTF runtime), its report, a
    model loaded onto an AIMLInferenceFunction through the real
    RuntimeLifecycle, and MLMR's own MLModel NRM view joining AIMgF's
    read-only cross-references back through R1.
    """
    model_id = mesh["mlmr"].post("/models", json={
        "modelType": "nrm-energy", "version": "1.0", "aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING",
    }).json()["modelId"]
    request = mesh["aimgf"].post("/ml-training-requests", json={"mLModelRef": model_id, "trainingRequestSource": "es-rapp"})
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    assert request.json()["attributes"]["mLTrainingType"] == "INITIAL_TRAINING"
    done = mesh["aimgf"].post(f"/training-jobs/{request_id}/complete", json={
        "succeeded": True, "modelPerformanceTraining": [{"performanceMetric": "MAE", "performanceScore": 1.8}]})
    assert done.status_code == 200, done.text

    mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": "APPROVE_TRAINING", "decided_by": "op"})
    testing = mesh["aimgf"].post("/ml-testing-requests", json={"mLModelRef": model_id})
    assert testing.status_code == 201, testing.text
    mesh["aimgf"].post(f"/validation-jobs/{testing.json()['id']}/complete", json={"succeeded": True})
    mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": "APPROVE_VALIDATION", "decided_by": "op"})
    emulation = mesh["aimgf"].post("/emulation-jobs", json={"modelId": model_id, "producerId": "es-rapp"}).json()
    mesh["aimgf"].post(f"/emulation-jobs/{emulation['emulationJobId']}/complete", json={"succeeded": True})
    for event in ("SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY"):
        assert mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "op"}).status_code == 200

    function_id = mesh["aimgf"].post("/aiml-inference-functions", json={"activationStatus": "ACTIVATED"}).json()["id"]
    loading = mesh["aimgf"].post("/ml-model-loading-requests", json={
        "aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [model_id]})
    assert loading.json()["attributes"]["requestStatus"] == "FINISHED", loading.text
    lifecycle = mesh["aimgf"].get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["runtimeLifecycleState"] == "ACTIVE" and lifecycle["nfDeploymentId"]

    nrm = mesh["mlmr"].get(f"/ml-models/{model_id}").json()["attributes"]
    assert nrm["mLTrainingType"] == "INITIAL_TRAINING"
    assert nrm["usedByFunctionRefList"] == [function_id]
    assert nrm["aIMLInferenceName"] == "NG_RAN_NETWORK_ENERGY_SAVING"


def test_ts28104_drift_report_reaches_aimgf_mlmf_and_request_delivery(mesh):
    """Wave 5 (TS 28.104 at REST level): an MDARequest receives a matching
    PREDICTION report, and a DRIFT report naming a model is forwarded
    through R1 to the real AIMgF MLMF subscription, whose guard-KPI floor
    marks the breach."""
    request = mesh["mdaf"].post("/mda-requests", json={
        "reportingMethod": "STREAMING", "requestedMDAOutputs": [{"mDAType": "PREDICTIONS_PM_DATA"}],
        "analyticsScope": {"managedEntitiesScope": ["cell-101"]}})
    assert request.status_code == 201, request.text
    report = mesh["mdaf"].post("/mda-reports", json={"managedEntitiesScope": ["cell-101"], "mDAOutputs": [{
        "mDAType": "PREDICTIONS_PM_DATA", "mDAOutputList": {"pmPredictions": [{"pmName": "RRU.PrbUsedDl", "pmPredictedValue": 2.8}]}}]})
    assert report.json()["attributes"]["deliveredToRequestRefList"] == [request.json()["id"]]

    model_id = mesh["mlmr"].post("/models", json={"modelType": "drifty", "version": "1"}).json()["modelId"]
    sub = mesh["aimgf"].post("/mlmf/subscriptions", params={"model_id": model_id, "dme_type_id": str(uuid.uuid4())},
                             json={"metric_types": ["accuracy"], "guard_kpi_floor": {"accuracy": 0.8}})
    assert sub.status_code == 201, sub.text
    drift = mesh["mdaf"].post("/mda-reports", json={"reportKind": "DRIFT", "mDAOutputs": [{
        "mDAType": "CORRELATION_ANALYTICS_TRAINING_DATA_ANALYSIS",
        "mDAOutputList": [{"mDAOutputIEName": "mLModelRef", "mDAOutputIEValue": model_id},
                          {"mDAOutputIEName": "accuracy", "mDAOutputIEValue": 0.61}]}]})
    assert drift.status_code == 201, drift.text
    reports = mesh["aimgf"].get(f"/mlmf/subscriptions/{sub.json()['subscriptionId']}/reports").json()["items"]
    assert reports[0]["metrics"] == {"accuracy": 0.61} and reports[0]["breachedFloor"] is True


def test_ts28105_cross_module_paths_resolve_inside_the_mesh(mesh):
    """Regression guard: AIMgF's ML-update completion, emulation on an
    AIMLInferenceEmulationFunction and testing-request cancel all reach
    code in app/nrm.py from app/main.py (and back). Those references must
    be bound at load time — a call-time `from .nrm import ...` breaks in
    this mesh, whose loader evicts `app.*` from sys.modules per service.
    """
    def promoted_model(name):
        model_id = mesh["mlmr"].post("/models", json={"modelType": name, "version": "1"}).json()["modelId"]
        job = mesh["aimgf"].post("/training-jobs", json={"modelId": model_id, "producerId": "p"}).json()["trainingJobId"]
        mesh["aimgf"].post(f"/training-jobs/{job}/complete", json={"succeeded": True})
        return model_id

    model_id = promoted_model("mesh-update")
    mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": "APPROVE_TRAINING", "decided_by": "op"})
    testing = mesh["aimgf"].post("/ml-testing-requests", json={"mLModelRef": model_id}).json()
    cancelled = mesh["aimgf"].patch(f"/ml-testing-requests/{testing['id']}", json={"cancelRequest": True})
    assert cancelled.status_code == 200, cancelled.text
    assert mesh["aimgf"].get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == "FAILED"

    update = mesh["aimgf"].post("/ml-update-requests", json={"mLModelRefList": [model_id], "newCapabilityVersionId": ["v2"]})
    assert update.status_code == 201, update.text
    process_id = update.json()["attributes"]["mLUpdateProcessRef"]
    job_id = mesh["aimgf"].get(f"/ml-update-processes/{process_id}").json()["attributes"]["trainingRequestRefList"][0]
    done = mesh["aimgf"].post(f"/training-jobs/{job_id}/complete", json={"succeeded": True, "metrics": {"MAE": 1.2}})
    assert done.status_code == 200, done.text
    assert mesh["aimgf"].get(f"/ml-update-processes/{process_id}").json()["attributes"]["progressStatus"]["status"] == "FINISHED"

    emu_fn = mesh["aimgf"].post("/aiml-inference-emulation-functions", json={}).json()["id"]
    other = mesh["mlmr"].post("/models", json={"modelType": "mesh-emu", "version": "1"}).json()["modelId"]
    job = mesh["aimgf"].post("/training-jobs", json={"modelId": other, "producerId": "p"}).json()["trainingJobId"]
    mesh["aimgf"].post(f"/training-jobs/{job}/complete", json={"succeeded": True})
    mesh["aimgf"].post(f"/models/{other}/advance", params={"event": "APPROVE_TRAINING", "decided_by": "op"})
    validation = mesh["aimgf"].post("/validation-jobs", json={"modelId": other, "producerId": "p"}).json()["validationJobId"]
    mesh["aimgf"].post(f"/validation-jobs/{validation}/complete", json={"succeeded": True})
    mesh["aimgf"].post(f"/models/{other}/advance", params={"event": "APPROVE_VALIDATION", "decided_by": "op"})
    emulation = mesh["aimgf"].post("/emulation-jobs", json={"modelId": other, "producerId": "p",
                                                            "aIMLInferenceEmulationFunctionRef": emu_fn})
    assert emulation.status_code == 201, emulation.text


def test_runtime_profile_flows_from_rapp_manifest_to_nfo_descriptor(mesh, loaded_apps, monkeypatch):
    """Wave 7 (W7-03): a real CSAR whose manifest.yaml declares
    runtimeProfiles onboards through the real Onboarding validator; an AIMgF
    training run naming that package gets an NFO descriptor sized with the
    TRAINING profile, read back from NFO itself."""
    import io
    import zipfile

    import httpx

    original = (Path(__file__).resolve().parent.parent / "samples" / "energy-saving-rapp.csar").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(buf, "w") as dst:
        for item in src.infolist():
            if item.filename != "manifest.yaml":
                dst.writestr(item, src.read(item.filename))
        dst.writestr("manifest.yaml", "rappManifest:\n  manifestVersion: \"1.0\"\n"
                                      "executionModes: [TRAINING, INFERENCE]\n"
                                      "runtimeProfiles:\n  TRAINING: {cpu: 8, memory: 16Gi, gpu: 0}\n")
    csar = buf.getvalue()

    class FakeResp:
        content = csar

        def raise_for_status(self):
            pass

    real_get = httpx.get
    monkeypatch.setattr(loaded_apps["onboarding"].httpx, "get",
                        lambda location, timeout=None, **kw: FakeResp() if location == "http://example/es.csar"
                        else real_get(location, timeout=timeout, **kw))
    package_id = mesh["onboarding"].post("/packages", json={"location": "http://example/es.csar"}).json()["packageId"]
    assert mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status").json()["state"] == "AVAILABLE"

    model_id = mesh["mlmr"].post("/models", json={"modelType": "w7-profile", "version": "1"}).json()["modelId"]
    job = mesh["aimgf"].post("/training-jobs", json={"modelId": model_id, "producerId": "p", "packageId": package_id})
    assert job.status_code == 201, job.text
    job_id = job.json()["trainingJobId"]
    descriptors = mesh["nfo"].get("/descriptors", params={"limit": 500}).json()["items"]
    mine = next(d for d in descriptors if d["name"] == f"aimgf-training-{job_id}")
    assert mine["workloadTemplate"]["resources"] == {"cpu": 8, "memory": "16Gi", "gpu": 0}


def _seed_managed_element(loaded_apps, db_connection, ref):
    """Inserts a managed element and the mock O1 adaptor endpoint it points at straight into RAN NF OAM's tables, inside the test's connection (a
    savepoint), so a test can start from a registered element without the registration routes.
    """
    from sqlalchemy.orm import Session

    ManagedEntity = loaded_apps["ran-nf-oam"].ManagedEntity
    O1AdaptorEndpoint = loaded_apps["ran-nf-oam"].O1AdaptorEndpoint
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        endpoint = O1AdaptorEndpoint(managed_element_ref=ref, adaptor_uri="http://mock-o1-adaptor:8000/edit-config",
                                     protocol_support=["NETCONF"])
        session.add(endpoint)
        session.flush()
        session.add(ManagedEntity(managed_element_ref=ref, entity_type="O-DU", o1_protocol="NETCONF",
                                  o1_adaptor_endpoint_id=endpoint.endpoint_id))
        session.commit()


def test_o1_cm_intent_handler_enacts_an_intent_through_dme_to_the_o1_adaptor(mesh, loaded_apps, db_connection):
    """Wave 8 (W8-07, D-1): SA SMOS registers as the generic O1-CM intent
    handler; an Intent setting NRCellDU.administrativeState=LOCKED on two
    cells of a managed element is pushed to it, written through DME's
    action mediation and RAN NF OAM's NETCONF edit-config to the real mock
    O1 adaptor, and reported back to Intent Service as FULFILLED with the
    action references."""
    _seed_managed_element(loaded_apps, db_connection, "gnb-du-01")
    registered = mesh["sa-smos"].post("/o1-cm-handler/registration", json={})
    assert registered.status_code == 201, registered.text

    intent = mesh["intent-service"].post("/intents", json={
        "userLabel": "sleep cells 101/102", "rmioId": "es-rapp", "rmihId": "sa-smos",
        "intentReportControl": [{"observationPeriod": 60}],
        "intentExpectations": [{
            "expectationId": "sleep", "expectationVerb": "DELIVER",
            "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": "gnb-du-01", "objectContexts": [
                {"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF", "contextValueRange": ["101", "102"]}]},
            "expectationTargets": [{"targetName": "NRCellDU.administrativeState", "targetCondition": "IS_EQUAL_TO",
                                    "targetValueRange": "LOCKED"}]}],
    })
    assert intent.status_code == 201, intent.text
    intent_id = intent.json()["intentId"]

    enactments = mesh["sa-smos"].get("/o1-cm-handler/enactments", params={"intent_id": intent_id}).json()["items"]
    assert enactments[0]["status"] == "FULFILLED", enactments
    action = mesh["dme"].get(f"/actions/{enactments[0]['actions'][0]['actionId']}").json()
    assert action["sourceContext"]["intentId"] == intent_id
    assert [c["managedFunctionRef"] for c in action["changes"]] == ["NRCellDU=101", "NRCellDU=102"]
    assert mesh["mock-o1-adaptor"].get("/edit-config/gnb-du-01").json()["attributeChanges"] == {"administrativeState": "LOCKED"}

    current = mesh["intent-service"].get(f"/intents/{intent_id}").json()["attributes"]["intentReportReference"]
    report = mesh["intent-service"].get(f"/intent-reports/{current}").json()["attributes"]
    assert report["intentFulfilmentReport"]["intentFulfilmentInfo"] == {"fulfilmentStatus": "FULFILLED"}
    assert '"forwardedJobId"' in report["intentFulfilmentReport"]["additionalFulfilmentInfo"]

    # an attribute value outside the registered CM target's range is not feasible
    rejected = mesh["intent-service"].post("/intents", json={
        "userLabel": "bad", "rmioId": "es-rapp", "rmihId": "sa-smos", "intentReportControl": [{"observationPeriod": 60}],
        "intentExpectations": [{"expectationId": "x", "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": "gnb-du-01"},
                                "expectationTargets": [{"targetName": "NRCellDU.operationalState", "targetCondition": "IS_EQUAL_TO",
                                                        "targetValueRange": "DISABLED"}]}]})
    assert rejected.status_code == 422


def test_vendor_onboarding_gates_o1_writes_by_capability_and_schema(mesh):
    """Wave 9 (W9-01..04): a vendor is onboarded as data — once its O1
    adaptor has registered, its capability declaration is discovered from
    that registered adaptor, its data-model
    descriptor loaded, its capability declared (COMBINED: the 3GPP
    TS 28.541 descriptor plus the vendor's named augments) — then its
    endpoint registers unchanged. A CM write through DME's action mediation
    is checked against that model: a valid one reaches the adaptor, an
    attribute the model lacks is refused with the standard error schema
    before anything is dispatched."""
    vendor_model = {"schemaName": "mock-vendor-nr", "revision": "1.0", "type": "OPENAPI_NRM",
                    "location": "builtin-test:mock-vendor-nr", "descriptor": {"classes": {"NRCellDU": {"mockBoost": {"type": "boolean"}}}}}
    registered = mesh["ran-nf-oam"].post("/o1-adaptor-endpoints", json={
        "managedElementRef": "gnb-du-vendor", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config",
        "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"})
    assert registered.status_code == 201, registered.text
    mesh["ran-nf-oam"].post(f"/o1-adaptor-endpoints/{registered.json()['endpointId']}/heartbeat")

    onboarded = mesh["ran-nf-oam"].post("/vendor-onboarding", json={
        "vendorName": "mock-vendor", "discoverFrom": "gnb-du-vendor", "conformanceMode": "COMBINED", "schemas": [vendor_model]})
    assert onboarded.status_code == 201, onboarded.text
    assert onboarded.json()["discovered"]["supportedVendorModes"] == ["O1_NETCONF", "O1_RESTCONF"]
    assert onboarded.json()["capability"]["discoveryUri"] == "http://mock-o1-adaptor:8000/capabilities"
    assert "O1_NETCONF" in mesh["ran-nf-oam"].get("/capabilities").json()["supportedVendorModes"]

    ok = mesh["dme"].post("/actions", json={"requestedBy": "es-rapp", "changes": [
        {"managedElementRef": "gnb-du-vendor", "className": "NRCellDU", "managedFunctionRef": "NRCellDU=1",
         "attributeChanges": {"administrativeState": "LOCKED", "mockBoost": True}}]})
    assert ok.status_code == 202 and ok.json()["status"] == "COMPLETED", ok.text
    assert mesh["mock-o1-adaptor"].get("/edit-config/gnb-du-vendor").json()["attributeChanges"] == {
        "administrativeState": "LOCKED", "mockBoost": "True"}

    refused = mesh["dme"].post("/actions", json={"requestedBy": "es-rapp", "changes": [
        {"managedElementRef": "gnb-du-vendor", "className": "NRCellDU", "attributeChanges": {"warpDrive": 9}}]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED" and "warpDrive" in refused.json()["detail"]["detail"]
    statuses = [a["status"] for a in mesh["dme"].get("/actions").json()["items"]]
    assert sorted(statuses) == ["COMPLETED", "REJECTED"]


def test_restconf_me_is_written_and_read_back_over_rfc8040(mesh):
    """OI-1-cm-sync-restconf, end to end: a vendor whose adaptor declares
    O1_RESTCONF registers an ME for RESTCONF (adaptorUri = the RESTCONF
    root); a CM write through DME's action mediation reaches the mock
    adaptor as RFC 8040 requests on the data resource, and RAN NF OAM's
    read-after-write reads it back with a GET. A delete of an object never
    written is a definite RESTCONF error (data-missing), rejected without
    retries."""
    oam = mesh["ran-nf-oam"]
    registered = oam.post("/o1-adaptor-endpoints", json={
        "managedElementRef": "gnb-du-restconf", "adaptorUri": "http://mock-o1-adaptor:8000/restconf",
        "protocolSupport": ["RESTCONF"], "o1Protocol": "RESTCONF", "entityType": "O-DU",
        "vendorName": "mock-vendor"})
    assert registered.status_code == 201, registered.text
    oam.post(f"/o1-adaptor-endpoints/{registered.json()['endpointId']}/heartbeat")
    onboarded = oam.post("/vendor-onboarding", json={"vendorName": "mock-vendor", "discoverFrom": "gnb-du-restconf"})
    assert onboarded.status_code == 201, onboarded.text
    assert "O1_RESTCONF" in onboarded.json()["capability"]["supportedVendorModes"]

    action = mesh["dme"].post("/actions", json={"requestedBy": "es-rapp", "changes": [
        {"managedElementRef": "gnb-du-restconf", "className": "NRCellDU", "managedFunctionRef": "NRCellDU=7",
         "attributeChanges": {"administrativeState": "LOCKED"}}]})
    assert action.status_code == 202 and action.json()["status"] == "COMPLETED", action.text

    on_nf = mesh["mock-o1-adaptor"].get("/objects/gnb-du-restconf", params={"function_ref": "NRCellDU=7"}).json()
    assert on_nf["attributes"]["administrativeState"] == "LOCKED"
    read = oam.get("/managed-entities/gnb-du-restconf/config", params={"managed_function_ref": "NRCellDU=7"})
    assert read.status_code == 200 and read.json()["attributes"] == {"administrativeState": "LOCKED", "operationalState": "ENABLED"}

    job = oam.post("/config-jobs", json={"requestedBy": "test", "scope": "cell", "changes": [
        {"managedElementRef": "gnb-du-restconf", "managedFunctionRef": "NRCellDU=8", "operation": "delete"}]}).json()
    sub = oam.get(f"/config-jobs/{job['jobId']}").json()["subChanges"][0]
    assert (job["status"], sub["status"], sub["rejectionReason"], sub["attempts"]) == \
        ("FAILED", "REJECTED", "RESTCONF_REQUEST_FAILED", 1)


def test_sa_smos_rollback_returns_a_rapp_to_its_previous_version(mesh, loaded_apps, db_connection, monkeypatch):
    """OI-1-sa-rollback, end to end: a rApp instance is upgraded v1 -> v2
    (two real packages through Onboarding and NFO); an SA SMOS monitor
    registered on the v1 instance id — superseded by the upgrade — issues
    ROLLBACK, which rApp Management resolves through its version history to
    the v2 instance and turns into an upgrade back to v1. Committing it
    deploys v1 again through NFO (its descriptor was released when v1 was
    retired) and retires v2's deployment. A second ROLLBACK has nothing
    left to undo and is escalated."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    monkeypatch.setattr(
        loaded_apps["onboarding"], "_validate_package",
        lambda location: ("Definitions/main.yaml", [], f"hash-{location}", {}),
    )
    v1 = mesh["onboarding"].post("/packages", json={"location": "http://example/rollback-v1.csar"}).json()["packageId"]
    v2 = mesh["onboarding"].post("/packages", json={"location": "http://example/rollback-v2.csar"}).json()["packageId"]
    for package_id in (v1, v2):
        assert mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status").json()["state"] == "AVAILABLE"

    rapp = mesh["rapp-mgmt"]
    v1_instance = rapp.post("/instances", json={"packageId": v1, "config": {"threshold": 1}}).json()["instanceId"]
    assert rapp.post(f"/instances/{v1_instance}/bootstrap-complete").status_code == 200
    v1_workload = rapp.get(f"/instances/{v1_instance}").json()["workloadRef"]

    monitor = mesh["sa-smos"].post("/monitors", params={"target_rapp_instance_id": v1_instance}, json={"kpi": 10}).json()

    v2_instance = rapp.post(f"/instances/{v1_instance}/upgrade", json={"newPackageId": v2}).json()["newInstanceId"]
    rapp.put(f"/instances/{v2_instance}/config", json={"threshold": 9})
    # call flow 07b: the replacement reporting itself up commits the upgrade
    assert rapp.post(f"/instances/{v2_instance}/bootstrap-complete").json()["upgradeCommitted"] is True
    assert rapp.get(f"/instances/{v1_instance}").status_code == 404
    v2_workload = rapp.get(f"/instances/{v2_instance}").json()["workloadRef"]

    rollback = mesh["sa-smos"].post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "ROLLBACK"})

    assert rollback.status_code == 201 and rollback.json()["outcome"] == "RESOLVED"
    started = rollback.json()["result"]
    assert (started["instanceId"], started["fromPackageId"], started["toPackageId"]) == (v2_instance, v2, v1)
    v1_again = started["newInstanceId"]
    assert rapp.get(f"/instances/{v1_again}").json()["configuration"] == {"threshold": 1}
    assert rapp.post(f"/instances/{v1_again}/bootstrap-complete").json() == {"instanceId": v1_again, "state": "RUNNING", "upgradeCommitted": True}

    history = rapp.get(f"/instances/{v1_instance}/versions").json()
    assert (history["instanceId"], history["packageId"], history["rollbackTarget"]) == (v1_again, v1, None)
    assert [v["kind"] for v in history["versions"]] == ["ROLLBACK", "UPGRADE"]

    NFDeployment = loaded_apps["nfo"].NFDeployment
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        live = {str(d.nf_deployment_id) for d in session.scalars(select(NFDeployment))}
    v1_again_workload = rapp.get(f"/instances/{v1_again}").json()["workloadRef"]
    assert v1_again_workload in live and v1_workload not in live and v2_workload not in live

    again = mesh["sa-smos"].post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "ROLLBACK"})
    assert again.status_code == 201 and again.json()["outcome"] == "ESCALATED"
    assert again.json()["detail"].startswith("ROLLBACK_HISTORY_UNAVAILABLE")

    reconnect = mesh["sa-smos"].post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})
    assert reconnect.json()["outcome"] == "RESOLVED"  # heals the current (v1 again) instance's workload


def test_an_idempotency_key_makes_a_repeated_nfo_instantiate_one_deployment_even_through_focom(mesh, db_connection):
    """PR-ST-3, against the shared database and the real FOCOM call NFO makes: the repeat is answered
    from the stored first answer (so FOCOM is not asked again and no second deployment exists), and the
    key is scoped to the caller the gateway vouches for."""
    descriptor_id = mesh["nfo"].post("/descriptors", json={
        "packageId": str(uuid.uuid4()), "name": "Definitions/main.yaml",
    }).json()["nfDeploymentDescriptorId"]
    body = {"nfDeploymentDescriptorId": descriptor_id, "name": "idempotent-deployment", "requiredResourceTypeId": "gpu-l40"}

    first = mesh["nfo"].post("/deployments", json=body, headers={"Idempotency-Key": "it-1", "X-R1-Invoker-Id": "rapp-a"})
    again = mesh["nfo"].post("/deployments", json=body, headers={"Idempotency-Key": "it-1", "X-R1-Invoker-Id": "rapp-a"})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"

    second_descriptor = mesh["nfo"].post("/descriptors", json={
        "packageId": str(uuid.uuid4()), "name": "Definitions/main.yaml",
    }).json()["nfDeploymentDescriptorId"]   # NFO deploys a descriptor once, so rApp B brings its own
    other_caller = mesh["nfo"].post(
        "/deployments", json={**body, "nfDeploymentDescriptorId": second_descriptor, "name": "idempotent-deployment-b"},
        headers={"Idempotency-Key": "it-1", "X-R1-Invoker-Id": "rapp-b"})
    assert other_caller.status_code == 202 and "Idempotent-Replayed" not in other_caller.headers
    assert other_caller.json()["nfDeploymentId"] != first.json()["nfDeploymentId"]
