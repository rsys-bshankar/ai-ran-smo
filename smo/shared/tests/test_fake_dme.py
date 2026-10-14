"""`smo_shared.testing.FakeDmeR1`: the DME double that rApp and SDK tests use to hold SEC-15.10b, tested here so those tests can trust what it answers.

Run with `cd shared && PYTHONPATH=. python -m pytest tests/test_fake_dme.py -q`; no network and no database.
"""

from smo_shared.r1_client import acting_for
from smo_shared.testing import FakeDmeR1

BODY = {"producerId": "process-id", "producerHealthCallbackUrl": "http://x/health", "jobCallbackUrl": "http://x/jobs"}


def test_the_invoker_id_is_the_process_s_or_the_instance_the_context_acts_for():
    """Outside `acting_for` the fake's caller is the process; inside it, `instance-<id>`; leaving the block restores the process."""
    fake = FakeDmeR1()
    assert fake.acting_invoker_id() == "process-id"
    with acting_for("a"):
        assert fake.acting_invoker_id() == "instance-a"
    assert fake.acting_invoker_id() == "process-id"


def test_a_producer_is_registered_listed_and_deregistered_under_the_callers_own_id():
    """The caller registers its own producer (201), sees it listed with its callbacks, replaces it by registering again, and deregisters it (204, again with nothing left too)."""
    fake = FakeDmeR1()
    resp = fake.post("/dme/production-capabilities", json=BODY)
    assert resp.status_code == 201 and resp.json() == {"dmeTypeId": "t-1"} and resp.content == b"{}"
    fake.post("/dme/production-capabilities", json={**BODY, "jobCallbackUrl": "http://x/other"})
    listed = fake.get("/dme/production-capabilities").json()
    assert [(p["producerId"], p["jobCallbackUrl"]) for p in listed] == [("process-id", "http://x/other")]
    gone = fake.delete("/dme/production-capabilities", params={"producer_id": "process-id"})
    assert gone.status_code == 204 and gone.json() is None and gone.content == b""
    assert fake.delete("/dme/production-capabilities", params={"producer_id": "process-id"}).status_code == 204
    assert fake.get("/dme/production-capabilities").json() == []


def test_a_caller_naming_another_producer_is_refused_and_nothing_changes():
    """Registering or deregistering a producer id that is not the caller's invoker id is 403 `DME_PRODUCER_NOT_OWNER`, and the producers held are the same afterwards."""
    fake = FakeDmeR1()
    fake.post("/dme/production-capabilities", json=BODY)
    with acting_for("a"):
        refused = fake.post("/dme/production-capabilities", json=BODY)
        assert refused.status_code == 403 and refused.json() == {"code": "DME_PRODUCER_NOT_OWNER"}
        assert fake.delete("/dme/production-capabilities", params={"producer_id": "process-id"}).status_code == 403
    assert list(fake.producers) == ["process-id"]


def test_the_callbacks_are_grouped_by_job_callback_url():
    """`callbacks()` lists the producer ids of each job callback URL, which is how two producers for one callback show up."""
    fake = FakeDmeR1()
    fake.post("/dme/production-capabilities", json=BODY)
    with acting_for("a"):
        fake.post("/dme/production-capabilities", json={**BODY, "producerId": "instance-a"})
        fake.post("/dme/production-capabilities", json={**BODY, "producerId": "instance-a", "jobCallbackUrl": "http://x/other"})
    with acting_for("b"):
        fake.post("/dme/production-capabilities", json={**BODY, "producerId": "instance-b"})
    assert fake.callbacks() == {"http://x/jobs": ["process-id", "instance-b"], "http://x/other": ["instance-a"]}
