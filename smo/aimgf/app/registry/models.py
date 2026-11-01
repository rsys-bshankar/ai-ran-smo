"""Database tables of MLMR: models, artifacts, coordination groups, repositories, TS 29.482 storages and profiles.

Used by `main.py` and `mlr.py`; the schema itself is created by the Alembic revisions in `migrations/` (the models are
checked against it by `scripts/check_migration_matches_models.py`). Table names are internal storage, not a contract: the
model table is still called `aiml_model` from before the class was renamed `MLModel`.

Lifecycle state, training jobs and node-group targeting are not here; they are AIMgF's `model_lifecycle` row. No other
module's table has a foreign key into these tables (revision 0022 dropped them; AIMgF's rows name a model by a plain id), so
deleting a model leaves AIMgF's rows about it. Within this module, `model_artifact`, `ml_model_profile` and
`model_change_subscription` reference `aiml_model` with `ON DELETE CASCADE` (on Postgres; `DELETE /models/{id}` also deletes the
artifacts and profiles itself), and a repository delete sets `ml_model_repository_id` to NULL on its models and groups.
"""

import datetime
import uuid

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from smo_shared.db import Base

# Wave 3 (AI Platform Service Decomposition) — TS29482_MLR_MLModelManagement.yaml's
# MLModelDomain enum, HISTORY.md §7's MLMR section.
# The closed MLModelDomain values of TS29482_MLR_MLModelManagement.yaml; `CUSTOM` is paired with `custom_domain`.
MODEL_DOMAINS = {"SPEECH_RECOGNITION", "IMAGE_RECOGNITION", "IMAGE_PROCESSING", "LOCATION_PREDICTION", "CUSTOM"}


class MLModelRepository(Base):
    """TS 28.105 MLModelRepository: the container models and coordination groups may be registered into. Containment is optional
    (`ml_model_repository_id` is nullable on both) and deleting a repository sets it to NULL on its members.
    """
    __tablename__ = "ml_model_repository"

    ml_model_repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_label: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))


class MLModelCoordinationGroup(Base):
    """A set of models that retrain together (at least two members; enforced by a CHECK on Postgres and by the route). `member_model_ids`
    is an array of model ids that is not a foreign key, so a member that is deleted stays listed.
    The list route returns `retrain_propagation` and `shared_feature_pipeline_ref`, which AIMgF reads.
    """
    __tablename__ = "ml_model_coordination_group"

    group_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_type: Mapped[str] = mapped_column(String, nullable=False, default="SHARED_MODEL")
    member_model_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid).with_variant(JSON(none_as_null=True), "sqlite"), nullable=False)
    member_use_cases: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    shared_feature_pipeline_ref: Mapped[str | None] = mapped_column(String)
    retrain_propagation: Mapped[str] = mapped_column(String, nullable=False, default="ANY_MEMBER_TRIGGERS")
    ml_model_repository_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ml_model_repository.ml_model_repository_id", ondelete="SET NULL"))


class MLModel(Base):
    """One registered model: identity, descriptive metadata and the TS 28.105 and TS 29.482 attributes.

    Identity is (`model_type`, `version`), a unique constraint, so a duplicate registration is a 409 rather than a second row.
    JSON columns hold the spec-shaped complex datatypes as sent. `phase_info` is written by AIMgF; `registered_at` is the base for
    `storeDiscReqs.duration` and `accessReqs.timePeriod`. Table `aiml_model`.
    """
    __tablename__ = "aiml_model"
    __table_args__ = (UniqueConstraint("model_type", "version"),)

    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    registration_id: Mapped[str] = mapped_column(String, nullable=False)
    model_type: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    training_data_lineage: Mapped[dict | None] = mapped_column(JSON)
    integrity_hash: Mapped[str | None] = mapped_column(String)
    artifact_location: Mapped[str | None] = mapped_column(String)
    required_resource_type_id: Mapped[str | None] = mapped_column(String)
    # NEW section 5: the reference's own ModelRelatedInformation/
    # ModelInformation/Metadata/TargetEnvironment (modelInfo.go) — all
    # required there, kept optional here since this build's own
    # RegisterModel was already permissive before this pass and nothing
    # should retroactively reject an existing caller. target_environments
    # stored as JSON, not a normalized child table — registered and read
    # back wholesale, the same adaptation this build already uses for
    # SME's aefProfiles/DMEType.collection_spec.
    description: Mapped[str | None] = mapped_column(String)
    author: Mapped[str | None] = mapped_column(String)
    owner: Mapped[str | None] = mapped_column(String)
    input_data_type: Mapped[str | None] = mapped_column(String)
    output_data_type: Mapped[str | None] = mapped_column(String)
    target_environments: Mapped[list[dict] | None] = mapped_column(JSON)
    # Wave 3: TS29482_MLR_MLModelManagement.yaml's MLModel schema
    # (HISTORY.md §7's MLMR section) — domain/customDomain mirror the
    # spec's own domain+CUSTOM-string pairing; vendors ties into DME's
    # own Wave 3 multi-vendor provenance principle
    # (docs/ARCHITECTURE.md (DME)), same theme applied to model
    # identity rather than data-source identity.
    domain: Mapped[str | None] = mapped_column(String)
    custom_domain: Mapped[str | None] = mapped_column(String)
    vendors: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(none_as_null=True), "sqlite"))
    # Wave 4 — TS 28.105 MLModel IOC attributes (TS28105_AiMlNrm.yaml),
    # spec-shaped JSON for the complex datatypes. mLModelId/mLModelVersion
    # are model_id/version; the read-only mLTrainingType,
    # aIMLInferenceReportRefList and usedByFunctionRefList are AIMgF's
    # truth and are joined in by `GET /ml-models/{id}`, not stored here.
    aiml_inference_name: Mapped[str | None] = mapped_column(String)
    expected_run_time_context: Mapped[dict | None] = mapped_column(JSON)
    training_context: Mapped[dict | None] = mapped_column(JSON)
    run_time_context: Mapped[dict | None] = mapped_column(JSON)
    supported_performance_indicators: Mapped[list | None] = mapped_column(JSON)
    ml_capabilities_info_list: Mapped[list | None] = mapped_column(JSON)
    inference_scope: Mapped[list | None] = mapped_column(JSON)
    retraining_events_monitor_ref: Mapped[str | None] = mapped_column(String)
    source_trained_ml_model_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ml_model_repository_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ml_model_repository.ml_model_repository_id", ondelete="SET NULL"))
    # TS 29.482 MLModel (TS29482_MLR_MLModelManagement.yaml): the rest of the
    # spec's attributes. SA-MLMR-6 / 7 / 8: storeDiscReqs, phaseInfo (with
    # trainingInfo.baseModelId) and usageReqs, stored spec-shaped.
    registered_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))
    ml_model_src_id: Mapped[str | None] = mapped_column(String)
    interop_info: Mapped[str | None] = mapped_column(String)
    val_service_ids: Mapped[list | None] = mapped_column(JSON)
    adae_analytics_id: Mapped[str | None] = mapped_column(String)
    usage_reqs: Mapped[dict | None] = mapped_column(JSON)
    phase_info: Mapped[dict | None] = mapped_column(JSON)
    store_disc_reqs: Mapped[dict | None] = mapped_column(JSON)


class MLModelsStorage(Base):
    """TS 29.482 MLModelsStorage (SA-MLMR-1): a group of model profiles and optional storage addresses (EndPoint list)."""
    __tablename__ = "ml_models_storage"

    storage_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ml_models_addresses: Mapped[list | None] = mapped_column(JSON)  # EndPoint list
    supp_feat: Mapped[str | None] = mapped_column(String)


class MLModelProfile(Base):
    """TS 29.482 MLModelProfile: names a registered model inside a storage, with the AIMLE ids and the model's URI. Deleted with
    its storage or its model.
    """
    __tablename__ = "ml_model_profile"

    profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    storage_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ml_models_storage.storage_id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("aiml_model.model_id", ondelete="CASCADE"), nullable=False)
    aimle_serv_id: Mapped[str | None] = mapped_column(String)
    aimle_rep_id: Mapped[str | None] = mapped_column(String)
    ml_model_uri: Mapped[dict | None] = mapped_column(JSON)  # EndPoint


class ModelArtifact(Base):
    """One uploaded artifact version of a model. The bytes are in the object store's `models` bucket at `object_key` (S2.5, `artifacts.py`), never in
    the row; `sha256` is their digest (checked on every read), `artifact_format` what the format gate found (`onnx`, `safetensors`, `zip-legacy`).
    `artifact_version` starts at 1 and is assigned by the upload route; the table does not constrain it to be unique per model.
    `size_bytes` is measured from the uploaded bytes and feeds `mlModelSize`.
    """

    __tablename__ = "model_artifact"
    __table_args__ = (
        CheckConstraint("artifact_version >= 1", name="artifact_version_positive"),
        CheckConstraint("artifact_format IN ('onnx', 'safetensors', 'zip-legacy')", name="artifact_format_known"),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("aiml_model.model_id", ondelete="CASCADE"), nullable=False)  # NEW section 5: deregister_model's cascade, same shape as DME's dme_type FKs
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_format: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_at: Mapped[datetime.datetime] = mapped_column(default=lambda: datetime.datetime.now(datetime.UTC))
    # Wave 3: TS29482_MLR_MLModelManagement.yaml's MLModel.mlModelSize
    # (HISTORY.md §7's MLMR section) — computed from the real uploaded
    # bytes, not a separately-declared value that could drift from them.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelChangeSubscription(Base):
    """Table declared for model-change subscriptions; no code in this module reads or writes it."""
    __tablename__ = "model_change_subscription"

    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("aiml_model.model_id", ondelete="CASCADE"))  # NEW section 5: deregister_model's cascade
    consumer_id: Mapped[str] = mapped_column(String, nullable=False)
