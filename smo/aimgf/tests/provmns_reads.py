"""Test helpers for reading and writing AIMgF's TS 28.105 instances over ProvMnS (`app/provmns/`), for the suites whose REST routes S2.7 and S2.10
replaced with the classes (the function, report and loading-process reads, the coordination-group view, `nrm-refs`).

  root(db_session_factory)                         makes ManagedElement=AIMgF, as start-up does (a TestClient runs no lifespan)
  create(client, db_session_factory, cls, attrs)   createMOI of a function under ROOT; returns its UUID as a string
  instance(client, cls, moi_id)                    the instance of class `cls` with id `moi_id`, wherever it is in the tree; find(...) its attributes
  ids(value)                                       the ids a DN, or a list of DNs, names (their last RDN)
  register(db_session_factory, model_id, **cols)   a registry row for a model the registry double holds, so references to it resolve
"""

import uuid

from smo_shared.mns import PROVMNS_VERSION

from app.provmns import projection
from app.provmns.tree import ROOT_DN
from app.registry import models as rm

BASE = f"/ProvMnS/{PROVMNS_VERSION}/"
FLAT = {"accept": "application/vnd.3gpp.object-tree-flat+json"}


def root(db_session_factory) -> None:
    """Makes ROOT when it is not there."""
    with db_session_factory() as db:
        projection.ensure_container(db, ROOT_DN)
        db.commit()


def create(client, db_session_factory, class_name: str, attributes: dict) -> str:
    """createMOI of a `class_name` under ROOT with `attributes`; asserts 201 and returns the new id."""
    root(db_session_factory)
    moi_id = str(uuid.uuid4())
    response = client.put(BASE + f"{ROOT_DN}/{class_name}={moi_id}".replace(",", "/"), json={"id": moi_id, "attributes": attributes})
    assert response.status_code == 201, response.text
    return moi_id


def find(client, class_name: str, moi_id: object) -> dict:
    """The attributes of the one `class_name` instance whose id is `moi_id`."""
    return instance(client, class_name, moi_id)["attributes"]


def instance(client, class_name: str, moi_id: object) -> dict:
    """The one `class_name` instance whose id is `moi_id`, wherever it is (a flat read of ROOT's subtree, filtered)."""
    response = client.get(BASE + ROOT_DN, headers=FLAT, params={"scope": '{"scopeType": "BASE_ALL"}',
                                                                "filter": f"@.objectClass == '{class_name}' && @.id == '{moi_id}'"})
    assert response.status_code == 200, response.text
    found = response.json()
    assert len(found) == 1, found
    return found[0]


def ids(value):
    """The id the DN `value` names, or the ids of a list of DNs (None stays None)."""
    if value is None:
        return None
    if isinstance(value, list):
        return [ids(item) for item in value]
    return value.rsplit("=", 1)[1]


def register(db_session_factory, model_id, **columns) -> None:
    """A registry row for `model_id` (the registry double of `test_main.py` holds no row, so references to the model would be left out)."""
    with db_session_factory() as db:
        db.add(rm.MLModel(model_id=model_id, registration_id=f"r-{model_id}", model_type=f"t-{model_id}", version="1", **columns))
        db.commit()
