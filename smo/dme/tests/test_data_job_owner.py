"""OI-7-rapp-data-jobs-teardown: DME records who created a data job (`created_by`, revision 0045) and deletes every job one invoker created in one call
(`DELETE /data-jobs?created_by=`), which rApp Management uses when it tears an instance down. Run: `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_data_job_owner.py -q`.
"""

from test_main import client, register_type_body  # noqa: F401  (client: a pytest fixture)

INSTANCE = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "api-invoker-instance-1"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "api-invoker-instance-2"}
RAPP_MGMT = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "rapp-mgmt"}


def _job(client, type_id, headers=None, consumer="energy-saving-rapp:i-1"):
    """Creates a pull job of `type_id` as the caller in `headers`; returns its id."""
    return client.post("/data-jobs", headers=headers or {}, json={"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": type_id,
                                                                 "dataDeliveryMethod": "PULL_HTTP", "consumerId": consumer}).json()["dataJobId"]


def test_a_job_records_the_invoker_that_created_it(client):
    """`createdBy` is the invoker id the gateway vouched for, not anything in the body; a call that did not come through the gateway leaves it null."""
    type_id = client.post("/production-capabilities", json=register_type_body()).json()["registrationId"]
    assert client.get(f"/data-jobs/{_job(client, type_id, INSTANCE)}").json()["createdBy"] == "api-invoker-instance-1"
    assert client.get(f"/data-jobs/{_job(client, type_id)}").json()["createdBy"] is None


def test_every_job_one_invoker_created_is_deleted_and_no_other(client, monkeypatch):
    """rApp Management's teardown call deletes the instance's jobs (whatever consumer id the rApp gave them) and leaves another instance's job, and a
    job with no recorded creator, alone; the producers are told as for any terminated job."""
    monkeypatch.setattr("app.main.httpx.delete", lambda url, timeout=None: None)
    type_id = client.post("/production-capabilities", json=register_type_body()).json()["registrationId"]
    mine = [_job(client, type_id, INSTANCE, "energy-saving-rapp:i-1"), _job(client, type_id, INSTANCE, "something-else")]
    others = [_job(client, type_id, OTHER, "energy-saving-rapp:i-2"), _job(client, type_id)]
    assert client.delete("/data-jobs", params={"created_by": "api-invoker-instance-1"}, headers=RAPP_MGMT).status_code == 204
    assert [client.get(f"/data-jobs/{j}").status_code for j in mine] == [404, 404]
    assert [client.get(f"/data-jobs/{j}").status_code for j in others] == [200, 200]


def test_an_rapp_may_delete_only_the_jobs_it_created(client):
    """An rApp naming another invoker in `created_by` is refused (403 DATA_JOBS_NOT_OWNER) and nothing is deleted; naming itself is allowed."""
    type_id = client.post("/production-capabilities", json=register_type_body()).json()["registrationId"]
    job = _job(client, type_id, INSTANCE)
    refused = client.delete("/data-jobs", params={"created_by": "api-invoker-instance-1"}, headers=OTHER)
    assert refused.status_code == 403 and "DATA_JOBS_NOT_OWNER" in refused.text
    assert client.get(f"/data-jobs/{job}").status_code == 200
    assert client.delete("/data-jobs", params={"created_by": "api-invoker-instance-1"}, headers=INSTANCE).status_code == 204
    assert client.get(f"/data-jobs/{job}").status_code == 404


def test_exactly_one_of_consumer_id_and_created_by_is_given(client):
    """Neither, or both, is a 422: a bulk delete with no filter must never delete every job."""
    assert client.delete("/data-jobs").status_code == 422
    assert client.delete("/data-jobs", params={"consumer_id": "a", "created_by": "b"}).status_code == 422
