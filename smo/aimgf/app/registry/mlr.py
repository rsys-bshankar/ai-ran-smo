"""TS 29.482 MLR: MLModel information, storages and profiles, discovery.

`TS29482_MLR_MLModelManagement.yaml` and `TS29482_MLR_ModelInformationDiscovery.yaml`
over the registry's model rows (the `registry` package of AIMgF, formerly MLMR).

* **MLModel** (`model_info_view`): the spec's MLModel record for a registered
  model: `mlModelId` (= the model id), `mlModelSize` (the latest artifact's size),
  `domain`, `vendors`, `valServiceIds`, `interopInfo`, `usageReqs` (SA-MLMR-8),
  `phaseInfo` with `trainingInfo.baseModelId` (SA-MLMR-7) and `storeDiscReqs`
  (SA-MLMR-6).
* **MLModelsStorage / MLModelProfile** (SA-MLMR-1): `/storages`. A profile names
  a registered model through `mlModelInfo.mlModelId`; the other `mlModelInfo`
  attributes, when sent, update that model. The model record is the source of
  truth: a profile adds the AIMLE ids and the model's URI.
* **storeDiscReqs** (SA-MLMR-6), enforced for discovery and artifact download:
  `duration` is how long, from registration, the model is discoverable and
  downloadable (afterwards 410 `MODEL_EXPIRED`; the registry record stays);
  `accessReqs.accessReq` is `PUBLICLY_AVAILABLE` (anyone), `RESTRICTED`
  (the model's owner, or an id in `valServerIds` / `valClientIds`) or
  `PRIVATE_USE_ONLY` (the owner), judged on the caller id R1 Termination
  forwards (`X-R1-Invoker-Id`, the introspected token's client id); `timePeriod`
  closes access that many seconds after registration. `accessReqs.location` is
  stored, not enforced (no requester location exists). The management routes
  (`GET /models`, `GET /models/{id}`) are not filtered: they serve the platform.
* **Discovery** (SA-MLMR-9): `GET /models?filt-criteria=<MLModel JSON>` matches
  the whole object. Every attribute in the criteria must be present in the
  model's MLModel record: scalars equal, lists contained, objects recursively.
"""

import base64
import datetime
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import invoker_id
from smo_shared.roles import ROLE_RAPP, role_of
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .models import MODEL_DOMAINS, MLModel, MLModelProfile, MLModelsStorage, ModelArtifact

router = APIRouter()

Phase = Literal["NOT_TRAINED", "IN_TRAINING", "TRAINED", "IN_RETRAINING", "DEPLOYED"]
Usage = Literal["TRAINING", "INFERENCE"]
AccessReq = Literal["PUBLICLY_AVAILABLE", "RESTRICTED", "PRIVATE_USE_ONLY"]
Freshness = Literal["REAL_TIME", "NEAR_REAL_TIME", "HOURLY", "DAILY", "WEEKLY", "MONTHLY"]


# Base for the TS 29.482 datatypes below: unknown fields are refused (422).
class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


# TS 29.482 MLModelPerf; at least one of its two attributes must be present (checked by the validator).
class MLModelPerf(_Spec):
    resourceAddr: int | None = Field(default=None, ge=0, le=100)
    appSpecPerfData: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.resourceAddr is None and self.appSpecPerfData is None:
            raise ValueError("MLModelPerf needs resourceAddr or appSpecPerfData")
        return self


# TS 29.482 MLModelTrainingInfo; at least one attribute must be present. `baseModelId` is the lineage link (SA-MLMR-7).
class MLModelTrainingInfo(_Spec):
    dataSources: str | None = None
    dataVolume: int | None = Field(default=None, ge=0)
    freshness: Freshness | None = None
    baseModelId: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if all(v is None for v in (self.dataSources, self.dataVolume, self.freshness, self.baseModelId)):
            raise ValueError("MLModelTrainingInfo needs one of dataSources, dataVolume, freshness, baseModelId")
        return self


# TS 29.482 MLModelPhaseInfo. `phase` is required here; AIMgF's training writes it in process (`access.record_phase`), anyone else through `PATCH /models/{id}/phase-info`.
class MLModelPhaseInfo(_Spec):
    phase: Phase
    observedPerf: MLModelPerf | None = None
    trainingInfo: MLModelTrainingInfo | None = None
    contTrainInd: bool | None = None
    contTrainParams: str | None = None


# TS 29.482 MLModelAccessReqs. How the three access kinds are judged is in `access_denied`.
class MLModelAccessReqs(_Spec):
    accessReq: AccessReq
    valServerIds: list[str] | None = None
    valClientIds: list[str] | None = None
    timePeriod: int | None = Field(default=None, ge=0)
    location: dict | None = None


# TS 29.482 MLModelStoreDiscReqs (SA-MLMR-6); enforced by `is_expired` and `access_denied`. At least one attribute must be present.
class MLModelStoreDiscReqs(_Spec):
    duration: int | None = Field(default=None, ge=0)
    accessReqs: MLModelAccessReqs | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.duration is None and self.accessReqs is None:
            raise ValueError("MLModelStoreDiscReqs needs duration or accessReqs")
        return self


# TS 29.482 MLModelUsageReqs (SA-MLMR-8); stored and returned, not enforced.
class MLModelUsageReqs(_Spec):
    usages: list[Usage] = Field(min_length=1)


class EndPoint(_Spec):
    """TS 29.558 EndPoint: exactly one of uri, fqdn, ipv4Addrs, ipv6Addrs."""
    uri: str | None = None
    fqdn: str | None = None
    ipv4Addrs: list[dict] | None = Field(default=None, min_length=1)
    ipv6Addrs: list[dict] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one(self):
        if sum(v is not None for v in (self.uri, self.fqdn, self.ipv4Addrs, self.ipv6Addrs)) != 1:
            raise ValueError("EndPoint needs exactly one of uri, fqdn, ipv4Addrs, ipv6Addrs")
        return self


class SpecAttributes(BaseModel):
    """The writable MLModel attributes beyond MLMR's own (a mixin: extra fields stay tolerated)."""
    mlModelSrcId: str | None = None
    interopInfo: str | None = None
    valServiceIds: list[str] | None = Field(default=None, min_length=1)
    adaeAnalyticsId: str | None = None
    usageReqs: MLModelUsageReqs | None = None
    phaseInfo: MLModelPhaseInfo | None = None
    storeDiscReqs: MLModelStoreDiscReqs | None = None


class MLModelInfo(SpecAttributes):
    """TS 29.482 MLModel as it appears in a profile or in discovery criteria."""
    model_config = ConfigDict(extra="forbid")
    mlModelId: str | None = None
    mlModelSize: int | None = Field(default=None, ge=0)
    domain: str | None = None
    customDomain: str | None = None
    vendors: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _domain(self):
        if self.domain is not None and self.domain not in MODEL_DOMAINS:
            raise ValueError(f"unknown domain {self.domain!r}")
        return self


def _invalid(detail: str):
    """Builds the 422 SCHEMA_VALIDATION_FAILED error with `detail` (returned, not raised; callers `raise` it)."""
    return framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=detail)


def _dump(value: Any) -> Any:
    """Plain-JSON form of a pydantic object or list of them, unset fields left out; other values pass through. Used for every
    JSON column written here.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True, mode="json")
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


def apply_spec_attributes(model: MLModel, body: SpecAttributes) -> None:
    """Writes the TS 29.482 attributes the caller sent onto `model`; an attribute left out (None) is kept as it is.

    That keep-if-omitted rule is what lets AIMgF own `phaseInfo`: an rApp's PUT that does not mention it cannot erase what AIMgF
    wrote. Shared by register, update and the profile handling in `_add_profiles`. Does not commit.
    """
    for field, column in (("mlModelSrcId", "ml_model_src_id"), ("interopInfo", "interop_info"), ("valServiceIds", "val_service_ids"),
                          ("adaeAnalyticsId", "adae_analytics_id"), ("usageReqs", "usage_reqs"), ("phaseInfo", "phase_info"),
                          ("storeDiscReqs", "store_disc_reqs")):
        value = getattr(body, field)
        if value is not None:
            setattr(model, column, _dump(value))


def spec_attributes_view(m: MLModel) -> dict:
    """The stored TS 29.482 attributes of a model under their spec names (None for unset)."""
    return {"mlModelSrcId": m.ml_model_src_id, "interopInfo": m.interop_info, "valServiceIds": m.val_service_ids,
            "adaeAnalyticsId": m.adae_analytics_id, "usageReqs": m.usage_reqs, "phaseInfo": m.phase_info,
            "storeDiscReqs": m.store_disc_reqs}


def model_info_view(db: Session, m: MLModel) -> dict:
    """The TS 29.482 MLModel record of a registered model, with unset attributes left out. `mlModelSize` is the size of the
    latest artifact version (one query), absent when nothing was uploaded. This record is also what discovery matches against.
    """
    size = db.scalar(select(ModelArtifact.size_bytes).where(ModelArtifact.model_id == m.model_id)
                     .order_by(ModelArtifact.artifact_version.desc()).limit(1))
    view = {"mlModelId": str(m.model_id), "adaeAnalyticsId": m.adae_analytics_id, "mlModelSize": size,
            "mlModelSrcId": m.ml_model_src_id, "valServiceIds": m.val_service_ids, "domain": m.domain,
            "customDomain": m.custom_domain, "vendors": m.vendors or None, "interopInfo": m.interop_info,
            "phaseInfo": m.phase_info, "storeDiscReqs": m.store_disc_reqs, "usageReqs": m.usage_reqs}
    return {k: v for k, v in view.items() if v is not None}


# ---------------------------------------------------------------- storeDiscReqs

def _aware(t: datetime.datetime) -> datetime.datetime:
    """Treats a naive timestamp as UTC; SQLite returns timezone-aware columns naive, Postgres does not."""
    return t if t.tzinfo else t.replace(tzinfo=datetime.UTC)


def _now() -> datetime.datetime:
    """Current UTC time; a module function so the expiry checks read one clock."""
    return datetime.datetime.now(datetime.UTC)


def is_expired(m: MLModel) -> bool:
    """True when the model's storeDiscReqs `duration` (seconds from `registered_at`) has passed. A model with no duration never
    expires. Expiry hides the model from discovery and download; the registry record itself stays.
    """
    duration = (m.store_disc_reqs or {}).get("duration")
    return duration is not None and _now() > _aware(m.registered_at) + datetime.timedelta(seconds=duration)


def access_denied(m: MLModel, caller: str | None) -> str | None:
    """Returns why `caller` may not use the model, or None when it may.

    Order of checks: no `accessReqs` means open; then `timePeriod` (seconds from registration) closes access for everyone,
    the owner included; then PUBLICLY_AVAILABLE is open; PRIVATE_USE_ONLY allows only the owner; RESTRICTED allows the owner and
    the ids in `valServerIds` / `valClientIds`. `caller` is the id R1 Termination forwards; None (no id) matches no owner and no
    listed id, so it is denied for the last two kinds. `accessReqs.location` is not evaluated.
    """
    access = (m.store_disc_reqs or {}).get("accessReqs")
    if not access:
        return None
    time_period = access.get("timePeriod")
    if time_period is not None and _now() > _aware(m.registered_at) + datetime.timedelta(seconds=time_period):
        return f"the access period ({time_period} s from registration) has ended"
    kind = access["accessReq"]
    if kind == "PUBLICLY_AVAILABLE":
        return None
    owner = caller is not None and caller == m.owner
    if kind == "PRIVATE_USE_ONLY":
        return None if owner else "the model is for private use only"
    allowed = set(access.get("valServerIds") or []) | set(access.get("valClientIds") or [])
    return None if owner or (caller is not None and caller in allowed) else "the model is restricted"


def require_usable(m: MLModel, caller: str | None) -> None:
    """Raises 410 MODEL_EXPIRED for an expired model, else 403 MODEL_ACCESS_DENIED when `access_denied` gives a reason; returns
    None when the model may be used. Called by the artifact download.
    """
    if is_expired(m):
        raise framework_error(FrameworkError.MODEL_EXPIRED, detail=f"model {m.model_id} passed its storage duration")
    reason = access_denied(m, caller)
    if reason is not None:
        raise framework_error(FrameworkError.MODEL_ACCESS_DENIED, detail=f"model {m.model_id}: {reason}")


# The model ownership rule of SEC-15.3 .. SEC-15.3d, written once for every MLMR route that changes a model (PUT, DELETE, artifact upload, phase-info, a coordination group naming
# the model; the routes of `main.py` call these) and for an rApp's artifact download: a caller with role `rapp` (the role the gateway vouched for) may act only on a model whose `owner` is its own invoker id; a
# model with no `owner` is open to any rApp; role `internal` (the console, SMO modules) and a call with no role are never asked.
def owner_may_act(request: Request, model: MLModel) -> bool:
    """True when the caller may act on `model` under the owner rule: not an rApp, or the model has no owner, or the owner is the caller's invoker id."""
    return role_of(request) != ROLE_RAPP or not model.owner or model.owner == invoker_id(request)


def refuse_unless_owner(request: Request, model: MLModel, action: str) -> None:
    """Raises 403 `MODEL_ACCESS_DENIED` when `owner_may_act` is false; `action` completes the sentence "only its owner may ..." in the error detail."""
    if not owner_may_act(request, model):
        raise framework_error(FrameworkError.MODEL_ACCESS_DENIED, detail=f"model {model.model_id} belongs to another producer: only its owner may {action}")


# ---------------------------------------------------------------- phase info (AIMgF)

# Body of `PATCH /models/{id}/phase-info`: every attribute optional here, the merge rules are on the route.
class PhaseInfoPatch(_Spec):
    phase: Phase | None = None
    observedPerf: MLModelPerf | None = None
    trainingInfo: MLModelTrainingInfo | None = None
    contTrainInd: bool | None = None
    contTrainParams: str | None = None


@router.patch("/models/{model_id}/phase-info")
def patch_phase_info(request: Request, model_id: uuid.UUID, body: PhaseInfoPatch, db: Session = Depends(get_session)):
    """AIMgF's write-back (SA-MLMR-7): the model's phase and training lineage
    (`trainingInfo.baseModelId`, `dataSources`). The sent attributes are merged
    into `phaseInfo`; the first write must carry a `phase`."""
    # Merge rules: the sent attributes overwrite the stored ones, except `trainingInfo`, which is merged field by field with
    # the stored one. The first write for a model must carry `phase` (422 SCHEMA_VALIDATION_FAILED). 404 MODEL_NOT_FOUND.
    # Single commit; returns the model's MLModel record.
    # SEC-15.3d: the route is on the rApp allow-list, so a caller with role `rapp` that is not the model's owner is 403 `MODEL_ACCESS_DENIED` (after the 404, before anything is merged);
    # AIMgF writes as an SMO module (role `internal`) and is not asked.
    model = db.get(MLModel, model_id)
    if model is None:
        raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail="no such model")
    refuse_unless_owner(request, model, "write its phase")
    merge_phase_info(model, body)
    db.commit()
    return model_info_view(db, model)


def merge_phase_info(model: MLModel, body: PhaseInfoPatch) -> None:
    """Merges a phase write into `model.phase_info`: the sent attributes overwrite the stored ones, `trainingInfo` is merged key by key. The first write must carry
    a `phase` (422 SCHEMA_VALIDATION_FAILED). Shared by the route above and AIMgF's in-process write-back (`access.record_phase`); does not commit."""
    info = dict(model.phase_info or {})
    if "phase" not in info and body.phase is None:
        raise _invalid("the first phaseInfo write needs a phase")
    sent = _dump(body) or {}
    # trainingInfo is merged key by key (sent keys win) rather than replaced, so a later write that names only baseModelId keeps dataSources.
    training = {**info.get("trainingInfo", {}), **sent.pop("trainingInfo", {})}
    info.update(sent)
    if training:
        info["trainingInfo"] = training
    model.phase_info = info


# ---------------------------------------------------------------- storages and profiles

# TS 29.482 MLModelProfile as sent in a storage; `mlModelInfo.mlModelId` must be present (validator) and names a registered model.
class ProfileBody(_Spec):
    mlModelProfId: uuid.UUID | None = None
    aimleServId: str | None = None
    aimleRepId: str | None = None
    mlModelInfo: MLModelInfo
    mlModelUri: EndPoint | None = None

    @model_validator(mode="after")
    def _has_id(self):
        if not self.mlModelInfo.mlModelId:
            raise ValueError("mlModelInfo.mlModelId is required")
        return self


# Body of `POST` and `PUT /storages`; at least one of `mlModels` and `mlModelsAddresses` is required.
class StorageBody(_Spec):
    mlModels: list[ProfileBody] | None = Field(default=None, min_length=1)
    mlModelsAddresses: list[EndPoint] | None = Field(default=None, min_length=1)
    suppFeat: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.mlModels is None and self.mlModelsAddresses is None:
            raise ValueError("MLModelsStorage needs mlModels or mlModelsAddresses")
        return self


# Body of `PATCH /storages/{id}`: a list that is present replaces the stored list, an absent one is kept.
class StoragePatch(_Spec):
    mlModels: list[ProfileBody] | None = Field(default=None, min_length=1)
    mlModelsAddresses: list[EndPoint] | None = Field(default=None, min_length=1)


def _profile_view(db: Session, p: MLModelProfile) -> dict:
    """A profile as returned: its own ids and URI plus the live MLModel record of the model it names (None if that model is gone)."""
    model = db.get(MLModel, p.model_id)
    view = {"mlModelProfId": str(p.profile_id), "aimleServId": p.aimle_serv_id, "aimleRepId": p.aimle_rep_id,
            "mlModelInfo": model_info_view(db, model) if model is not None else None, "mlModelUri": p.ml_model_uri}
    return {k: v for k, v in view.items() if v is not None}


def _storage_view(db: Session, s: MLModelsStorage) -> dict:
    """A storage with its profiles (one query for the profiles, plus the model record per profile)."""
    profiles = db.scalars(select(MLModelProfile).where(MLModelProfile.storage_id == s.storage_id)).all()
    view = {"storageId": str(s.storage_id), "mlModels": [_profile_view(db, p) for p in profiles] or None,
            "mlModelsAddresses": s.ml_models_addresses, "suppFeat": s.supp_feat}
    return {k: v for k, v in view.items() if v is not None}


def _add_profiles(db: Session, storage: MLModelsStorage, profiles: list[ProfileBody]) -> None:
    """Adds one MLModelProfile per entry to `storage`, and applies each entry's `mlModelInfo` to the model it names.

    Raises 422 when the model id is not a UUID and 404 MODEL_NOT_FOUND when it names no model. Side effect on purpose: the
    domain, customDomain, vendors and spec attributes in `mlModelInfo` update the registered model (the model record is the
    source of truth, the profile only points at it). Adds rows to the session; the caller commits.
    """
    for item in profiles:
        try:
            model_id = uuid.UUID(item.mlModelInfo.mlModelId)
        except ValueError:
            raise _invalid(f"mlModelInfo.mlModelId {item.mlModelInfo.mlModelId!r} is not a model id") from None
        model = db.get(MLModel, model_id)
        if model is None:
            raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail=f"no such model {model_id}")
        info = item.mlModelInfo
        if info.domain is not None:
            model.domain = info.domain
        if info.customDomain is not None:
            model.custom_domain = info.customDomain
        if info.vendors is not None:
            model.vendors = info.vendors
        apply_spec_attributes(model, info)
        db.add(MLModelProfile(profile_id=item.mlModelProfId or uuid.uuid4(), storage_id=storage.storage_id, model_id=model_id,
                              aimle_serv_id=item.aimleServId, aimle_rep_id=item.aimleRepId, ml_model_uri=_dump(item.mlModelUri)))


def _get_storage(db: Session, storage_id: uuid.UUID) -> MLModelsStorage:
    """Loads a storage or raises 404 NRM_OBJECT_NOT_FOUND."""
    s = db.get(MLModelsStorage, storage_id)
    if s is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such MLModelsStorage {storage_id}")
    return s


def _clear_profiles(db: Session, storage_id: uuid.UUID) -> None:
    """Deletes all profiles of a storage and flushes, so a following insert in the same transaction sees them gone."""
    for p in db.scalars(select(MLModelProfile).where(MLModelProfile.storage_id == storage_id)).all():
        db.delete(p)
    db.flush()


@router.post("/storages", status_code=201)
def create_storage(body: StorageBody, db: Session = Depends(get_session)):
    # Creates a storage and its profiles in one transaction (201). Any profile error (422 or 404) leaves nothing stored.
    storage = MLModelsStorage(ml_models_addresses=[_dump(a) for a in body.mlModelsAddresses] if body.mlModelsAddresses else None,
                              supp_feat=body.suppFeat)
    db.add(storage)
    db.flush()
    _add_profiles(db, storage, body.mlModels or [])
    db.commit()
    return _storage_view(db, storage)


@router.get("/storages")
def list_storages(storage_ids: list[str] | None = Query(default=None, alias="storage-ids"),
                  profile_ids: list[str] | None = Query(default=None, alias="profile-ids"),
                  limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paginated storages. `storage-ids` and `profile-ids` filter (a malformed id is 422); the two filters combine with AND.
    stmt = select(MLModelsStorage)
    if storage_ids:
        try:
            stmt = stmt.where(MLModelsStorage.storage_id.in_([uuid.UUID(i) for i in storage_ids]))
        except ValueError:
            raise _invalid("storage-ids must be storage ids") from None
    if profile_ids:
        try:
            wanted = [uuid.UUID(i) for i in profile_ids]
        except ValueError:
            raise _invalid("profile-ids must be profile ids") from None
        stmt = stmt.where(MLModelsStorage.storage_id.in_(select(MLModelProfile.storage_id).where(MLModelProfile.profile_id.in_(wanted))))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_storage_view(db, s) for s in page["items"]]}


@router.get("/storages/{storage_id}")
def get_storage(storage_id: uuid.UUID, db: Session = Depends(get_session)):
    # One storage with its profiles; 404 NRM_OBJECT_NOT_FOUND.
    return _storage_view(db, _get_storage(db, storage_id))


@router.put("/storages/{storage_id}")
def replace_storage(storage_id: uuid.UUID, body: StorageBody, db: Session = Depends(get_session)):
    # Replaces the storage: its profiles are deleted and re-created from the body and the addresses and suppFeat are overwritten
    # (a PUT with no addresses clears them). 404 for an unknown storage.
    storage = _get_storage(db, storage_id)
    _clear_profiles(db, storage_id)
    storage.ml_models_addresses = [_dump(a) for a in body.mlModelsAddresses] if body.mlModelsAddresses else None
    storage.supp_feat = body.suppFeat
    _add_profiles(db, storage, body.mlModels or [])
    db.commit()
    return _storage_view(db, storage)


@router.patch("/storages/{storage_id}")
def modify_storage(storage_id: uuid.UUID, body: StoragePatch, db: Session = Depends(get_session)):
    """MLModelsStoragePatch: a list that is sent replaces that list; the other is kept."""
    storage = _get_storage(db, storage_id)
    if body.mlModels is not None:
        _clear_profiles(db, storage_id)
        _add_profiles(db, storage, body.mlModels)
    if body.mlModelsAddresses is not None:
        storage.ml_models_addresses = [_dump(a) for a in body.mlModelsAddresses]
    db.commit()
    return _storage_view(db, storage)


@router.delete("/storages/{storage_id}", status_code=204)
def delete_storage(storage_id: uuid.UUID, db: Session = Depends(get_session)):
    """Profiles go with the storage; the models they named stay registered."""
    storage = db.get(MLModelsStorage, storage_id)
    if storage is not None:
        _clear_profiles(db, storage_id)
        db.delete(storage)
        db.commit()


# ---------------------------------------------------------------- discovery

def subset_match(criteria: Any, record: Any) -> bool:
    """Whole-object matching for discovery. `criteria` matches `record` when every key of a criteria dict is in the record with a
    matching value; a scalar must be equal; a criteria list matches when each of its items matches some item of the record
    list (so the record may have more); nested dicts recurse. An empty criteria dict matches everything.
    """
    if isinstance(criteria, dict):
        return isinstance(record, dict) and all(k in record and subset_match(v, record[k]) for k, v in criteria.items())
    if isinstance(criteria, list):
        return isinstance(record, list) and all(any(subset_match(c, r) for r in record) for c in criteria)
    return criteria == record


def discover(db: Session, criteria: MLModelInfo, caller: str | None, include_models: bool, limit: int, offset: int) -> dict:
    """TS 29.482 ModelInformationDiscovery over every stored model, in registration order.

    A model is a candidate only if it is not expired and the caller may access it; it matches when `criteria` is a subset of
    its MLModel record. Pagination is applied to the matches, so `offset`/`limit` count matches, not rows. 404 MODEL_NOT_FOUND
    when the requested page is empty. With `include_models` the answer carries the latest artifact of each page entry,
    base64 encoded (`indicator` true); otherwise the stored profile of each model (or a bare record when it has none).
    Loads all models and evaluates them in Python, so cost grows with the number of models.
    """
    wanted = _dump(criteria) or {}
    matches = []
    for model in db.scalars(select(MLModel).order_by(MLModel.registered_at, MLModel.model_id)).all():
        if is_expired(model) or access_denied(model, caller) is not None:
            continue
        if subset_match(wanted, model_info_view(db, model)):
            matches.append(model)
    page = matches[offset:offset + limit]
    if not page:
        raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail="no stored ML model matches the filtering criteria")
    if include_models:
        files = []
        for model in page:
            artifact = db.scalar(select(ModelArtifact).where(ModelArtifact.model_id == model.model_id)
                                 .order_by(ModelArtifact.artifact_version.desc()).limit(1))
            files.append({"mlModelId": str(model.model_id),
                          **({"mlModel": base64.b64encode(artifact.content).decode()} if artifact else {})})
        return {"mlModels": files, "indicator": True}
    profiles = []
    for model in page:
        stored = db.scalars(select(MLModelProfile).where(MLModelProfile.model_id == model.model_id)).first()
        profiles.append(_profile_view(db, stored) if stored else {"mlModelInfo": model_info_view(db, model)})
    return {"profiles": profiles, "indicator": False}
