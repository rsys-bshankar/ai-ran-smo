"""Call flow 11 across the modules (DOC-CF-1.8): a data job follows its type's producers (ACTIVE, NO_PRODUCER when the last one leaves, ACTIVE again when one joins),
an rApp may not delete a type, and RAN NF OAM's PM subscription registers it as the producer of its counters.

The rApp calls carry the headers R1 Termination would stamp (`X-R1-Role: rapp`, `X-R1-Invoker-Id`); the modules' own calls go through the mesh.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_dme_producer_lifecycle.py -q
"""

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "producer-a"}


def _register(dme, producer):
    """Registers `producer` (an rApp, under its own invoker id) for RAN/Flow11Type/1.0.0; returns the type id."""
    resp = dme.post("/production-capabilities", headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": producer}, json={
        "namespace": "RAN", "name": "Flow11Type", "version": "1.0.0", "typeName": "RAN.Flow11Type", "producerId": producer,
        "dataProductionSchema": {"type": "object"}, "producerHealthCallbackUrl": f"http://{producer}:8000/health",
        "jobCallbackUrl": f"http://{producer}:8000/jobs"})
    assert resp.status_code == 201, resp.text
    return resp.json()["registrationId"]


def test_a_job_follows_its_types_producers_and_an_rapp_cannot_delete_the_type(mesh):
    """11a, 11b and 11d: two producers, one leaves (ACTIVE), the other leaves (NO_PRODUCER), a third joins (ACTIVE); an rApp's type deletion is 403."""
    dme = mesh["dme"]
    type_id = _register(dme, "producer-a")
    _register(dme, "producer-b")
    job = dme.post("/data-jobs", json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id, "consumerId": "consumer-rapp",
                                       "dataDeliveryMethod": "PUSH_HTTP"}).json()["dataJobId"]
    status = lambda: dme.get(f"/data-jobs/{job}/status").json()["status"]  # noqa: E731
    assert dme.delete("/production-capabilities", params={"producer_id": "producer-a"}, headers=RAPP).status_code == 204
    assert status() == "ACTIVE"
    assert dme.delete("/production-capabilities", params={"producer_id": "producer-b"},
                      headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "producer-b"}).status_code == 204
    assert status() == "NO_PRODUCER"

    refused = dme.delete(f"/dme-types/{type_id}", headers=RAPP)
    assert refused.status_code == 403 and refused.json()["detail"]["title"] == "ROLE_NOT_PERMITTED"
    _register(dme, "producer-c")
    assert status() == "ACTIVE"


def test_a_pm_subscription_registers_ran_nf_oam_as_the_producer(mesh, loaded_apps, db_connection):
    """11a: RAN NF OAM is a producer of the PM counter type once a PM subscription exists, registered before the subscription was stored."""
    from test_cross_service import _seed_managed_element
    _seed_managed_element(loaded_apps, db_connection, "gnb-flow11")
    assert mesh["ran-nf-oam"].post("/pm-subscriptions", params={"managed_element_ref": "gnb-flow11", "counter_type": "Flow11Counter",
                                                                "delivery_method": "pull"}).status_code == 200
    types = [t for t in mesh["dme"].get("/dme-types", params={"data_category": "RAN"}).json() if t["typeName"] == "RAN.PMCounters.Flow11Counter"]
    assert [t["producerIds"] for t in types] == [["ran-nf-oam"]]
