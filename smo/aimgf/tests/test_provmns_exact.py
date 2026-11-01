"""Exact behaviour of AIMgF's ProvMnS hooks and projection (`app/provmns/`), written to the mutation pass of plan task S2.10: each refusal's status,
code, reason and the attributes and objects it names; every attribute a request's createMOI passes to the run it starts; the flags and the owner
rule of each request class; the registry, producer and start-up `service.py` builds; the projection's containers, moves and dependants.

Most tests call a hook directly (`HANDLERS[class]`) with a `MoiContext` on the test database, which also reaches attributes the class's schema
check would refuse before the hook (TS 28.105 v19.5.0's `oneOf` defects, `aimgf/README.md` 1.2). Fixtures come from `test_main.py`.
"""

import uuid

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from provmns_reads import BASE, register, root

from smo_shared.mns import Moi, MoiContext, MnsError
from smo_shared.mns.store import ManagedObject

from app import models as m
from app.governance.statemachine import ModelLifecycleState
from app.provmns import handlers, projection, service, tree
from app.provmns.handlers import HANDLERS, current_request
from app.provmns.tree import ROOT_DN

RAPP_B = {"x-r1-role": "rapp", "x-r1-invoker-id": "rapp-b"}
MERGE = {"content-type": "application/merge-patch+json"}


class _Request:
    """A request as the hooks read it: headers only."""

    def __init__(self, headers: dict):
        self.headers = Headers(headers=headers)


def _moi(dn: str, attributes: dict | None = None) -> Moi:
    class_name, moi_id = dn.rsplit(",", 1)[-1].split("=", 1)
    return Moi(dn, class_name, moi_id, attributes or {}, 0)


def _refusal(call) -> tuple:
    """(status, type, reason, badObjects, badAttributes) of the MnsError `call` raises."""
    with pytest.raises(MnsError) as caught:
        call()
    e = caught.value
    return e.status, e.type, e.reason, e.bad_objects, e.bad_attributes


def _ctx(db: Session, dn: str, operation: str = "createMOI") -> MoiContext:
    return MoiContext(operation, db, dn)


def _function(db: Session, table, **columns):
    row = table(**columns)
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------- the helpers' answers

def test_a_route_refusal_keeps_its_status_and_names_its_code_and_text():
    """`_domain`: the ProblemDetails title and detail joined in `reason`, the status kept, the DN named; a plain-string detail, and a detail
    without a title, keep what they have."""
    exc = HTTPException(409, detail={"title": "LIFECYCLE_ILLEGAL_TRANSITION", "detail": "not now"})
    e = handlers._domain(exc, "X=1")
    assert (e.status, e.type, e.reason, e.bad_objects, e.bad_attributes) == (409, "VETOED", "LIFECYCLE_ILLEGAL_TRANSITION: not now", ["X=1"], None)
    assert handlers._domain(HTTPException(404, detail="gone"), "X=1").reason == "gone"
    assert handlers._domain(HTTPException(403, detail={"code": "C"}), "X=1").reason == "C"


def test_an_id_and_references_that_are_not_aimgf_instances_are_refused():
    """`_uuid_id`, `_ref` and `_refs`: 422 VETOED with the exact reason and the attribute named; an unset reference is None, an unset list
    empty, and a DN of a UUID-named instance gives its UUID."""
    model = uuid.uuid4()
    assert _refusal(lambda: handlers._uuid_id(_moi("A=1,MLTestingFunction=mine"))) == (
        422, "VETOED", "AIMgF names its MLTestingFunction instances by UUID; 'mine' is not one", ["A=1,MLTestingFunction=mine"], None)
    moi = _moi("A=1,X=2", {"r": "x", "l": [f"M={model}", "y"], "ok": f"P=1,M={model}"})
    assert _refusal(lambda: handlers._ref(moi, "r")) == (422, "VETOED", "r must be the DN of an AIMgF instance, not 'x'", ["A=1,X=2"], ["r"])
    assert _refusal(lambda: handlers._refs(moi, "l")) == (422, "VETOED", "l must list DNs of AIMgF instances, not 'y'", ["A=1,X=2"], ["l"])
    assert (handlers._ref(moi, "ok"), handlers._ref(moi, "none"), handlers._refs(moi, "none")) == (model, None, [])
    assert handlers._refs(_moi("A=1", {"l": [f"M={model}", f"N={model}"]}), "l") == [model, model]


def test_only_names_every_attribute_it_does_not_take_sorted():
    """`_only`: 422 VETOED naming the extra attributes in order, with the caller's words first."""
    assert _refusal(lambda: handlers._only(_moi("A=1", {"z": 1, "a": 2, "k": 3}), {"k"}, "a new X")) == (
        422, "VETOED", "a new X: AIMgF does not take a, z", ["A=1"], ["a", "z"])
    handlers._only(_moi("A=1", {"k": 1}), {"k", "j"}, "x")                     # nothing extra: no refusal


def test_changed_names_what_differs_both_ways():
    """`_changed`: an attribute added, removed or with another value; not one that is equal."""
    old, new = _moi("A=1", {"a": 1, "b": 2, "c": 3}), _moi("A=1", {"a": 1, "b": 5, "d": 4})
    assert handlers._changed(old, new) == {"b", "c", "d"}


def test_a_hook_outside_http_judges_a_caller_with_no_role(db_session_factory):
    """`_request`: the request `current_request` holds, or one with no headers (no role, which the owner rule trusts)."""
    assert handlers._request().headers == {}
    token = current_request.set(_Request({"x": "y"}))
    try:
        assert dict(handlers._request().headers) == {"x": "y"}
        assert handlers._caller_name() == "provmns"
    finally:
        current_request.reset(token)
    token = current_request.set(_Request({"x-r1-invoker-id": "rapp-7"}))
    try:
        assert handlers._caller_name() == "rapp-7"
    finally:
        current_request.reset(token)


# ---------------------------------------------------------------------------- read-only classes and ROOT

@pytest.mark.parametrize("name", ["MLModel", "MLModelRepository", "MLModelCoordinationGroup", "MLModelGovernance", "MLTrainingReport",
                                  "MLTestingReport", "MLUpdateProcess", "MLUpdateReport", "MLModelLoadingProcess"])
def test_each_read_only_class_refuses_with_where_it_is_written(name, db_session_factory):
    """Create, modify and a delete of the instance itself are 409 VETOED naming the class and its `why`; a delete of its container passes and
    queues its row to be projected again."""
    handler = HANDLERS[name]
    dn = f"{ROOT_DN},X=1,{name}={uuid.uuid4()}"
    reason = f"{name} is read only over ProvMnS: {handler.why}"
    with db_session_factory() as db:
        assert _refusal(lambda: handler.before_create(_ctx(db, dn), _moi(dn))) == (409, "VETOED", reason, [dn], None)
        assert _refusal(lambda: handler.before_modify(_ctx(db, dn), _moi(dn), _moi(dn))) == (409, "VETOED", reason, [dn], None)
        assert _refusal(lambda: handler.before_delete(_ctx(db, dn, "deleteMOI"), _moi(dn))) == (409, "VETOED", reason, [dn], None)
        handler.before_delete(_ctx(db, f"{ROOT_DN},X=1", "deleteMOI"), _moi(dn))
        assert (projection.BY_CLASS[name].table, uuid.UUID(dn.rsplit("=", 1)[1])) in db.info[projection._PENDING]


def test_the_reasons_name_where_each_read_only_class_is_written():
    """Each `why`, word for word (the console and an operator read them)."""
    assert HANDLERS["MLModel"].why == handlers._REGISTRY_WHY == (
        "models, repositories and coordination groups are registered through /aimgf/models and its routes until S2.11")
    assert HANDLERS["MLModelGovernance"].why == "governance decisions are POST /aimgf/models/{id}/advance by a console user (D-07) until S2.11"
    assert HANDLERS["MLTrainingReport"].why == HANDLERS["MLTestingReport"].why == "AIMgF writes a report when its run ends"
    assert HANDLERS["MLUpdateProcess"].why == "AIMgF makes a process when its request starts; cancel or suspend the MLUpdateRequest"
    assert HANDLERS["MLUpdateReport"].why == "AIMgF writes a report when its update ends"
    assert HANDLERS["MLModelLoadingProcess"].why == "AIMgF makes a process when a request or policy loads models"
    assert HANDLERS["MLTrainingProcess"].why == "AIMgF makes a process when its request starts"
    assert HANDLERS["AIMLInferenceReport"].why == "a report is not changed or deleted"
    assert HANDLERS["ManagedElement"].why == "it is AIMgF itself"


def test_root_is_never_deleted_and_a_contained_delete_of_an_unnamed_instance_queues_nothing(db_session_factory):
    """ManagedElement: delete is 409 naming the DN, whatever the target; a contained instance whose id is not a UUID queues nothing."""
    with db_session_factory() as db:
        assert _refusal(lambda: HANDLERS["ManagedElement"].before_delete(_ctx(db, "SubNetwork=1", "deleteMOI"), _moi(ROOT_DN))) == (
            409, "VETOED", f"{ROOT_DN} is AIMgF itself and is not deleted", [ROOT_DN], None)
        assert handlers._contained_delete(_ctx(db, ROOT_DN, "deleteMOI"), _moi(f"{ROOT_DN},MLTrainingFunction=default")) is True
        assert projection._PENDING not in db.info


# ---------------------------------------------------------------------------- functions

def test_a_function_is_created_only_under_root_and_a_policy_only_under_its_function(db_session_factory):
    """A function elsewhere than ROOT, and a loading policy whose container is not UUID-named, are 422 VETOED with their reasons; an attribute a
    function does not take is named."""
    with db_session_factory() as db:
        dn = f"SubNetwork=1,MLTestingFunction={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLTestingFunction"].before_create(_ctx(db, dn), _moi(dn))) == (
            422, "VETOED", f"AIMgF's MLTestingFunction instances are created under {ROOT_DN}", [dn], None)
        dn = f"{ROOT_DN},AIMLInferenceFunction=default,MLModelLoadingPolicy={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLModelLoadingPolicy"].before_create(_ctx(db, dn), _moi(dn))) == (
            422, "VETOED", "a MLModelLoadingPolicy is created under one of AIMgF's functions", [dn], None)
        dn = f"{ROOT_DN},MLTestingFunction={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLTestingFunction"].before_create(_ctx(db, dn), _moi(dn, {"vnfParametersList": []}))) == (
            422, "VETOED", "a new MLTestingFunction: AIMgF does not take vnfParametersList", [dn], ["vnfParametersList"])


def test_every_function_column_is_written_from_its_attribute(db_session_factory):
    """Each function class's columns take their attributes on create (references as row ids), an inference function starts DEACTIVATED with no
    models, and a modify writes every column again (one left out goes back to its default) and refuses a read-only attribute's change."""
    repository, model = uuid.uuid4(), uuid.uuid4()
    with db_session_factory() as db:
        dn = f"{ROOT_DN},MLTrainingFunction={uuid.uuid4()}"
        attributes = {"userLabel": "t", "supportedLearningTechnology": {"learningTechnologyName": ["DL"]}, "fLParticipationInfo": {"fLRole": "FL_SERVER"},
                      "mLKnowledge": {"mLKnowledgeName": "k"}, "mLModelRepositoryRef": f"{ROOT_DN},MLModelRepository={repository}"}
        HANDLERS["MLTrainingFunction"].before_create(_ctx(db, dn), _moi(dn, attributes))
        f = db.get(m.MLTrainingFunction, uuid.UUID(dn.rsplit("=", 1)[1]))
        assert (f.user_label, f.supported_learning_technology, f.fl_participation_info, f.ml_knowledge, f.ml_model_repository_ref) == (
            "t", {"learningTechnologyName": ["DL"]}, {"fLRole": "FL_SERVER"}, {"mLKnowledgeName": "k"}, repository)
        old, new = _moi(dn, {**attributes, "mLTrainingType": "RE_TRAINING"}), _moi(dn, {"userLabel": "u", "mLTrainingType": "RE_TRAINING"})
        HANDLERS["MLTrainingFunction"].before_modify(_ctx(db, dn, "modifyMOIAttributes"), old, new)
        assert (f.user_label, f.ml_knowledge, f.ml_model_repository_ref) == ("u", None, None)
        assert _refusal(lambda: HANDLERS["MLTrainingFunction"].before_modify(_ctx(db, dn), new, _moi(dn, {"userLabel": "u"}))) == (
            422, "VETOED", "MLTrainingFunction: AIMgF does not take mLTrainingType", [dn], ["mLTrainingType"])

        dn = f"{ROOT_DN},AIMLInferenceFunction={uuid.uuid4()}"
        HANDLERS["AIMLInferenceFunction"].before_create(_ctx(db, dn), _moi(dn, {"managedActivationScope": {"dNList": ["c"]}}))
        f = db.get(m.AIMLInferenceFunction, uuid.UUID(dn.rsplit("=", 1)[1]))
        assert (f.activation_status, f.ml_model_refs, f.managed_activation_scope) == ("DEACTIVATED", [], {"dNList": ["c"]})
        HANDLERS["AIMLInferenceFunction"].before_modify(_ctx(db, dn), _moi(dn, {"activationStatus": "ACTIVATED"}), _moi(dn, {"userLabel": "x"}))
        assert (f.activation_status, f.user_label, f.managed_activation_scope) == ("DEACTIVATED", "x", None)

        dn = f"{ROOT_DN},MLUpdateFunction={uuid.uuid4()}"
        HANDLERS["MLUpdateFunction"].before_create(_ctx(db, dn), _moi(dn, {"availMLCapabilityReport": {"mLCapabilityVersionId": ["v"]},
                                                                          "mLModelRef": [f"M={model}"]}))
        f = db.get(m.MLUpdateFunction, uuid.UUID(dn.rsplit("=", 1)[1]))
        assert (f.avail_ml_capability_report, f.ml_model_refs) == ({"mLCapabilityVersionId": ["v"]}, [str(model)])

        function = _function(db, m.AIMLInferenceFunction, activation_status="DEACTIVATED", ml_model_refs=[])
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},MLModelLoadingPolicy={uuid.uuid4()}"
        HANDLERS["MLModelLoadingPolicy"].before_create(_ctx(db, dn), _moi(dn, {"aIMLInferenceName": "n", "policyForLoading": {"p": 1},
                                                                              "mLModelRef": [f"M={model}"]}))
        p = db.get(m.MLModelLoadingPolicy, uuid.UUID(dn.rsplit("=", 1)[1]))
        assert (p.aiml_inference_function_id, p.aiml_inference_name, p.policy_for_loading, p.ml_model_refs) == (
            function.aiml_inference_function_id, "n", {"p": 1}, [str(model)])


def test_a_function_modify_or_delete_without_its_row(db_session_factory):
    """A modify of an instance whose row is gone is 404 VETOED; a delete of one is nothing (the row is not there to delete); a delete of the
    instance itself deletes the row."""
    with db_session_factory() as db:
        dn = f"{ROOT_DN},MLTestingFunction={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLTestingFunction"].before_modify(_ctx(db, dn), _moi(dn), _moi(dn, {"userLabel": "x"}))) == (
            404, "VETOED", f"{dn} has no row in AIMgF", [dn], None)
        HANDLERS["MLTestingFunction"].before_delete(_ctx(db, dn, "deleteMOI"), _moi(dn))
        row = _function(db, m.MLTestingFunction, user_label="x")
        dn = f"{ROOT_DN},MLTestingFunction={row.ml_testing_function_id}"
        HANDLERS["MLTestingFunction"].before_delete(_ctx(db, f"{ROOT_DN}", "deleteMOI"), _moi(dn))      # its container's delete: kept
        assert db.get(m.MLTestingFunction, row.ml_testing_function_id) is not None
        HANDLERS["MLTestingFunction"].before_delete(_ctx(db, dn, "deleteMOI"), _moi(dn))
        assert db.get(m.MLTestingFunction, row.ml_testing_function_id) is None


# ---------------------------------------------------------------------------- requests

def test_a_training_request_passes_every_attribute_to_its_run(db_session_factory, mlmr):
    """Each TS 28.105 attribute of an MLTrainingRequest lands on its TrainingJob's column, the run is named by the instance's id and placed in its
    function, its source is the attribute's (else the caller); the two-target rule is 422 VETOED with its reason."""
    model = mlmr.add_model()
    attributes = {"aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING", "fLRequirement": {"f": 1}, "candidateTrainingDataSource": ["PRB"],
                  "trainingDataQualityScore": 0.5, "trainingRequestSource": "es", "performanceRequirements": [{"performanceMetric": "MAE"}],
                  "rLRequirement": {"r": 1}, "trainingDataStatisticalProperties": {"s": 1}, "distributedTrainingExpectation": {"d": 1},
                  "mLKnowledgeName": "k", "mLTrainingType": "PRE_SPECIALISED_TRAINING", "expectedInferenceScope": ["x"], "clusteringInfo": [{"c": 1}],
                  "mLModelRef": f"{ROOT_DN},MLModelRepository=default,MLModel={model}"}
    with db_session_factory() as db:
        function = _function(db, m.MLTrainingFunction)
        key = uuid.uuid4()
        dn = f"{ROOT_DN},MLTrainingFunction={function.ml_training_function_id},MLTrainingRequest={key}"
        HANDLERS["MLTrainingRequest"].before_create(_ctx(db, dn), _moi(dn, attributes))
        j = db.get(m.TrainingJob, key)
        assert (j.model_id, j.model_coordination_group_id, j.ml_training_function_id, j.producer_id, j.training_request_source) == (
            model, None, function.ml_training_function_id, "es", "es")
        assert (j.aiml_inference_name, j.fl_requirement, j.candidate_training_data_source, j.training_data_quality_score) == (
            "NG_RAN_NETWORK_ENERGY_SAVING", {"f": 1}, ["PRB"], 0.5)
        assert (j.performance_requirements, j.rl_requirement, j.training_data_statistical_properties, j.distributed_training_expectation) == (
            [{"performanceMetric": "MAE"}], {"r": 1}, {"s": 1}, {"d": 1})
        assert (j.ml_knowledge_name, j.ml_training_type, j.expected_inference_scope, j.clustering_info, j.dme_data_job_ids) == (
            "k", "PRE_SPECIALISED_TRAINING", ["x"], [{"c": 1}], [])
        assert j.status == "IN_PROGRESS"

        group = uuid.uuid4()
        key = uuid.uuid4()
        dn = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingRequest={key}"
        HANDLERS["MLTrainingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelCoordinationGroupRef": f"G={group}"}))
        j = db.get(m.TrainingJob, key)
        assert (j.model_id, j.model_coordination_group_id, j.ml_training_function_id, j.producer_id, j.candidate_training_data_source) == (
            None, group, None, "provmns", [])
        for targets in ({}, {"mLModelRef": f"M={model}", "mLModelCoordinationGroupRef": f"G={group}"}):
            dn = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingRequest={uuid.uuid4()}"
            assert _refusal(lambda dn=dn, targets=targets: HANDLERS["MLTrainingRequest"].before_create(_ctx(db, dn), _moi(dn, targets))) == (
                422, "VETOED", "COORDINATION_GROUP_MISMATCH: exactly one of mLModelRef and mLModelCoordinationGroupRef", [dn],
                ["mLModelRef", "mLModelCoordinationGroupRef"])


def test_a_request_is_kept_under_its_own_function_class_and_starts_no_flag(db_session_factory, mlmr):
    """A request under another function class, or at the top, is 422 VETOED naming where AIMgF keeps it; a new training request with a flag is
    an attribute it does not take; a loading request may start suspended."""
    with db_session_factory() as db:
        for dn in (f"{ROOT_DN},MLTestingFunction=default,MLTrainingRequest={uuid.uuid4()}", f"MLTrainingRequest={uuid.uuid4()}"):
            assert _refusal(lambda dn=dn: HANDLERS["MLTrainingRequest"].before_create(_ctx(db, dn), _moi(dn))) == (
                422, "VETOED", "AIMgF keeps a MLTrainingRequest under its MLTrainingFunction", [dn], None)
        dn = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingRequest={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLTrainingRequest"].before_create(_ctx(db, dn), _moi(dn, {"cancelRequest": False}))) == (
            422, "VETOED", "a new MLTrainingRequest: AIMgF does not take cancelRequest", [dn], ["cancelRequest"])
        dn = f"{ROOT_DN},AIMLInferenceFunction=default,MLModelLoadingRequest={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLModelLoadingRequest"].before_create(_ctx(db, dn), _moi(dn))) == (
            422, "VETOED", "an MLModelLoadingRequest is created under the AIMLInferenceFunction it loads onto", [dn], None)
        model = mlmr.add_model()
        _set_lifecycle(db_session_factory, model, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
        function = _function(db, m.AIMLInferenceFunction, activation_status="DEACTIVATED", ml_model_refs=[])
        key = uuid.uuid4()
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},MLModelLoadingRequest={key}"
        HANDLERS["MLModelLoadingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelToLoadRef": [f"M={model}"], "suspendRequest": True}))
        r = db.get(m.MLModelLoadingRequest, key)
        assert (r.request_status, r.suspend_request, r.cancel_request, r.ml_model_to_load_refs) == ("SUSPENDED", True, False, [str(model)])
        key = uuid.uuid4()
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},MLModelLoadingRequest={key}"
        HANDLERS["MLModelLoadingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelToLoadRef": [f"M={model}"], "cancelRequest": True}))
        assert db.get(m.MLModelLoadingRequest, key).request_status == "CANCELLED"


def test_an_update_and_a_testing_request_pass_their_attributes(db_session_factory, mlmr):
    """MLUpdateRequest: each attribute on its row, its function, the caller as the source of its runs; MLTestingRequest: the testing function
    and the instance's id on the ValidationJob, the caller as producer."""
    model = mlmr.add_model()
    _set_lifecycle(db_session_factory, model, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    with db_session_factory() as db:
        function = _function(db, m.MLUpdateFunction, ml_model_refs=[])
        key = uuid.uuid4()
        dn = f"{ROOT_DN},MLUpdateFunction={function.ml_update_function_id},MLUpdateRequest={key}"
        HANDLERS["MLUpdateRequest"].before_create(_ctx(db, dn), _moi(dn, {
            "mLModelRefList": [f"M={model}"], "performanceGainThreshold": [{"performanceMetric": "MAE"}], "newCapabilityVersionId": ["v2"],
            "updateTimeDeadline": {"t": 1}, "mLUpdateReportingPeriod": {"p": 1}}))
        r = db.get(m.MLUpdateRequest, key)
        assert (r.ml_update_function_id, r.performance_gain_threshold, r.new_capability_version_ids, r.update_time_deadline,
                r.ml_update_reporting_period, r.ml_model_refs) == (function.ml_update_function_id, [{"performanceMetric": "MAE"}], ["v2"], {"t": 1},
                                                                  {"p": 1}, [str(model)])
        assert db.scalar(select(m.TrainingJob.producer_id).where(m.TrainingJob.model_id == model)) == "provmns"

    other = mlmr.add_model()
    _set_lifecycle(db_session_factory, other, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    with db_session_factory() as db:
        function = _function(db, m.MLTestingFunction)
        key = uuid.uuid4()
        dn = f"{ROOT_DN},MLTestingFunction={function.ml_testing_function_id},MLTestingRequest={key}"
        HANDLERS["MLTestingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelRef": f"M={other}"}))
        v = db.get(m.ValidationJob, key)
        assert (v.model_id, v.ml_testing_function_id, v.producer_id, v.model_coordination_group_id) == (other, function.ml_testing_function_id,
                                                                                                         "provmns", None)


def test_a_request_takes_only_its_flags_and_refuses_one_with_no_row(db_session_factory):
    """modifyMOIAttributes of a request: another attribute is 422 VETOED naming it; an instance whose row is gone is 404."""
    with db_session_factory() as db:
        dn = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingRequest={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["MLTrainingRequest"].before_modify(_ctx(db, dn), _moi(dn, {"a": 1}), _moi(dn, {"a": 2, "cancelRequest": True}))) == (
            422, "VETOED", "only cancelRequest and suspendRequest of a MLTrainingRequest change", [dn], ["a"])
        assert _refusal(lambda: HANDLERS["MLTrainingRequest"].before_modify(_ctx(db, dn), _moi(dn), _moi(dn, {"cancelRequest": True}))) == (
            404, "VETOED", f"{dn} has no row in AIMgF", [dn], None)
        assert _refusal(lambda: HANDLERS["MLTrainingRequest"].before_delete(_ctx(db, dn, "deleteMOI"), _moi(dn))) == (
            409, "VETOED", "a MLTrainingRequest is kept as the record of its run; set cancelRequest to end it", [dn], None)


def _provmns_request(client, db_session_factory, function_class: str, request_class: str, attributes: dict) -> str:
    """A function and a request in it, over HTTP; returns the request's DN."""
    root(db_session_factory)
    function_dn = f"{ROOT_DN},{function_class}={uuid.uuid4()}"
    assert client.put(BASE + function_dn.replace(",", "/"), json={"id": function_dn.rsplit("=", 1)[1], "attributes": {}}).status_code == 201
    dn = f"{function_dn},{request_class}={uuid.uuid4()}"
    response = client.put(BASE + dn.replace(",", "/"), json={"id": dn.rsplit("=", 1)[1], "attributes": attributes})
    assert response.status_code == 201, response.text
    return dn


def _patch(client, dn: str, attributes: str, headers=None):
    return client.patch(BASE + dn.replace(",", "/"), content='{"attributes": ' + attributes + "}", headers={**MERGE, **(headers or {})})


def test_testing_update_and_loading_flags_and_their_owner_rule(client, mlmr, db_session_factory):
    """Over ProvMnS: another rApp may not suspend or cancel a testing, update or loading request of the owner's models (403 VETOED naming
    MODEL_ACCESS_DENIED, and the run unchanged); the owner may, and the run follows."""
    tested, updated, loaded = (mlmr.add_model(owner="rapp-a") for _ in range(3))
    for model in (tested, updated, loaded):
        register(db_session_factory, model, owner="rapp-a")
    _set_lifecycle(db_session_factory, tested, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    _set_lifecycle(db_session_factory, updated, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    _set_lifecycle(db_session_factory, loaded, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    owner = {"x-r1-role": "rapp", "x-r1-invoker-id": "rapp-a"}
    cases = [
        ("MLTestingFunction", "MLTestingRequest", {"mLModelRef": f"M={tested}"}, m.ValidationJob, "status", "SUSPENDED", "cancel or suspend its testing"),
        ("MLUpdateFunction", "MLUpdateRequest", {"mLModelRefList": [f"M={updated}"]}, m.MLUpdateRequest, "request_status", "SUSPENDED",
         "cancel or suspend its update"),
        ("AIMLInferenceFunction", "MLModelLoadingRequest", {"mLModelToLoadRef": [f"M={loaded}"], "suspendRequest": True}, m.MLModelLoadingRequest,
         "request_status", "CANCELLED", "cancel or suspend its loading"),
    ]
    for function_class, request_class, attributes, table, column, after, action in cases:
        dn = _provmns_request(client, db_session_factory, function_class, request_class, attributes)
        flag = '{"cancelRequest": true}' if request_class == "MLModelLoadingRequest" else '{"suspendRequest": true}'
        refused = _patch(client, dn, flag, RAPP_B)
        assert (refused.status_code, refused.json()["type"]) == (403, "VETOED"), request_class
        assert refused.json()["reason"].startswith("MODEL_ACCESS_DENIED") and action in refused.json()["reason"], refused.json()["reason"]
        assert _patch(client, dn, flag, owner).status_code == 200, request_class
        with db_session_factory() as db:
            assert getattr(db.get(table, uuid.UUID(dn.rsplit("=", 1)[1])), column) == after, request_class
        assert _patch(client, dn, flag.replace("true", "false"), owner).status_code in (200, 409)


def test_a_training_request_cancel_by_another_rapp_is_refused(client, mlmr, db_session_factory):
    """The training request's owner rule over ProvMnS: another rApp's cancel is 403 naming the action; the run stays IN_PROGRESS."""
    model = mlmr.add_model(owner="rapp-a")
    register(db_session_factory, model, owner="rapp-a")
    dn = _provmns_request(client, db_session_factory, "MLTrainingFunction", "MLTrainingRequest", {"mLModelRef": f"M={model}"})
    refused = _patch(client, dn, '{"cancelRequest": true}', RAPP_B)
    assert refused.status_code == 403 and "cancel or suspend its training" in refused.json()["reason"]
    with db_session_factory() as db:
        assert db.get(m.TrainingJob, uuid.UUID(dn.rsplit("=", 1)[1])).status == "IN_PROGRESS"


# ---------------------------------------------------------------------------- the training process and the inference report

def test_a_training_process_writes_each_attribute_alone(client, mlmr, db_session_factory):
    """priority, then terminationConditions, then cancelProcess, each changed alone; priority cleared is 0; another rApp is 403 naming the action;
    an instance with no row is 404."""
    model = mlmr.add_model(owner="rapp-a")
    register(db_session_factory, model, owner="rapp-a")
    request_dn = _provmns_request(client, db_session_factory, "MLTrainingFunction", "MLTrainingRequest", {"mLModelRef": f"M={model}"})
    job_id = uuid.UUID(request_dn.rsplit("=", 1)[1])
    with db_session_factory() as db:
        process = db.scalar(select(m.MLTrainingProcess).where(m.MLTrainingProcess.training_job_id == job_id))
    dn = f"{request_dn.rsplit(',', 1)[0]},MLTrainingProcess={process.ml_training_process_id}"
    refused = _patch(client, dn, '{"priority": 3}', RAPP_B)
    assert refused.status_code == 403 and "change its training" in refused.json()["reason"]
    assert _patch(client, dn, '{"priority": 3}').status_code == 200
    assert _patch(client, dn, '{"terminationConditions": "t"}').status_code == 200
    with db_session_factory() as db:
        p = db.get(m.MLTrainingProcess, process.ml_training_process_id)
        assert (p.priority, p.termination_conditions, db.get(m.TrainingJob, job_id).status) == (3, "t", "IN_PROGRESS")
    assert _patch(client, dn, '{"priority": null}').status_code == 200
    assert _patch(client, dn, '{"cancelProcess": true}').status_code == 200
    with db_session_factory() as db:
        assert (db.get(m.MLTrainingProcess, process.ml_training_process_id).priority, db.get(m.TrainingJob, job_id).status) == (0, "CANCELLED")
    missing = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingProcess={uuid.uuid4()}"
    with db_session_factory() as db:
        assert _refusal(lambda: HANDLERS["MLTrainingProcess"].before_modify(_ctx(db, missing), _moi(missing), _moi(missing, {"priority": 1}))) == (
            404, "VETOED", f"{missing} has no row in AIMgF", [missing], None)
        assert _refusal(lambda: HANDLERS["MLTrainingProcess"].before_modify(_ctx(db, missing), _moi(missing), _moi(missing, {"x": 1}))) == (
            422, "VETOED", "only priority, terminationConditions, cancelProcess and suspendProcess of an MLTrainingProcess change", [missing], ["x"])


def test_an_inference_report_takes_its_attributes_and_its_function(db_session_factory, mlmr):
    """The report's outputs, impact and models land on its row under its function; outside an AIMgF function it is 422; another attribute is
    named; a model another rApp owns is 403 naming MODEL_ACCESS_DENIED."""
    model = mlmr.add_model(owner="rapp-a")
    with db_session_factory() as db:
        function = _function(db, m.AIMLInferenceFunction, activation_status="ACTIVATED", ml_model_refs=[])
        key = uuid.uuid4()
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},AIMLInferenceReport={key}"
        HANDLERS["AIMLInferenceReport"].before_create(_ctx(db, dn), _moi(dn, {"inferenceOutputs": [{"o": 1}], "potentialImpactInfo": {"i": 1},
                                                                             "mLModelRefList": [f"M={model}"]}))
        r = db.get(m.AIMLInferenceReport, key)
        assert (r.aiml_inference_function_id, r.inference_outputs, r.potential_impact_info, r.ml_model_refs, r.aiml_inference_emulation_function_id) == (
            function.aiml_inference_function_id, [{"o": 1}], {"i": 1}, [str(model)], None)
        dn = f"{ROOT_DN},AIMLInferenceFunction=default,AIMLInferenceReport={uuid.uuid4()}"
        assert _refusal(lambda: HANDLERS["AIMLInferenceReport"].before_create(_ctx(db, dn), _moi(dn))) == (
            422, "VETOED", "an AIMLInferenceReport is created under the AIMLInferenceFunction it reports on", [dn], None)
        assert _refusal(lambda: HANDLERS["AIMLInferenceReport"].before_create(_ctx(db, dn), _moi(dn, {"x": 1}))) == (
            422, "VETOED", "a new AIMLInferenceReport: AIMgF does not take x", [dn], ["x"])
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},AIMLInferenceReport={uuid.uuid4()}"
        token = current_request.set(_Request(RAPP_B))
        try:
            status, kind, reason, objects, _ = _refusal(lambda: HANDLERS["AIMLInferenceReport"].before_create(
                _ctx(db, dn), _moi(dn, {"mLModelRefList": [f"M={model}"]})))
        finally:
            current_request.reset(token)
        assert (status, kind, objects) == (403, "VETOED", [dn]) and reason.startswith("MODEL_ACCESS_DENIED")
        key = uuid.uuid4()
        dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id},AIMLInferenceReport={key}"
        HANDLERS["AIMLInferenceReport"].before_create(_ctx(db, dn), _moi(dn, {"mLModelRefList": []}))
        assert (db.get(m.AIMLInferenceReport, key).inference_outputs, db.get(m.AIMLInferenceReport, key).potential_impact_info) == ([], None)


# ---------------------------------------------------------------------------- service.py

def test_the_producer_is_aimgfs():
    """The producer's event source, system DN, served classes (each with its own hook), cross-cutting checks named after AIMgF, and the registry
    the projection validates with."""
    mns = service.mns
    assert (mns.source, mns.system_dn, mns.checks.module) == ("/smo/aimgf", ROOT_DN, "aimgf")
    assert all(mns._handler(name) is HANDLERS[name] for name in service.CLASSES)
    assert projection.registry() is mns.registry and isinstance(mns.registry, service.AimgfRegistry)
    assert service.TS28105_FILE == "TS28105_AiMlNrm.yaml" and service.GOVERNANCE_CLASS == "MLModelGovernance"
    assert service.CLASSES == ("ManagedElement", *service.TS28105_CLASSES, "MLModelGovernance")
    assert not mns.registry.may_contain("MLTrainingFunction", "MLModelGovernance") and not mns.registry.may_contain(None, "MLModelGovernance")
    assert mns.registry.may_contain(None, "ManagedElement")


def test_a_restricted_build_serves_only_its_classes():
    """`build(classes)`: only those classes are served, each with its hook."""
    mns = service.build(("ManagedElement", "MLModel"))
    assert mns.served("MLModel") and not mns.served("MLTrainingFunction") and mns._handler("MLModel") is HANDLERS["MLModel"]


def test_the_registration_record_and_the_stub(caplog):
    """MNS_INFO names ProvMnS at /aimgf/ProvMnS, its scope and version; `register` logs it and returns it."""
    assert service.MNS_INFO == {"mnsLabel": "AIMgF ProvMnS", "mnsType": "ProvMnS", "mnsVersion": "19.2.0", "mnsAddress": "/aimgf/ProvMnS",
                                "mnsScope": [ROOT_DN], "classes": list(service.CLASSES)}
    with caplog.at_level("INFO", logger="app.provmns.service"):
        assert service.register() is service.MNS_INFO
    assert "pending S1.12" in caplog.text


def test_start_up_projects_once_and_survives_a_missing_store(db_session_factory, monkeypatch, caplog):
    """`start`: with ROOT present it projects nothing; with no store (the database not migrated yet) it logs and rolls back, raising nothing."""
    with db_session_factory() as db:
        projection.ensure_container(db, ROOT_DN)
        db.add(m.MLTestingFunction(user_label="x"))
        db.commit()
        db.execute(ManagedObject.__table__.delete().where(ManagedObject.dn != ROOT_DN))
        db.commit()
    monkeypatch.setattr("smo_shared.db.SessionLocal", db_session_factory)
    service.start()
    with db_session_factory() as db:
        assert db.scalars(select(ManagedObject.dn)).all() == [ROOT_DN]
        ManagedObject.__table__.drop(db.connection())
        db.commit()
    with caplog.at_level("WARNING", logger="app.provmns.service"):
        service.start()
    assert "start-up projection not done" in caplog.text


def test_the_openapi_says_what_the_router_answers():
    """`document`: each ProvMnS operation declares the TS 28.623 error body for the router's statuses; another route keeps the SMO's envelope."""
    from app.main import app
    schema = app.openapi()
    assert schema["components"]["schemas"][service.PROVMNS_ERROR] == service.PROVMNS_ERROR_SCHEMA
    for method in ("get", "put", "patch", "delete"):
        responses = schema["paths"]["/ProvMnS/{version}/{ldn}"][method]["responses"]
        for status in ("400", "403", "404", "409", "412", "415", "422"):
            assert responses[status]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ProvMnSErrorResponse"}
        assert responses["401"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert schema["paths"]["/models"]["post"]["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"}


# ---------------------------------------------------------------------------- projection.py

def test_a_moved_row_takes_what_it_contains_and_a_deleted_row_its_instance(db_session_factory):
    """A training request moved to another function (its row's reference changed) is deleted at its old DN, with what it contained, and written
    at the new one; a row deleted takes its instance with it."""
    with db_session_factory() as db:
        a, b = _function(db, m.MLTrainingFunction), _function(db, m.MLTrainingFunction)
        job = m.TrainingJob(model_id=uuid.uuid4(), producer_id="p", status="IN_PROGRESS", ml_training_function_id=a.ml_training_function_id)
        db.add(job)
        db.commit()
        old = f"{ROOT_DN},MLTrainingFunction={a.ml_training_function_id},MLTrainingRequest={job.training_job_id}"
        assert db.get(ManagedObject, old) is not None
        job.ml_training_function_id = b.ml_training_function_id
        db.commit()
        assert db.get(ManagedObject, old) is None
        assert db.get(ManagedObject, f"{ROOT_DN},MLTrainingFunction={b.ml_training_function_id},MLTrainingRequest={job.training_job_id}") is not None
        db.delete(db.get(m.MLTrainingFunction, b.ml_training_function_id))
        db.commit()
        assert db.get(ManagedObject, f"{ROOT_DN},MLTrainingFunction={b.ml_training_function_id}") is None


def test_dependants_follow_the_row_they_read(db_session_factory, mlmr):
    """A run's status reaches its process; an inference run reaches its function's consumers; a governance decision reaches the governance
    instance; an update process its request; an update report its process."""
    model = mlmr.add_model()
    register(db_session_factory, model, owner="o")
    with db_session_factory() as db:
        job = m.TrainingJob(model_id=model, producer_id="p", status="IN_PROGRESS")
        db.add(job)
        db.flush()
        process = m.MLTrainingProcess(training_job_id=job.training_job_id, status="RUNNING")
        db.add(process)
        db.commit()
        function = _function(db, m.AIMLInferenceFunction, activation_status="ACTIVATED", ml_model_refs=[])
        db.add(m.ModelLifecycle(model_id=model, model_lifecycle_state="REGISTERED", runtime_lifecycle_state="NOT_DEPLOYED"))
        db.commit()
        db.add(m.InferenceJob(model_id=model, aiml_inference_function_id=function.aiml_inference_function_id, consumer_ref="es", status="RUNNING"))
        db.add(m.CertificationRecord(model_id=model, decision="APPROVE", decided_by="alice", rationale="ok"))
        db.commit()
        function_dn = f"{ROOT_DN},AIMLInferenceFunction={function.aiml_inference_function_id}"
        assert db.get(ManagedObject, function_dn).attributes["usedByFunctionRefList"] == ["es"]
        governance = db.get(ManagedObject, f"{ROOT_DN},MLModelRepository=default,MLModel={model},MLModelGovernance={model}").attributes
        assert governance["decisions"][0]["event"] == "APPROVE" and governance["decisions"][0]["reason"] == "ok" and governance["owner"] == "o"
        request = m.MLUpdateRequest(request_status="IN_PROGRESS", ml_model_refs=[])
        db.add(request)
        db.flush()
        update = m.MLUpdateProcess(ml_update_request_id=request.ml_update_request_id, status="RUNNING", ml_model_refs=[])
        db.add(update)
        db.commit()
        request_dn = f"{ROOT_DN},MLUpdateFunction=default,MLUpdateRequest={request.ml_update_request_id}"
        assert db.get(ManagedObject, request_dn).attributes["mLUpdateProcessRef"].endswith(f"MLUpdateProcess={update.ml_update_process_id}")
        db.add(m.MLUpdateReport(ml_update_process_id=update.ml_update_process_id, ml_model_refs=[]))
        db.commit()
        process_dn = f"{ROOT_DN},MLUpdateFunction=default,MLUpdateProcess={update.ml_update_process_id}"
        assert "MLUpdateReport=" in db.get(ManagedObject, process_dn).attributes["mLUpdateReportRef"]
        job.status = "FINISHED"
        db.commit()
        process_dn = f"{ROOT_DN},MLTrainingFunction=default,MLTrainingProcess={process.ml_training_process_id}"
        assert db.get(ManagedObject, process_dn).attributes["mLModelRef"] == f"{ROOT_DN},MLModelRepository=default,MLModel={model}"


def test_the_training_request_status_maps_failed_to_finished():
    """`_training_request_adjust`: FAILED is FINISHED (the outcome is in the report); every other status, and none, is kept."""
    assert projection._training_request_adjust({"requestStatus": "FAILED", "a": 1}) == {"requestStatus": "FINISHED", "a": 1}
    assert projection._training_request_adjust({"requestStatus": "CANCELLED"}) == {"requestStatus": "CANCELLED"}
    assert projection._training_request_adjust({}) == {}


def test_a_dn_for_a_class_or_id_aimgf_does_not_have_is_none(db_session_factory):
    """`dn_for`: an unknown class, a value that is not a UUID, or an id with no row: None, so the reference is left out."""
    with db_session_factory() as db:
        assert projection.dn_for(db, "Nothing", uuid.uuid4()) is None
        assert projection.dn_for(db, "MLModel", "not-a-uuid") is None
        assert projection.dn_for(db, "MLModel", uuid.uuid4()) is None
    assert projection._as_uuids(["x", str(uuid.UUID(int=1)), uuid.UUID(int=2)]) == [uuid.UUID(int=1), uuid.UUID(int=2)]


def test_the_tree_helpers():
    """`tree`: a container's DN by id or `default`, the last id of a DN, a UUID or None."""
    key = uuid.uuid4()
    assert tree.container("MLTrainingFunction", key) == f"{ROOT_DN},MLTrainingFunction={key}"
    assert tree.container("MLTrainingFunction", None) == f"{ROOT_DN},MLTrainingFunction=default"
    assert (tree.last_id("A=1,B=x=y"), tree.uuid_of(f"A=1,B={key}"), tree.uuid_of("A=1,B=2"), tree.uuid_of(None), tree.uuid_of("plain")) == (
        "x=y", key, None, None, None)
    assert tree.as_uuid(None) is None and tree.ROOT_DN == "ManagedElement=AIMgF" and tree.DEFAULT_ID == "default"


# ---------------------------------------------------------------------------- second pass: what the first mutation run left

from smo_shared import events  # noqa: E402
from app.registry import access as registry_access  # noqa: E402

REAL_MODEL_JSON = registry_access.model_json          # the registry read as it is, before a fixture puts its double over it


def _events(db, dn: str) -> list[dict]:
    """The bodies of the notifications published for `dn`, oldest first."""
    rows = db.scalars(select(events.SmoEvent).where(events.SmoEvent.subject == dn).order_by(events.SmoEvent.seq)).all()
    return [row.data for row in rows]


def test_each_projected_change_is_its_ts_28532_notification(db_session_factory):
    """A created, changed and deleted instance each publish their notification, from AIMgF itself: the TS 28.532 header (href in the one form,
    notificationId in TS 28.623's range, notificationType, a UTC eventTime, systemDN) and RESOURCE_OPERATION; creation carries the attributes,
    a change the new and the old values of what changed, deletion the attributes it had; an instance with no attribute carries no list."""
    with db_session_factory() as db:
        f = _function(db, m.MLTestingFunction, user_label="a")
        db.commit()
        dn = f"{ROOT_DN},MLTestingFunction={f.ml_testing_function_id}"
        f.user_label = "b"
        db.commit()
        db.delete(f)
        db.commit()
        created, changed, deleted = _events(db, dn)
        root = _events(db, ROOT_DN)[0]
    for body in (created, changed, deleted):
        assert body["href"] == f"/ProvMnS/19.2.0/{dn.replace(',', '/')}" and body["systemDN"] == ROOT_DN
        assert body["sourceIndicator"] == "RESOURCE_OPERATION" and 1 <= body["notificationId"] <= 2**53 - 1
        assert body["eventTime"].endswith("+00:00")
    assert (created["notificationType"], created["attributeList"]) == ("notifyMOICreation", {"userLabel": "a"})
    assert (changed["notificationType"], changed["attributeListValueChanges"]) == ("notifyMOIAttributeValueChanges", [{"userLabel": "b"}, {"userLabel": "a"}])
    assert (deleted["notificationType"], deleted["attributeList"]) == ("notifyMOIDeletion", {"userLabel": "b"})
    assert root["attributeList"] == {"userLabel": "AIMgF"}
    with db_session_factory() as db:
        f = _function(db, m.MLTestingFunction)
        db.commit()
        dn = f"{ROOT_DN},MLTestingFunction={f.ml_testing_function_id}"
        db.delete(f)
        db.commit()
        assert [("attributeList" in body) for body in _events(db, dn)] == [False, False]


def test_an_added_and_a_removed_attribute_are_changes_with_null_on_the_other_side(db_session_factory):
    """A change that adds one attribute and removes another lists both: the added one's old value and the removed one's new value are null."""
    with db_session_factory() as db:
        f = _function(db, m.MLTestingFunction, user_label="a")
        db.commit()
        dn = f"{ROOT_DN},MLTestingFunction={f.ml_testing_function_id}"
        projection._write(db, dn, "MLTestingFunction", {"mLModelRef": ["x"]})
        db.flush()
        assert _events(db, dn)[-1]["attributeListValueChanges"] == [{"mLModelRef": ["x"], "userLabel": None}, {"mLModelRef": None, "userLabel": "a"}]
        assert db.get(ManagedObject, dn).attributes == {"mLModelRef": ["x"]}
        projection._write(db, dn, "MLTestingFunction", {"mLModelRef": ["x"]})            # the same attributes: no change, no notification
        db.flush()
        assert len(_events(db, dn)) == 2


def test_an_instance_knows_its_container_and_depth(db_session_factory):
    """`moi_store` rows carry their parent DN and depth (the router's scope reads them)."""
    with db_session_factory() as db:
        f = _function(db, m.MLTestingFunction)
        db.commit()
        row = db.get(ManagedObject, f"{ROOT_DN},MLTestingFunction={f.ml_testing_function_id}")
        assert (row.parent_dn, row.depth, row.moi_id, row.object_class) == (ROOT_DN, 2, str(f.ml_testing_function_id), "MLTestingFunction")
        assert (db.get(ManagedObject, ROOT_DN).parent_dn, db.get(ManagedObject, ROOT_DN).depth) == (None, 1)


def test_a_container_three_levels_down_is_made_from_its_row(db_session_factory, mlmr):
    """`ensure_container`: an instance whose container is not in the store yet (a governance instance whose model's instance was lost) makes it from
    its row, with the containers above it; a `default` container is made with no attribute."""
    model = mlmr.add_model()
    repository = registry_models_repository(db_session_factory)
    register(db_session_factory, model, ml_model_repository_id=repository)
    _set_lifecycle(db_session_factory, model, model_lifecycle_state=ModelLifecycleState.TRAINING)
    model_dn = f"{ROOT_DN},MLModelRepository={repository},MLModel={model}"
    with db_session_factory() as db:
        db.execute(ManagedObject.__table__.delete())
        db.commit()
        projection.project_one(db, projection.BY_CLASS["MLModelGovernance"], model)
        db.commit()
        assert db.get(ManagedObject, f"{model_dn},MLModelGovernance={model}").attributes["lifecycleState"] == "TRAINING"
        assert db.get(ManagedObject, model_dn).attributes["mLModelId"] == str(model)
        assert db.get(ManagedObject, f"{ROOT_DN},MLModelRepository={repository}").attributes == {}
        projection.ensure_container(db, f"{ROOT_DN},MLUpdateFunction=default")
        assert db.get(ManagedObject, f"{ROOT_DN},MLUpdateFunction=default").attributes == {}


def registry_models_repository(db_session_factory) -> uuid.UUID:
    """A registry repository; returns its id."""
    from app.registry import models as rm
    with db_session_factory() as db:
        repository = rm.MLModelRepository(user_label="r")
        db.add(repository)
        db.commit()
        return repository.ml_model_repository_id


def test_a_model_is_placed_in_its_repository_with_its_identity(db_session_factory, mlmr):
    """An MLModel sits in its repository, with `mLModelId` and `mLModelVersion` from its row; a governance instance's decisions are oldest first,
    with a reason only when one was given, and its owner is the model's."""
    import datetime
    model = mlmr.add_model()
    repository = registry_models_repository(db_session_factory)
    register(db_session_factory, model, ml_model_repository_id=repository, owner="rapp-o")
    with db_session_factory() as db:
        db.add(m.ModelLifecycle(model_id=model, model_lifecycle_state="CERTIFIED", runtime_lifecycle_state="NOT_DEPLOYED"))
        db.add(m.CertificationRecord(model_id=model, decision="CERTIFY", decided_by="bob", decided_at=datetime.datetime(2026, 2, 1)))
        db.add(m.CertificationRecord(model_id=model, decision="APPROVE", decided_by="alice", rationale="good", decided_at=datetime.datetime(2026, 1, 1)))
        db.commit()
        model_dn = f"{ROOT_DN},MLModelRepository={repository},MLModel={model}"
        attributes = db.get(ManagedObject, model_dn).attributes
        assert (attributes["mLModelId"], attributes["mLModelVersion"]) == (str(model), "1")
        governance = db.get(ManagedObject, f"{model_dn},MLModelGovernance={model}").attributes
    assert governance == {"mLModelRef": model_dn, "lifecycleState": "CERTIFIED", "runtimeState": "NOT_DEPLOYED", "servingState": "VALIDATED",
                          "owner": "rapp-o", "decisions": [
                              {"event": "APPROVE", "decidedBy": "alice", "decidedAt": "2026-01-01T00:00:00", "reason": "good"},
                              {"event": "CERTIFY", "decidedBy": "bob", "decidedAt": "2026-02-01T00:00:00"}]}


def test_update_instances_sit_in_their_function_and_a_count_of_the_start_up_projection(db_session_factory):
    """An update request, its process and its report sit in the request's function; `project_all` counts what it wrote."""
    with db_session_factory() as db:
        function = _function(db, m.MLUpdateFunction, ml_model_refs=[])
        request = m.MLUpdateRequest(ml_update_function_id=function.ml_update_function_id, request_status="IN_PROGRESS", ml_model_refs=[])
        db.add(request)
        db.flush()
        process = m.MLUpdateProcess(ml_update_request_id=request.ml_update_request_id, status="RUNNING", ml_model_refs=[])
        db.add(process)
        db.flush()
        report = m.MLUpdateReport(ml_update_process_id=process.ml_update_process_id, ml_model_refs=[])
        db.add(report)
        db.commit()
        base = f"{ROOT_DN},MLUpdateFunction={function.ml_update_function_id}"
        for dn in (f"{base},MLUpdateRequest={request.ml_update_request_id}", f"{base},MLUpdateProcess={process.ml_update_process_id}",
                   f"{base},MLUpdateReport={report.ml_update_report_id}"):
            assert db.get(ManagedObject, dn) is not None, dn
        db.execute(ManagedObject.__table__.delete())
        assert projection.project_all(db) == 4


def test_a_row_deleted_through_the_orm_takes_its_contained_instances_and_they_come_back_where_they_belong(db_session_factory):
    """A function row deleted directly (not over ProvMnS): its instance and its requests' go, deepest first, and the requests, whose reference the
    database set to NULL, are projected again under `default`."""
    raw = db_session_factory.kw["bind"].raw_connection()
    raw.execute("PRAGMA foreign_keys=ON")
    with db_session_factory() as db:
        f = _function(db, m.MLTrainingFunction)
        job = m.TrainingJob(model_id=uuid.uuid4(), producer_id="p", status="IN_PROGRESS", ml_training_function_id=f.ml_training_function_id)
        db.add(job)
        db.commit()
        function_dn = f"{ROOT_DN},MLTrainingFunction={f.ml_training_function_id}"
        db.delete(db.get(m.MLTrainingFunction, f.ml_training_function_id))
        db.commit()
        assert db.get(ManagedObject, function_dn) is None
        assert db.get(ManagedObject, f"{function_dn},MLTrainingRequest={job.training_job_id}") is None
        assert db.get(ManagedObject, f"{ROOT_DN},MLTrainingFunction=default,MLTrainingRequest={job.training_job_id}") is not None
        deletions = [row.subject for row in db.scalars(select(events.SmoEvent).order_by(events.SmoEvent.seq))
                     if row.data["notificationType"] == "notifyMOIDeletion"]
        assert deletions == [f"{function_dn},MLTrainingRequest={job.training_job_id}", function_dn]


def test_a_registered_model_owner_is_judged_on_the_registry(client, mlmr, db_session_factory, monkeypatch):
    """With the registry read as it is (not its double), another rApp's flag on a training, testing, update or loading request, or its change of
    a training process, is 403 with the route's exact reason and the instance's DN; the owner's resume of a suspended loading request loads."""
    monkeypatch.setattr("app.registry.access.model_json", REAL_MODEL_JSON)
    models = [mlmr.add_model(owner="rapp-a") for _ in range(4)]
    for model in models:
        register(db_session_factory, model, owner="rapp-a")
    trained, tested, updated, loaded = models
    _set_lifecycle(db_session_factory, tested, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    _set_lifecycle(db_session_factory, updated, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    _set_lifecycle(db_session_factory, loaded, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    owner = {"x-r1-role": "rapp", "x-r1-invoker-id": "rapp-a"}
    for function_class, request_class, attributes, model, action in (
            ("MLTrainingFunction", "MLTrainingRequest", {"mLModelRef": f"M={trained}"}, trained, "cancel or suspend its training"),
            ("MLTestingFunction", "MLTestingRequest", {"mLModelRef": f"M={tested}"}, tested, "cancel or suspend its testing"),
            ("MLUpdateFunction", "MLUpdateRequest", {"mLModelRefList": [f"M={updated}"]}, updated, "cancel or suspend its update"),
            ("AIMLInferenceFunction", "MLModelLoadingRequest", {"mLModelToLoadRef": [f"M={loaded}"], "suspendRequest": True}, loaded,
             "cancel or suspend its loading")):
        dn = _provmns_request(client, db_session_factory, function_class, request_class, attributes)
        refused = _patch(client, dn, '{"cancelRequest": true}', RAPP_B)
        assert refused.json() == {"status": "403", "type": "VETOED", "title": "The producer's domain logic refused the change", "badObjects": [dn],
                                  "reason": f"MODEL_ACCESS_DENIED: model {model} belongs to another producer: only its owner may {action}"}
    assert _patch(client, dn, '{"suspendRequest": false}', owner).json()["attributes"]["requestStatus"] == "FINISHED"
    with db_session_factory() as db:
        process = db.scalar(select(m.MLTrainingProcess).join(m.TrainingJob, m.TrainingJob.training_job_id == m.MLTrainingProcess.training_job_id)
                            .where(m.TrainingJob.model_id == trained))
        process_dn = f"{ROOT_DN},MLTrainingFunction={db.get(m.TrainingJob, process.training_job_id).ml_training_function_id},MLTrainingProcess={process.ml_training_process_id}"
    refused = _patch(client, process_dn, '{"priority": 1}', RAPP_B)
    assert refused.json()["reason"] == f"MODEL_ACCESS_DENIED: model {trained} belongs to another producer: only its owner may change its training"
    assert refused.json()["badObjects"] == [process_dn]


def test_each_requests_flags_act_on_its_run(client, mlmr, db_session_factory):
    """The flags over ProvMnS, one by one: a training request suspended then resumed, a testing request cancelled (its model FAILED), an update
    request cancelled (its run CANCELLED); a domain refusal names the instance (a suspend of the cancelled testing request)."""
    trained, tested, updated = (mlmr.add_model() for _ in range(3))
    _set_lifecycle(db_session_factory, tested, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    _set_lifecycle(db_session_factory, updated, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    dn = _provmns_request(client, db_session_factory, "MLTrainingFunction", "MLTrainingRequest", {"mLModelRef": f"M={trained}"})
    assert _patch(client, dn, '{"suspendRequest": true}').json()["attributes"]["requestStatus"] == "SUSPENDED"
    assert _patch(client, dn, '{"suspendRequest": false}').json()["attributes"]["requestStatus"] == "IN_PROGRESS"
    dn = _provmns_request(client, db_session_factory, "MLTestingFunction", "MLTestingRequest", {"mLModelRef": f"M={tested}"})
    assert _patch(client, dn, '{"cancelRequest": true}').json()["attributes"]["requestStatus"] == "CANCELLED"
    with db_session_factory() as db:
        assert db.get(m.ModelLifecycle, tested).model_lifecycle_state == ModelLifecycleState.FAILED
    refused = _patch(client, dn, '{"suspendRequest": true}')
    assert (refused.status_code, refused.json()["badObjects"]) == (409, [dn]) and refused.json()["reason"].startswith("TRAINING_JOB_ILLEGAL_TRANSITION")
    dn = _provmns_request(client, db_session_factory, "MLUpdateFunction", "MLUpdateRequest", {"mLModelRefList": [f"M={updated}"]})
    assert _patch(client, dn, '{"cancelRequest": true}').json()["attributes"]["requestStatus"] == "CANCELLED"
    with db_session_factory() as db:
        assert db.scalar(select(m.TrainingJob.status).where(m.TrainingJob.model_id == updated)) == "CANCELLED"


def test_a_start_refusal_names_the_instance_and_a_testing_request_takes_a_group(db_session_factory, mlmr):
    """A run the route refuses (an untrained model) is 409 VETOED naming the instance; a testing request may target a coordination group."""
    model = mlmr.add_model()
    with db_session_factory() as db:
        dn = f"{ROOT_DN},MLTestingFunction=default,MLTestingRequest={uuid.uuid4()}"
        status, _, _, objects, _ = _refusal(lambda: HANDLERS["MLTestingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelRef": f"M={model}"})))
        assert (status, objects) == (409, [dn])
        group, key = uuid.uuid4(), uuid.uuid4()
        dn = f"{ROOT_DN},MLTestingFunction=default,MLTestingRequest={key}"
        HANDLERS["MLTestingRequest"].before_create(_ctx(db, dn), _moi(dn, {"mLModelCoordinationGroupRef": f"G={group}"}))
        assert (db.get(m.ValidationJob, key).model_coordination_group_id, db.get(m.ValidationJob, key).model_id) == (group, None)


def test_the_answer_shows_what_aimgf_made_of_a_function(client, db_session_factory):
    """createMOI and modifyMOIAttributes of an inference function answer with what AIMgF made of it: `activationStatus` DEACTIVATED, as the row
    defaults it, and back to DEACTIVATED when a patch removes it; two attributes AIMgF does not take are both named."""
    root(db_session_factory)
    dn = f"{ROOT_DN},AIMLInferenceFunction={uuid.uuid4()}"
    path = BASE + dn.replace(",", "/")
    created = client.put(path, json={"id": dn.rsplit("=", 1)[1], "attributes": {"userLabel": "u"}})
    assert created.json()["attributes"] == {"userLabel": "u", "activationStatus": "DEACTIVATED"}
    assert _patch(client, dn, '{"activationStatus": "ACTIVATED"}').json()["attributes"]["activationStatus"] == "ACTIVATED"
    assert _patch(client, dn, '{"activationStatus": null}').json()["attributes"]["activationStatus"] == "DEACTIVATED"
    refused = _patch(client, dn, '{"priorityLabel": 1, "mLModelRefList": ["x"]}')
    assert refused.json()["reason"] == "AIMLInferenceFunction: AIMgF does not take mLModelRefList, priorityLabel"


def test_the_registry_and_the_openapi_from_scratch(caplog, monkeypatch, db_session_factory):
    """A registry built anew has the TS 28.105 classes and MLModelGovernance under MLModel; `document` on a fresh app declares the router's error
    body; `start` logs how many instances it projected."""
    from fastapi import FastAPI
    registry = service.AimgfRegistry()
    assert "MLTrainingRequest" in registry and registry.info("MLModelGovernance").attribute_names == frozenset(service.GOVERNANCE_ATTRIBUTES["properties"])
    assert registry.may_contain("MLModel", "MLModelGovernance") and not registry.may_contain(None, "MLModelGovernance")
    app = FastAPI()
    app.get("/ProvMnS/{version}/{ldn}")(lambda version, ldn: {})
    app.get("/other")(lambda: {})
    service.document(app)
    schema = app.openapi()
    assert schema["components"]["schemas"]["ProvMnSErrorResponse"]["required"] == ["status", "type", "reason", "title"]
    assert schema["paths"]["/ProvMnS/{version}/{ldn}"]["get"]["responses"]["412"]["description"] == "TS 28.623 error"
    assert "412" not in schema["paths"]["/other"]["get"]["responses"]
    monkeypatch.setattr("smo_shared.db.SessionLocal", db_session_factory)
    with caplog.at_level("INFO", logger="app.provmns.service"):
        service.start()
    projected = [r for r in caplog.records if r.msg.startswith("aimgf provmns: projected")]
    assert [(r.getMessage(), r.levelname) for r in projected] == [("aimgf provmns: projected 0 instances into an empty store", "INFO")]
    registered = [r for r in caplog.records if "pending S1.12" in r.msg]
    assert registered[0].getMessage() == f"aimgf provmns: MnS registry registration pending S1.12; would register {service.MNS_INFO}"


# ---------------------------------------------------------------------------- third pass

def test_a_producer_built_anew_is_aimgfs():
    """`build`: the producer's event source, system DN and the checks' module name (the audit's and the metric's label)."""
    mns = service.build(("ManagedElement",))
    assert (mns.source, mns.system_dn, mns.checks.module) == ("/smo/aimgf", ROOT_DN, "aimgf")


def test_the_middleware_hands_the_hooks_the_request_and_forgets_it():
    """`RequestContext.dispatch`: the request it serves is `current_request` while the call runs, the call gets that request, and it is gone after."""
    import asyncio
    seen = []

    async def call_next(request):
        seen.append((request, current_request.get()))
        return "answer"

    request = object()
    assert asyncio.run(service.RequestContext(app=None).dispatch(request, call_next)) == "answer"
    assert seen == [(request, request)] and current_request.get() is None


def test_document_declares_the_error_body_on_an_app_with_nothing_else():
    """On an app whose schema has no components yet, `document` adds the error schema and puts exactly the TS 28.623 body on each status of a
    ProvMnS path; another path is left as it is."""
    from fastapi import FastAPI
    app = FastAPI()
    app.get("/ProvMnS/x")(lambda: {})
    app.get("/other")(lambda: {})
    service.document(app)
    schema = app.openapi()
    assert schema["components"] == {"schemas": {"ProvMnSErrorResponse": service.PROVMNS_ERROR_SCHEMA}}
    responses = schema["paths"]["/ProvMnS/x"]["get"]["responses"]
    body = {"description": "TS 28.623 error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProvMnSErrorResponse"}}}}
    assert {status: responses[status] for status in service.PROVMNS_ERROR_STATUSES} == {status: body for status in service.PROVMNS_ERROR_STATUSES}
    assert set(schema["paths"]["/other"]["get"]["responses"]) == {"200"}


def test_a_start_up_without_a_store_logs_why(db_session_factory, monkeypatch, caplog):
    """The warning of a start-up that could not project: its words and the exception with it."""
    with db_session_factory() as db:
        ManagedObject.__table__.drop(db.connection())
        db.commit()
    monkeypatch.setattr("smo_shared.db.SessionLocal", db_session_factory)
    with caplog.at_level("WARNING", logger="app.provmns.service"):
        service.start()
    [record] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert record.getMessage() == "aimgf provmns: start-up projection not done" and record.exc_info is not None


def test_a_notification_id_is_in_ts_28623s_range_and_its_event_is_a_provmns_one(db_session_factory, monkeypatch):
    """notificationId is 1 .. 2^53 - 1 (the lowest and highest draws), and the event's type is `org.oran.smo.provmns.<notificationType>` from
    `/smo/aimgf`."""
    draws = iter([0, 2**53 - 2])
    monkeypatch.setattr("app.provmns.projection.secrets.randbelow", lambda bound: next(draws) if bound == 2**53 - 1 else 0)
    with db_session_factory() as db:
        projection._notify(db, "notifyMOICreation", "A=1", {})
        projection._notify(db, "notifyMOIDeletion", "A=1", {})
        db.flush()
        rows = db.scalars(select(events.SmoEvent).where(events.SmoEvent.subject == "A=1").order_by(events.SmoEvent.seq)).all()
    assert [r.data["notificationId"] for r in rows] == [1, 2**53 - 1]
    assert [(r.type, r.source, r.data["notificationType"]) for r in rows] == [
        ("org.oran.smo.provmns.notifyMOICreation", "/smo/aimgf", "notifyMOICreation"), ("org.oran.smo.provmns.notifyMOIDeletion", "/smo/aimgf", "notifyMOIDeletion")]


def test_a_change_notification_names_only_what_changed(db_session_factory):
    """An attribute whose value stays the same is not in the change notification; one whose value changes is, with both values."""
    with db_session_factory() as db:
        projection._write(db, "ManagedElement=Z", "ManagedElement", {"a": 1, "b": 2})
        projection._write(db, "ManagedElement=Z", "ManagedElement", {"a": 1, "b": 3})
        db.flush()
        assert _events(db, "ManagedElement=Z")[-1]["attributeListValueChanges"] == [{"b": 3}, {"b": 2}]


def test_a_container_already_there_is_left_and_one_missing_is_made_from_its_row(db_session_factory):
    """`ensure_container`: an existing container is not written again (its attributes stay as they are); a missing UUID-named one is its row's
    projection."""
    with db_session_factory() as db:
        projection.ensure_container(db, f"{ROOT_DN},MLTrainingFunction=default")
        db.get(ManagedObject, f"{ROOT_DN},MLTrainingFunction=default").attributes = {"userLabel": "kept"}
        db.flush()
        projection.ensure_container(db, f"{ROOT_DN},MLTrainingFunction=default")
        assert db.get(ManagedObject, f"{ROOT_DN},MLTrainingFunction=default").attributes == {"userLabel": "kept"}
        f = m.MLTestingFunction(user_label="from its row")
        db.add(f)
        db.flush()
        db.info.pop(projection._PENDING, None)
        dn = f"{ROOT_DN},MLTestingFunction={f.ml_testing_function_id}"
        projection.ensure_container(db, dn)
        assert db.get(ManagedObject, dn).attributes == {"userLabel": "from its row"}


def test_a_process_change_and_a_report_answer_with_what_aimgf_made_of_them(client, mlmr, db_session_factory):
    """suspendProcess answers with the process SUSPENDED (its run's state, projected at once); an inference report naming its model by a short DN
    answers with the model's full DN."""
    model = mlmr.add_model()
    register(db_session_factory, model)
    request_dn = _provmns_request(client, db_session_factory, "MLTrainingFunction", "MLTrainingRequest", {"mLModelRef": f"M={model}"})
    with db_session_factory() as db:
        process_id = db.scalar(select(m.MLTrainingProcess.ml_training_process_id)
                               .where(m.MLTrainingProcess.training_job_id == uuid.UUID(request_dn.rsplit("=", 1)[1])))
    process_dn = f"{request_dn.rsplit(',', 1)[0]},MLTrainingProcess={process_id}"
    assert _patch(client, process_dn, '{"suspendProcess": true}').json()["attributes"]["progressStatus"]["status"] == "SUSPENDED"
    function_dn = f"{ROOT_DN},AIMLInferenceFunction={uuid.uuid4()}"
    assert client.put(BASE + function_dn.replace(",", "/"), json={"id": function_dn.rsplit("=", 1)[1], "attributes": {}}).status_code == 201
    report_dn = f"{function_dn},AIMLInferenceReport={uuid.uuid4()}"
    answer = client.put(BASE + report_dn.replace(",", "/"), json={"id": report_dn.rsplit("=", 1)[1], "attributes": {"mLModelRefList": [f"M={model}"]}})
    assert answer.json()["attributes"]["mLModelRefList"] == [f"{ROOT_DN},MLModelRepository=default,MLModel={model}"]


def test_a_model_and_its_governance_instance_share_an_id_and_both_stay(db_session_factory, mlmr):
    """A model and its governance instance are named by the same UUID; projecting one does not take the other for itself (each is looked up in
    its own class), and a commit that touches both leaves both."""
    model = mlmr.add_model()
    register(db_session_factory, model)
    with db_session_factory() as db:
        db.add(m.ModelLifecycle(model_id=model, model_lifecycle_state="REGISTERED", runtime_lifecycle_state="NOT_DEPLOYED"))
        db.commit()
        from app.registry import models as rm
        db.get(rm.MLModel, model).description = "changed"
        db.get(m.ModelLifecycle, model).model_lifecycle_state = "TRAINING"
        db.commit()
        model_dn = f"{ROOT_DN},MLModelRepository=default,MLModel={model}"
        assert db.get(ManagedObject, model_dn) is not None
        assert db.get(ManagedObject, f"{model_dn},MLModelGovernance={model}").attributes["lifecycleState"] == "TRAINING"
