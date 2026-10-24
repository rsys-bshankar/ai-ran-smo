"""The generic O1-CM intent handler of SA SMOS (`app/o1cm.py`): registration, enactment of an Intent as DME actions, the fulfilment report, and the pushes
of an intent's admin-state change and deletion (INTENT-LIFECYCLE-PUSH).

Intent Service and DME are faked at the R1Client boundary by the `platform` fixture; the real round trip (Intent Service, SA SMOS, DME, RAN NF OAM, mock O1 adaptor) is tested in
`tests_integration/test_cross_service.py`. The `client` fixture and `FakeR1Response` are imported from `test_main.py`, so this file needs `tests/` on the import path (pytest's default when run from
`smo/sa-smos`). Run: `cd smo/sa-smos && PYTHONPATH=.:../shared python -m pytest tests/test_o1cm.py -q`.
"""

import uuid

import pytest

from test_main import FakeR1Response, client  # noqa: F401  (pytest fixture)


def _intent(intent_id, targets, instance="gnb-du-01", cells=("101",), admin="ACTIVATED", rmih="sa-smos"):
    """Builds an Intent as Intent Service returns it: one RAN_SUBNETWORK expectation `e1` with `targets`, the managed element `instance`, a Cell context of `cells`, the admin state and the owning RMIH.
    """
    obj = {"objectType": "RAN_SUBNETWORK"}
    if instance:
        obj["objectInstance"] = instance
    if cells:
        obj["objectContexts"] = [{"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF", "contextValueRange": list(cells)}]
    return {"intentId": str(intent_id), "rmihId": rmih, "rmioId": "es-rapp", "intentAdminState": admin,
            "attributes": {"intentExpectations": [{"expectationId": "e1", "expectationObject": obj, "expectationTargets": targets}]}}


@pytest.fixture
def platform(monkeypatch):
    """Fakes Intent Service and DME for the O1-CM handler and returns the state the tests read and set.

    `state["intents"]` holds the intents served by id; `posts` and `deletes` record the calls; `action_status` is the status DME gives every new action (default COMPLETED). The CM-target lookup answers an empty
    registry, so the handler uses its defaults.
    """
    state = {"intents": {}, "posts": [], "deletes": [], "action_status": "COMPLETED"}

    def get(self, path, **kw):
        if path == "/intent-service/intent-handling-functions":
            return FakeR1Response(200, {"items": []})  # unregistered -> handler defaults
        intent = state["intents"].get(path.rsplit("/", 1)[-1])
        return FakeR1Response(200, intent) if intent else FakeR1Response(404, {})

    def post(self, path, json=None, **kw):
        state["posts"].append((path, json))
        if path == "/dme/actions":
            return FakeR1Response(202, {"actionId": str(uuid.uuid4()), "forwardedJobId": str(uuid.uuid4()),
                                        "status": state["action_status"]})
        if path == "/intent-service/intent-reports":
            return FakeR1Response(201, {"reportId": str(uuid.uuid4())})
        return FakeR1Response(201, {})

    monkeypatch.setattr("app.o1cm.R1Client.get", get)
    monkeypatch.setattr("app.o1cm.R1Client.post", post)
    monkeypatch.setattr("app.o1cm.R1Client.delete", lambda self, path, **kw: state["deletes"].append(path) or FakeR1Response(204))
    return state


LOCK = {"targetName": "NRCellDU.administrativeState", "targetCondition": "IS_EQUAL_TO", "targetValueRange": "LOCKED"}


def test_registration_declares_the_cm_targets(client, platform):
    """Registration is one POST of the default CM targets in order (Intent Service updates an earlier one in place, call flow 09c, so nothing is deleted), and
    a target name without a '.' is refused."""
    resp = client.post("/o1-cm-handler/registration", json={})
    assert resp.status_code == 201
    path, body = platform["posts"][0]
    assert path == "/intent-service/intent-handling-functions" and body["rmihId"] == "sa-smos"
    names = [t["supportedTargetName"] for t in body["intentHandlingCapabilityList"][0]["supportedExpectationTargetInfoList"]]
    assert names == ["NRCellDU.administrativeState", "CESManagementFunction.energySavingControl",
                     "NRCellRelation.cellIndividualOffset", "CommonBeamformingFunction.digitalTilt",
                     "NRSectorCarrier.configuredMaxTxPower", "NRFreqRelation.cellReselectionPriority"]
    assert platform["deletes"] == []  # deleting first used to cascade away every intent addressed to the handler
    assert client.post("/o1-cm-handler/registration", json={"cmTargets": {"noDot": []}}).status_code == 422


@pytest.mark.parametrize("answer, expected", [
    (FakeR1Response(204), 204), (FakeR1Response(404, {}), 204),
    (FakeR1Response(409, {"detail": {"title": "INTENT_HANDLING_FUNCTION_IN_USE", "detail": "2 intent(s)"}}), 409),
    (FakeR1Response(500, {}), 502),
])
def test_deregistration_passes_on_intent_services_refusal(client, platform, monkeypatch, answer, expected):
    """Call flow 09c: deregistering while intents are addressed to the handler is Intent Service's 409, passed on; another failure is 502; gone is 204."""
    monkeypatch.setattr("app.o1cm.R1Client.delete", lambda self, path, **kw: answer)
    assert client.delete("/o1-cm-handler/registration").status_code == expected


def test_enacts_each_cell_and_reports_fulfilled(client, platform):
    """One expectation with two cells gives one DME action with a change per cell, carrying the intent and expectation ids as source context, and a FULFILLED report with the achieved value.
    """
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK], cells=("101", "102"))
    enactment = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    assert enactment["status"] == "FULFILLED"
    action = next(body for path, body in platform["posts"] if path == "/dme/actions")
    assert action["changes"] == [
        {"managedElementRef": "gnb-du-01", "className": "NRCellDU", "attributeChanges": {"administrativeState": "LOCKED"},
         "managedFunctionRef": "NRCellDU=101"},
        {"managedElementRef": "gnb-du-01", "className": "NRCellDU", "attributeChanges": {"administrativeState": "LOCKED"},
         "managedFunctionRef": "NRCellDU=102"}]
    assert action["sourceContext"] == {"intentId": str(intent_id), "expectationId": "e1", "rmioId": "es-rapp"}
    report = next(body for path, body in platform["posts"] if path == "/intent-service/intent-reports")
    result = report["intentFulfilmentReport"]["expectationFulfilmentResult"][0]
    assert result["targetFulfilmentResults"][0]["targetAchievedValue"] == "LOCKED"


def test_failed_config_job_reports_degraded(client, platform):
    """A DME action whose status is not COMPLETED makes the intent NOT_FULFILLED and DEGRADED, with the action status in the reasons."""
    platform["action_status"] = "PARTIAL_SUCCESS"
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    enactment = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    assert enactment["status"] == "NOT_FULFILLED"
    info = next(b for p, b in platform["posts"] if p == "/intent-service/intent-reports")["intentFulfilmentReport"]["intentFulfilmentInfo"]
    assert info["notFullfilledState"] == "DEGRADED" and "PARTIAL_SUCCESS" in info["notFulfilledReasons"][0]


# One row per reason a target is not enacted: not a CM target, a condition other than IS_EQUAL_TO, a value outside the allowed list, and no managed element.
@pytest.mark.parametrize("target, instance, reason", [
    ({"targetName": "RANEnergyConsumption", "targetCondition": "IS_LESS_THAN", "targetValueRange": 1}, "gnb", "not a CM target"),
    ({**LOCK, "targetCondition": "IS_ONE_OF"}, "gnb", "IS_EQUAL_TO"),
    ({**LOCK, "targetValueRange": "HALF_LOCKED"}, "gnb", "one of"),
    (LOCK, None, "objectInstance"),
])
def test_unenactable_targets_are_reported_not_written(client, platform, target, instance, reason):
    """An unsupported target is reported with its reason, the intent is NOT_FULFILLED, and no DME action is posted."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [target], instance=instance)
    enactment = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    assert enactment["status"] == "NOT_FULFILLED"
    assert reason in enactment["unsupportedTargets"][0]["reason"]
    assert not [p for p, _ in platform["posts"] if p == "/dme/actions"]


def test_deactivated_foreign_and_unknown_intents(client, platform):
    """A DEACTIVATED intent is SKIPPED, one addressed to another RMIH is 422, an unknown one is 404, and none of them records an enactment."""
    a, b = uuid.uuid4(), uuid.uuid4()
    platform["intents"][str(a)] = _intent(a, [LOCK], admin="DEACTIVATED")
    platform["intents"][str(b)] = _intent(b, [LOCK], rmih="so-smos")
    assert client.post("/o1-cm-handler/intents", json={"intentId": str(a)}).json()["status"] == "SKIPPED"
    assert client.post("/o1-cm-handler/intents", json={"intentId": str(b)}).status_code == 422
    assert client.post("/o1-cm-handler/intents", json={"intentId": str(uuid.uuid4())}).status_code == 404
    assert client.get("/o1-cm-handler/enactments").json()["total"] == 0


def _posted(platform, path):
    """The bodies the handler posted to `path`, in order."""
    return [body for p, body in platform["posts"] if p == path]


def test_a_re_pushed_intent_is_answered_from_its_enactment_and_reported_once(client, platform):
    """SA-REPLAY: Intent Service's outbox pushes again when the first push got no answer in time. The second push is answered with the recorded enactment,
    `replayed: true`, and writes no second DME action, records no second enactment and publishes no second IntentReport."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    first = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    again = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    assert first["replayed"] is False and again["replayed"] is True
    assert {k: v for k, v in again.items() if k != "replayed"} == {k: v for k, v in first.items() if k != "replayed"}
    assert len(_posted(platform, "/dme/actions")) == 1 and len(_posted(platform, "/intent-service/intent-reports")) == 1
    assert client.get("/o1-cm-handler/enactments").json()["total"] == 1


def test_two_pushes_at_once_record_one_enactment_and_the_second_answers_the_first(client, platform, monkeypatch):
    """Two pushes of one intent that both pass the lookup: the unique index on intent_id keeps one row. Here the lookup of the second push misses the first
    one's row (not yet committed when it looked); its insert then fails, it rolls back and answers the first enactment, replayed, without a second report.
    Both sent the same uuid5 action id, so DME applied the change once (W10-18)."""
    import app.o1cm
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    first = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    real_lookup, looked = app.o1cm._enactment_of, []

    def lookup(db, iid):
        # the first lookup of the second push is too early to see the first push's row; later lookups see it
        if not looked:
            looked.append(iid)
            return None
        return real_lookup(db, iid)
    monkeypatch.setattr("app.o1cm._enactment_of", lookup)
    second = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)})
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True and second.json()["enactmentId"] == first["enactmentId"]
    assert client.get("/o1-cm-handler/enactments").json()["total"] == 1
    assert len(_posted(platform, "/intent-service/intent-reports")) == 1
    ids = [body["actionId"] for body in _posted(platform, "/dme/actions")]
    assert ids == [str(uuid.uuid5(app.o1cm.ACTION_ID_NAMESPACE, f"{intent_id}:e1"))] * 2


def test_an_action_dme_answers_as_a_replay_is_judged_by_its_first_status(client, platform, monkeypatch):
    """When a push is enacted again after a crash before its record was committed, DME answers the repeated action id IGNORED with the first status;
    that first status decides fulfilment, and the action is marked `replayed`."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    monkeypatch.setattr("app.o1cm.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(200, {
        "actionId": json["actionId"], "status": "IGNORED", "originalStatus": "COMPLETED"}) if path == "/dme/actions" else FakeR1Response(201, {"reportId": str(uuid.uuid4())}))
    enactment = client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()
    assert enactment["status"] == "FULFILLED" and enactment["actions"][0]["replayed"] is True


def test_a_cio_target_writes_the_offset_on_each_named_relation(client, platform):
    """A cell individual offset target writes the six-value offset on each relation named by the Cell context (NRCellRelation=<id>)."""
    intent_id = uuid.uuid4()
    cio = {"targetName": "NRCellRelation.cellIndividualOffset", "targetCondition": "IS_EQUAL_TO",
           "targetValueRange": [2, 2, 2, 2, 2, 2]}
    platform["intents"][str(intent_id)] = _intent(intent_id, [cio], cells=("201-202",))
    assert client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()["status"] == "FULFILLED"
    action = next(body for path, body in platform["posts"] if path == "/dme/actions")
    assert action["changes"][0]["managedFunctionRef"] == "NRCellRelation=201-202"
    assert action["changes"][0]["attributeChanges"] == {"cellIndividualOffset": [2, 2, 2, 2, 2, 2]}


def test_tilt_and_power_targets_write_each_named_cell(client, platform):
    """A digital tilt target writes its integer value on the IOC the target names, for each named cell (CommonBeamformingFunction=<cell>)."""
    intent_id = uuid.uuid4()
    tilt = {"targetName": "CommonBeamformingFunction.digitalTilt", "targetCondition": "IS_EQUAL_TO", "targetValueRange": 70}
    platform["intents"][str(intent_id)] = _intent(intent_id, [tilt], cells=("301",))
    assert client.post("/o1-cm-handler/intents", json={"intentId": str(intent_id)}).json()["status"] == "FULFILLED"
    action = next(body for path, body in platform["posts"] if path == "/dme/actions")
    assert action["changes"][0]["managedFunctionRef"] == "CommonBeamformingFunction=301"
    assert action["changes"][0]["attributeChanges"] == {"digitalTilt": 70}



# --- INTENT-LIFECYCLE-PUSH: Intent Service pushes an admin-state change and a delete too -------------------------------------------------------

def _push(client, intent_id, event=None, admin=None):
    """Posts one push of Intent Service for `intent_id`, with `event` and `adminState` when given (none: a push of a release before the events)."""
    body = {"intentId": str(intent_id), "expectationObjectTypes": ["RAN_SUBNETWORK"], "rmioId": "es-rapp"}
    if event:
        body["event"] = event
    if admin:
        body["adminState"] = admin
    return client.post("/o1-cm-handler/intents", json=body)


def test_an_intent_created_deactivated_is_enacted_once_when_it_is_activated(client, platform):
    """An intent created DEACTIVATED is SKIPPED; the ADMIN_STATE_CHANGED push of its activation enacts it, one DME action and one report; a repeat of the
    activation push and a late repeat of the create push are answered from the enactment and do nothing more."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK], admin="DEACTIVATED")
    assert _push(client, intent_id, "INTENT_CREATED", "DEACTIVATED").json()["status"] == "SKIPPED"
    assert _posted(platform, "/dme/actions") == []
    platform["intents"][str(intent_id)]["intentAdminState"] = "ACTIVATED"
    enacted = _push(client, intent_id, "ADMIN_STATE_CHANGED", "ACTIVATED").json()
    assert enacted["status"] == "FULFILLED" and enacted["replayed"] is False
    assert _push(client, intent_id, "ADMIN_STATE_CHANGED", "ACTIVATED").json()["replayed"] is True
    assert _push(client, intent_id, "INTENT_CREATED", "DEACTIVATED").json()["replayed"] is True
    assert len(_posted(platform, "/dme/actions")) == 1 and len(_posted(platform, "/intent-service/intent-reports")) == 1
    assert client.get("/o1-cm-handler/enactments").json()["total"] == 1


def test_deactivation_and_reactivation_are_recorded_and_nothing_is_written(client, platform):
    """Deactivating an enacted intent sets `deactivatedAt` and sends nothing to DME or Intent Service's reports; a repeat keeps the first time;
    reactivation clears it and does not enact the intent again."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    assert _push(client, intent_id).json()["deactivatedAt"] is None     # a push without an event is a create push
    posts = len(platform["posts"])
    platform["intents"][str(intent_id)]["intentAdminState"] = "DEACTIVATED"
    first = _push(client, intent_id, "ADMIN_STATE_CHANGED", "DEACTIVATED").json()
    again = _push(client, intent_id, "ADMIN_STATE_CHANGED", "DEACTIVATED").json()
    assert first["deactivatedAt"] and again["deactivatedAt"] == first["deactivatedAt"] and again["replayed"] is True
    platform["intents"][str(intent_id)]["intentAdminState"] = "ACTIVATED"
    reactivated = _push(client, intent_id, "ADMIN_STATE_CHANGED", "ACTIVATED").json()
    assert reactivated["deactivatedAt"] is None and reactivated["replayed"] is True
    assert len(platform["posts"]) == posts                                # no DME action, no report since the enactment


def test_a_late_deactivation_push_does_not_undo_a_later_activation(client, platform):
    """The handler acts on the state it reads back, not on the push's `adminState`: a DEACTIVATED push delivered after the intent was activated again
    records nothing."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    _push(client, intent_id, "INTENT_CREATED", "ACTIVATED")
    assert _push(client, intent_id, "ADMIN_STATE_CHANGED", "DEACTIVATED").json()["deactivatedAt"] is None


def test_a_delete_is_recorded_once_without_reading_the_intent_back_or_writing(client, platform):
    """INTENT_DELETED sets `deletedAt` on the enactment from the push alone (the intent is gone, 404), a repeat keeps the first time, and nothing is sent to
    DME; a delete of an intent this handler never enacted is SKIPPED, 200."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    _push(client, intent_id, "INTENT_CREATED", "ACTIVATED")
    del platform["intents"][str(intent_id)]
    posts = len(platform["posts"])
    first = _push(client, intent_id, "INTENT_DELETED", "ACTIVATED")
    again = _push(client, intent_id, "INTENT_DELETED", "ACTIVATED").json()
    assert first.status_code == 200 and first.json()["deletedAt"] and again["deletedAt"] == first.json()["deletedAt"]
    assert len(platform["posts"]) == posts
    unknown = _push(client, uuid.uuid4(), "INTENT_DELETED", "DEACTIVATED")
    assert unknown.status_code == 200 and unknown.json()["status"] == "SKIPPED"


def test_an_admin_state_push_for_an_intent_gone_or_foreign_is_refused_like_a_create(client, platform):
    """ADMIN_STATE_CHANGED reads the intent back: an intent Intent Service no longer knows is 404 and one addressed to another handler is 422, both of
    which Intent Service's outbox counts as delivered (no retry)."""
    foreign = uuid.uuid4()
    platform["intents"][str(foreign)] = _intent(foreign, [LOCK], rmih="so-smos")
    assert _push(client, uuid.uuid4(), "ADMIN_STATE_CHANGED", "ACTIVATED").status_code == 404
    assert _push(client, foreign, "ADMIN_STATE_CHANGED", "ACTIVATED").status_code == 422


def test_an_unknown_event_is_answered_ignored_and_does_nothing(client, platform):
    """Forward compatibility: an event this handler does not know is 200 IGNORED, with nothing read back, written or recorded."""
    intent_id = uuid.uuid4()
    platform["intents"][str(intent_id)] = _intent(intent_id, [LOCK])
    resp = _push(client, intent_id, "INTENT_MODIFIED", "ACTIVATED")
    assert resp.status_code == 200 and resp.json()["status"] == "IGNORED"
    assert platform["posts"] == [] and client.get("/o1-cm-handler/enactments").json()["total"] == 0
