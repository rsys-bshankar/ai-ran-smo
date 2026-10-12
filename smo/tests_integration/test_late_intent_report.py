"""The fulfilment report of a dispatched Intent can arrive after the dispatch answers, and the rApps wait for it (the Deploy gate's intermittent `KeyError: 'actionId'` in
demo_09, decision 301 `{"path": "INTENT", "status": "NOT_ENACTED"}`).

Intent Service does not enact anything: it queues the push to the handler (SA SMOS's O1-CM handler) as an outbox row and sends it right after the commit with a 2 s timeout; a
push that gets no answer in time is retried by the delivery sweep a few seconds later, and the handler publishes the report only when it has enacted the change. Under load on a
live stack the first send times out, so the report is not there when the rApp reads it right after the dispatch.

Here the first send to the handler is made to "time out" (nothing delivered, a retryable failure, as `outbox._send` returns it), and the delivery sweep runs each time the rApp
sleeps between two reads of the reports.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_late_intent_report.py -q
"""

import datetime
import time
import types

from coverage_env import RAPP, Clock, decision, evaluate, scenario
from smo_shared import outbox


def _handler_push_times_out_once(monkeypatch, db_connection, loaded_apps, wait_seconds):
    """Helper: makes the first push to the O1-CM handler time out and the delivery sweep deliver it when the rApp next sleeps; returns the list of sweeps run.

    `wait_seconds` is how long the rApp may wait for the report (0 is the behaviour before the fix: one read, no wait).
    """
    real_send, sweeps, pushed = outbox._send, [], []

    def send(destination, payload, method="POST"):
        if "/o1-cm-handler/" in destination and not pushed:
            pushed.append(destination)
            return False, "no answer from the destination", True           # what a send that ran into the 2 s timeout returns
        return real_send(destination, payload, method)

    def sleep(seconds):                                                    # the rApp is waiting for the report: the worker's sweep runs
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5)
        sweeps.append(outbox.drain(db_connection, now=later))

    monkeypatch.setattr(outbox, "_send", send)
    main = loaded_apps[RAPP]
    monkeypatch.setattr(main, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    monkeypatch.setattr(main, "INTENT_REPORT_WAIT_SECONDS", wait_seconds)
    return sweeps


def test_a_push_that_timed_out_is_enacted_by_the_retry_and_the_rapp_waits_for_the_report(mesh, loaded_apps, monkeypatch, db_connection):
    """The first send to the handler gets no answer; the sweep retries it while the rApp polls; the change is enacted, verified and EXECUTED, not NOT_ENACTED."""
    iid = scenario(mesh, loaded_apps, monkeypatch, {"rapp": "AUTONOMOUS"})["rapp"]["instanceId"]
    Clock(mesh, loaded_apps).hour({"301": "OVERSHOOT"})
    sweeps = _handler_push_times_out_once(monkeypatch, db_connection, loaded_apps, wait_seconds=30.0)
    d301 = decision(evaluate(mesh, iid), "301")
    assert d301["action"]["status"] == "COMPLETED" and d301["outcome"] == "EXECUTED"
    assert sweeps and sweeps[0]["sent"] >= 1


def test_without_the_wait_the_same_late_report_is_the_not_enacted_failure(mesh, loaded_apps, monkeypatch, db_connection):
    """Pins the failure the wait removes: with no wait the rApp reads once, finds no report and records NOT_ENACTED (what the Deploy gate showed)."""
    iid = scenario(mesh, loaded_apps, monkeypatch, {"rapp": "AUTONOMOUS"})["rapp"]["instanceId"]
    Clock(mesh, loaded_apps).hour({"301": "OVERSHOOT"})
    _handler_push_times_out_once(monkeypatch, db_connection, loaded_apps, wait_seconds=0.0)
    d301 = decision(evaluate(mesh, iid), "301")
    assert d301["action"] == {"path": "INTENT", "status": "NOT_ENACTED"}
