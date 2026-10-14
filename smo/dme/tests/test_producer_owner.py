"""SEC-15.10b: a caller with the rApp role may register, replace or deregister a DME producer only under its own invoker id.

Covers `POST /production-capabilities` (body `producerId`) and `DELETE /production-capabilities?producer_id=`, the two routes that take a producer id from the caller and change
something. An SMO module (role `internal`) and a call with no role are not asked. Fixtures (`client`) and `register_type_body` come from `test_main.py`. Run with
`PYTHONPATH=.:../shared python -m pytest tests/test_producer_owner.py -q`.
"""

from test_main import client, register_type_body  # noqa: F401  (client: a pytest fixture)

ALICE = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-alice"}
MALLORY = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-mallory"}
RAN_NF_OAM = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "module-invoker-1"}


def _body(producer, **extra):
    """The registration body of the type `RAN/PMCounters/1.0.0` (or the type `extra` names) for `producer`."""
    return register_type_body(producerId=producer, **extra)


def test_an_rapp_registers_under_its_own_invoker_id(client):
    """Alice naming `rapp-alice` is 201, and may register again (replace its own callbacks)."""
    assert client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE).status_code == 201
    again = _body("rapp-alice", jobCallbackUrl="http://alice.example/jobs")
    assert client.post("/production-capabilities", json=again, headers=ALICE).status_code == 201
    assert client.get("/production-capabilities/rapp-alice").json()["jobCallbackUrl"] == "http://alice.example/jobs"


def test_an_rapp_cannot_replace_another_producers_callbacks(client):
    """Mallory naming Alice's producer id, with the very same type definition, is 403 DME_PRODUCER_NOT_OWNER and Alice's callbacks are unchanged."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    evil = _body("rapp-alice", jobCallbackUrl="http://attacker.example/jobs", producerHealthCallbackUrl="http://attacker.example/health")
    refused = client.post("/production-capabilities", json=evil, headers=MALLORY)
    assert refused.status_code == 403 and "DME_PRODUCER_NOT_OWNER" in refused.text
    assert "'rapp-alice' is not this caller's invoker id" in refused.json()["detail"]["detail"]
    stored = client.get("/production-capabilities/rapp-alice").json()
    assert stored["jobCallbackUrl"] == "http://ran-nf-oam:8000/dme-jobs" and "attacker" not in stored["producerHealthCallbackUrl"]


def test_a_refused_registration_changes_nothing(client):
    """A refused call creates no type and no producer, even though its type definition is new."""
    refused = client.post("/production-capabilities", json=_body("rapp-alice", name="Fresh"), headers=MALLORY)
    assert refused.status_code == 403
    assert client.get("/dme-types").json() == [] and client.get("/production-capabilities").json() == []


def test_an_rapp_without_an_invoker_id_is_refused(client):
    """An rApp-role call that carries no invoker id has nothing to compare the producer id with."""
    assert client.post("/production-capabilities", json=_body("rapp-alice"), headers={"X-R1-Role": "rapp"}).status_code == 403


def test_an_smo_module_and_a_call_without_a_role_name_any_producer(client):
    """RAN NF OAM (role internal) registers as `ran-nf-oam`, a call that did not come through the gateway as anything, and an internal caller acting for an rApp is not asked either."""
    assert client.post("/production-capabilities", json=_body("ran-nf-oam"), headers=RAN_NF_OAM).status_code == 201
    assert client.post("/production-capabilities", json=_body("whatever")).status_code == 201
    on_behalf = {**RAN_NF_OAM, "X-R1-On-Behalf-Of": "rapp-alice"}
    assert client.post("/production-capabilities", json=_body("someone-else"), headers=on_behalf).status_code == 201


def test_an_rapp_deregisters_only_its_own_producer(client):
    """Mallory's delete of Alice's producer is 403 and the producer stays; Alice's own is 204 and removes it; a producer id that is not registered is refused for another caller too."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    refused = client.delete("/production-capabilities", params={"producer_id": "rapp-alice"}, headers=MALLORY)
    assert refused.status_code == 403 and "DME_PRODUCER_NOT_OWNER" in refused.text
    assert client.get("/production-capabilities/rapp-alice").status_code == 200
    assert client.delete("/production-capabilities", params={"producer_id": "nobody"}, headers=MALLORY).status_code == 403
    assert client.delete("/production-capabilities", params={"producer_id": "rapp-alice"}, headers=ALICE).status_code == 204
    assert client.get("/production-capabilities/rapp-alice").status_code == 404


def test_rapp_management_and_a_call_without_a_role_may_deregister_any_producer(client):
    """rApp Management tears an instance's registration down as an SMO module: any producer id, 204."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    client.post("/production-capabilities", json=_body("rapp-bob", name="Other"), headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-bob"})
    assert client.delete("/production-capabilities", params={"producer_id": "rapp-alice"}, headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "rapp-mgmt"}).status_code == 204
    assert client.delete("/production-capabilities", params={"producer_id": "rapp-bob"}).status_code == 204
    assert client.get("/production-capabilities").json() == []
