"""Call flow 12 across the modules (DOC-CF-1.8): RAN NF OAM, an SMO module, delivers PM records into an rApp's data job; another rApp can neither read,
change nor feed that job; and the job's owner pages its records by a cursor.

The rApp calls carry the headers R1 Termination would stamp (`X-R1-Role: rapp`, `X-R1-Invoker-Id`); the modules' own calls go through the mesh.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_dme_data_ownership.py -q
"""

from test_cross_service import _seed_managed_element

OWNER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "flow12-consumer"}
ROGUE = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "flow12-rogue"}


def test_a_modules_records_reach_the_owner_and_no_other_rapp(mesh, loaded_apps, db_connection):
    """12a and 12b: a PM subscription makes RAN NF OAM the producer of the counter type; the rApp's job on it receives RAN NF OAM's records; another rApp
    sees no such job, cannot change it and cannot deliver to it; the owner pages the records newest first with `before`."""
    _seed_managed_element(loaded_apps, db_connection, "gnb-flow12")
    assert mesh["ran-nf-oam"].post("/pm-subscriptions", params={"managed_element_ref": "gnb-flow12", "counter_type": "Flow12Counter",
                                                                "delivery_method": "pull"}).status_code == 200
    type_id = next(t["dmeTypeId"] for t in mesh["dme"].get("/dme-types", params={"data_category": "RAN"}).json()
                   if t["typeName"] == "RAN.PMCounters.Flow12Counter")
    dme = mesh["dme"]
    job = dme.post("/data-jobs", headers=OWNER, json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id, "consumerId": "flow12-consumer",
                                                      "dataDeliveryMethod": "PULL_HTTP"}).json()["dataJobId"]
    for n in range(3):
        assert dme.post(f"/data-jobs/{job}/records", json={"payload": {"n": n}}).status_code == 201     # RAN NF OAM's delivery, through the mesh

    assert dme.get(f"/data-jobs/{job}/records", headers=ROGUE).status_code == 404
    assert job not in {j["dataJobId"] for j in dme.get("/data-jobs", headers=ROGUE).json()["items"]}
    assert dme.delete(f"/data-jobs/{job}", headers=ROGUE).status_code == 204          # not seen: nothing done
    assert dme.post(f"/data-jobs/{job}/records", json={"payload": {"forged": True}}, headers=ROGUE).status_code == 403

    first = dme.get(f"/data-jobs/{job}/records", params={"limit": 2}, headers=OWNER).json()["items"]
    rest = dme.get(f"/data-jobs/{job}/records", params={"limit": 2, "before": first[-1]["recordId"]}, headers=OWNER).json()["items"]
    assert [r["payload"]["n"] for r in first + rest] == [2, 1, 0]
