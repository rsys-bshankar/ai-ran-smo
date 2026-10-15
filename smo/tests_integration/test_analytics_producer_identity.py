"""Call flow 08 across the modules (DOC-CF-1.8): an rApp registers itself as an analytics producer (RAN Analytics enrols it in SME and publishes
`mdaf.<type>.<producer>`), MDAF asks RAN Analytics before taking the rApp's report, and an rApp cannot remove another consumer's subscription.

The calls carry the headers R1 Termination would stamp on an rApp's request (`X-R1-Role: rapp`, `X-R1-Invoker-Id`); the modules' own calls go through the mesh.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_analytics_producer_identity.py -q
"""

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "kpi-rapp"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rogue-rapp"}
REPORT = {"output": {"rrcSetupSuccess": 91.0}, "input_sources": []}


def test_a_registered_rapp_publishes_and_an_unregistered_one_is_refused(mesh):
    """08a and 08c: the producer's SME service is published under its own name; its report is taken and records it; a rogue rApp is refused, before and after it
    tries to register under the producer's id; after deregistration the producer may not publish and SME no longer lists the service."""
    ran = mesh["ran-analytics"]
    assert ran.post("/producers", params={"producer_id": "kpi-rapp", "analytics_type": "cell-kpi"},
                    json={"dme_input_types": [], "output_schema": {}}, headers=RAPP).status_code == 201
    services = mesh["sme"].get("/published-apis/v1/kpi-rapp/service-apis").json()
    names = [s["serviceName"] for s in (services["items"] if isinstance(services, dict) else services)]
    assert "mdaf.cell-kpi.kpi-rapp" in names

    taken = mesh["mdaf"].post("/reports", params={"analytics_type": "cell-kpi"}, json=REPORT, headers=RAPP)
    assert taken.status_code == 201
    assert mesh["mdaf"].get(f"/reports/{taken.json()['reportId']}").json()["publishedBy"] == "kpi-rapp"

    assert mesh["mdaf"].post("/reports", params={"analytics_type": "cell-kpi"}, json=REPORT, headers=OTHER).status_code == 403
    assert ran.post("/producers", params={"producer_id": "kpi-rapp", "analytics_type": "cell-kpi"},
                    json={"dme_input_types": [], "output_schema": {}}, headers=OTHER).status_code == 403

    assert ran.delete("/producers/kpi-rapp", params={"analytics_type": "cell-kpi"}, headers=RAPP).status_code == 204
    assert mesh["mdaf"].post("/reports", params={"analytics_type": "cell-kpi"}, json=REPORT, headers=RAPP).status_code == 403
    services = mesh["sme"].get("/published-apis/v1/kpi-rapp/service-apis").json()
    names = [s["serviceName"] for s in (services["items"] if isinstance(services, dict) else services)]
    assert "mdaf.cell-kpi.kpi-rapp" not in names


def test_an_rapp_cannot_silence_an_assurance_monitor(mesh):
    """08b with call flow 04a: SA SMOS's subscription for a monitor cannot be removed by an rApp."""
    monitor = mesh["sa-smos"].post("/monitors", params={"analytics_type": "cell-kpi"}, json={"rrcSetupSuccess": 98}).json()
    refused = mesh["mdaf"].delete(f"/subscriptions/{monitor['analyticsSubscriptionId']}", headers=OTHER)
    assert refused.status_code == 403 and refused.json()["detail"]["title"] == "SUBSCRIPTION_ACCESS_DENIED"
    assert [s["subscriptionId"] for s in mesh["mdaf"].get("/subscriptions", params={"analytics_type": "cell-kpi"}).json()["items"]] == [monitor["analyticsSubscriptionId"]]
