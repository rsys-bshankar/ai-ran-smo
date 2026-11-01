"""sdk.models: a thin client over AIMgF's model registry (`aimgf/app/registry/`), the model repository of TS 28.105 (TS 29.482 `MLModelManagement`).

One method per route, with the query-versus-body shape of each checked against the route's OpenAPI schema; no validation of its own. `store_model` is the one convenience: register-or-reuse a
model and add an artifact version (`HISTORY.md` W10-03, D-4). Part of the SDK (`sdk/README.md`).
"""

import uuid

from ._common import BaseClient, ensure_ok


class ModelsClient(BaseClient):
    """The `sdk.models` namespace: model registration and discovery, artifacts and coordination groups in AIMgF's registry."""
    def register_model(self, model_type: str, version: str, required_resource_type_id: str | None = None,
                        description: str | None = None, author: str | None = None, owner: str | None = None,
                        input_data_type: str | None = None, output_data_type: str | None = None,
                        target_environments: list[dict] | None = None, domain: str | None = None,
                        custom_domain: str | None = None, vendors: list[str] | None = None) -> dict:
        """Register a model (`POST /aimgf/models`).

        `domain` is SPEECH_RECOGNITION, IMAGE_RECOGNITION, IMAGE_PROCESSING, LOCATION_PREDICTION or CUSTOM (with `custom_domain`); the registry answers 422 (raised as `SdkError`) for any other value.
        `vendors` and the two domain fields come from the MLModel schema of `TS29482_MLR_MLModelManagement.yaml`.
        """
        return ensure_ok(self._r1.post("/aimgf/models", json={
            "modelType": model_type, "version": version, "requiredResourceTypeId": required_resource_type_id,
            "description": description, "author": author, "owner": owner, "inputDataType": input_data_type,
            "outputDataType": output_data_type, "targetEnvironments": target_environments or [],
            "domain": domain, "customDomain": custom_domain, "vendors": vendors,
        }))

    def discover_models(self, model_type: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/models", params={"model_type": model_type}))

    def get_model(self, model_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/aimgf/models/{model_id}"))

    def update_model(self, model_id: uuid.UUID | str, model_type: str, version: str, **fields) -> dict:
        """Replace a model's metadata (`PUT /aimgf/models/{id}`).

        `model_type` and `version` are the model's immutable identity: the registry answers 400 when they differ from the stored record. `fields` may hold any of requiredResourceTypeId, trainingDataLineage,
        integrityHash, description, author, owner, inputDataType, outputDataType, targetEnvironments, domain, customDomain and vendors (JSON names). The cleared node groups live on AIMgF's model
        lifecycle, not here: see `LifecycleClient`.
        """
        return ensure_ok(self._r1.put(f"/aimgf/models/{model_id}", json={
            "modelType": model_type, "version": version, **fields,
        }))

    def deregister_model(self, model_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/aimgf/models/{model_id}"))

    def upload_artifact(self, model_id: uuid.UUID | str, filename: str, content: bytes) -> dict:
        """Upload a model artifact (`POST /aimgf/models/{id}/artifact`, multipart, content type `application/zip`); each upload is a new artifact version, and as it carries `files` it is never repeated by the conflict retry."""
        return ensure_ok(self._r1.post(f"/aimgf/models/{model_id}/artifact", files={
            "file": (filename, content, "application/zip"),
        }))

    def store_model(self, model_type: str, version: str, artifact: bytes, filename: str = "model.zip", **metadata) -> dict:
        """Register the model under (`model_type`, `version`), or reuse the one already registered, and store `artifact` as its next artifact version; returns the upload answer (`modelId`, `artifactVersion`, ...).

        `metadata` is passed to `register_model` only when the model is new; for an existing model it is ignored. Makes up to three calls (discover, register, upload).
        """
        existing = next((m for m in self.discover_models(model_type) if m.get("version") == version), None)
        model_id = existing["modelId"] if existing else self.register_model(model_type, version, **metadata)["modelId"]
        return self.upload_artifact(model_id, filename, artifact)

    def download_artifact(self, model_id: uuid.UUID | str, artifact_version: int):
        """The raw response for an artifact version (not JSON): the bytes are in `.content` (`application/zip`). Raises `SdkError` with the response text for a status of 400 or more."""
        resp = self._r1.get(f"/aimgf/models/{model_id}/artifact/{artifact_version}")
        if resp.status_code >= 400:
            from ._common import SdkError
            raise SdkError(resp.status_code, resp.text)
        return resp

    def create_coordination_group(self, member_model_ids: list[uuid.UUID | str], member_use_cases: list[str] | None = None,
                                   shared_feature_pipeline_ref: str | None = None,
                                   retrain_propagation: str = "ANY_MEMBER_TRIGGERS") -> dict:
        """Create a model coordination group (`POST /aimgf/coordination-groups`): models that are retrained together; `retrain_propagation` says which member triggers the others."""
        return ensure_ok(self._r1.post("/aimgf/coordination-groups", json={
            "memberModelIds": [str(m) for m in member_model_ids], "memberUseCases": member_use_cases or [],
            "sharedFeaturePipelineRef": shared_feature_pipeline_ref, "retrainPropagation": retrain_propagation,
        }))

    def list_coordination_groups(self) -> list[dict]:
        return ensure_ok(self._r1.get("/aimgf/coordination-groups"))
