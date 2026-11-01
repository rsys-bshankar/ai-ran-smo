"""The model registry as the other capability packages of AIMgF use it: in-process reads and the one write, in the caller's database session.

What it is: three functions over the registry's tables (`registry/models.py`), which AIMgF reached over R1 at `/mlmr/*` while the registry was a service of its
own (MLMR, until S2.1 of `docs/plan/CONVERGED_REWRITE_PLAN.md`; ADR 0010 section 3.1):

  model_json(db, id)          a model's management JSON, the body `GET /models/{id}` answers, or None (was `GET /mlmr/models/{id}`)
  coordination_groups(db)     every coordination group, as `GET /coordination-groups` lists them (was `GET /mlmr/coordination-groups`, one page)
  record_phase(db, id, body)  merges a TS 29.482 `phaseInfo` write into the model (was `PATCH /mlmr/models/{id}/phase-info`, best effort)

Where it sits: called by the ownership and lookup helpers of `common.py` and by the training and monitoring packages; it calls nothing outside this package.
The other packages call these through the module (`registry.model_json(...)`), never by importing the names, so the unit tests can stand a double in for the
registry by patching this module (`tests/test_main.py`, fixture `mlmr`).

Before editing: nothing here commits. A read sees the caller's uncommitted writes, and `record_phase` is committed, or rolled back, with the caller's own change.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mlr
from .models import MLModel, MLModelCoordinationGroup
from .routes import _model_view, coordination_group_view


def model_json(db: Session, model_id: uuid.UUID) -> dict | None:
    """The model's management JSON (`routes._model_view`: identity, metadata, the TS 29.482 and TS 28.105 attributes, `owner`, `phaseInfo`), or None when the
    registry has no model with this id."""
    model = db.get(MLModel, model_id)
    return _model_view(model) if model is not None else None


def coordination_groups(db: Session) -> list[dict]:
    """Every coordination group, oldest first by id order of the table, each as the list route renders it (`groupId`, `memberModelIds`, `retrainPropagation`, ...)."""
    return [coordination_group_view(g) for g in db.scalars(select(MLModelCoordinationGroup))]


def record_phase(db: Session, model_id: uuid.UUID, body: dict) -> None:
    """Merges `body` (`phase` and, optionally, `trainingInfo`) into the model's `phaseInfo` by the rules of `PATCH /models/{id}/phase-info` (`mlr.merge_phase_info`).

    A model the registry does not know, or a body the merge refuses, is skipped: the phase is lineage, and a training run never fails on it (SA-MLMR-7). Flushes
    nothing and does not commit.
    """
    model = db.get(MLModel, model_id)
    if model is None:
        return
    try:
        mlr.merge_phase_info(model, mlr.PhaseInfoPatch.model_validate(body))
    except Exception:  # noqa: BLE001, S110 — an unusable phase body only loses the lineage record, as the R1 write it replaces did
        pass
