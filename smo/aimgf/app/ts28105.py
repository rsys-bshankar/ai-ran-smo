"""The TS 28.105 AI/ML NRM datatypes (`specs/5G_APIs/TS28105_AiMlNrm.yaml`) as pydantic models and closed enums, used to validate request bodies and to
store attribute values as spec-shaped JSON.

What it is: one class per spec datatype that AIMgF accepts or returns, keeping the spec's own camelCase attribute names (hence names such as `fLRole`),
and the closed `Literal` enums (`RequestStatus`, `MLTrainingType`, `ActivationStatus`, ...). Every model inherits `_Spec`, whose `extra="forbid"` turns an
attribute the spec does not define into a 422 instead of letting it be stored silently. `dump()` writes a model (or list of models) as plain JSON with
`None` fields left out, which is what the JSON columns of `models.py` hold, so what is read back is exactly what the spec describes.

Where it sits: the NRM files of the capability packages (`training/nrm.py`, `inference/nrm.py`, ...) use the datatypes in their request bodies and call `dump()` before storing; the run routes use `ModelPerformance`, `FLReportPerClient`,
`InferenceOutput` and `PotentialImpactInfo` in the completion and resolve bodies. Design record: HISTORY.md W4-04, `docs/STANDARDS.md` decision D-9.

Owns: validation of the datatype shapes. Does not own: any storage, any HTTP route, or the DN containment tree. Attributes of type `Dn`, `DnRo` and `DnListRo`
are carried as this build's own resource ids (UUID strings): flat resources instead of a DN tree is the one recorded deviation from the spec.

Before editing: the classes are part of the published request schemas, so a change to a field is a change to `docs/openapi/aimgf.json` (rerun
`scripts/generate_openapi_specs.py`; `tests_integration/test_openapi_specs.py` fails otherwise). That is also why the notes on the classes are `#` comments:
a docstring on a model would be published as its schema description.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequestStatus = Literal["NOT_STARTED", "IN_PROGRESS", "SUSPENDED", "FINISHED", "CANCELLED", "CANCELLING"]
MLTrainingType = Literal["INITIAL_TRAINING", "PRE_SPECIALISED_TRAINING", "RE_TRAINING", "FINE_TUNING"]
ActivationStatus = Literal["ACTIVATED", "DEACTIVATED"]
LearningTechnology = Literal["RL", "FL", "DL"]
RLEnvironment = Literal["SIMULATION_ENVIRONMENTS", "REAL_NETWORK_ENVIRONMENTS"]
FLRole = Literal["FL_SERVER", "FL_CLIENT"]
KnowledgeType = Literal["TABLE", "STATISTIC", "REGRESSION"]
# AIMLInferenceName is a oneOf over MDAType / NwdafAnalyticsType /
# NgRanInferenceType / VSExtensionType — all strings, the last
# vendor-extensible — so it's validated only as a non-empty string.
# NgRanInferenceType's own closed values, for reference:
NG_RAN_INFERENCE_TYPES = {"NG_RAN_NETWORK_ENERGY_SAVING", "NG_RAN_LOAD_BALANCING", "NG_RAN_MOBILITY_OPTIMIZATION"}


# Base of every datatype here: `extra="forbid"` so an attribute the spec does not define is a 422, not a silently stored typo.
class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def dump(self) -> dict:
        """Returns the model as plain JSON for a JSON column: nested models included, fields that are None left out."""
        return self.model_dump(exclude_none=True)


def dump(value):
    """Returns `value` ready to store in a JSON column: a `_Spec` becomes its `dump()`, a list has each `_Spec` item dumped, anything else (including None) is returned as it is.

    Callers pass optional request attributes straight through, so an absent attribute stays None.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [v.dump() if isinstance(v, _Spec) else v for v in value]
    return value.dump() if isinstance(value, _Spec) else value


# TS 28.105 ModelPerformance: one measured score of an inference output or metric. Used for performance requirements, expected gains and the training,
# validation and testing reports.
class ModelPerformance(_Spec):
    inferenceOutputName: str | None = None
    performanceMetric: str | None = None
    performanceScore: float | None = None
    decisionConfidenceScore: float | None = None


# The writable part of ProcessMonitor: what an execution runtime reports back while a run progresses (`progressPercentage` 0 to 100 is required).
class ProcessMonitorUpdate(_Spec):
    """The writable part of ProcessMonitor — what an execution runtime
    reports back as a run progresses."""
    progressPercentage: int = Field(ge=0, le=100)
    progressStateInfo: str | None = None
    resultStateInfo: str | None = None


# TS 28.105 MLKnowledge of an MLTrainingFunction: a named piece of knowledge and its type (TABLE, STATISTIC or REGRESSION). `predictorResponseArray` is a list
# of string pairs.
class MLKnowledge(_Spec):
    mLKnowledgeName: str | None = None
    knowledgeType: KnowledgeType | None = None
    predictorResponseArray: list[list[str]] | None = None


# TS 28.105 SupportedLearningTechnology of an MLTrainingFunction. `supportedFLRole` holds one or two roles; `supportedInferenceNameList` must not be empty when
# given. Stored and returned only: no engine acts on it.
class SupportedLearningTechnology(_Spec):
    learningTechnologyName: list[LearningTechnology] | None = None
    supportedRLEnvironment: list[RLEnvironment] | None = None
    supportedFLRole: list[FLRole] | None = Field(default=None, min_length=1, max_length=2)
    supportedInferenceNameList: list[str] | None = Field(default=None, min_length=1)


# TS 28.105 FLParticipationInfo of an MLTrainingFunction (federated learning role and availability). Stored and returned only: there is no distributed-training
# engine.
class FLParticipationInfo(_Spec):
    fLRole: FLRole | None = None
    isAvailableForFLTraining: bool = False
    candidateFLClientRefList: list[str] | None = None


# TS 28.105 FLClientSelectionCriteria: how federated-learning clients would be chosen. Validated and stored, not evaluated.
class FLClientSelectionCriteria(_Spec):
    minimumAvailableDataSamples: int | None = None
    minimumAvailableTimeDuration: int | None = None
    minimumInterimModelPerformance: list[ModelPerformance] | None = None
    servingGeoArea: dict | None = None
    clientRedundancy: int | None = None
    trainingDataWithOrWithoutOutliers: bool | None = None
    uniformlyDistributedTrainingData: bool | None = None


# TS 28.105 FLRequirement of an MLTrainingRequest. Validated and stored, not evaluated.
class FLRequirement(_Spec):
    fLClientSelectionCriteria: FLClientSelectionCriteria | None = None


# TS 28.105 EnvironmentScope used by RLRequirement: entities, area and time window an environment covers (area and time window are free-form objects).
class EnvironmentScope(_Spec):
    managedEntitiesScope: list[str] | None = None
    areaScope: dict | None = None
    timeWindow: dict | None = None


# TS 28.105 RLRequirement of an MLTrainingRequest. `rLEnvironmentScope` and `rLImpactedScope` must not be empty when given. Validated and stored, not evaluated.
class RLRequirement(_Spec):
    rLEnvironmentType: list[RLEnvironment] | None = None
    rLEnvironmentScope: list[EnvironmentScope] | None = Field(default=None, min_length=1)
    rLImpactedScope: list[EnvironmentScope] | None = Field(default=None, min_length=1)
    rLPerformanceRequirements: list[dict] | None = None  # TS 28.623 ThresholdInfo


# TS 28.105 DataStatisticalProperties of the training data an MLTrainingRequest declares.
class DataStatisticalProperties(_Spec):
    uniformlyDistributedTrainingData: bool | None = None
    trainingDataWithOrWithoutOutliers: bool | None = None


# TS 28.105 DistributedTrainingExpectation of an MLTrainingRequest. Validated and stored, not evaluated.
class DistributedTrainingExpectation(_Spec):
    expectedTrainingTime: int | None = None
    dataSplitIndication: bool | None = None
    suggestedTrainingNodeList: list[str] | None = None


# TS 28.105 ClusteringCriteria of an MLTrainingRequest (`clusteringInfo` is a list of these).
class ClusteringCriteria(_Spec):
    performanceMetric: str | None = None
    taskType: str | None = None
    allowedClusterTrainingTime: dict | None = None
    preferredModelDiversity: str | None = None


# TS 28.105 ManagedActivationScope, a oneOf in the spec (dNList, timeWindow or geoPolygon). The model accepts any combination of the three: it does not enforce
# that exactly one is set. Used for the scope an AIMLInferenceFunction is activated for and for a PotentialImpactInfo.
class ManagedActivationScope(_Spec):
    """oneOf in the spec (dNList | timeWindow | geoPolygon); accepted as
    any one of the three."""
    dNList: list[str] | None = None
    timeWindow: list[dict] | None = None
    geoPolygon: list[dict] | None = None


# TS 28.105 AIMLManagementPolicy: the `policyForLoading` of an MLModelLoadingPolicy. `thresholdList` holds TS 28.623 ThresholdInfo objects, stored as given and
# not evaluated by AIMgF.
class AIMLManagementPolicy(_Spec):
    thresholdList: list[dict] | None = None  # TS 28.623 ThresholdInfo
    managedActivationScope: ManagedActivationScope | None = None


# TS 28.105 AvailMLCapabilityReport of an MLUpdateFunction (capability version and expected gains); an MLUpdateReport carries the updated one.
class AvailMLCapabilityReport(_Spec):
    availMLCapabilityReportID: str | None = None
    mLCapabilityVersionId: list[str] | None = None        # an array in TS 28.105 (a single string until S2.10)
    expectedPerformanceGains: list[ModelPerformance] | None = None
    mLModelRef: list[str] | None = None                   # DnListRo


# TS 28.105 InferenceOutput of an AIMLInferenceReport. `outputResult` is an open name-to-value map; `aIMLInferenceName` is a free string because the spec's type
# is vendor-extensible.
class InferenceOutput(_Spec):
    inferenceOutputId: list[str] | None = None
    aIMLInferenceName: str | None = None
    inferenceOutputTime: list[str] | None = None
    inferencePerformance: ModelPerformance | None = None
    inferenceExplanationInfo: list[str] | None = None
    outputResult: dict | None = None  # AttributeNameValuePairSet


# TS 28.105 ImpactedPM: one performance measurement a PotentialImpactInfo names.
class ImpactedPM(_Spec):
    pMIdentifier: str | None = None


# TS 28.105 PotentialImpactInfo of an AIMLInferenceReport: the scope and measurements an inference result could affect.
class PotentialImpactInfo(_Spec):
    impactedScope: ManagedActivationScope | None = None
    impactedPM: list[ImpactedPM] | None = None


# TS 28.105 FLReportPerClient of an MLTrainingReport (one federated-learning client's contribution). Stored when supplied; AIMgF produces none itself.
class FLReportPerClient(_Spec):
    clientRef: str | None = None
    numberOfDataSamplesUsed: int | None = None
    trainingTimeDuration: int | None = None
    modelPerformanceOnClient: list[ModelPerformance] | None = None
