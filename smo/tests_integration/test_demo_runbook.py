"""Proves every request in DEMO_RUNBOOK.md's sequence actually succeeds
against this build's real routes — a regression guard for the runbook
itself, not just the sample package `test_cross_service.py`'s own
onboard/deploy test already covers. If a future change to any of
SME/DME/rApp Mgmt/NFO/FOCOM's request or response shapes breaks this
test, DEMO_RUNBOOK.md's copy-pasted commands would have silently gone
stale too — this is what would have caught it.

Run with: pytest smo/tests_integration -q
"""

import os
from pathlib import Path

# PR-SEC-2: the compose mTLS replay (`SMO_MTLS=on`, .github/workflows/smo-tests.yml) registers the in-stack notification destinations as https://, since a module
# that requires a client certificate has no plain HTTP port; everything else in this file is the same request.
INTERNAL = "https" if os.environ.get("SMO_MTLS", "off").strip().lower() in ("on", "1", "true", "yes", "require") else "http"


def test_full_runbook_sequence_succeeds(mesh, loaded_apps, monkeypatch, callbacks):
    """Every step of DEMO_RUNBOOK.md (onboard, deploy, bootstrap, operate, RAN NF OAM writes and alarms, FOCOM, Intent Service, SME, the AI
    platform lifecycle, RAN Analytics, SA SMOS, SO SMOS, DME subscriptions, topology export, the AI/ML workflow feature groups, event
    subscriptions, and retire) succeeds against the real routes in the in-process mesh, with the outbound notifications intercepted at the
    module's own `httpx.post`.
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
    csar_location = callbacks.csar_url("http://example/energy-saving-rapp.csar")  # live: served by the test container

    # DEMO_RUNBOOK.md step 2: onboard
    onboard = mesh["onboarding"].post("/packages", json={"location": csar_location})
    package_id = onboard.json()["packageId"]
    status = mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status")
    assert status.json()["state"] == "AVAILABLE"

    # A real validation failure: onboarding the exact same CSAR a second
    # time hits _validate_package's own real duplicate-content check
    # (matching integrity_hash) — a genuine FAILED outcome via the async
    # onboarding-status contract (still 202 synchronously), not a bug,
    # and it must not disturb the first, already-AVAILABLE package.
    duplicate_onboard = mesh["onboarding"].post("/packages", json={"location": csar_location})
    assert duplicate_onboard.status_code == 202
    duplicate_package_id = duplicate_onboard.json()["packageId"]
    duplicate_status = mesh["onboarding"].get(f"/packages/{duplicate_package_id}/onboarding-status")
    assert duplicate_status.json()["state"] == "FAILED"

    still_available = mesh["onboarding"].get(f"/packages/{package_id}/onboarding-status")
    assert still_available.json()["state"] == "AVAILABLE"

    # step 3: deploy
    create = mesh["rapp-mgmt"].post("/instances", json={"packageId": package_id, "config": {}})
    assert create.status_code == 202
    instance_id = create.json()["instanceId"]

    inv = mesh["focom"].get("/inventory")
    assert inv.status_code == 200

    # step 4: bootstrap — provider/invoker registration, token, service-api publish, DME producer
    boot = mesh["r1-termination"].get("/bootstrap")
    assert boot.status_code == 200
    assert {e["apiName"] for e in boot.json()["apiEndpoints"]} == {"service-apis", "published-apis"}

    prov = mesh["sme"].post("/provider-registrations", json={
        "apfId": "energy-saving-rapp", "providerDomainInfo": "Energy Saving rApp — demo provider domain",
    })
    assert prov.status_code == 201

    # HISTORY.md §7 SME item 1: apiInvokerId/onboardingSecret are now
    # server-generated, not client-supplied — the client submits its
    # own public key instead.
    inv_reg = mesh["sme"].post("/invoker-registrations", json={"apiInvokerPublicKey": "demo-rapp-public-key"})
    assert inv_reg.status_code == 201
    invoker = inv_reg.json()

    token = mesh["sme"].post("/oauth2/token", json={
        "grant_type": "client_credentials", "client_id": invoker["apiInvokerId"],
        "client_secret": invoker["onboardingSecret"],
    })
    assert token.status_code == 200
    assert token.json()["token_type"] == "Bearer"

    svc = mesh["sme"].post("/published-apis/v1/energy-saving-rapp/service-apis", json={
        "serviceName": "energy-saving-api", "producerId": "energy-saving-rapp",
        "endpoint": "http://energy-saving-rapp:8080/energy-saving/v1", "version": "v1",
        "fullApiVersions": ["v1"], "moduleScope": "energy-saving-rapp",
    })
    assert svc.status_code == 201
    energy_saving_service_id = svc.json()["serviceId"]

    dme_prod = mesh["dme"].post("/production-capabilities", json={
        "namespace": "demo", "name": "energy-saving-metrics", "version": "1.0",
        "typeName": "energy-saving-metrics-v1", "producerId": "energy-saving-rapp",
        "dataProductionSchema": {"type": "object", "properties": {"greeting": {"type": "string"}}},
        "producerHealthCallbackUrl": "http://energy-saving-rapp:8080/health",
        "jobCallbackUrl": "http://energy-saving-rapp:8080/dme-jobs",
    })
    assert dme_prod.status_code == 201

    # step 5: bootstrap-complete — DEPLOYING -> RUNNING. A package that
    # bundles Files/Sme/ declarations has them registered with SME here;
    # the Energy Saving package ships none, so nothing is registered.
    bc = mesh["rapp-mgmt"].post(f"/instances/{instance_id}/bootstrap-complete")
    assert bc.status_code == 200
    assert bc.json()["state"] == "RUNNING"

    get_inst = mesh["rapp-mgmt"].get(f"/instances/{instance_id}")
    assert get_inst.status_code == 200
    assert get_inst.json()["state"] == "RUNNING"
    assert not get_inst.json()["smeServiceIds"]

    # step 6: operate
    perf = mesh["rapp-mgmt"].post(f"/instances/{instance_id}/performance", json={"cellsAsleep": 2, "prbSavedPercent": 12.5})
    assert perf.status_code == 200

    # step 7: RAN NF OAM closed-loop — register a managed element, dispatch
    # a real CM write, raise/ack/clear a fault alarm
    reg = mesh["ran-nf-oam"].post("/o1-adaptor-endpoints", json={
        "managedElementRef": "demo-o-du-1", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config",
        "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU",
    })
    assert reg.status_code == 201
    assert reg.json()["healthStatus"] == "DISCOVERED"
    endpoint_id = reg.json()["endpointId"]

    hb = mesh["ran-nf-oam"].post(f"/o1-adaptor-endpoints/{endpoint_id}/heartbeat")
    assert hb.status_code == 200
    assert hb.json()["healthStatus"] == "ACTIVE"

    job = mesh["ran-nf-oam"].post("/config-jobs", json={
        "requestedBy": "energy-saving-rapp", "scope": "cell",
        "changes": [{"managedElementRef": "demo-o-du-1", "attributeChanges": {"adminState": "UNLOCKED"}}],
    })
    assert job.status_code == 202
    job_status = mesh["ran-nf-oam"].get(f"/config-jobs/{job.json()['jobId']}")
    assert job_status.json()["status"] == "COMPLETED"
    assert job_status.json()["subChanges"][0]["status"] == "APPLIED"

    applied = mesh["mock-o1-adaptor"].get("/edit-config/demo-o-du-1")
    assert applied.json()["attributeChanges"] == {"adminState": "UNLOCKED"}

    # A real partial failure: one healthy, registered ME alongside one
    # that was never registered settles the job as PARTIAL_SUCCESS, not
    # an all-or-nothing outcome — WriteConfigurationChanges' own real
    # per-ME dispatch gate (ENDPOINT_UNREACHABLE), not a scripted one.
    partial_job = mesh["ran-nf-oam"].post("/config-jobs", json={
        "requestedBy": "energy-saving-rapp", "scope": "cell",
        "changes": [
            {"managedElementRef": "demo-o-du-1", "attributeChanges": {"adminState": "LOCKED"}},
            {"managedElementRef": "demo-o-du-2-never-registered", "attributeChanges": {"adminState": "LOCKED"}},
        ],
    })
    assert partial_job.status_code == 202
    partial_status = mesh["ran-nf-oam"].get(f"/config-jobs/{partial_job.json()['jobId']}")
    assert partial_status.json()["status"] == "PARTIAL_SUCCESS"
    sub_changes_by_me = {sc["managedElementRef"]: sc for sc in partial_status.json()["subChanges"]}
    assert sub_changes_by_me["demo-o-du-1"]["status"] == "APPLIED"
    assert sub_changes_by_me["demo-o-du-2-never-registered"]["status"] == "REJECTED"
    assert sub_changes_by_me["demo-o-du-2-never-registered"]["rejectionReason"] == "ENDPOINT_UNREACHABLE"

    alarm = mesh["ran-nf-oam"].post("/alarms/ingest", params={
        "source_alarm_id": "demo-alarm-1", "managed_element_ref": "demo-o-du-1",
        "severity": "major", "alarm_type": "EQUIPMENT_ALARM",
    })
    assert alarm.status_code == 200
    alarm_id = alarm.json()["alarmId"]

    ack = mesh["ran-nf-oam"].patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "demo-operator"})
    assert ack.status_code == 200
    assert ack.json()["ackState"] == "ACKNOWLEDGED"

    cleared = mesh["ran-nf-oam"].patch(f"/alarms/{alarm_id}/clear", params={"clear_user_id": "demo-operator"})
    assert cleared.status_code == 200
    assert cleared.json()["severity"] == "cleared"
    assert cleared.json()["clearUserId"] == "demo-operator"

    # step 8: FOCOM resource management — subscribe to inventory changes,
    # provision a matching resource, observe the real outbound CREATE
    # notification, deprovision it, observe the real DELETE notification.
    # Intercepted at the same httpx.post call FOCOM's own code makes
    # (_notify_inventory_subscribers), same technique as fake_get above —
    # proves the real, unmodified notification code path fires, not a
    # reimplementation of it.
    notifications = callbacks.capture("focom", "http://demo-consumer:9000/inventory-events")

    sub = mesh["focom"].post("/inventory/subscriptions", json={
        "callback": "http://demo-consumer:9000/inventory-events",
        "resourceTypeId": "gpu-l40", "consumerSubscriptionId": "demo-sub-1",
    })
    assert sub.status_code == 201

    provisioned = mesh["focom"].post("/resources/provision", json={"resourceTypeId": "gpu-l40", "description": "demo GPU node"})
    assert provisioned.status_code == 200
    resource_id = provisioned.json()["resourceId"]

    pool_resources = mesh["focom"].get("/resource-pools/pool-0/resources")
    assert any(r["resourceId"] == resource_id for r in pool_resources.json()["items"])

    callbacks.await_deliveries(notifications, 1)
    assert len(notifications) == 1
    assert notifications[0]["notificationEventType"] == "CREATE"
    assert notifications[0]["resourceId"] == resource_id
    assert notifications[0]["resourceTypeId"] == "gpu-l40"
    assert notifications[0]["consumerSubscriptionId"] == "demo-sub-1"

    deprovisioned = mesh["focom"].delete(f"/resources/{resource_id}")
    assert deprovisioned.status_code == 200

    callbacks.await_deliveries(notifications, 2)
    assert len(notifications) == 2
    assert notifications[1]["notificationEventType"] == "DELETE"
    assert notifications[1]["resourceId"] == resource_id

    # FOCOM FCAPS — a distinct domain from RAN NF OAM's RAN-function
    # alarms: real infrastructure/O-Cloud alarm ingest + query, then the
    # performance query (empty: FOCOM collects nothing) and the runbook's
    # node-utilisation ingest and read (PR-GUI-9.8b).
    alarm = mesh["focom"].post("/alarms/ingest", params={"resource_ref": "phase1-degenerate-cluster", "severity": "critical"})
    assert alarm.status_code == 200
    alarms = mesh["focom"].get("/alarms")
    assert any(a["resourceRef"] == "phase1-degenerate-cluster" and a["severity"] == "critical" for a in alarms.json()["items"])

    performance = mesh["focom"].get("/performance")
    assert performance.status_code == 200
    assert performance.json()["items"] == []
    for name, value in (("CPU_UTILIZATION", 63.5), ("MEMORY_UTILIZATION", 41.0)):
        ingested = mesh["focom"].post("/performance/ingest", json={
            "resourceId": "phase1-degenerate-cluster", "performanceMeasurementDefinitionId": name, "measurementValue": value})
        assert ingested.status_code == 201
    utilisation = mesh["focom"].get("/resources/phase1-degenerate-cluster/utilisation").json()
    assert (utilisation["cpuPercent"], utilisation["memoryPercent"]) == (63.5, 41.0) and utilisation["at"]
    batch = mesh["focom"].get("/utilisation", params={"resource_ids": "phase1-degenerate-cluster,unreported-node"}).json()["items"]
    assert [i["cpuPercent"] for i in batch] == [63.5, None]

    # step 10: Intent Service automation — register an RMIH, create an
    # Intent addressed to it (Wave 3's consumer-side selection —
    # docs/ARCHITECTURE.md's Intent Service Wave 3 resolution),
    # observe the real dispatch notification, retract.
    # Intercepted at the same httpx.post call create_intent makes,
    # same technique as FOCOM's step above.
    # In-process the two dispatch destinations are intercepted; live they are
    # the real so-smos / sa-smos services, whose own receipt is not observable
    # from here, so the delivery assertions below run in-process only.
    intent_notifications = callbacks.capture("intent-service", f"{INTERNAL}://so-smos:8000/intents/notify")
    sa_smos_notifications = callbacks.capture("intent-service", f"{INTERNAL}://sa-smos:8000/intents/notify")

    rmih = mesh["intent-service"].post("/intent-handling-functions", json={
        "rmihId": "so-smos", "smeServiceId": "so-smos-svc",
        "intentHandlingCapabilityList": [{"intentHandlingCapabilityId": "ran-energy", "supportedExpectationObjectType": "RAN_SUBNETWORK", "supportedExpectationTargetInfoList": [{"supportedTargetName": "RANEnergyConsumption"}]}],
        "notificationDestination": f"{INTERNAL}://so-smos:8000/intents/notify",
        "intentHandlingScope": ["RAN"],
    })
    assert rmih.status_code == 201

    intent = mesh["intent-service"].post("/intents", json={
        "userLabel": "demo energy intent", "intentReportControl": [{"observationPeriod": 60}], "intentExpectations": [{"expectationId": "e1", "expectationVerb": "DELIVER", "expectationObject": {"objectType": "RAN_SUBNETWORK"}, "expectationTargets": [{"targetName": "RANEnergyConsumption", "targetCondition": "IS_LESS_THAN", "targetValueRange": 500}]}],
        "rmioId": "energy-saving-rapp", "rmihId": "so-smos", "intentHandlingScope": "RAN",
    })
    assert intent.status_code == 201
    intent_id = intent.json()["intentId"]

    if not callbacks.live:
        assert len(intent_notifications) == 1
        assert intent_notifications[0]["intentId"] == intent_id
        assert intent_notifications[0]["expectationObjectTypes"] == ["RAN_SUBNETWORK"]

    get_intent = mesh["intent-service"].get(f"/intents/{intent_id}")
    assert get_intent.status_code == 200
    assert get_intent.json()["intentAdminState"] == "ACTIVATED"
    assert get_intent.json()["rmihId"] == "so-smos"

    # A real negative case: a second RMIH with the same capability but a
    # different declared scope (CN-only) — addressing a RAN-scoped Intent
    # at it directly is now rejected at creation (RMIH_CAPABILITY_MISMATCH),
    # not silently un-notified — consumer-side selection means the caller's
    # own addressing choice is validated, not just filtered around.
    rmih2 = mesh["intent-service"].post("/intent-handling-functions", json={
        "rmihId": "sa-smos", "smeServiceId": "sa-smos-svc",
        "intentHandlingCapabilityList": [{"intentHandlingCapabilityId": "ran-energy", "supportedExpectationObjectType": "RAN_SUBNETWORK", "supportedExpectationTargetInfoList": [{"supportedTargetName": "RANEnergyConsumption"}]}],
        "notificationDestination": f"{INTERNAL}://sa-smos:8000/intents/notify",
        "intentHandlingScope": ["CN"],
    })
    assert rmih2.status_code == 201

    rejected = mesh["intent-service"].post("/intents", json={
        "userLabel": "demo energy intent", "intentReportControl": [{"observationPeriod": 60}], "intentExpectations": [{"expectationId": "e1", "expectationVerb": "DELIVER", "expectationObject": {"objectType": "RAN_SUBNETWORK"}, "expectationTargets": [{"targetName": "RANEnergyConsumption", "targetCondition": "IS_LESS_THAN", "targetValueRange": 500}]}],
        "rmioId": "energy-saving-rapp", "rmihId": "sa-smos", "intentHandlingScope": "RAN",
    })
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["title"] == "RMIH_CAPABILITY_MISMATCH"
    if not callbacks.live:
        assert sa_smos_notifications == []  # rejected before any dispatch was ever attempted

    intent2 = mesh["intent-service"].post("/intents", json={
        "userLabel": "demo energy intent", "intentReportControl": [{"observationPeriod": 60}], "intentExpectations": [{"expectationId": "e1", "expectationVerb": "DELIVER", "expectationObject": {"objectType": "RAN_SUBNETWORK"}, "expectationTargets": [{"targetName": "RANEnergyConsumption", "targetCondition": "IS_LESS_THAN", "targetValueRange": 500}]}],
        "rmioId": "energy-saving-rapp", "rmihId": "so-smos", "intentHandlingScope": "RAN",
    })
    assert intent2.status_code == 201
    intent2_id = intent2.json()["intentId"]

    if not callbacks.live:
        assert len(intent_notifications) == 2  # so-smos notified again, for this second intent
        assert intent_notifications[1]["intentId"] == intent2_id

    del_intent = mesh["intent-service"].delete(f"/intents/{intent_id}")
    assert del_intent.status_code == 204
    del_intent2 = mesh["intent-service"].delete(f"/intents/{intent2_id}")
    assert del_intent2.status_code == 204

    del_rmih = mesh["intent-service"].delete("/intent-handling-functions/so-smos")
    assert del_rmih.status_code == 204
    del_rmih2 = mesh["intent-service"].delete("/intent-handling-functions/sa-smos")
    assert del_rmih2.status_code == 204

    # step 11 was A1 Policy Management; it went with the A1 module (out of scope), and the later steps keep their numbers.

    # step 12: SME Trusted Invokers — register a real security context
    # for the invoker registered in step 4, confirm default redaction,
    # confirm real values on request, revoke, confirm removal.
    register_ti = mesh["sme"].put(f"/trusted-invokers/{invoker['apiInvokerId']}", json={
        "notificationDestination": "http://demo-consumer:9000/security-notify",
        "securityInfo": [{"aefId": "energy-saving-rapp", "apiId": "energy-saving-api", "authenticationInfo": "demo-auth-info",
                           "authorizationInfo": "demo-authz-info", "prefSecurityMethods": ["OAUTH"]}],
    })
    assert register_ti.status_code == 201
    assert register_ti.json()["securityInfo"][0]["selSecurityMethod"] == "OAUTH"

    redacted = mesh["sme"].get(f"/trusted-invokers/{invoker['apiInvokerId']}")
    assert redacted.status_code == 200
    assert redacted.json()["securityInfo"][0]["authenticationInfo"] == ""
    assert redacted.json()["securityInfo"][0]["authorizationInfo"] == ""

    revealed = mesh["sme"].get(f"/trusted-invokers/{invoker['apiInvokerId']}", params={"authentication_info": True, "authorization_info": True})
    assert revealed.json()["securityInfo"][0]["authenticationInfo"] == "demo-auth-info"
    assert revealed.json()["securityInfo"][0]["authorizationInfo"] == "demo-authz-info"

    revoke = mesh["sme"].post(f"/trusted-invokers/{invoker['apiInvokerId']}/delete", json={
        "aefId": "energy-saving-rapp", "apiIds": ["energy-saving-api"], "apiInvokerId": invoker["apiInvokerId"], "cause": "UNEXPECTED_REASON",
    })
    assert revoke.status_code == 204

    gone = mesh["sme"].get(f"/trusted-invokers/{invoker['apiInvokerId']}")
    assert gone.status_code == 404

    # step 13: AI Platform (Wave 2) — register a model (MLMR), request
    # training/validation/emulation and drive governance to PROMOTED
    # (AIMgF's own ModelLifecycle), deploy+activate its runtime (AIMgF +
    # NFO's own RuntimeLifecycle), upload/download a real artifact
    # (MLMR), deregister.
    model = mesh["mlmr"].post("/models", json={
        "modelType": "energy-saving-anomaly-detector", "version": "1.0.0",
        "description": "Demo anomaly-detection model for the Energy Saving rApp",
        "author": "energy-saving-rapp", "owner": "energy-saving-rapp",
        "inputDataType": "application/json", "outputDataType": "application/json",
    })
    assert model.status_code == 201
    model_id = model.json()["modelId"]

    training = mesh["aimgf"].post("/training-jobs", json={
        "modelId": model_id, "producerId": "energy-saving-rapp", "runId": "demo-run-1",
        "trainingDataset": "s3://demo/energy-saving-train", "validationDataset": "s3://demo/energy-saving-val",
    })
    assert training.status_code == 201
    training_job_id = training.json()["trainingJobId"]

    lifecycle = mesh["aimgf"].get(f"/models/{model_id}/lifecycle")
    assert lifecycle.json()["modelLifecycleState"] == "TRAINING"

    artifact_bytes = b"demo-model-weights-bytes"
    artifact = mesh["mlmr"].post(f"/models/{model_id}/artifact",
                                  files={"file": ("energy-saving-model.zip", artifact_bytes, "application/zip")})
    assert artifact.status_code == 201
    assert artifact.json()["artifactVersion"] == 1

    metrics = mesh["aimgf"].post(f"/training-jobs/{training_job_id}/model-metrics", json={"accuracy": 0.94, "f1Score": 0.91})
    assert metrics.status_code == 200
    assert metrics.json()["modelMetrics"] == {"accuracy": 0.94, "f1Score": 0.91}

    # Training completes through its own job route (advance refuses
    # job-driven events such as TRAINING_COMPLETE, OI-2-governance-bypass).
    refused = mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": "TRAINING_COMPLETE"})
    assert refused.status_code == 422
    completed = mesh["aimgf"].post(f"/training-jobs/{training_job_id}/complete",
                                   json={"succeeded": True, "metrics": {"accuracy": 0.94, "f1Score": 0.91}})
    assert completed.status_code == 200
    assert completed.json()["status"] == "FINISHED"
    assert mesh["aimgf"].get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == "TRAINED"

    # HISTORY.md OI-6.1 — an operator must approve training before validation can start.
    approve_training = mesh["aimgf"].post(f"/models/{model_id}/advance",
                                           params={"event": "APPROVE_TRAINING", "decided_by": "demo-operator", "rationale": "training approved for the demo"})
    assert approve_training.status_code == 200

    validation = mesh["aimgf"].post("/validation-jobs", json={"modelId": model_id, "trainingJobId": training_job_id, "producerId": "energy-saving-rapp"})
    assert validation.status_code == 201
    validation_complete = mesh["aimgf"].post(f"/validation-jobs/{validation.json()['validationJobId']}/complete", json={"succeeded": True, "metrics": {"accuracy": 0.95}})
    assert validation_complete.json()["status"] == "COMPLETED"

    # HISTORY.md OI-6.1 — same gate between validation and emulation.
    approve_validation = mesh["aimgf"].post(f"/models/{model_id}/advance",
                                             params={"event": "APPROVE_VALIDATION", "decided_by": "demo-operator", "rationale": "validation approved for the demo"})
    assert approve_validation.status_code == 200

    emulation = mesh["aimgf"].post("/emulation-jobs", json={"modelId": model_id, "producerId": "energy-saving-rapp"})
    assert emulation.status_code == 201
    emulation_complete = mesh["aimgf"].post(f"/emulation-jobs/{emulation.json()['emulationJobId']}/complete", json={"succeeded": True, "metrics": {"latencyMs": 8}})
    assert emulation_complete.json()["status"] == "COMPLETED"

    for event in ["SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY", "PROMOTE"]:
        governed = mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "demo-operator", "rationale": f"{event} for the demo"})
        assert governed.status_code == 200
    assert governed.json()["modelLifecycleState"] == "PROMOTED"

    history = mesh["aimgf"].get(f"/models/{model_id}/governance-history")
    assert [h["decision"] for h in history.json()["items"]] == [
        "APPROVE_TRAINING", "APPROVE_VALIDATION", "SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY", "PROMOTE",
    ]

    deployed = mesh["aimgf"].post(f"/models/{model_id}/runtime/deploy")
    assert deployed.status_code == 201
    assert deployed.json()["runtimeLifecycleState"] == "DEPLOYED"
    assert deployed.json()["nfDeploymentId"] is not None

    activated = mesh["aimgf"].post(f"/models/{model_id}/runtime/activate")
    assert activated.json()["runtimeLifecycleState"] == "ACTIVE"

    downloaded = mesh["mlmr"].get(f"/models/{model_id}/artifact/1")
    assert downloaded.status_code == 200
    assert downloaded.content == artifact_bytes

    terminated = mesh["aimgf"].post(f"/models/{model_id}/runtime/terminate")
    assert terminated.json()["runtimeLifecycleState"] == "TERMINATED"

    deregistered = mesh["mlmr"].delete(f"/models/{model_id}")
    assert deregistered.status_code == 204

    # step 14: RAN Analytics — register a producer (real cross-module SME
    # enrolment + service publish), subscribe with a real notification
    # destination, publish a report, observe the real notification fire
    # (same intercept technique as FOCOM/Intent Service above),
    # unsubscribe.
    analytics_notifications = callbacks.capture("mdaf", "http://demo-consumer:9000/analytics-reports")

    producer = mesh["ran-analytics"].post("/producers",
        params={"producer_id": "energy-saving-rapp", "analytics_type": "coverage-issue-analysis"},
        json={"dme_input_types": [], "output_schema": {"type": "object", "properties": {"issue": {"type": "string"}}}})
    assert producer.status_code == 201

    producers = mesh["ran-analytics"].get("/producers", params={"analytics_type": "coverage-issue-analysis"})
    assert any(p["producerId"] == "energy-saving-rapp" for p in producers.json()["items"])

    subscription = mesh["mdaf"].post("/subscriptions", params={
        "analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos",
    }, json={"notificationDestination": "http://demo-consumer:9000/analytics-reports"})
    assert subscription.status_code == 201
    subscription_id = subscription.json()["subscriptionId"]

    report = mesh["mdaf"].post("/reports", params={"analytics_type": "coverage-issue-analysis"},
        json={"output": {"issue": "demo-cell-1 coverage hole detected"}, "input_sources": []})
    assert report.status_code == 201
    report_id = report.json()["reportId"]

    callbacks.await_deliveries(analytics_notifications, 1)
    assert len(analytics_notifications) == 1
    assert analytics_notifications[0]["reportId"] == report_id
    assert analytics_notifications[0]["output"] == {"issue": "demo-cell-1 coverage hole detected"}

    reports = mesh["mdaf"].get("/reports", params={"analytics_type": "coverage-issue-analysis"})
    assert any(r["reportId"] == report_id for r in reports.json()["items"])

    unsubscribed = mesh["mdaf"].delete(f"/subscriptions/{subscription_id}")
    assert unsubscribed.status_code == 204

    # step 15: SA SMOS — a real assurance monitor, a genuine RECONNECT
    # heal (resolving a concrete nfDeploymentId via a live SO SMOS order
    # lookup), and a ROLLBACK refusal for an order-scoped monitor. RECONNECT needs a real,
    # RUNNING NFDeployment distinct from the sample rApp's own deployment
    # above (NFO's real duplication guard means a descriptor can only be
    # deployed once), so this creates a second descriptor against the
    # same already-onboarded package, then deploys it via a real SO SMOS
    # order (SO SMOS's own dispatch table, exercised further by step 16
    # below).
    descriptor2 = mesh["nfo"].post("/descriptors", json={"packageId": package_id, "name": "sa-smos-demo-descriptor"})
    assert descriptor2.status_code == 201
    descriptor2_id = descriptor2.json()["nfDeploymentDescriptorId"]

    deploy_order = mesh["so-smos"].post("/orders", json={
        "scope": "sa-smos-demo-deploy",
        "steps": [
            {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": descriptor2_id, "name": "sa-smos-demo-deployment"},
        ],
    })
    assert deploy_order.status_code == 202
    deploy_steps = deploy_order.json()["steps"]
    assert deploy_steps[0]["status"] == "COMPLETED"
    assert deploy_steps[0]["result"]["state"] == "RUNNING"
    sa_smos_order_id = deploy_order.json()["orderId"]
    nf_deployment_id = deploy_steps[0]["result"]["nfDeploymentId"]

    monitor = mesh["sa-smos"].post("/monitors", params={"target_order_id": sa_smos_order_id}, json={"latency": 100})
    assert monitor.status_code == 201
    monitor_id = monitor.json()["monitorId"]

    evaluated = mesh["sa-smos"].post(f"/monitors/{monitor_id}/evaluate", json={"latency": 80})
    assert evaluated.json()["breaches"] == {"latency": 100}

    reconnect = mesh["sa-smos"].post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "RECONNECT"})
    assert reconnect.status_code == 201
    assert reconnect.json()["outcome"] == "RESOLVED"

    # An order-scoped monitor watches a bare NF deployment, which keeps no
    # version history; ROLLBACK needs a rApp-instance-scoped monitor
    # (test_cross_service.py's SA SMOS rollback test, OI-1-sa-rollback).
    rollback = mesh["sa-smos"].post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "ROLLBACK"})
    assert rollback.status_code == 409
    assert rollback.json()["detail"]["title"] == "ROLLBACK_HISTORY_UNAVAILABLE"

    terminate_second = mesh["nfo"].delete(f"/deployments/{nf_deployment_id}")
    assert terminate_second.status_code == 204

    # step 16: SO SMOS — a real multi-step order dispatched over the real
    # R1 client to two different downstream modules (FOCOM, NFO),
    # proving the real fail-fast halt (a genuine downstream rejection
    # halts the order; the never-attempted step stays PENDING), then
    # cancel to turn the PENDING step CANCELLED.
    order = mesh["so-smos"].post("/orders", json={
        "scope": "demo-multi-step-order",
        "steps": [
            {"stepType": "INFRA", "targetModule": "FOCOM", "spec": {"resourceTypeId": "gpu-l40", "description": "SO SMOS provisioned node"}},
            {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": "00000000-0000-0000-0000-000000000000", "name": "no-such-descriptor"},
            {"stepType": "TRAINING", "targetModule": "AI_ML_WORKFLOW", "producerId": "energy-saving-rapp"},
        ],
    })
    assert order.status_code == 202
    order_id = order.json()["orderId"]
    steps = order.json()["steps"]
    assert steps[0]["status"] == "COMPLETED"
    assert steps[1]["status"] == "FAILED"
    assert steps[2]["status"] == "PENDING"

    order_status = mesh["so-smos"].get(f"/orders/{order_id}")
    assert order_status.json()["steps"][2]["status"] == "PENDING"

    cancelled = mesh["so-smos"].post(f"/orders/{order_id}/cancel")
    assert cancelled.status_code == 200
    cancelled_steps = cancelled.json()["steps"]
    assert cancelled_steps[0]["status"] == "COMPLETED"
    assert cancelled_steps[1]["status"] == "FAILED"
    assert cancelled_steps[2]["status"] == "CANCELLED"

    # step 17: DME type subscriptions — a real consumer notified when any
    # DmeType is registered or removed, closed in an earlier §5 pass but
    # never demonstrated. Intercepted the same way as FOCOM's/RAN Analytics' own notification steps above.
    dme_type_notifications = callbacks.capture("dme", "http://demo-consumer:9000/dme-type-events")

    dme_sub = mesh["dme"].post("/type-subscriptions", json={
        "notificationDestination": "http://demo-consumer:9000/dme-type-events", "owner": "energy-saving-rapp",
    })
    assert dme_sub.status_code == 201
    dme_subscription_id = dme_sub.json()["subscriptionId"]

    new_type = mesh["dme"].post("/production-capabilities", json={
        "namespace": "demo", "name": "dme-type-sub-demo", "version": "1.0",
        "typeName": "dme-type-sub-demo-v1", "producerId": "energy-saving-rapp",
        "dataProductionSchema": {"type": "object", "properties": {"reading": {"type": "number"}}},
        "producerHealthCallbackUrl": "http://energy-saving-rapp:8080/health",
        "jobCallbackUrl": "http://energy-saving-rapp:8080/dme-jobs",
    })
    assert new_type.status_code == 201
    new_type_id = new_type.json()["registrationId"]
    callbacks.await_deliveries(dme_type_notifications, 1)
    assert len(dme_type_notifications) == 1
    assert dme_type_notifications[0]["infoTypeId"] == new_type_id
    assert dme_type_notifications[0]["status"] == "REGISTERED"

    deregistered = mesh["dme"].delete("/production-capabilities", params={"producer_id": "energy-saving-rapp"})
    assert deregistered.status_code == 204
    # HISTORY.md §7's Producer/Type conflation finding, closed: Producer
    # and Type are two real, separately-owned entities now (ICS's own
    # deleteInfoProducer never touches info-types at all) — no
    # notification fires here; both energy-saving-rapp's DmeTypes (step 4's
    # energy-saving-metrics and the new demo one above) stay registered,
    # just DISABLED (no producer left).
    assert len(dme_type_notifications) == 1
    remaining = {t["typeName"]: t for t in mesh["dme"].get("/dme-types").json()}
    assert remaining["dme-type-sub-demo-v1"]["producerIds"] == []
    assert remaining["dme-type-sub-demo-v1"]["typeStatus"] == "DISABLED"

    # Only delete_dme_type (ICS's own DELETE /info-types/{id}) actually
    # removes a type, and only once every producer has left it — this is
    # what fires the real DEREGISTERED notification.
    deleted = mesh["dme"].delete(f"/dme-types/{new_type_id}")
    assert deleted.status_code == 204
    callbacks.await_deliveries(dme_type_notifications, 2)
    assert len(dme_type_notifications) == 2
    assert dme_type_notifications[1]["infoTypeId"] == new_type_id
    assert dme_type_notifications[1]["status"] == "DEREGISTERED"

    dme_unsub = mesh["dme"].delete(f"/type-subscriptions/{dme_subscription_id}")
    assert dme_unsub.status_code == 204

    # step 18: FOCOM topology export — the real ResourceType/ResourcePool/
    # DeploymentManager/Resource rows (including the gpu-l40 ResourceType
    # auto-registered by SO SMOS's own INFRA step above, and its
    # never-deprovisioned Resource) exported in the reference's own
    # TEIV wire shape, closed in an earlier §5 pass but never demonstrated.
    topology = mesh["focom"].get("/topology")
    assert topology.status_code == 200
    topology_json = topology.json()
    entity_keys = {key for entity in topology_json["entities"] for key in entity}
    assert entity_keys == {
        "o-ran-smo-teiv-cloud:ResourceType", "o-ran-smo-teiv-cloud:ResourcePool",
        "o-ran-smo-teiv-cloud:DeploymentManager", "o-ran-smo-teiv-cloud:Resource",
    }
    resource_type_ids = {
        rt["id"] for entity in topology_json["entities"] if "o-ran-smo-teiv-cloud:ResourceType" in entity
        for rt in entity["o-ran-smo-teiv-cloud:ResourceType"]
    }
    assert any(rt_id.endswith(":gpu-l40") for rt_id in resource_type_ids)
    relationship_keys = {key for rel in topology_json["relationships"] for key in rel}
    assert "o-ran-smo-teiv-cloud:RESOURCE_IS_OF_TYPE_RESOURCETYPE" in relationship_keys
    assert "o-ran-smo-teiv-cloud:RESOURCE_CONTAINED_IN_RESOURCEPOOL" in relationship_keys

    # step 19: AI/ML Workflow feature groups — a whole entity added in an
    # earlier §5 pass but never touched by any demo phase. Real
    # registration + listing, then the reference's own real duplicate-name
    # rejection (a genuine UniqueConstraint) and invalid-name rejection
    # (the reference's own \w+, 3-63 character rule).
    feature_group_body = {
        "featureGroupName": "demo_coverage_features", "featureList": "rsrp,rsrq,sinr",
        "datalakeSource": "INFLUX", "host": "influx.demo", "port": "8086", "bucket": "demo-bucket",
        "tokenRef": "demo-lake-token", "dbOrg": "demo-org", "measurement": "coverage_metrics",
    }
    created_group = mesh["aimgf"].post("/feature-groups", json=feature_group_body)
    assert created_group.status_code == 201
    feature_group_id = created_group.json()["featureGroupId"]

    listed_groups = mesh["aimgf"].get("/feature-groups")
    assert listed_groups.status_code == 200
    assert any(g["featureGroupId"] == feature_group_id for g in listed_groups.json()["items"])

    duplicate_group = mesh["aimgf"].post("/feature-groups", json=feature_group_body)
    assert duplicate_group.status_code == 409
    assert duplicate_group.json()["detail"]["title"] == "FEATURE_GROUP_ALREADY_REGISTERED"

    invalid_group = mesh["aimgf"].post("/feature-groups", json={**feature_group_body, "featureGroupName": "no spaces allowed"})
    assert invalid_group.status_code == 400
    assert invalid_group.json()["detail"]["title"] == "FEATURE_GROUP_NAME_INVALID"

    # step 20: SA SMOS coordination-group remedial action — a
    # coordination-group-scoped AssuranceMonitor always dispatches a real
    # group retrain via AI/ML Workflow's RequestTraining, regardless of
    # actionType (already real and unit-tested, but never demonstrated —
    # step 15's own monitor was targetOrderId-scoped throughout).
    group_model_ids = []
    for i in range(2):
        group_model = mesh["mlmr"].post("/models", json={
            "modelType": f"demo-coordination-group-model-{i}", "version": "1.0.0",
            "author": "energy-saving-rapp", "owner": "energy-saving-rapp",
        })
        assert group_model.status_code == 201
        group_model_ids.append(group_model.json()["modelId"])

    # A single-member group 422s with COORDINATION_GROUP_TOO_SMALL — the
    # migration's own member_model_ids CHECK constraint (array_length >= 2)
    # enforces this at the DB layer, but with no pre-validation this used to
    # surface as an unhandled IntegrityError (bare 500) instead; SQLite's
    # test schema (built from the ORM models, which never mirrored the
    # constraint) never caught it, only a real-Postgres run did.
    too_small = mesh["mlmr"].post("/coordination-groups", json={"memberModelIds": [group_model_ids[0]]})
    assert too_small.status_code == 422
    assert too_small.json()["detail"]["title"] == "COORDINATION_GROUP_TOO_SMALL"

    coordination_group = mesh["mlmr"].post("/coordination-groups", json={"memberModelIds": group_model_ids})
    assert coordination_group.status_code == 201
    group_id = coordination_group.json()["groupId"]

    group_monitor = mesh["sa-smos"].post("/monitors", params={"target_coordination_group_id": group_id}, json={})
    assert group_monitor.status_code == 201
    group_monitor_id = group_monitor.json()["monitorId"]

    group_remedial = mesh["sa-smos"].post(f"/monitors/{group_monitor_id}/remedial-actions", params={"action_type": "SCALE"})
    assert group_remedial.status_code == 201
    assert group_remedial.json()["outcome"] == "RESOLVED"

    running_jobs = mesh["aimgf"].get("/training-jobs", params={"status": "IN_PROGRESS"})
    assert running_jobs.status_code == 200
    matching_jobs = [j for j in running_jobs.json()["items"] if j["modelCoordinationGroupId"] == group_id]
    assert len(matching_jobs) == 1
    assert matching_jobs[0]["producerId"] == "sa-smos"

    # step 21: SME event-subscription apiId filtering — SubscribeEvents'
    # own apiIds filter (real and unit-tested since an earlier pass) has
    # never appeared anywhere in this runbook. A subscriber scoped to
    # energy-saving-api's own serviceId must not be notified about an
    # unrelated service's events, but must be notified about
    # energy-saving-api's own. Intercepted the same way as DME's own
    # type-subscription step above.
    sme_notifications = callbacks.capture("sme", "http://demo-consumer:9000/sme-events-", with_location=True)

    unscoped_sub = mesh["sme"].post("/capif-events/v1/consumer-unscoped/subscriptions", json={
        "subscriberId": "consumer-unscoped", "eventTypes": ["SERVICE_API_UPDATE"],
        "callbackUri": "http://demo-consumer:9000/sme-events-unscoped",
    })
    assert unscoped_sub.status_code == 201
    unscoped_sub_id = unscoped_sub.json()["subscriptionId"]

    scoped_sub = mesh["sme"].post("/capif-events/v1/consumer-scoped/subscriptions", json={
        "subscriberId": "consumer-scoped", "eventTypes": ["SERVICE_API_UPDATE"],
        "callbackUri": "http://demo-consumer:9000/sme-events-scoped", "apiIds": [energy_saving_service_id],
    })
    assert scoped_sub.status_code == 201
    scoped_sub_id = scoped_sub.json()["subscriptionId"]

    other_svc = mesh["sme"].post("/published-apis/v1/energy-saving-rapp/service-apis", json={
        "serviceName": "other-api", "producerId": "energy-saving-rapp",
        "endpoint": "http://energy-saving-rapp:8080/other/v1", "version": "1.0", "moduleScope": "energy-saving-rapp",
    })
    assert other_svc.status_code == 201
    other_svc_update = mesh["sme"].post("/published-apis/v1/energy-saving-rapp/service-apis", json={
        "serviceName": "other-api", "producerId": "energy-saving-rapp",
        "endpoint": "http://energy-saving-rapp:8080/other/v1", "version": "2.0", "moduleScope": "energy-saving-rapp",
    })
    assert other_svc_update.status_code == 201
    # only consumer-unscoped's callback — consumer-scoped's own apiIds
    # filter (scoped to energy-saving-api, not other-api) excludes it.
    callbacks.await_deliveries(sme_notifications, 1)
    assert [loc for loc, _ in sme_notifications] == ["http://demo-consumer:9000/sme-events-unscoped"]

    energy_saving_update = mesh["sme"].post("/published-apis/v1/energy-saving-rapp/service-apis", json={
        "serviceName": "energy-saving-api", "producerId": "energy-saving-rapp",
        "endpoint": "http://energy-saving-rapp:8080/energy-saving/v1", "version": "v2",
        "fullApiVersions": ["v1"], "moduleScope": "energy-saving-rapp",
    })
    assert energy_saving_update.status_code == 201
    # both — this update matches consumer-scoped's own apiIds filter too.
    callbacks.await_deliveries(sme_notifications, 3)
    assert sorted(loc for loc, _ in sme_notifications[1:]) == sorted([
        "http://demo-consumer:9000/sme-events-scoped", "http://demo-consumer:9000/sme-events-unscoped",
    ])
    assert all(n[1]["serviceId"] == energy_saving_service_id for n in sme_notifications[1:])

    assert mesh["sme"].delete(f"/capif-events/v1/consumer-unscoped/subscriptions/{unscoped_sub_id}").status_code == 204
    assert mesh["sme"].delete(f"/capif-events/v1/consumer-scoped/subscriptions/{scoped_sub_id}").status_code == 204

    # step 22 was A1 Related's service supervision sweep; it went with the A1 module (out of scope), and the later steps keep their numbers.

    # step 23: retire — the real package priming lifecycle (COMMISSIONED-
    # equivalent AVAILABLE -> PRIMING -> PRIMED), a genuine deprime
    # refusal while the sample rApp's own instance is still deployed
    # (the reference's own deprimeRapp guard, a real query against
    # PackageUsageRegistration), terminate (which itself calls
    # Onboarding's real usage/stop), then deprime succeeding for real,
    # then terminate/delete.
    primed = mesh["onboarding"].post(f"/packages/{package_id}/prime")
    assert primed.status_code == 200
    assert primed.json()["state"] == "PRIMED"

    blocked_deprime = mesh["onboarding"].post(f"/packages/{package_id}/deprime")
    assert blocked_deprime.status_code == 409
    assert blocked_deprime.json()["detail"]["title"] == "SERVICE_NAME_CONFLICT"

    term = mesh["rapp-mgmt"].post(f"/instances/{instance_id}/terminate")
    assert term.status_code == 200
    assert term.json()["state"] == "UNDEPLOYED"

    deprimed = mesh["onboarding"].post(f"/packages/{package_id}/deprime")
    assert deprimed.status_code == 200
    assert deprimed.json()["state"] == "AVAILABLE"

    delete = mesh["rapp-mgmt"].delete(f"/instances/{instance_id}")
    assert delete.status_code == 204


def test_energy_saving_demo_01_to_11_runs_end_to_end(mesh, loaded_apps, monkeypatch, capsys, callbacks):
    """DEMO_RUNBOOK.md §24 — the Wave 10.1 EnergySaving rApp demo (Demo
    00–11). Not a mirror of the runbook: it runs the runbook's own script,
    samples/energy-saving-rapp/demo.py, step by step through the mesh."""
    import importlib.util

    from energy_saving_env import CSAR_URL, serve_csar

    spec = importlib.util.spec_from_file_location("energy_saving_demo", Path(__file__).resolve().parent.parent
                                                  / "samples" / "energy-saving-rapp" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "CSAR_URL", callbacks.csar_url(CSAR_URL))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")

    state = {}
    for step in demo.STEPS:
        demo.run(step, state)
    out = capsys.readouterr().out
    assert "Demo 11" in out
    assert state["historyRecords"] == 72 * len(demo.CELLS)                       # Demo 02
    assert state["emulation"]["midnightRecommendation"] == "LOCKED"               # Demo 05
    assert state["promoted"] == "PROMOTED" and state["runtime"] == "ACTIVE"       # Demo 06/07
    assert state["decision"]["decision"] == "LOCK"                                # Demo 08
    assert state["action"]["status"] == "COMPLETED"                               # Demo 09
    assert state["o1"]["administrativeState"] == "LOCKED"                         # Demo 10
    cells = {c["cellId"]: c for c in state["dashboard"]["cells"]}                 # Demo 11
    assert cells["101"]["state"] == "SLEEP" and cells["101"]["prbTrend"]
    assert cells["103"]["latestDecision"]["reason"] == "SAFETY_BLOCKED:EMERGENCY_CELL"


def test_mobility_optimization_demo_00_to_11_runs_end_to_end(mesh, loaded_apps, monkeypatch, capsys, callbacks):
    """DEMO_RUNBOOK.md §25 — the Wave 10.2 Mobility Optimization rApp demo
    (Demo 00–11). It runs the runbook's own script,
    samples/mobility-optimization-rapp/demo.py, step by step through the mesh."""
    import importlib.util

    from mobility_env import CSAR_URL, serve_csar

    spec = importlib.util.spec_from_file_location("mobility_demo", Path(__file__).resolve().parent.parent
                                                  / "samples" / "mobility-optimization-rapp" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "CSAR_URL", callbacks.csar_url(CSAR_URL))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")

    state = {}
    for step in demo.STEPS:
        demo.run(step, state)
    out = capsys.readouterr().out
    assert "Demo 11" in out
    assert state["historyRecords"] == 72 * len(demo.RELATIONS)                    # Demo 02
    assert state["emulation"]["directionAccuracy"] >= 0.9                          # Demo 05
    assert state["promoted"] == "PROMOTED" and state["runtime"] == "ACTIVE"       # Demo 06/07
    assert state["dmro"] == "VERIFIED"                                              # Demo 07
    assert (state["decision"]["decision"], state["decision"]["toCio"]) == ("RAISE_CIO", 2)   # Demo 08
    assert (state["early"]["decision"], state["early"]["toCio"]) == ("LOWER_CIO", -2)
    assert state["action"]["status"] == "COMPLETED"                               # Demo 09
    assert state["o1"]["cellIndividualOffset"] == "[2, 2, 2, 2, 2, 2]"
    assert state["kpi"]["outcome"] == "CONFIRMED"                                  # Demo 10
    rels = {r["relation"]: r for r in state["dashboard"]["relations"]}            # Demo 11
    assert rels["201-203"]["state"] == "STEADY" and rels["201-203"]["cio"] == 2 and rels["201-203"]["rateTrend"]
    assert rels["203-204"]["latestDecision"]["reason"].startswith("SAFETY_BLOCKED")


def test_coverage_optimization_demo_00_to_11_runs_end_to_end(mesh, loaded_apps, monkeypatch, capsys, callbacks):
    """DEMO_RUNBOOK.md §26 — the Wave 10.3 Coverage Optimization rApp demo
    (Demo 00–11). It runs the runbook's own script,
    samples/coverage-optimization-rapp/demo.py, step by step through the mesh."""
    import importlib.util

    from coverage_env import CSAR_URL, serve_csar

    spec = importlib.util.spec_from_file_location("coverage_demo", Path(__file__).resolve().parent.parent
                                                  / "samples" / "coverage-optimization-rapp" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "CSAR_URL", callbacks.csar_url(CSAR_URL))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")

    state = {}
    for step in demo.STEPS:
        demo.run(step, state)
    out = capsys.readouterr().out
    assert "Demo 11" in out
    assert state["historyRecords"] == 72 * len(demo.CELLS)                         # Demo 02
    assert state["emulation"]["moveAccuracy"] >= 0.9                                # Demo 05
    assert state["promoted"] == "PROMOTED" and state["runtime"] == "ACTIVE"       # Demo 06/07
    assert state["plan"]["moves"]["301"] == "DOWNTILT"                              # Demo 08
    assert state["decision"]["toSetting"]["digitalTilt"] == 70
    assert state["action"]["status"] == "COMPLETED" and state["o1"]["digitalTilt"] == "70"   # Demo 09
    assert state["kpi"]["verdict"] == "IMPROVED_OR_EQUAL"                           # Demo 10
    assert state["kpi"]["postObjective"] < state["kpi"]["preObjective"]
    cells = {c["cellId"]: c for c in state["dashboard"]["cells"]}                 # Demo 11
    assert cells["301"]["digitalTilt"] == 70 and cells["301"]["state"] == "STEADY" and cells["301"]["shareTrend"]


def test_traffic_steering_demo_00_to_11_runs_end_to_end(mesh, loaded_apps, monkeypatch, capsys, callbacks):
    """DEMO_RUNBOOK.md §27 — the Wave 10.4 Traffic Steering rApp demo (Demo
    00–11). It runs the runbook's own script,
    samples/traffic-steering-rapp/demo.py, step by step through the mesh."""
    import importlib.util

    from traffic_env import CSAR_URL, serve_csar

    spec = importlib.util.spec_from_file_location("traffic_demo", Path(__file__).resolve().parent.parent
                                                  / "samples" / "traffic-steering-rapp" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "CSAR_URL", callbacks.csar_url(CSAR_URL))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")

    state = {}
    for step in demo.STEPS:
        demo.run(step, state)
    out = capsys.readouterr().out
    assert "Demo 11" in out
    assert state["historyRecords"] == 72 * 4                                       # Demo 02
    assert state["emulation"]["steeringAccuracy"] >= 0.9                           # Demo 05
    assert state["promoted"] == "PROMOTED" and state["runtime"] == "ACTIVE"       # Demo 06/07
    d = state["decision"]                                                          # Demo 08
    assert d["decision"].startswith("STEER_") and d["prediction"]["plan"]["targetForecastAfter"] <= 55
    assert state["action"]["status"] == "COMPLETED"                               # Demo 09
    assert state["kpi"]["verdict"] == "IMPROVED_OR_EQUAL"                          # Demo 10
    cells = {c["cellId"]: c for c in state["dashboard"]["cells"]}                 # Demo 11
    assert cells["401"]["state"] == "STEADY" and cells["401"]["scoreTrend"]
    assert cells["401"]["steering"]["cio"] or cells["401"]["steering"]["prio"]
