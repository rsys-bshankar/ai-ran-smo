"""Training data: the feature groups a training run reads (`/feature-groups`), each optionally backed by a DME data job.

What it is: create, read, list and delete of a feature group, with the data-lake credential given by reference (`tokenRef`, resolved here from the
environment or a file, SEC-15.2) and the DME data job created and terminated with the group. ADR 0010 section 3.7 replaces feature groups with the DME data
types a training request names (S2.12), so this file goes away then.

Before editing: the route docstrings and the body are published in `docs/openapi/aimgf.json`; a credential is never returned.
"""

import httpx
import re
import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Literal

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.secretfile import read_secret

from ..models import FeatureGroup
from ..common import _r1

router = APIRouter()


# Body of POST /feature-groups. The name rule (3 to 63 word characters) is checked in the route, 400 `FEATURE_GROUP_NAME_INVALID`. The data-lake credential is given as
# `tokenRef` (the name of a secret the service resolves itself, SEC-15.2) or as the deprecated clear-text `token`; exactly one of them, checked in the route so that a
# validation error never echoes a pasted secret (422 `SCHEMA_VALIDATION_FAILED`). Neither is ever returned. `enableDme` needs `dmeTypeId` (422 `FEATURE_GROUP_DME_JOB_REFUSED` otherwise) and then creates a DME
# data job delivered by `dataDeliveryMethod`; `dmePort` is stored and not used by the job.
class CreateFeatureGroupRequest(BaseModel):
    featureGroupName: str
    featureList: str
    datalakeSource: str
    host: str
    port: str
    bucket: str
    # Deprecated (SEC-15.2): a clear-text credential, accepted for one more minor release and never returned. Use `tokenRef`.
    token: str | None = Field(default=None, json_schema_extra={"deprecated": True},
                              description="Deprecated: the data-lake token in clear text. Stored, never returned; send `tokenRef` instead. Exactly one of `token` and `tokenRef`.")
    # The name of the secret that holds the data-lake token (lower-case letters, digits, '-' and '_', starting with a letter), resolved by the service from
    # AIMGF_FEATURE_GROUP_TOKEN_<NAME> or its _FILE when it connects to the data lake.
    tokenRef: str | None = Field(default=None, description="The name of the secret that holds the data-lake token, lower-case letters, digits, '-' and '_', starting with a letter. "
                                 "The service reads the secret itself; the name is shown on read, the token never is.")
    dbOrg: str
    measurement: str
    enableDme: bool = False
    measuredObjClass: str | None = None
    dmePort: str | None = None
    sourceName: str | None = None
    # OI-5-aiml-featuregroup-dme: required when enableDme — the DME type the
    # group's data job collects, and how the trainer gets the data.
    dmeTypeId: uuid.UUID | None = None
    dataDeliveryMethod: Literal["PULL_HTTP", "PUSH_HTTP", "STREAMING_KAFKA"] = "PULL_HTTP"


@router.post("/feature-groups", status_code=201)
def create_feature_group(body: CreateFeatureGroupRequest, response: Response, db: Session = Depends(get_session)):
    """HISTORY.md §5: no feature-group/feature-store concept
    existed at all. Matches the reference's own
    CreateFeatureGroup (featuregroup_controller.py): name must be
    `\\w+` (word characters only) and 3-63 characters long, and a
    duplicate `featureGroupName` 409s, matching `DBException`
    ("already exist") there.

    OI-5-aiml-featuregroup-dme: with `enableDme`, the group's DME data job
    is created first, like the reference's create_dme_filtered_data_job —
    a CONTINUOUS TRAINING-stage job of `dmeTypeId`, consumer
    `aimgf:feature-group:<name>`, whose production job definition carries
    the group's features and filters. If DME refuses it (an unknown type, a
    definition its schema rejects, a delivery method no offer commits to)
    the group is not created: 422 FEATURE_GROUP_DME_JOB_REFUSED with DME's
    reason. `dmeTypeId` is required with `enableDme`.

    The data-lake credential is `tokenRef`, the name of a secret the service resolves itself, or the deprecated clear-text `token` (one of the two, else 422). The
    answer never contains the token: it shows `tokenSet` and, for a reference, `tokenRef`. A call that sends `token` is answered with a `Deprecation: true` header
    and a `warnings` entry, and the field is dropped in a later minor release.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class c): `featureGroupName` is unique, so a repeat is refused 409 FEATURE_GROUP_ALREADY_REGISTERED and creates no
    # second group.
    # Route notes. 201 with the group, without its token (`tokenSet`, `tokenRef`). Checks in order: the credential (422 `SCHEMA_VALIDATION_FAILED`: both or neither of
    # `token` and `tokenRef`, or a `tokenRef` that is not a secret name; the message never repeats the value), the name pattern and length (400 `FEATURE_GROUP_NAME_INVALID`), a
    # group of that name already exists (409 `FEATURE_GROUP_ALREADY_REGISTERED`), `enableDme` without `dmeTypeId` (422 `FEATURE_GROUP_DME_JOB_REFUSED`), then
    # the DME data job (422 with DME's reason when DME refuses it), then the insert. If the insert loses a race on the unique name, the session is rolled back,
    # the DME job just created is terminated and the answer is the same 409.
    # Checked here and not in the model so that the 422 never echoes a pasted secret (a pydantic error carries the input).
    if (body.token is None) == (body.tokenRef is None):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="give exactly one of tokenRef (the name of a secret) and the deprecated token")
    if body.tokenRef is not None and not TOKEN_REF.fullmatch(body.tokenRef):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="tokenRef must be a secret name (lower-case letters, digits, '-' and '_', starting with a letter), "
                                                                          "never the token itself")
    if not re.fullmatch(r"\w+", body.featureGroupName) or not (3 <= len(body.featureGroupName) <= 63):
        raise framework_error(FrameworkError.FEATURE_GROUP_NAME_INVALID, detail=f"featureGroupName {body.featureGroupName!r} must be 3-63 word characters")
    if db.scalar(select(FeatureGroup).where(FeatureGroup.feature_group_name == body.featureGroupName)) is not None:
        raise framework_error(FrameworkError.FEATURE_GROUP_ALREADY_REGISTERED, detail=f"feature group {body.featureGroupName!r} already exists")
    if body.enableDme and body.dmeTypeId is None:
        raise framework_error(FrameworkError.FEATURE_GROUP_DME_JOB_REFUSED, detail="enableDme needs the dmeTypeId the group's data job collects")
    # The DME job is created before the group row, so a refusal by DME means no group is stored; if the insert then fails, the job is terminated below.
    data_job_id = _create_feature_group_data_job(body) if body.enableDme else None
    group = FeatureGroup(
        feature_group_name=body.featureGroupName, feature_list=body.featureList, datalake_source=body.datalakeSource,
        host=body.host, port=body.port, bucket=body.bucket, token=body.token, token_ref=body.tokenRef, db_org=body.dbOrg,
        measurement=body.measurement, enable_dme=body.enableDme, measured_obj_class=body.measuredObjClass,
        dme_port=body.dmePort, source_name=body.sourceName,
        dme_type_id=body.dmeTypeId if body.enableDme else None, dme_data_job_id=data_job_id,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _terminate_feature_group_data_job(data_job_id)  # lost a race on the name: don't leave the job behind
        raise framework_error(FrameworkError.FEATURE_GROUP_ALREADY_REGISTERED, detail=f"feature group {body.featureGroupName!r} already exists") from exc
    view = _feature_group_view(group)
    if body.token is not None:
        # RELEASES.md deprecation policy: the field still works for one more minor release, and the answer says so, in the header and in the body.
        response.headers["Deprecation"] = "true"
        view["warnings"] = ["token is deprecated and is dropped in a later release: register a tokenRef, the name of a secret the service resolves itself"]
    return view


def _create_feature_group_data_job(body: CreateFeatureGroupRequest) -> uuid.UUID:
    """Creates the DME data job for an `enableDme` feature group and returns its id (`POST /dme/data-jobs`).

    The job is CONTINUOUS, lifecycle stage TRAINING, consumer `aimgf:feature-group:<name>`, delivered by `body.dataDeliveryMethod`. Its production definition carries the group name, the features
    (`featureList` split on commas, blanks dropped) and `measuredObjClass`, `sourceName` and `measurement` when they are non-empty. Raises 422 `FEATURE_GROUP_DME_JOB_REFUSED` with DME's reason when
    DME answers 300 or above; a transport failure raises (500). HISTORY.md OI-5-aiml-featuregroup-dme.
    """
    definition = {"featureGroupName": body.featureGroupName,
                  "features": [f.strip() for f in body.featureList.split(",") if f.strip()]}
    definition.update({k: v for k, v in (("measuredObjClass", body.measuredObjClass), ("sourceName", body.sourceName),
                                         ("measurement", body.measurement)) if v})
    resp = _r1.post("/dme/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": str(body.dmeTypeId), "productionJobDefinition": definition,
        "dataDeliveryMethod": body.dataDeliveryMethod, "deliveryDetails": {}, "lifecycleStage": "TRAINING",
        "consumerId": f"aimgf:feature-group:{body.featureGroupName}",
    })
    # DME's reason is in `detail`, either a string or a problem object; both are folded into one message, and an answer that is not JSON still gives a refusal.
    if resp.status_code >= 300:
        try:
            reason = resp.json().get("detail")
        except ValueError:
            reason = None
        if isinstance(reason, dict):
            reason = f"{reason.get('title')}: {reason.get('detail')}"
        raise framework_error(FrameworkError.FEATURE_GROUP_DME_JOB_REFUSED,
                              detail=f"DME refused the data job ({resp.status_code}): {reason or 'no reason given'}")
    return uuid.UUID(resp.json()["dataJobId"])


def _terminate_feature_group_data_job(data_job_id: uuid.UUID | None) -> str:
    """Terminates a feature group's DME data job and returns the outcome: `SKIPPED` (no job), `DONE` (deleted, or DME already had no such job: 404 counts as done) or `FAILED: <reason>`.

    Best effort, like every teardown here: an HTTP error is reported in the outcome and never raised. Calls `DELETE /dme/data-jobs/{id}` over R1.
    """
    if data_job_id is None:
        return "SKIPPED"
    try:
        resp = _r1.delete(f"/dme/data-jobs/{data_job_id}")
    except httpx.HTTPError as exc:
        return f"FAILED: {exc.__class__.__name__}"
    return "DONE" if resp.status_code < 300 or resp.status_code == 404 else f"FAILED: HTTP {resp.status_code}"


@router.get("/feature-groups/{feature_group_name}")
def get_feature_group(feature_group_name: str, db: Session = Depends(get_session)):
    # Route notes: 404 `FEATURE_GROUP_NOT_FOUND`; the answer never includes the token (`tokenSet`, `tokenRef`).
    return _feature_group_view(_feature_group_or_404(db, feature_group_name))


@router.delete("/feature-groups/{feature_group_name}")
def delete_feature_group(feature_group_name: str, db: Session = Depends(get_session)):
    """DeleteFeatureGroup — and, for an enable_dme group, terminates its DME
    data job (best effort; the outcome is in `dmeDataJobTeardown`)."""
    # Route notes: 404 `FEATURE_GROUP_NOT_FOUND`. The DME data job is terminated before the group row is deleted, and the outcome is reported but does not stop
    # the delete: the group is removed even when the teardown FAILED. Answers 200 with the name and the teardown outcome (not 204).
    group = _feature_group_or_404(db, feature_group_name)
    teardown = _terminate_feature_group_data_job(group.dme_data_job_id)
    db.delete(group)
    db.commit()
    return {"featureGroupName": feature_group_name, "dmeDataJobTeardown": teardown}


TOKEN_REF = re.compile(r"[a-z][a-z0-9_-]{0,62}")  # a secret's name, not its value: the same shape as ran-nf-oam's credentialRef


def feature_group_token(group: FeatureGroup) -> str | None:
    """Returns the data-lake token of a feature group, for the code that connects to the data lake (none does yet: AIMgF stores the group and does not read the lake).

    A group with a `token_ref` has it resolved from this service's own environment, `AIMGF_FEATURE_GROUP_TOKEN_<REF>` or the file named by its `_FILE` (`smo_shared.secretfile`;
    the reference upper-cased, `-` as `_`); a reference that resolves to nothing is None, never a fall-back to another secret. A group from before the reference
    existed returns its stored clear-text token. Raises `SecretConflict` or `SecretFileError` as `read_secret` does, naming the variable and never the value.
    """
    if group.token_ref:
        # config-ref: AIMGF_FEATURE_GROUP_TOKEN_<REF>, AIMGF_FEATURE_GROUP_TOKEN_<REF>_FILE
        return read_secret("AIMGF_FEATURE_GROUP_TOKEN_" + group.token_ref.upper().replace("-", "_"))
    return group.token or None


def _feature_group_or_404(db: Session, name: str) -> FeatureGroup:
    """Returns the feature group with that name or raises 404 `FEATURE_GROUP_NOT_FOUND`."""
    group = db.scalar(select(FeatureGroup).where(FeatureGroup.feature_group_name == name))
    if group is None:
        raise framework_error(FrameworkError.FEATURE_GROUP_NOT_FOUND, detail=f"no feature group {name!r}")
    return group


@router.get("/feature-groups")
def list_feature_groups(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; a group is listed without its token (`tokenSet`, `tokenRef`).
    page = paginate(db, select(FeatureGroup), limit, offset)
    return {**page, "items": [_feature_group_view(g) for g in page["items"]]}


def _feature_group_view(g: FeatureGroup) -> dict:
    """Returns the feature group as the dict the feature-group routes answer. The token is never included: `tokenSet` says whether the group has a credential and `tokenRef`
    names the secret (a name, not a secret), or is null for a group that carries a clear-text token or none."""
    return {
        "featureGroupId": str(g.feature_group_id), "featureGroupName": g.feature_group_name,
        "featureList": g.feature_list, "datalakeSource": g.datalake_source, "host": g.host, "port": g.port,
        "bucket": g.bucket, "tokenSet": bool(g.token or g.token_ref), "tokenRef": g.token_ref, "dbOrg": g.db_org, "measurement": g.measurement,
        "enableDme": g.enable_dme, "measuredObjClass": g.measured_obj_class, "dmePort": g.dme_port,
        "sourceName": g.source_name,
        "dmeTypeId": str(g.dme_type_id) if g.dme_type_id else None,
        "dmeDataJobId": str(g.dme_data_job_id) if g.dme_data_job_id else None,
    }
