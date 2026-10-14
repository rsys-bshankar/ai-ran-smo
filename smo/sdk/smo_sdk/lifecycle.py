"""sdk.lifecycle: a thin client over AIMgF (`aimgf/`) and MLLF (`mllf/`): training, validation and emulation jobs, certification, inference, MLMF monitoring and deployment.

One namespace for both services because an rApp author thinks of "advance a model through its lifecycle to deployment" as one journey, although AIMgF and MLLF are separate services
(`docs/ARCHITECTURE.md`). The query-versus-body shape of each method was checked against the routes' OpenAPI schemas; where a route takes one body parameter the body is that value itself,
unwrapped, and where it takes two FastAPI embeds them under their names. Part of the SDK (`sdk/README.md`); the `start_*`, `complete_*` and runtime methods are the Wave 10 execution-mode
lifecycle (`HISTORY.md` W10-03, D-4).
"""

import uuid
import warnings

from ._common import BaseClient, SdkError, ensure_ok


class LifecycleClient(BaseClient):
    # ---------------------------------------------------------------- AIMgF: training

    """The `sdk.lifecycle` namespace: AIMgF training, validation, emulation and inference jobs, the model lifecycle, MLMF performance monitoring and feature groups; MLLF deployment."""
    def request_training(self, producer_id: str, model_id: uuid.UUID | str | None = None,
                          model_coordination_group_id: uuid.UUID | str | None = None, required_data: dict | None = None,
                          validation_criteria: dict | None = None, notification_uri: str | None = None,
                          run_id: str | None = None, training_dataset: str | None = None,
                          validation_dataset: str | None = None, consumer_rapp_id: str | None = None,
                          producer_rapp_id: str | None = None) -> dict:
        """Request a training job (`POST /aimgf/training-jobs`) for a model or a model coordination group, with the datasets and validation criteria; ids are sent as strings and an unset one as null."""
        return ensure_ok(self._r1.post("/aimgf/training-jobs", json={
            "modelId": str(model_id) if model_id else None,
            "modelCoordinationGroupId": str(model_coordination_group_id) if model_coordination_group_id else None,
            "producerId": producer_id, "requiredData": required_data or {}, "validationCriteria": validation_criteria or {},
            "notificationUri": notification_uri, "runId": run_id, "trainingDataset": training_dataset,
            "validationDataset": validation_dataset, "consumerRappId": consumer_rapp_id, "producerRappId": producer_rapp_id,
        }))

    def get_training_job_status(self, training_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/training-jobs/{training_job_id}/status"))

    def cancel_training(self, training_job_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/aimgf/training-jobs/{training_job_id}"))

    def suspend_training(self, training_job_id: uuid.UUID | str) -> dict:
        """Suspend a training job (HISTORY.md section 7, AI/ML Workflow item 6); legal only against an IN_PROGRESS job, AIMgF answers 409 otherwise."""
        return ensure_ok(self._r1.post(f"/aimgf/training-jobs/{training_job_id}/suspend"))

    def resume_training(self, training_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.post(f"/aimgf/training-jobs/{training_job_id}/resume"))

    def update_training_job_model_metrics(self, training_job_id: uuid.UUID | str, model_metrics: dict) -> dict:
        # model_metrics is the route's only body-eligible parameter — the
        # JSON body is the metrics dict itself, unwrapped.
        return ensure_ok(self._r1.post(f"/aimgf/training-jobs/{training_job_id}/model-metrics", json=model_metrics))

    def get_training_job_model_metrics(self, training_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/training-jobs/{training_job_id}/model-metrics"))

    def list_training_jobs(self, model_id: uuid.UUID | str | None = None, status: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/training-jobs", params={"model_id": model_id, "status": status}))

    # ---------------------------------------------------------------- AIMgF: lifecycle FSM + inference

    def advance_model_lifecycle(self, model_id: uuid.UUID | str, event: str,
                                 decided_by: str | None = None, rationale: str | None = None) -> dict:
        """Deprecated, and no longer sends anything: always raises `SdkError` (403, `ROLE_NOT_PERMITTED`) before a call is made.

        `POST /aimgf/models/{id}/advance` carries the governance decisions (approve, certify, promote, ...) and the end-of-life events (`aimgf/app/statemachine.py`,
        `ADVANCEABLE_EVENTS`). Those are decisions of the operator, not of a rApp: since SEC-15.1 AIMgF refuses a caller with the rApp role and since SEC-15.1b the gateway
        refuses the call too (it is off `roles.RAPP_MAY_CHANGE`), so this method could only ever fail, after a round trip. It is kept, not deleted, so that a rApp that
        calls it gets a clear error instead of an `AttributeError` (`docs/RELEASES.md`, deprecation policy; removal waits for a release that may break the SDK's API).
        What a rApp may still do for a model's lifecycle: `request_training`, `complete_training`, `start_validation`, `deploy_runtime`, `activate_runtime`, inference.
        The arguments are ignored.
        """
        raise SdkError(403, {"title": "ROLE_NOT_PERMITTED", "status": 403,
                             "detail": "advance_model_lifecycle is deprecated and always refused: the governance and end-of-life events of a model are decisions "
                                       "of the operator (the console), not of a rApp. See sdk/README.md."})

    def request_inference(self, model_id: uuid.UUID | str, notification_destination: str | None = None) -> dict:
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/inference-jobs",
                                        params={"notification_destination": notification_destination}))

    def get_inference_job_status(self, inference_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/inference-jobs/{inference_job_id}/status"))

    def resolve_inference(self, inference_job_id: uuid.UUID | str, succeeded: bool,
                          inference_outputs: list[dict] | None = None, potential_impact_info: str | None = None) -> dict:
        """Resolve an inference job as `succeeded` or not; `inference_outputs` (TS 28.105 InferenceOutput, whose `outputResult` carries the rApp's own result) becomes the job's AIMLInferenceReport."""
        body = None
        if inference_outputs is not None or potential_impact_info is not None:
            body = {"inferenceOutputs": inference_outputs or [], "potentialImpactInfo": potential_impact_info}
        return ensure_ok(self._r1.post(f"/aimgf/inference-jobs/{inference_job_id}/resolve",
                                       params={"succeeded": succeeded}, json=body))

    def get_inference_report(self, report_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/aiml-inference-reports/{report_id}"))

    def list_inference_jobs(self, model_id: uuid.UUID | str | None = None, status: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/inference-jobs", params={"model_id": model_id, "status": status}))

    # ---------------------------------------------------------------- AIMgF: MLMF performance monitoring

    def subscribe_performance_monitoring(self, model_id: uuid.UUID | str, metric_types: list[str],
                                          dme_type_id: uuid.UUID | str, guard_kpi_floor: dict | None = None,
                                          notification_destination: str | None = None) -> dict:
        """Subscribe to a model's performance monitoring (`POST /aimgf/mlmf/subscriptions`); the ids go in the query and the two body fields under their own keys. `notification_destination` is optional, as for every subscription-shaped resource."""
        return ensure_ok(self._r1.post("/aimgf/mlmf/subscriptions", params={
            "model_id": str(model_id), "dme_type_id": str(dme_type_id), "notification_destination": notification_destination,
        }, json={"metric_types": metric_types, "guard_kpi_floor": guard_kpi_floor}))

    def unsubscribe_performance_monitoring(self, subscription_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/aimgf/mlmf/subscriptions/{subscription_id}"))

    def report_performance(self, subscription_id: uuid.UUID | str, metrics: dict) -> dict:
        # metrics is the route's only body-eligible parameter — unwrapped.
        return ensure_ok(self._r1.post(f"/aimgf/mlmf/subscriptions/{subscription_id}/reports", json=metrics))

    def list_performance_subscriptions(self, model_id: uuid.UUID | str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/mlmf/subscriptions", params={"model_id": model_id}))

    def list_performance_reports(self, subscription_id: uuid.UUID | str, limit: int = 100) -> list[dict]:
        return ensure_ok(self._r1.get(f"/aimgf/mlmf/subscriptions/{subscription_id}/reports", params={"limit": limit}))

    def list_recent_performance_reports(self, breached_only: bool = False, limit: int = 50) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/mlmf/reports", params={"breached_only": breached_only, "limit": limit}))

    # ---------------------------------------------------------------- AIMgF: feature groups

    def create_feature_group(self, feature_group_name: str, feature_list: str, datalake_source: str, host: str,
                              port: str, bucket: str, token: str | None = None, db_org: str | None = None,
                              measurement: str | None = None, enable_dme: bool = False,
                              measured_obj_class: str | None = None, dme_port: str | None = None,
                              source_name: str | None = None, dme_type_id: uuid.UUID | str | None = None,
                              data_delivery_method: str = "PULL_HTTP", *, token_ref: str | None = None) -> dict:
        """Create a feature group (`POST /aimgf/feature-groups`); with `enable_dme` AIMgF creates the group's DME data job of `dme_type_id` (required then) and returns its `dmeDataJobId`.

        The data-lake credential is `token_ref`, the NAME of a secret that the AIMgF service resolves itself (lower-case letters, digits, `-` and `_`, starting
        with a letter; the secret is `AIMGF_FEATURE_GROUP_TOKEN_<NAME>`, `docs/SECRETS.md`). Give `token_ref`, `db_org` and `measurement` by keyword. The answer
        shows `tokenSet` and `tokenRef`, never a token.

        `token` (the clear-text credential) is DEPRECATED (`SEC-15.2b`): it is still sent when given, for one more minor release, and then the server drops it;
        a call that uses it raises a `DeprecationWarning` (which never contains the value). Give exactly one of `token_ref` and `token`, else `ValueError`
        (the message never contains either value). `db_org` and `measurement` are required; they have a default only so that `token` can keep its old position.
        """
        if (token is None) == (token_ref is None):
            raise ValueError("create_feature_group: give exactly one of token_ref (the name of a secret) and the deprecated token")
        if db_org is None or measurement is None:
            raise ValueError("create_feature_group: db_org and measurement are required")
        body: dict = {
            "featureGroupName": feature_group_name, "featureList": feature_list, "datalakeSource": datalake_source,
            "host": host, "port": port, "bucket": bucket, "dbOrg": db_org, "measurement": measurement,
            "enableDme": enable_dme, "measuredObjClass": measured_obj_class, "dmePort": dme_port, "sourceName": source_name,
            "dmeTypeId": _str(dme_type_id), "dataDeliveryMethod": data_delivery_method,
        }
        if token_ref is not None:
            body["tokenRef"] = token_ref
        else:
            warnings.warn("create_feature_group(token=...) is deprecated and the server drops the field in a later minor release: register a secret and pass token_ref",
                          DeprecationWarning, stacklevel=2)
            body["token"] = token
        return ensure_ok(self._r1.post("/aimgf/feature-groups", json=body))

    def list_feature_groups(self) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/feature-groups"))

    def delete_feature_group(self, feature_group_name: str) -> dict:
        """Delete the feature group and terminate its DME data job, if it has one."""
        return ensure_ok(self._r1.delete(f"/aimgf/feature-groups/{feature_group_name}"))

    # ---------------------------------------------------------------- MLLF: deploy

    def deploy_model(self, model_id: uuid.UUID | str, node_groups: list[str]) -> dict:
        # node_groups is the route's only body parameter (a plain list) —
        # unwrapped, not {"node_groups": [...]}.
        return ensure_ok(self._r1.post(f"/mllf/models/{model_id}/deploy", json=node_groups))

    # ---------------------------------------------------------------- Wave 10.1: the full execution-mode lifecycle
    # HISTORY.md W10-03 (decision D-4): convenience calls
    # named as in the Wave 10 documents, over the existing AIMgF routes.

    def start_training(self, model_id: uuid.UUID | str, producer_id: str, package_id: uuid.UUID | str | None = None,
                       dme_data_job_ids: list[uuid.UUID | str] | None = None, runtime_profile: dict | None = None,
                       timeout_seconds: int | None = None, notification_uri: str | None = None,
                       required_data: dict | None = None, validation_criteria: dict | None = None) -> dict:
        """Start a training run on an MLTF runtime sized from the package's TRAINING runtime profile (or from `runtime_profile`); unset fields are left out of the body."""
        return ensure_ok(self._r1.post("/aimgf/training-jobs", json=_drop_none({
            "modelId": str(model_id), "producerId": producer_id, "packageId": _str(package_id),
            "dmeDataJobIds": [str(j) for j in dme_data_job_ids] if dme_data_job_ids else None,
            "runtimeProfile": runtime_profile, "timeoutSeconds": timeout_seconds, "notificationUri": notification_uri,
            "requiredData": required_data, "validationCriteria": validation_criteria,
        })))

    def report_training_progress(self, training_job_id: uuid.UUID | str, step: str) -> dict:
        """The execution runtime's step report: DATA_EXTRACTION, TRAINING or TRAINED_MODEL, forward only (OI-5-aiml-trainingjob-steps)."""
        return ensure_ok(self._r1.post(f"/aimgf/training-jobs/{training_job_id}/progress", json={"step": step}))

    def complete_training(self, training_job_id: uuid.UUID | str, succeeded: bool, metrics: dict | None = None,
                          **ts28105_fields) -> dict:
        """Complete a training job as `succeeded` or not with its `metrics`; `ts28105_fields` are further TS 28.105 result fields sent by their names."""
        return self._complete("training-jobs", training_job_id, succeeded, metrics, ts28105_fields)

    def start_validation(self, model_id: uuid.UUID | str, producer_id: str, package_id: uuid.UUID | str | None = None,
                         validation_criteria: dict | None = None, training_job_id: uuid.UUID | str | None = None,
                         timeout_seconds: int | None = None) -> dict:
        """Start a validation job for a model, with an optional package id, training job id and timeout; unset fields are left out of the body."""
        return ensure_ok(self._r1.post("/aimgf/validation-jobs", json=_drop_none({
            "modelId": str(model_id), "producerId": producer_id, "packageId": _str(package_id),
            "validationCriteria": validation_criteria, "trainingJobId": _str(training_job_id), "timeoutSeconds": timeout_seconds,
        })))

    def complete_validation(self, validation_job_id: uuid.UUID | str, succeeded: bool, metrics: dict | None = None,
                            **ts28105_fields) -> dict:
        """Complete a validation job as `succeeded` or not with its `metrics`; `ts28105_fields` are further TS 28.105 result fields sent by their names."""
        return self._complete("validation-jobs", validation_job_id, succeeded, metrics, ts28105_fields)

    def start_emulation(self, model_id: uuid.UUID | str, producer_id: str, package_id: uuid.UUID | str | None = None,
                        emulation_criteria: dict | None = None, timeout_seconds: int | None = None) -> dict:
        """Start an emulation job for a model with its `emulation_criteria`; unset fields are left out of the body."""
        return ensure_ok(self._r1.post("/aimgf/emulation-jobs", json=_drop_none({
            "modelId": str(model_id), "producerId": producer_id, "packageId": _str(package_id),
            "emulationCriteria": emulation_criteria, "timeoutSeconds": timeout_seconds,
        })))

    def complete_emulation(self, emulation_job_id: uuid.UUID | str, succeeded: bool, metrics: dict | None = None,
                           **ts28105_fields) -> dict:
        """Complete an emulation job as `succeeded` or not with its `metrics`; `ts28105_fields` are further TS 28.105 result fields sent by their names."""
        return self._complete("emulation-jobs", emulation_job_id, succeeded, metrics, ts28105_fields)

    def _complete(self, kind: str, job_id, succeeded: bool, metrics: dict | None, fields: dict) -> dict:
        """The shared `POST /aimgf/{kind}/{id}/complete` of the three job kinds (training-jobs, validation-jobs, emulation-jobs); extra result fields are merged into the body after `succeeded` and `metrics`."""
        return ensure_ok(self._r1.post(f"/aimgf/{kind}/{job_id}/complete",
                                       json={"succeeded": succeeded, "metrics": metrics or {}, **fields}))

    def get_model_lifecycle(self, model_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/models/{model_id}/lifecycle"))

    def deploy_runtime(self, model_id: uuid.UUID | str, package_id: uuid.UUID | str | None = None,
                       runtime_profile: dict | None = None) -> dict:
        """MLIF: instantiate the inference runtime through NFO (needs the model CERTIFIED or PROMOTED); the runtime profile is the body, the package id a query parameter."""
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/runtime/deploy",
                                       params=_drop_none({"package_id": _str(package_id)}), json=runtime_profile))

    def activate_runtime(self, model_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/runtime/activate"))

    def scale_runtime(self, model_id: uuid.UUID | str) -> dict:
        """Scales the serving runtime of a model this rApp owns (`POST /aimgf/models/{id}/runtime/scale`, call flow 17c): ACTIVE -> SCALING -> ACTIVE.
        Phase 1: NFO's scale takes no target size. 403 for another rApp's model, 409 unless ACTIVE, 502/503 when NFO refuses or does not answer."""
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/runtime/scale"))

    def terminate_runtime(self, model_id: uuid.UUID | str) -> dict:
        """Terminates the serving runtime of a model this rApp owns (`POST /aimgf/models/{id}/runtime/terminate`, call flow 17d): its NFO deployment and
        descriptor go, the runtime is TERMINATED and can be deployed again. 403 for another rApp's model, 502/503 when NFO does not terminate it."""
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/runtime/terminate"))


def _str(value) -> str | None:
    """`str(value)`, or None for None, so an optional id is sent as a JSON string or null."""
    return str(value) if value is not None else None


def _drop_none(d: dict) -> dict:
    """The mapping without its None values, so an unset optional field is absent from the body instead of null."""
    return {k: v for k, v in d.items() if v is not None}
