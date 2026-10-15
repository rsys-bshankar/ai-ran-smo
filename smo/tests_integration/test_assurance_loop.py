"""Call flow 04 across the modules (DOC-CF-1.8): SA SMOS closes the assurance loop on MDAF's reports.

A monitor registered with an analytics type is subscribed at MDAF; a report MDAF stores is pushed by its notification outbox to SA SMOS, which reads it back from MDAF,
evaluates it and, on a new breach, runs its automatic action: here a CONFIG_CHANGE whose remedy RAN NF OAM writes to the mock O1 adaptor. Without an automatic action the
breach is escalated to the operator. The outbox is drained explicitly, as the worker's sweep would.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_assurance_loop.py -q
"""

import datetime

from sqlalchemy.orm import Session

from smo_shared import outbox

ME = "ME-assurance-1"
REMEDY = [{"managedElementRef": ME, "attributeChanges": {"administrativeState": "UNLOCKED"}}]


def _element(loaded_apps, db_connection):
    """A managed element RAN NF OAM writes to through the mock O1 adaptor."""
    oam = loaded_apps["ran-nf-oam"]
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        endpoint = oam.O1AdaptorEndpoint(managed_element_ref=ME, adaptor_uri="http://mock-o1-adaptor:8000/edit-config", protocol_support=["NETCONF"])
        session.add(endpoint)
        session.flush()
        session.add(oam.ManagedEntity(managed_element_ref=ME, entity_type="O-DU", o1_protocol="NETCONF", o1_adaptor_endpoint_id=endpoint.endpoint_id))
        session.commit()


def _publish(mesh, db_connection, output):
    """A producer publishes a `cell-kpi` report; the outbox is then drained so its push reaches SA SMOS. Returns the report id."""
    report_id = mesh["mdaf"].post("/reports", params={"analytics_type": "cell-kpi"}, json={"output": output, "input_sources": []}).json()["reportId"]
    outbox.drain(db_connection, now=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5))
    return report_id


def test_a_breach_in_a_pushed_report_runs_the_monitors_automatic_action(mesh, loaded_apps, db_connection):
    """04a-04c: the monitor subscribes at MDAF; a healthy report runs nothing; a breaching report makes SA SMOS write the remedy by itself, recorded RESOLVED, autoExecuted,
    naming the report; the element received the change; the next breaching report runs nothing more."""
    _element(loaded_apps, db_connection)
    sa = mesh["sa-smos"]
    monitor = sa.post("/monitors", params={"analytics_type": "cell-kpi"}, json={"rrcSetupSuccess": 98}).json()
    monitor_id = monitor["monitorId"]
    subscriptions = mesh["mdaf"].get("/subscriptions", params={"analytics_type": "cell-kpi"}).json()["items"]
    assert [s["subscriptionId"] for s in subscriptions] == [monitor["analyticsSubscriptionId"]]
    assert subscriptions[0]["notificationDestination"].endswith(f"/monitors/{monitor_id}/reports")
    assert sa.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY}).status_code == 200
    assert sa.put(f"/monitors/{monitor_id}/automation", json={"autoAction": "CONFIG_CHANGE"}).status_code == 200

    healthy = _publish(mesh, db_connection, {"rrcSetupSuccess": 99.2})
    assert sa.get(f"/monitors/{monitor_id}").json()["lastEvaluation"]["reportId"] == healthy
    assert sa.get("/remedial-actions", params={"monitor_id": monitor_id}).json()["items"] == []

    breaching = _publish(mesh, db_connection, {"rrcSetupSuccess": 91.0})
    last = sa.get(f"/monitors/{monitor_id}").json()["lastEvaluation"]
    assert (last["reportId"], last["breached"], last["breaches"]) == (breaching, True, {"rrcSetupSuccess": 98})
    [action] = sa.get("/remedial-actions", params={"monitor_id": monitor_id}).json()["items"]
    assert (action["actionType"], action["outcome"], action["autoExecuted"], action["reportId"]) == ("CONFIG_CHANGE", "RESOLVED", True, breaching)
    assert action["result"]["status"] == "COMPLETED"
    assert mesh["mock-o1-adaptor"].get(f"/edit-config/{ME}").json()["attributeChanges"] == {"administrativeState": "UNLOCKED"}

    _publish(mesh, db_connection, {"rrcSetupSuccess": 90.0})
    assert len(sa.get("/remedial-actions", params={"monitor_id": monitor_id}).json()["items"]) == 1


def test_a_breach_without_an_automatic_action_reaches_the_operator_queue(mesh, db_connection):
    """04b and 04d: with no automatic action the breach is an ESCALATE action in the operator's queue, saying which report breached what."""
    sa = mesh["sa-smos"]
    monitor_id = sa.post("/monitors", params={"analytics_type": "cell-kpi"}, json={"latencyMs": {"max": 20}}).json()["monitorId"]
    report_id = _publish(mesh, db_connection, {"latencyMs": 35})
    queue = sa.get("/remedial-actions", params={"outcome": "ESCALATED", "monitor_id": monitor_id}).json()["items"]
    assert [(a["actionType"], a["reportId"], a["requestedBy"]) for a in queue] == [("ESCALATE", report_id, "sa-smos")]
    assert "breach of latencyMs" in queue[0]["detail"]
