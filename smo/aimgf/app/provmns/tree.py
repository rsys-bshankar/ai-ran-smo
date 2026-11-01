"""Where AIMgF's managed object instances sit in the ProvMnS tree (plan tasks S2.7, S2.10): the DN of each instance, derived from AIMgF's own rows.

    ManagedElement=AIMgF                                      ROOT: AIMgF itself; made by the projection, never by a consumer
      MLModelRepository=<id> | =default                       a registry repository; `default` holds what no repository holds
        MLModel=<id>                                          a registered model
          MLModelGovernance=<id>                              its 14-state lifecycle [D] (D-07), id = the model's
        MLModelCoordinationGroup=<id>
      MLTrainingFunction=<id> | =default                      `default` holds the requests, processes and reports that name no function
        MLTrainingRequest=<id>, MLTrainingProcess=<id>, MLTrainingReport=<id>
      MLTestingFunction=<id> | =default
        MLTestingRequest=<id>, MLTestingReport=<id>
      MLUpdateFunction=<id> | =default
        MLUpdateRequest=<id>, MLUpdateProcess=<id>, MLUpdateReport=<id>
      AIMLInferenceFunction=<id> | =default                   `default` holds the reports of an emulation function (TS 28.105 v19.5.0 names
        MLModelLoadingPolicy=<id>, MLModelLoadingRequest=<id>,  AIMLInferenceReport under AIMLInferenceEmulationFunction as an attribute,
        MLModelLoadingProcess=<id>, AIMLInferenceReport=<id>  not a contained class, so the registry does not let it contain one)
      AIMLInferenceEmulationFunction=<id>

Every id is the UUID of AIMgF's row, so an instance's DN never changes while its row lives; `default` is the one id that is not a UUID, and only
AIMgF writes it. The DN is the containment of TS 28.105 (the registry derives it from `TS28105_AiMlNrm.yaml`), with one [D] addition, the governance
instance under its model.
"""

import uuid

ROOT_CLASS = "ManagedElement"
ROOT_ID = "AIMgF"
ROOT_DN = f"{ROOT_CLASS}={ROOT_ID}"
DEFAULT_ID = "default"

FUNCTION_CLASSES = ("MLTrainingFunction", "MLTestingFunction", "MLUpdateFunction", "AIMLInferenceFunction", "AIMLInferenceEmulationFunction")
DEFAULT_CONTAINERS = ("MLModelRepository", "MLTrainingFunction", "MLTestingFunction", "MLUpdateFunction", "AIMLInferenceFunction")


def child(parent_dn: str, class_name: str, moi_id: object) -> str:
    """The DN of the instance `class_name=moi_id` under `parent_dn`."""
    return f"{parent_dn},{class_name}={moi_id}"


def container(class_name: str, row_id: uuid.UUID | None) -> str:
    """The DN of the container `class_name` with id `row_id` under ROOT, or of its `default` instance when `row_id` is None."""
    return child(ROOT_DN, class_name, row_id if row_id is not None else DEFAULT_ID)


def last_id(dn: str) -> str:
    """The id of the last RDN of `dn`."""
    return dn.split(",")[-1].partition("=")[2]


def as_uuid(value: str) -> uuid.UUID | None:
    """`value` as a UUID, or None when it is not one (the `default` instances, a DN a consumer wrote by hand)."""
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


def uuid_of(dn: object) -> uuid.UUID | None:
    """The UUID that names the instance at `dn` (its last RDN's id); None when `dn` is not a DN of a UUID-named instance."""
    if not isinstance(dn, str) or "=" not in dn:
        return None
    return as_uuid(last_id(dn))
