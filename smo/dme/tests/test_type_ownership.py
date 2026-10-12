"""SEC-15.10: only the producer that registered a DME type first (or an SMO module / the operator) may change its definition.

Covers `POST /production-capabilities` on a type that already exists: the same definition is a join or an idempotent re-registration for anyone; a different definition (name,
schema, collection spec, source) needs the original caller, an `internal` role or a call that did not come through the gateway; a type registered before revision 0040 (no
`registered_by`) is open to the producers linked to it. Fixtures (`client`) and `register_type_body` come from `test_main.py`. Run with
`PYTHONPATH=.:../shared python -m pytest tests/test_type_ownership.py -q`.
"""

from sqlalchemy.orm import Session

from app.models import DMEType

from test_main import client, register_type_body  # noqa: F401  (client: a pytest fixture)

ALICE = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-alice"}
BOB = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-bob"}
OPERATOR = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _body(producer, **extra):
    """The registration body of the same type `RAN/PMCounters/1.0.0` as `producer`; `extra` changes fields (a schema, say)."""
    return register_type_body(producerId=producer, **extra)


def _stored(client):
    """The one stored DMEType row's schema and `registered_by`, read fresh."""
    with Session(app_engine(client)) as db:
        row = db.query(DMEType).one()
        return row.data_production_schema, row.registered_by


def app_engine(client):
    """The test engine the `client` fixture put on the app."""
    return client.app.state.test_engine


def test_a_second_rapp_cannot_overwrite_the_type_another_registered(client):
    """Bob's different schema for Alice's type is 403 DME_TYPE_NOT_OWNER and the stored schema and owner are unchanged; Alice may change it herself."""
    assert client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE).status_code == 201
    assert _stored(client) == ({"type": "object"}, "rapp-alice")
    evil = client.post("/production-capabilities", json=_body("rapp-bob", dataProductionSchema={"type": "string"}), headers=BOB)
    assert evil.status_code == 403 and "DME_TYPE_NOT_OWNER" in evil.text
    assert "the type RAN/PMCounters/1.0.0 was registered by another producer" in evil.json()["detail"]["detail"]      # the refusal names the type
    assert _stored(client) == ({"type": "object"}, "rapp-alice")
    mine = client.post("/production-capabilities", json=_body("rapp-alice", dataProductionSchema={"type": "array"}), headers=ALICE)
    assert mine.status_code == 201
    assert _stored(client) == ({"type": "array"}, "rapp-alice")


def test_a_refused_overwrite_does_not_touch_the_producer_row(client):
    """The refusal comes before the producer upsert: the callbacks of a producer id the refused caller named are not replaced."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    evil = _body("rapp-alice", dataProductionSchema={"type": "string"}, jobCallbackUrl="http://attacker.example/jobs")
    assert client.post("/production-capabilities", json=evil, headers=BOB).status_code == 403
    assert client.get("/production-capabilities/rapp-alice").json()["jobCallbackUrl"] == "http://ran-nf-oam:8000/dme-jobs"


def test_the_same_definition_by_another_producer_still_joins_the_type(client):
    """Bob registering exactly Alice's definition is the old join: 201, two producers on the type, and Alice still owns it (Bob cannot then change it)."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    assert client.post("/production-capabilities", json=_body("rapp-bob"), headers=BOB).status_code == 201
    assert _stored(client)[1] == "rapp-alice"
    assert client.post("/production-capabilities", json=_body("rapp-bob", collectionSpec={"every": "5m"}), headers=BOB).status_code == 403


def test_an_smo_module_the_operator_and_a_call_without_a_role_may_overwrite(client):
    """An `internal` caller and a call that did not come through the gateway change the definition of any type, as before."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    assert client.post("/production-capabilities", json=_body("ran-nf-oam", dataProductionSchema={"type": "string"}), headers=OPERATOR).status_code == 201
    assert _stored(client) == ({"type": "string"}, "rapp-alice")
    assert client.post("/production-capabilities", json=_body("x", dataProductionSchema={"type": "array"})).status_code == 201
    assert _stored(client)[0] == {"type": "array"}


def test_a_rapp_without_an_invoker_id_cannot_overwrite(client):
    """A rApp-role call that carries no invoker id has no identity to compare, so it cannot redefine a type."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    anonymous = client.post("/production-capabilities", json=_body("rapp-alice", dataProductionSchema={"type": "string"}), headers={"X-R1-Role": "rapp"})
    assert anonymous.status_code == 403


def test_a_type_from_before_the_owner_was_recorded_is_open_to_its_linked_producers_only(client):
    """With `registered_by` NULL (a row from before revision 0040) a producer linked to the type may redefine it and another rApp may not."""
    client.post("/production-capabilities", json=_body("rapp-alice"), headers=ALICE)
    with Session(app_engine(client)) as db:
        db.query(DMEType).one().registered_by = None
        db.commit()
    assert client.post("/production-capabilities", json=_body("rapp-bob", dataProductionSchema={"type": "string"}), headers=BOB).status_code == 403
    assert client.post("/production-capabilities", json=_body("rapp-alice", dataProductionSchema={"type": "array"}), headers=ALICE).status_code == 201


def test_the_owner_is_the_invoker_the_gateway_vouched_for_not_the_producer_id_in_the_body(client):
    """`registered_by` is the gateway's invoker id when there is one, and the body's `producerId` only for a call that did not come through the gateway (a body naming another producer
    must not become the owner: Bob's token could then never be told from Alice's)."""
    assert client.post("/production-capabilities", json=_body("some-producer-name"), headers=ALICE).status_code == 201
    assert _stored(client)[1] == "rapp-alice"
    # the owner is Alice's invoker id, so Alice (not the producer name she gave) may change the definition and Bob may not
    assert client.post("/production-capabilities", json=_body("some-producer-name", dataProductionSchema={"type": "string"}), headers=BOB).status_code == 403
    assert client.post("/production-capabilities", json=_body("other", dataProductionSchema={"type": "string"}), headers=ALICE).status_code == 201


def test_a_type_registered_without_the_gateway_is_owned_by_the_producer_id_of_the_body(client):
    """With no gateway headers there is no invoker id, so the body's `producerId` is recorded as the owner."""
    assert client.post("/production-capabilities", json=_body("direct-producer")).status_code == 201
    assert _stored(client)[1] == "direct-producer"
