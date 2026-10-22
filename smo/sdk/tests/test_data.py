"""Tests of `sdk.data` (`DataClient`): that each method calls its DME or RAN NF OAM route with the expected verb, path, query and body, and that an error status becomes `SdkError`. The alarm scope has its own file (`test_alarm_scope.py`) and `get_dataset` is in `test_wave10_wrappers.py`.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_data.py -q`; no network and no database. The `r1` fixture (`conftest.py`) is a recording fake of `R1Client`: every call is kept as `{verb, path, params, json, files}` and answered from `r1.script(...)` (default: 200 with `{}`).
"""

import uuid

import pytest

from smo_sdk import data as smo_data
from smo_sdk._common import SdkError
from smo_sdk.data import DataClient
from smo_shared.r1_client import acting_for
from smo_shared.testing import FakeDmeR1


@pytest.fixture
def client(r1):
    """A `DataClient` over the recording fake."""
    return DataClient(r1)


def test_register_type(client, r1):
    """`register_type` posts the producer's type with its schema and callbacks, and the provenance fields null when not given."""
    client.register_type("RAN", "CoverageIssue", "1.0", "RAN.CoverageIssue", "rapp-1", {"type": "object"},
                          "http://x/health", "http://x/jobs", collection_spec={"a": 1})
    call = r1.calls[0]
    assert call == {"verb": "post", "path": "/dme/production-capabilities", "params": None, "files": None, "json": {
        "namespace": "RAN", "name": "CoverageIssue", "version": "1.0", "typeName": "RAN.CoverageIssue",
        "producerId": "rapp-1", "dataProductionSchema": {"type": "object"}, "collectionSpec": {"a": 1},
        "producerHealthCallbackUrl": "http://x/health", "jobCallbackUrl": "http://x/jobs",
        "sourceDomain": None, "sourceContext": None,
    }}


def test_own_invoker_id_is_what_the_r1_client_reports(client):
    """`own_invoker_id` asks the R1 client which invoker id its calls carry (what DME compares a producer id with, SEC-15.10b)."""
    assert client.own_invoker_id() == "invoker-1"


def test_register_type_with_source_provenance(client, r1):
    """A DIGITAL_TWIN source domain and its context reach the route, which drives the inference-eligibility rule."""
    client.register_type("RAN", "CoverageIssue", "1.0", "RAN.CoverageIssue", "rapp-1", {"type": "object"},
                          "http://x/health", "http://x/jobs", source_domain="DIGITAL_TWIN", source_context={"vendor": "acme"})
    call = r1.calls[0]
    assert call["json"]["sourceDomain"] == "DIGITAL_TWIN"
    assert call["json"]["sourceContext"] == {"vendor": "acme"}


def test_discover_types(client, r1):
    """`discover_types` filters by data category and returns the route's list."""
    r1.script(200, [{"dmeTypeId": "x"}])
    result = client.discover_types(data_category="RAN")
    assert r1.calls[0] == {"verb": "get", "path": "/dme/dme-types", "params": {"data_category": "RAN"}}
    assert result == [{"dmeTypeId": "x"}]


def test_list_producers(client, r1):
    """`list_producers` reads the producers and returns the list."""
    r1.script(200, [{"producerId": "rapp-1"}])
    result = client.list_producers()
    assert r1.calls[0] == {"verb": "get", "path": "/dme/production-capabilities", "params": None}
    assert result == [{"producerId": "rapp-1"}]


def test_get_producer(client, r1):
    """`get_producer` reads one producer by id."""
    client.get_producer("rapp-1")
    assert r1.calls[0] == {"verb": "get", "path": "/dme/production-capabilities/rapp-1", "params": None}


def test_deregister_producer(client, r1):
    """`deregister_producer` passes the producer id as a query parameter of the collection route."""
    client.deregister_producer("rapp-1")
    assert r1.calls[0] == {"verb": "delete", "path": "/dme/production-capabilities", "params": {"producer_id": "rapp-1"}}


def test_query_producer_status(client, r1):
    """`query_producer_status` reads the producer's status sub-resource."""
    client.query_producer_status("rapp-1")
    assert r1.calls[0]["path"] == "/dme/production-capabilities/rapp-1/status"


def test_create_data_job(client, r1):
    """`create_data_job` sends the type id as a string, the lifecycle stage, and empty dicts for the definition and details not given."""
    dme_type_id = uuid.uuid4()
    client.create_data_job(dme_type_id, "ONE_TIME", "PULL_HTTP", "consumer-1", production_job_definition={"a": 1},
                            lifecycle_stage="TRAINING")
    call = r1.calls[0]
    assert call["path"] == "/dme/data-jobs"
    assert call["json"] == {
        "dataDeliveryMode": "ONE_TIME", "dmeTypeId": str(dme_type_id), "productionJobDefinition": {"a": 1},
        "dataDeliveryMethod": "PULL_HTTP", "deliveryDetails": {}, "consumerId": "consumer-1",
        "lifecycleStage": "TRAINING",
    }


def test_get_data_job(client, r1):
    """`get_data_job` reads one job by id."""
    job_id = uuid.uuid4()
    client.get_data_job(job_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/dme/data-jobs/{job_id}", "params": None}


def test_update_data_job(client, r1):
    """`update_data_job` replaces the job with PUT."""
    job_id, dme_type_id = uuid.uuid4(), uuid.uuid4()
    client.update_data_job(job_id, dme_type_id, "ONE_TIME", "PULL_HTTP", "consumer-1")
    assert r1.calls[0]["verb"] == "put"
    assert r1.calls[0]["path"] == f"/dme/data-jobs/{job_id}"


def test_query_data_job_status(client, r1):
    """`query_data_job_status` reads the job's status sub-resource."""
    job_id = uuid.uuid4()
    client.query_data_job_status(job_id)
    assert r1.calls[0]["path"] == f"/dme/data-jobs/{job_id}/status"


def test_terminate_data_job(client, r1):
    """`terminate_data_job` deletes the job by id."""
    job_id = uuid.uuid4()
    client.terminate_data_job(job_id)
    assert r1.calls[0] == {"verb": "delete", "path": f"/dme/data-jobs/{job_id}", "params": None}


def test_terminate_data_jobs_for_consumer(client, r1):
    """`terminate_data_jobs_for_consumer` deletes every job of a consumer with one call on the collection."""
    client.terminate_data_jobs_for_consumer("rapp-1")
    assert r1.calls[0] == {"verb": "delete", "path": "/dme/data-jobs", "params": {"consumer_id": "rapp-1"}}


def test_list_data_jobs(client, r1):
    """`list_data_jobs` filters by consumer and sends the unset type id as null."""
    client.list_data_jobs(consumer_id="consumer-1")
    assert r1.calls[0] == {"verb": "get", "path": "/dme/data-jobs", "params": {"dme_type_id": None, "consumer_id": "consumer-1"}}


def test_create_data_offer(client, r1):
    """`create_data_offer` sends the delivery methods and the termination notification URI."""
    dme_type_id = uuid.uuid4()
    client.create_data_offer(dme_type_id, "ONE_TIME", ["PULL_HTTP"], "http://x/terminate")
    call = r1.calls[0]
    assert call["path"] == "/dme/offers"
    assert call["json"]["dataDeliveryMethods"] == ["PULL_HTTP"]
    assert call["json"]["dataOfferTerminationNotificationUri"] == "http://x/terminate"


def test_get_and_terminate_data_offer(client, r1):
    """An offer is read and terminated by id."""
    offer_id = uuid.uuid4()
    client.get_data_offer(offer_id)
    client.terminate_data_offer(offer_id)
    assert r1.calls[0] == {"verb": "get", "path": f"/dme/offers/{offer_id}", "params": None}
    assert r1.calls[1] == {"verb": "delete", "path": f"/dme/offers/{offer_id}", "params": None}


def test_list_data_offers(client, r1):
    """`list_data_offers` reads the collection with the unset type filter as null."""
    client.list_data_offers()
    assert r1.calls[0] == {"verb": "get", "path": "/dme/offers", "params": {"dme_type_id": None}}


def test_notify_data_available_sends_raw_unwrapped_body(client, r1):
    """`notify_data_available` sends the payload dict itself as the body, not wrapped in `{"payload": ...}`, because the route's only body parameter is that dict."""
    offer_id = uuid.uuid4()
    client.notify_data_available(offer_id, {"ready": True})
    assert r1.calls[0] == {"verb": "post", "path": f"/dme/offers/{offer_id}/notify", "params": None, "files": None, "json": {"ready": True}}


def test_subscribe_type_changes(client, r1):
    """`subscribe_type_changes` sends the notification destination and the owner."""
    client.subscribe_type_changes("http://x/notify", "owner-1")
    assert r1.calls[0]["json"] == {"notificationDestination": "http://x/notify", "owner": "owner-1"}


def test_list_and_get_and_unsubscribe_type_changes(client, r1):
    """Type subscriptions are listed by owner, read by id and deleted by id."""
    sub_id = uuid.uuid4()
    client.list_type_subscriptions(owner="owner-1")
    client.get_type_subscription(sub_id)
    client.unsubscribe_type_changes(sub_id)
    assert r1.calls[0] == {"verb": "get", "path": "/dme/type-subscriptions", "params": {"owner": "owner-1"}}
    assert r1.calls[1] == {"verb": "get", "path": f"/dme/type-subscriptions/{sub_id}", "params": None}
    assert r1.calls[2] == {"verb": "delete", "path": f"/dme/type-subscriptions/{sub_id}", "params": None}


def test_raises_sdk_error_on_a_4xx_response(client, r1):
    """A 404 answer is raised as `SdkError` carrying the status code and the body."""
    r1.script(404, {"detail": "no such data job"})
    with pytest.raises(SdkError) as exc_info:
        client.get_data_job(uuid.uuid4())
    assert exc_info.value.status_code == 404
    assert exc_info.value.body == {"detail": "no such data job"}


def test_ingest_and_fetch_data_records(client, r1):
    """A record is ingested as `{payload}` and records are fetched with the limit as a query parameter."""
    job_id = uuid.uuid4()
    client.ingest_data_record(job_id, {"kpi": 1.0})
    r1.script(200, [{"recordId": "x", "dataJobId": str(job_id), "payload": {"kpi": 1.0}, "producedAt": "2026-01-01T00:00:00+00:00"}])
    result = client.fetch_data_records(job_id, limit=10)
    assert r1.calls[0] == {"verb": "post", "path": f"/dme/data-jobs/{job_id}/records", "params": None, "files": None, "json": {"payload": {"kpi": 1.0}}}
    assert r1.calls[1] == {"verb": "get", "path": f"/dme/data-jobs/{job_id}/records", "params": {"limit": 10}}
    assert result[0]["payload"] == {"kpi": 1.0}


def test_mediate_action(client, r1):
    """`mediate_action` posts the requester, the changes, the default scope and the source context to DME."""
    client.mediate_action("energy-optimizer", [{"managedElementRef": "me-1", "attributeChanges": {"x": 1}}],
                           source_context={"vendor": "acme"})
    call = r1.calls[0]
    assert call["path"] == "/dme/actions"
    assert call["json"] == {
        "requestedBy": "energy-optimizer", "changes": [{"managedElementRef": "me-1", "attributeChanges": {"x": 1}}],
        "scope": "single-ME", "msacRole": None, "sourceContext": {"vendor": "acme"},
    }


def test_mediate_action_forwards_the_decision_context_only_when_given(client, r1):
    """The decision context (why the rApp acts, PR-AI-13) is sent only when given, so an action without one has no `decision` key."""
    client.mediate_action("energy-optimizer", [{"managedElementRef": "me-1"}], decision={"inputsRef": "dme://jobs/1", "modelVersion": "m 1.0", "rationale": "low load"})
    assert r1.calls[0]["json"]["decision"] == {"inputsRef": "dme://jobs/1", "modelVersion": "m 1.0", "rationale": "low load"}


def test_get_and_list_actions(client, r1):
    """An action is read by id and the actions are listed filtered by requester."""
    action_id = uuid.uuid4()
    client.get_action(action_id)
    client.list_actions(requested_by="energy-optimizer")
    assert r1.calls[0] == {"verb": "get", "path": f"/dme/actions/{action_id}", "params": None}
    assert r1.calls[1] == {"verb": "get", "path": "/dme/actions", "params": {"managed_element_ref": None, "requested_by": "energy-optimizer"}}


def test_ran_inventory_reads(client, r1):
    """The RAN NF OAM reads call their routes and the cell-guard query leaves unset filters out."""
    client.query_cell_guards(managed_element_ref="me-1", cell_class="EMERGENCY")
    client.get_managed_entity("me-1")
    client.get_vendor_capability("acme")
    client.get_o1_capabilities()
    assert r1.calls[0] == {"verb": "get", "path": "/ran-nf-oam/cell-guards", "params": {
        "managed_element_ref": "me-1", "cell_class": "EMERGENCY"}}
    assert [c["path"] for c in r1.calls[1:]] == [
        "/ran-nf-oam/managed-entities/me-1", "/ran-nf-oam/vendor-capabilities/acme", "/ran-nf-oam/capabilities"]


# ---------------------------------------------------------------- one producer per callback (SEC-15.10b)

@pytest.fixture
def dme():
    """A `DataClient` over a fake DME that holds the SEC-15.10b rule, with the record of what this process registered emptied; yields (client, fake)."""
    smo_data._registered_as.clear()
    fake = FakeDmeR1()
    yield DataClient(fake), fake
    smo_data._registered_as.clear()


def _register(client, producer_id, jobs="http://x/jobs"):
    """Registers the same type under `producer_id` with the callback `jobs`."""
    return client.register_type("RAN", "T", "1.0", "RAN.T", producer_id, {}, "http://x/health", jobs)


def test_the_process_and_an_instance_registering_leave_one_producer(dme):
    """The same callback registered under the process identity and then an instance's leaves DME the instance's producer only, and again in the other order."""
    client, fake = dme
    _register(client, client.own_invoker_id())
    with acting_for("a"):
        _register(client, client.own_invoker_id())
    assert fake.callbacks() == {"http://x/jobs": ["instance-a"]}
    _register(client, client.own_invoker_id())
    assert fake.callbacks() == {"http://x/jobs": ["process-id"]}


def test_several_instances_registering_leave_one_producer(dme):
    """Instance a, instance b and the process in turn: one producer for the callback at every step, the last to register."""
    client, fake = dme
    for acting, expected in (("a", "instance-a"), ("b", "instance-b"), (None, "process-id")):
        with acting_for(acting):
            _register(client, client.own_invoker_id())
        assert fake.callbacks() == {"http://x/jobs": [expected]}


def test_a_registration_repeated_under_the_same_id_is_kept(dme):
    """Registering again under the id already registered replaces it and takes nothing down."""
    client, fake = dme
    _register(client, "process-id")
    _register(client, "process-id")
    assert fake.callbacks() == {"http://x/jobs": ["process-id"]}


def test_the_process_producer_of_an_earlier_run_is_taken_down_by_an_instance(dme):
    """After a restart the record of what was registered is empty, yet the process's own producer left in DME is still dropped when an instance registers."""
    client, fake = dme
    _register(client, "process-id")
    smo_data._registered_as.clear()
    with acting_for("a"):
        _register(client, client.own_invoker_id())
    assert fake.callbacks() == {"http://x/jobs": ["instance-a"]}


def test_a_producer_of_another_caller_or_another_callback_is_left(dme):
    """Only this process's own producers for the same callback are deregistered: another caller's producer, even for that callback, and any producer for another callback stay."""
    client, fake = dme
    fake.producers["someone-else"] = {"producerId": "someone-else", "producerHealthCallbackUrl": "h", "jobCallbackUrl": "http://x/jobs", "supportedTypeIds": []}
    _register(client, "process-id", jobs="http://x/other")
    with acting_for("a"):
        _register(client, client.own_invoker_id())
    assert sorted(fake.producers) == ["instance-a", "process-id", "someone-else"]


def test_a_producer_that_cannot_be_deregistered_does_not_fail_the_registration(dme):
    """An instance's identity that is gone leaves its producer (rApp Management tears it down) and the new registration still succeeds."""
    client, fake = dme
    with acting_for("a"):
        _register(client, client.own_invoker_id())
    smo_data._registered_as["instance-a"] = "gone"      # the instance it was registered for is no longer one this fake can act as
    assert _register(client, client.own_invoker_id()) == {"dmeTypeId": "t-1"}
    assert sorted(fake.producers) == ["instance-a", "process-id"]
