"""Wave 10.1 (HISTORY.md W10-17/W10-19/W10-20): per-function
addressing, NETCONF retries with backoff, the alarm raised when they are
exhausted, and read-after-write. Run with: pytest smo/ran-nf-oam/tests -q
"""

import pytest

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.netconf_client import EditResult

CHANGE = {"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=101",
          "attributeChanges": {"administrativeState": "LOCKED"}}


@pytest.fixture
def adaptor(monkeypatch):
    """Scripted edit-config outcomes, consumed one per attempt."""
    state = {"outcomes": [], "calls": [], "slept": []}

    def fake_send(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", managed_function_ref=None):
        state["calls"].append((target_ref, managed_function_ref, attribute_changes))
        return state["outcomes"].pop(0) if state["outcomes"] else EditResult(True)

    monkeypatch.setattr("app.main.send_edit_config", fake_send)
    monkeypatch.setattr("app.main._sleep", state["slept"].append)
    return state


def _write(client):
    job = client.post("/config-jobs", json={"requestedBy": "rapp", "scope": "cell", "changes": [CHANGE]}).json()
    return job, client.get(f"/config-jobs/{job['jobId']}").json()["subChanges"][0]


def test_the_managed_function_ref_is_dispatched(client, db_session_factory, adaptor):
    """The managed function ref of a change reaches the adaptor, so a per-cell change addresses the cell."""
    _make_me(db_session_factory)
    job, sub = _write(client)
    assert job["status"] == "COMPLETED" and sub["attempts"] == 1 and sub["managedFunctionRef"] == "NRCellDU=101"
    assert adaptor["calls"] == [("ME-1", "NRCellDU=101", {"administrativeState": "LOCKED"})]


def test_a_timeout_is_retried_with_backoff(client, db_session_factory, adaptor):
    """A timeout or unreachable adaptor is retried with the 5 and 10 second back-off and the change then succeeds, with no alarm."""
    _make_me(db_session_factory)
    adaptor["outcomes"] = [EditResult(False, "NETCONF_TIMEOUT"), EditResult(False, "NETCONF_UNREACHABLE")]
    job, sub = _write(client)
    assert job["status"] == "COMPLETED" and sub["attempts"] == 3
    assert adaptor["slept"] == [5.0, 10.0]
    assert client.get("/alarms", params={"managed_element_ref": "ME-1"}).json()["items"] == []


def test_exhausted_retries_fail_the_change_and_raise_an_alarm(client, db_session_factory, adaptor):
    """When the retries are used up the sub-change is REJECTED with the last reason, the job FAILED, and a major communications alarm names the
    target.
    """
    _make_me(db_session_factory)
    adaptor["outcomes"] = [EditResult(False, "NETCONF_TIMEOUT")] * 4
    job, sub = _write(client)
    assert job["status"] == "FAILED"
    assert (sub["status"], sub["rejectionReason"], sub["attempts"]) == ("REJECTED", "NETCONF_TIMEOUT", 4)
    assert adaptor["slept"] == [5.0, 10.0, 20.0]
    alarm = client.get("/alarms", params={"managed_element_ref": "ME-1"}).json()["items"][0]
    assert (alarm["probableCause"], alarm["alarmType"], alarm["severity"]) == ("NETCONF_TIMEOUT", "COMMUNICATIONS_ALARM", "major")
    assert "NRCellDU=101" in alarm["specificProblem"]


def test_an_rpc_error_is_not_retried(client, db_session_factory, adaptor):
    """An rpc-error answer is definite: one attempt, no retry and no alarm."""
    _make_me(db_session_factory)
    adaptor["outcomes"] = [EditResult(False, "NETCONF_RPC_FAILED")]
    job, sub = _write(client)
    assert (job["status"], sub["attempts"], sub["rejectionReason"]) == ("FAILED", 1, "NETCONF_RPC_FAILED")
    assert client.get("/alarms").json()["items"] == []


def test_read_after_write(client, db_session_factory, monkeypatch):
    """The config read returns the attributes the adaptor reports for the element or function, is 503 when the read fails, and an unknown element
    is a 404.
    """
    _make_me(db_session_factory)
    seen = []
    monkeypatch.setattr("app.main.send_get_config", lambda uri, ref, message_id, managed_function_ref=None:
                        seen.append((ref, managed_function_ref)) or {"administrativeState": "LOCKED"})
    resp = client.get("/managed-entities/ME-1/config", params={"managed_function_ref": "NRCellDU=101"})
    assert resp.json() == {"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=101",
                           "attributes": {"administrativeState": "LOCKED"}}
    assert seen == [("ME-1", "NRCellDU=101")]
    monkeypatch.setattr("app.main.send_get_config", lambda *a, **kw: None)
    assert client.get("/managed-entities/ME-1/config").status_code == 503
    ghost = client.get("/managed-entities/ghost/config", params={"managed_function_ref": ""})
    assert ghost.status_code == 404 and ghost.json()["detail"]["title"] == "MANAGED_ENTITY_NOT_FOUND"      # unknown: not found; known with no way to reach it: 503 (above)


def test_restconf_uses_the_same_retry_policy_and_alarm(client, db_session_factory, monkeypatch):
    """OI-1-cm-sync-restconf: a transient RESTCONF failure is retried like a
    NETCONF one; exhausting the retries raises the same alarm."""
    from app.restconf_client import RestconfResult

    _make_me(db_session_factory, protocol="RESTCONF")
    outcomes = [RestconfResult(False, "RESTCONF_TIMEOUT")] * 4
    monkeypatch.setattr("app.main.restconf_client.send_edit", lambda *a, **kw: outcomes.pop(0))
    slept = []
    monkeypatch.setattr("app.main._sleep", slept.append)

    job, sub = _write(client)

    assert (job["status"], sub["rejectionReason"], sub["attempts"]) == ("FAILED", "RESTCONF_TIMEOUT", 4)
    assert slept == [5.0, 10.0, 20.0]
    alarm = client.get("/alarms", params={"managed_element_ref": "ME-1"}).json()["items"][0]
    assert alarm["probableCause"] == "RESTCONF_TIMEOUT"


def test_a_restconf_error_reply_is_not_retried(client, db_session_factory, monkeypatch):
    """A RESTCONF errors answer is not retried: one attempt and the reason RESTCONF_REQUEST_FAILED."""
    from app.restconf_client import RestconfResult

    _make_me(db_session_factory, protocol="RESTCONF")
    outcomes = [RestconfResult(False, "RESTCONF_REQUEST_FAILED", "invalid-value")]
    monkeypatch.setattr("app.main.restconf_client.send_edit", lambda *a, **kw: outcomes.pop(0))
    job, sub = _write(client)
    assert (job["status"], sub["attempts"], sub["rejectionReason"]) == ("FAILED", 1, "RESTCONF_REQUEST_FAILED")


def test_read_after_write_over_restconf(client, db_session_factory, monkeypatch):
    """For a RESTCONF element the read goes to the RESTCONF client and never to NETCONF."""
    _make_me(db_session_factory, protocol="RESTCONF")
    monkeypatch.setattr("app.main.send_get_config", lambda *a, **kw: pytest.fail("should not use NETCONF"))
    monkeypatch.setattr("app.main.restconf_client.send_get", lambda uri, ref, message_id, managed_function_ref=None:
                        {"administrativeState": "LOCKED"})
    resp = client.get("/managed-entities/ME-1/config", params={"managed_function_ref": "NRCellDU=101"})
    assert resp.json()["attributes"] == {"administrativeState": "LOCKED"}


def test_pm_reports_are_delivered_to_every_data_job_of_the_counter_type(client, db_session_factory, monkeypatch):
    """Wave 10.1 (W10-04): O1 PM → RAN NF OAM → DME."""
    _make_me(db_session_factory)
    posted = []

    class Resp:
        status_code = 200            # an accepted call (call flow 11a reads the producer registration's answer)

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_get(self, path, **kw):
        if path == "/dme/dme-types":
            return Resp([{"dmeTypeId": "t-1", "typeName": "RAN.PMCounters.PRB_UTILIZATION"}])
        return Resp({"items": [{"dataJobId": "j-1"}, {"dataJobId": "j-2"}]})

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posted.append((path, json)) or Resp({}))
    report = {"managedElementRef": "ME-1", "counterType": "PRB_UTILIZATION",
              "measurements": [{"cellId": "101", "value": 3.2, "timestamp": "2026-01-01T00:00:00Z"}]}
    assert client.post("/pm-reports", json=report).status_code == 422  # no PM subscription yet
    client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": "PRB_UTILIZATION", "delivery_method": "pull"})
    posted.clear()
    out = client.post("/pm-reports", json=report).json()
    assert (out["dataJobs"], out["recordsDelivered"]) == (2, 2)
    assert posted[0] == ("/dme/data-jobs/j-1/records", {"payload": {
        "managedElementRef": "ME-1", "cellId": "101", "counter": "PRB_UTILIZATION", "value": 3.2,
        "timestamp": "2026-01-01T00:00:00+00:00"}})
    # DOC-CF-2.3: a record DME refuses (here the second job's, 409) is not counted as delivered
    refused = Resp({})
    refused.status_code = 409
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: refused if "j-2" in path else Resp({}))
    assert client.post("/pm-reports", json=report).json()["recordsDelivered"] == 1


def test_pm_reports_carry_multi_counter_per_relation_measurements(client, db_session_factory, monkeypatch):
    """Wave 10.2 (W10.2-03): handover counters per neighbour relation."""
    _make_me(db_session_factory)
    posted = []

    class Resp:
        status_code = 200            # an accepted call (call flow 11a reads the producer registration's answer)

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: Resp(
        [{"dmeTypeId": "t-1", "typeName": "RAN.PMCounters.HO_PERFORMANCE"}] if path == "/dme/dme-types"
        else {"items": [{"dataJobId": "j-1"}]}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posted.append(json) or Resp({}))
    client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": "HO_PERFORMANCE", "delivery_method": "pull"})
    posted.clear()
    counters = {"MM.HoExeAtt": 120, "MM.HoFailTooLate": 9}
    assert client.post("/pm-reports", json={"managedElementRef": "ME-1", "counterType": "HO_PERFORMANCE", "measurements": [
        {"cellId": "201", "relation": "201-202", "values": counters, "timestamp": "2026-01-01T00:00:00Z"}]}).status_code == 201
    assert posted[0]["payload"]["values"] == counters and posted[0]["payload"]["relation"] == "201-202"
    assert client.post("/pm-reports", json={"managedElementRef": "ME-1", "counterType": "HO_PERFORMANCE", "measurements": [
        {"cellId": "201", "timestamp": "2026-01-01T00:00:00Z"}]}).status_code == 422


# ---------------------------------------------------------------- the retry time budget (PR-ST-9)

class FakeClock:
    """Time that only moves when the code sleeps or an attempt 'takes' time, so the bound can be asserted exactly."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        self._monkeypatch = monkeypatch
        monkeypatch.setattr("app.main._monotonic", lambda: self.now)
        monkeypatch.setattr("app.main._sleep", self._sleep)

    def _sleep(self, seconds):
        self.now += seconds

    def dispatch_failing(self, attempt_seconds):
        """One sub-change against an adaptor whose every attempt takes `attempt_seconds` and fails transiently."""
        import app.main as main

        def send(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", managed_function_ref=None):
            self.now += attempt_seconds
            return EditResult(False, "NETCONF_TIMEOUT")

        self._monkeypatch.setattr("app.main.send_edit_config", send)
        applied, reason, attempts, _detail = main._dispatch_with_retries("http://adaptor", CHANGE, {"a": 1}, "job-1", "merge")
        return applied, reason, attempts


@pytest.fixture
def clock(monkeypatch):
    return FakeClock(monkeypatch)


def test_the_default_worst_case_is_the_budget_plus_one_attempt_in_flight():
    """The documented defaults hold: a 35 second retry budget, delays 0, 5, 10 and 20, and a 65 second worst case per sub-change."""
    import app.main as main
    assert main.DISPATCH_RETRY_BUDGET_SECONDS == 35.0 and main.NETCONF_RETRY_DELAYS == [0.0, 5.0, 10.0, 20.0]
    assert main.worst_case_dispatch_seconds() == 65.0


def test_an_adaptor_that_fails_fast_gets_the_whole_retry_schedule(clock):
    """An adaptor that refuses at once gets four attempts over the full 35 seconds of back-off."""
    applied, reason, attempts = clock.dispatch_failing(attempt_seconds=0)
    assert (applied, reason, attempts) == (False, "NETCONF_TIMEOUT", 4)
    assert clock.now == 35.0                                   # 5 + 10 + 20, the documented schedule, unchanged


def test_an_adaptor_that_waits_out_the_exchange_timeout_is_cut_off_inside_the_worst_case(clock):
    """An adaptor that waits out the 30 second timeout on each attempt gets two attempts, because the budget stops the retries, and the total stays
    within the worst case.
    """
    import app.main as main
    applied, reason, attempts = clock.dispatch_failing(attempt_seconds=30)
    assert (applied, attempts) == (False, 2)                   # not four attempts of 30 s: the budget stops it
    assert clock.now <= main.worst_case_dispatch_seconds() == 65.0


@pytest.mark.parametrize("attempt_seconds", [0, 1, 7, 15, 30])
def test_no_attempt_time_takes_one_sub_change_past_the_worst_case(clock, attempt_seconds):
    """Whatever time each attempt takes, one sub-change never exceeds the documented worst case."""
    import app.main as main
    clock.dispatch_failing(attempt_seconds)
    assert clock.now <= main.worst_case_dispatch_seconds()


def test_a_smaller_budget_stops_the_retries_earlier(clock, monkeypatch):
    """A smaller retry budget stops the retries earlier, and the worst case follows the budget."""
    monkeypatch.setattr("app.main.DISPATCH_RETRY_BUDGET_SECONDS", 12.0)
    applied, reason, attempts = clock.dispatch_failing(attempt_seconds=0)
    assert attempts == 2 and clock.now == 5.0                  # 0, then +5; the +10 would end at 15 s > 12 s
    import app.main as main
    assert main.worst_case_dispatch_seconds() == 12.0 + 30.0


def test_the_first_attempt_is_always_made_whatever_the_budget(clock, monkeypatch):
    """The first attempt is made even with a zero budget."""
    monkeypatch.setattr("app.main.DISPATCH_RETRY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr("app.main.NETCONF_RETRY_DELAYS", [7.0, 7.0])
    applied, reason, attempts = clock.dispatch_failing(attempt_seconds=0)
    assert attempts == 1 and reason == "NETCONF_TIMEOUT"
