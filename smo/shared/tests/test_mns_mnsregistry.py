"""The MnS registry helpers of `smo_shared.mns.mns_registry` (plan tasks S1.7, S1.8, S1.12), the store under another table name (`store.store_model`,
`ProvMnS(model=...)`), and the class registry reading OpenAPI `oneOf` as `anyOf` (registry.py).

SME's producer of the registry is tested in `sme/tests/test_mns_registry.py`, R1 Termination's routing in `r1-termination/tests/test_mns_routing.py`,
and the whole path in `tests_integration/test_m1_echo_through_r1.py` (milestone M1).
Run: `cd smo/shared && PYTHONPATH=. python -m pytest tests/test_mns_mnsregistry.py -q`.
"""

import logging
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from smo_shared import audit, events
from smo_shared.mns import ProvMnS, default_registry, mns_registry as reg, store
from smo_shared.mns.errors import MnsError
from smo_shared.mns.registry import openapi_to_jsonschema
from smo_shared.mns.store import ManagedObject, store_model
from smo_shared.testing import make_test_engine

ECHO = "http://echo:8000/ProvMnS/19.2.0"


# ------------------------------------------------------------------------------------------- oneOf read as anyOf

def test_one_of_reads_as_any_of_at_every_level_and_the_rest_is_kept():
    """`oneOf` becomes `anyOf` wherever it is, with its branches converted and every other key unchanged."""
    assert openapi_to_jsonschema({"oneOf": [{"type": "string"}, {"type": "integer"}], "description": "d"}) == \
        {"anyOf": [{"type": "string"}, {"type": "integer"}], "description": "d"}
    assert openapi_to_jsonschema({"type": "array", "items": {"oneOf": [{"$ref": "#/a"}, {"$ref": "#/b", "nullable": True}]}}) == \
        {"type": "array", "items": {"anyOf": [{"$ref": "#/a"}, {"anyOf": [{"$ref": "#/b"}, {"type": "null"}]}]}}


def test_a_node_with_both_keeps_its_one_of():
    """A node that already has `anyOf` keeps its `oneOf` as it is (renaming it would overwrite one of the two)."""
    node = {"oneOf": [{"type": "string"}], "anyOf": [{"minLength": 1}]}
    assert openapi_to_jsonschema(node) == node


def test_a_nullable_one_of_allows_null_around_the_any_of():
    """`nullable` next to a `oneOf` (no type of its own) wraps the converted node with a null branch."""
    assert openapi_to_jsonschema({"oneOf": [{"type": "string"}], "nullable": True}) == {"anyOf": [{"anyOf": [{"type": "string"}]}, {"type": "null"}]}


def test_management_data_made_only_of_the_enum_values_is_valid():
    """TS 28.623 ManagementData is an enum list or a string list: an optimisation domain alone matches both, which `oneOf` refused."""
    registry = default_registry()
    registry.validate("MgmtDataInfo", {"supportedManagementData": [["ENERGY_EFFICIENCY"]]})
    registry.validate("MgmtDataInfo", {"supportedManagementData": [["SubNetwork", "ManagedElement"], ["COVERAGE", "MOBILITY"]]})
    with pytest.raises(MnsError):
        registry.validate("MgmtDataInfo", {"supportedManagementData": ["SubNetwork"]})       # each value is itself a list


# ------------------------------------------------------------------------------------------- the store under another name

def test_store_model_maps_the_same_columns_on_another_table_once_per_name():
    """`store_model` is `ManagedObject` for `moi_store`, else one class per table name with the same columns, version counter and named indexes."""
    assert store_model("moi_store") is ManagedObject
    model = store_model("test_registry_store")
    assert store_model("test_registry_store") is model and model is not ManagedObject
    assert model.__tablename__ == "test_registry_store"
    assert list(model.__table__.columns.keys()) == list(ManagedObject.__table__.columns.keys())
    assert sorted(index.name for index in model.__table__.indexes) == ["ix_test_registry_store_object_class", "ix_test_registry_store_parent_dn"]
    assert model.__mapper__.version_id_col is model.__table__.c.moi_version


def test_create_all_makes_every_store_table_and_a_producer_writes_only_its_own():
    """`create_all` makes `moi_store` and the other store tables; a producer given `model` reads and writes that table, the default one `moi_store`."""
    model = store_model("test_other_store")
    engine = make_test_engine()
    store.create_all(engine)
    for table in (audit.AuditEntry.__table__, audit.AuditHead.__table__, events.SmoEvent.__table__):     # what the producer's checks write
        table.create(engine)
    assert {"moi_store", "test_other_store"} <= set(inspect(engine).get_table_names())
    other = ProvMnS(source="/smo/test", system_dn="ManagementNode=test", classes=["SubNetwork"], model=model)
    default = ProvMnS(source="/smo/test", system_dn="ManagementNode=test", classes=["SubNetwork"])
    assert other.model is model and default.model is ManagedObject
    with Session(engine) as db:
        other.create_moi(db, "SubNetwork=A", {"id": "A"})
        assert db.get(model, "SubNetwork=A") is not None and db.get(ManagedObject, "SubNetwork=A") is None
        assert other.get_moi_attributes(db, "SubNetwork=A").items[0]["objectInstance"] == "SubNetwork=A"
        with pytest.raises(MnsError):
            default.get_moi_attributes(db, "SubNetwork=A")                              # 404: not in moi_store
        other.modify_moi_attributes(db, "SubNetwork=A", "application/merge-patch+json", {"attributes": {"userLabel": "a"}})
        assert db.get(model, "SubNetwork=A").attributes == {"userLabel": "a"}
        assert other.delete_moi(db, "SubNetwork=A") == 1
        assert db.get(model, "SubNetwork=A") is None


# ------------------------------------------------------------------------------------------- what a producer registers

def test_the_dns_of_a_registration():
    """MnsInfo and MgmtDataInfo of a module sit directly under the MnsRegistry, named after the module."""
    assert reg.REGISTRY_DN == "SubNetwork=SMO,MnsRegistry=1"
    assert reg.mns_info_dn("echo") == "SubNetwork=SMO,MnsRegistry=1,MnsInfo=echo"
    assert reg.mgmt_data_info_dn("ran-nf-oam") == "SubNetwork=SMO,MnsRegistry=1,MgmtDataInfo=ran-nf-oam"
    assert reg.REGISTRY_PATH == "/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1"


def test_a_registration_is_the_mgmt_data_info_then_the_mns_info_that_names_it():
    """Two Resources in order; the MnsInfo is a PROV_MNS of the router's version at the address, naming its MgmtDataInfo; classes sorted, once each."""
    (data_dn, data), (info_dn, info) = reg.registration("echo", ["SubNetwork", "ManagedElement", "SubNetwork"], ECHO)
    assert data_dn == reg.mgmt_data_info_dn("echo") and info_dn == reg.mns_info_dn("echo")
    assert data == {"id": "echo", "objectClass": "MgmtDataInfo", "objectInstance": data_dn,
                    "attributes": {"supportedManagementData": [["ManagedElement", "SubNetwork"]]}}
    assert info == {"id": "echo", "objectClass": "MnsInfo", "objectInstance": info_dn,
                    "attributes": {"mnsLabel": "echo", "mnsType": "PROV_MNS", "mnsVersion": "19.2.0", "mnsAddress": ECHO, "mgmtDataInfoRef": [data_dn]}}
    registry = default_registry()
    registry.validate("MgmtDataInfo", data["attributes"])
    registry.validate("MnsInfo", info["attributes"])


def test_a_registration_with_a_scope_capabilities_and_domains():
    """The scope is one MnsScope of DNs, the capabilities and the domains sorted lists; each is valid TS 28.623."""
    (_, data), (_, info) = reg.registration("x", ["SubNetwork"], ECHO, scope=["SubNetwork=B", "SubNetwork=A"], capabilities=["NR_PROVISIONING"],
                                            domains=["MOBILITY", "COVERAGE", "MOBILITY"])
    assert data["attributes"]["supportedManagementData"] == [["SubNetwork"], ["COVERAGE", "MOBILITY"]]
    assert info["attributes"]["mnsScope"] == [["SubNetwork=A", "SubNetwork=B"]] and info["attributes"]["mnsCapability"] == ["NR_PROVISIONING"]
    default_registry().validate("MnsInfo", info["attributes"])
    default_registry().validate("MgmtDataInfo", data["attributes"])


def test_a_registration_needs_a_class_and_knows_the_domains():
    """No class, or a domain that is not one of the TS 28.537 five, is a ValueError naming what is wrong."""
    with pytest.raises(ValueError, match="at least one class"):
        reg.registration("x", [], ECHO)
    with pytest.raises(ValueError, match="SPEED, TASTE"):
        reg.registration("x", ["SubNetwork"], ECHO, domains=["TASTE", "COVERAGE", "SPEED"])


@pytest.mark.parametrize("configured, expected", [(None, "http://echo:8000/ProvMnS/19.2.0"), ("", "http://echo:8000/ProvMnS/19.2.0"),
                                                  (" https://e.example/ProvMnS/19.2.0/ ", "https://e.example/ProvMnS/19.2.0")])
def test_the_default_address(monkeypatch, configured, expected):
    """`http://<module>:8000/ProvMnS/<version>` unless SMO_MNS_ADDRESS says otherwise (trimmed, no trailing slash)."""
    if configured is None:
        monkeypatch.delenv(reg.ADDRESS_ENV, raising=False)
    else:
        monkeypatch.setenv(reg.ADDRESS_ENV, configured)
    assert reg.default_address("echo") == expected


def test_the_default_address_is_https_with_mtls(monkeypatch):
    """With SMO_MTLS=on the default address is https, as every module's address is."""
    monkeypatch.delenv(reg.ADDRESS_ENV, raising=False)
    monkeypatch.setattr(reg.mtls, "enabled", lambda: True)
    assert reg.default_address("echo") == "https://echo:8000/ProvMnS/19.2.0"


# ------------------------------------------------------------------------------------------- reading the registry

def _flat(*registrations) -> list[dict]:
    out = []
    for module, classes, address, options in registrations:
        out += [{**body, "objectInstance": dn} for dn, body in reg.registration(module, classes, address, **options)]
    return out


def test_entries_join_each_mns_info_with_the_mgmt_data_info_it_names():
    """Classes and domains come from the referenced MgmtDataInfo; the producers are in DN order; other classes and stray values are ignored."""
    flat = _flat(("zeta", ["NRCellDU"], "http://z/ProvMnS/19.2.0", {"domains": ["COVERAGE"], "capabilities": ["NR_PROVISIONING"]}),
                 ("echo", ["SubNetwork"], ECHO, {"scope": ["SubNetwork=A"]}))
    flat += [{"objectClass": "MnsRegistry", "objectInstance": reg.REGISTRY_DN, "attributes": {}}, "not a resource",
             {"objectClass": "MnsInfo", "objectInstance": reg.mns_info_dn("lost"), "attributes": {"mnsType": "PROV_MNS", "mgmtDataInfoRef": ["nowhere"]}},
             {"objectClass": "MnsInfo", "objectInstance": reg.mns_info_dn("odd"), "attributes": {"mnsScope": "SubNetwork=A", "mgmtDataInfoRef": 7}},
             {"objectClass": "MnsInfo", "objectInstance": reg.mns_info_dn("bare")}]
    found = reg.entries(flat)
    assert [e.label for e in found] == [None, "echo", None, None, "zeta"]
    bare, echo, lost, odd, zeta = found
    assert echo == reg.MnsEntry(dn=reg.mns_info_dn("echo"), label="echo", mns_type="PROV_MNS", version="19.2.0", address=ECHO, scope=("SubNetwork=A",),
                                capabilities=(), classes=frozenset({"SubNetwork"}), domains=frozenset())
    assert zeta.classes == {"NRCellDU"} and zeta.domains == {"COVERAGE"} and zeta.capabilities == ("NR_PROVISIONING",)
    assert lost.classes == frozenset() and lost.mns_type == "PROV_MNS"
    assert odd.scope == () and odd.classes == frozenset() and bare.address is None


def test_discover_by_class_api_cat_and_domain():
    """Each criterion narrows; none given is everything; api-cat matches the mnsType or any mnsCapability."""
    found = reg.entries(_flat(("a", ["SubNetwork"], ECHO, {"domains": ["COVERAGE"]}),
                              ("b", ["NRCellDU", "SubNetwork"], ECHO, {"capabilities": ["NR_PROVISIONING"], "domains": ["MOBILITY"]})))
    a, b = found
    assert reg.discover(found) == [a, b]
    assert reg.discover(found, class_name="SubNetwork") == [a, b] and reg.discover(found, class_name="NRCellDU") == [b]
    assert reg.discover(found, api_cat="PROV_MNS") == [a, b] and reg.discover(found, api_cat="NR_PROVISIONING") == [b]
    assert reg.discover(found, api_cat="FAULT_SUPERVISION_MNS") == []
    assert reg.discover(found, domain="COVERAGE") == [a] and reg.discover(found, domain="CAPACITY") == []
    assert reg.discover(found, class_name="SubNetwork", domain="MOBILITY") == [b]
    assert reg.discover(found, class_name="SubNetwork", api_cat="NR_PROVISIONING", domain="COVERAGE") == []


def _entry(name, classes=("SubNetwork",), scope=(), version="19.2.0", address=ECHO, mns_type="PROV_MNS"):
    return reg.MnsEntry(dn=reg.mns_info_dn(name), label=name, mns_type=mns_type, version=version, address=address, scope=tuple(scope),
                        classes=frozenset(classes))


def test_producer_for_takes_the_class_the_version_an_address_and_prov_mns():
    """A producer of another class, another version, no address or another mnsType is never chosen."""
    candidates = [_entry("a", classes=["ManagedElement"]), _entry("b", version="18.0.0"), _entry("c", address=None), _entry("d", address=""),
                  _entry("e", mns_type="FAULT_SUPERVISION_MNS")]
    assert reg.producer_for(candidates, "SubNetwork", "SubNetwork=A", "19.2.0") is None
    good = _entry("f")
    assert reg.producer_for([*candidates, good], "SubNetwork", "SubNetwork=A", "19.2.0") == good
    assert reg.producer_for([*candidates, good], "SubNetwork", "SubNetwork=A", "18.0.0") == candidates[1]
    assert reg.producer_for([*candidates, good], "SubNetwork", "SubNetwork=A", "17.0.0") is None


def test_producer_for_follows_the_scope_and_the_closest_one_wins():
    """A scoped producer serves only DNs under its scope (RDN by RDN); among those that hold the DN the longest scope DN wins, an unscoped one last,
    then the first by DN."""
    wide, narrow, other, anywhere = (_entry("w", scope=["SubNetwork=A"]), _entry("n", scope=["SubNetwork=B", "SubNetwork=A,SubNetwork=A1"]),
                                     _entry("o", scope=["SubNetwork=C"]), _entry("z"))
    found = [anywhere, other, wide, narrow]
    assert reg.producer_for(found, "SubNetwork", "SubNetwork=A,SubNetwork=A1,SubNetwork=x", "19.2.0") == narrow
    assert reg.producer_for(found, "SubNetwork", "SubNetwork=A,SubNetwork=A2", "19.2.0") == wide
    assert reg.producer_for(found, "SubNetwork", "SubNetwork=A", "19.2.0") == wide
    assert reg.producer_for(found, "SubNetwork", "SubNetwork=AB", "19.2.0") == anywhere            # `SubNetwork=A` is no prefix of it, RDN-wise
    assert reg.producer_for(found, "SubNetwork", "SubNetwork=D", "19.2.0") == anywhere
    assert reg.producer_for([other, wide], "SubNetwork", "SubNetwork=D", "19.2.0") is None
    twins = [_entry("t2", scope=["SubNetwork=A"]), _entry("t1", scope=["SubNetwork=A"])]
    assert reg.producer_for(twins, "SubNetwork", "SubNetwork=A", "19.2.0").label == "t1"
    assert reg.producer_for([_entry("y"), _entry("x")], "SubNetwork", "SubNetwork=A", "19.2.0").label == "x"


@pytest.mark.parametrize("link, following", [(None, None), ("", None), ('</a?cursor=1>; rel="next"', "/a?cursor=1"),
                                             ('</p>; rel="prev", </n>; rel = "next"', "/n"), ('</p>; rel="prev"', None)])
def test_next_page(link, following):
    """The `rel="next"` target of a Link header, whichever of several it is; none when there is no such link."""
    assert reg.next_page(link) == following


class _Page:
    def __init__(self, status, body, link=None):
        self.status_code, self._body, self.headers = status, body, ({"link": link} if link else {})

    def json(self):
        return self._body


def test_read_follows_every_page_with_the_query_on_the_first_only():
    """`read` asks REGISTRY_PATH with READ_PARAMS and the flat media type, then each next page as given, and returns every resource in order."""
    asked = []
    pages = [_Page(200, [{"n": 1}], '</next?cursor=x>; rel="next"'), _Page(200, [{"n": 2}, {"n": 3}])]

    def get(path, params=None, headers=None):
        asked.append((path, params, headers))
        return pages[len(asked) - 1]

    assert reg.read(get) == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert asked == [(reg.REGISTRY_PATH, reg.READ_PARAMS, reg.READ_HEADERS), ("/next?cursor=x", None, reg.READ_HEADERS)]
    assert reg.READ_HEADERS == {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
    assert reg.READ_PARAMS == {"scope": '{"scopeType": "BASE_ALL"}', "limit": "500"}


def test_read_refuses_a_page_that_is_not_a_200():
    """A page answered with anything but 200 is a RuntimeError naming the status."""
    with pytest.raises(RuntimeError, match="503"):
        reg.read(lambda path, **kw: _Page(503, None))


# ------------------------------------------------------------------------------------------- registering at start-up

def test_register_sends_the_two_createmoi_and_accepts_200_and_201(monkeypatch):
    """`register` PUTs the MgmtDataInfo then the MnsInfo to their URIs with the default address; a 200 (unchanged) or 201 (new) is success."""
    monkeypatch.delenv(reg.ADDRESS_ENV, raising=False)
    sent = []
    answers = iter([_Page(201, {}), _Page(200, {})])

    def put(path, json=None):
        sent.append((path, json))
        return next(answers)

    reg.register("echo", ["SubNetwork"], put=put, domains=["COVERAGE"])
    assert [path for path, _ in sent] == ["/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MgmtDataInfo=echo",
                                          "/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=echo"]
    assert sent[1][1]["attributes"]["mnsAddress"] == "http://echo:8000/ProvMnS/19.2.0"
    assert sent[0][1]["attributes"]["supportedManagementData"] == [["SubNetwork"], ["COVERAGE"]]


@pytest.mark.parametrize("status", [202, 204, 403, 422, 503])
def test_register_fails_on_any_other_answer_and_stops_there(status):
    """Any other status of the first createMOI is a RuntimeError naming it, and the MnsInfo is not sent."""
    sent = []

    def put(path, json=None):
        sent.append(path)
        return _Page(status, {})

    with pytest.raises(RuntimeError, match=str(status)):
        reg.register("echo", ["SubNetwork"], put=put, address=ECHO)
    assert len(sent) == 1


def test_register_goes_through_r1_by_default(monkeypatch):
    """With no `put`, the registration is sent by an R1Client (through R1 Termination, with the module's token)."""
    seen = []

    class Client:
        def put(self, path, json=None):
            seen.append(path)
            return _Page(201, {})

    import smo_shared.r1_client as r1_client
    monkeypatch.setattr(r1_client, "R1Client", Client)
    reg.register("echo", ["SubNetwork"], address=ECHO)
    assert len(seen) == 2


def test_self_registration_retries_until_it_succeeds_and_logs_each_failure(caplog):
    """Each failure is logged and followed by the next wait (the last one repeating); success sets `registered` and stops the attempts."""
    outcomes = iter([RuntimeError("down"), RuntimeError("still down"), RuntimeError("again"), None])
    waits = []

    def put(path, json=None):
        failure = next(outcomes) if path.endswith("MgmtDataInfo=echo") else None
        if failure is not None:
            raise failure
        return _Page(201, {})

    registration = reg.SelfRegistration("echo", ["SubNetwork"], waits=(0.01, 0.02), put=put, address=ECHO)
    registration._stopping.wait = lambda seconds: waits.append(seconds) or False
    with caplog.at_level(logging.WARNING, logger="smo.mns.registry"):
        registration._run()
    assert registration.registered.is_set() and registration.attempts == 4 and waits == [0.01, 0.02, 0.02]
    assert [r.getMessage() for r in caplog.records] == [f"MnS registry: registering echo failed (attempt {n}): {why}"
                                                         for n, why in ((1, "down"), (2, "still down"), (3, "again"))]


def test_self_registration_stops_when_told():
    """`stop()` during an attempt ends the attempts after it; stopped before the first, none is made. Nothing is registered."""
    def put(path, json=None):
        registration.stop()
        return _Page(503, {})

    registration = reg.SelfRegistration("echo", ["SubNetwork"], put=put, address=ECHO)
    registration._run()
    assert registration.attempts == 1 and not registration.registered.is_set()
    stopped = reg.SelfRegistration("echo", ["SubNetwork"], put=put, address=ECHO)
    stopped.stop()
    stopped._run()
    assert stopped.attempts == 0


def test_self_registration_starts_one_daemon_thread():
    """`start()` runs the attempts in one daemon thread, however often it is called."""
    done = threading.Event()
    registration = reg.SelfRegistration("echo", ["SubNetwork"], put=lambda path, json=None: done.set() or _Page(201, {}), address=ECHO)
    registration.start()
    first = registration._thread
    registration.start()
    assert registration.registered.wait(5) and registration._thread is first and first.daemon and first.name == "mns-registration-echo"


@pytest.mark.parametrize("value, on", [(None, True), ("on", True), ("ON", True), ("true", True), ("off", False), (" Off ", False), ("false", False),
                                       ("0", False), ("no", False)])
def test_the_switch(monkeypatch, value, on):
    """SMO_MNS_REGISTRATION is on unless it says off, false, 0 or no (any case, spaces ignored)."""
    if value is None:
        monkeypatch.delenv(reg.SWITCH_ENV, raising=False)
    else:
        monkeypatch.setenv(reg.SWITCH_ENV, value)
    assert reg.enabled() is on


def test_install_self_registration_registers_when_the_app_starts_and_keeps_its_own_lifespan(monkeypatch):
    """The app's start registers (in the background) and its own lifespan still runs; its shutdown stops the attempts."""
    monkeypatch.setenv(reg.SWITCH_ENV, "on")
    order = []

    async def own_lifespan(app):
        order.append("own start")
        yield
        order.append("own stop")

    from contextlib import asynccontextmanager
    app = FastAPI(lifespan=asynccontextmanager(own_lifespan))
    registration = reg.install_self_registration(app, "echo", ["SubNetwork"], put=lambda path, json=None: _Page(201, {}), address=ECHO)
    assert registration._thread is None                       # nothing before the app starts
    with TestClient(app):
        assert registration.registered.wait(5)
        assert order == ["own start"]
    assert order == ["own start", "own stop"] and registration._stopping.is_set()


def test_install_self_registration_does_nothing_when_switched_off(monkeypatch):
    """With SMO_MNS_REGISTRATION=off at start-up no attempt is made."""
    monkeypatch.setenv(reg.SWITCH_ENV, "off")
    app = FastAPI()
    registration = reg.install_self_registration(app, "echo", ["SubNetwork"], put=lambda path, json=None: _Page(201, {}), address=ECHO)
    with TestClient(app):
        pass
    assert registration._thread is None and registration.attempts == 0


# ------------------------------------------------------------------------------------------- the router's error answers in the OpenAPI document

def test_every_route_declares_its_ts_28532_error_answers_and_the_envelope_leaves_them_alone():
    """Each of the four operations documents 400, 403, 404, 409, 412, 415 and 422 with the TS 28.532 error body and says so (`x-smo-error-body`),
    so the SMO's OpenAPI hook keeps its 422 and adds the ProblemDetails envelope only for the statuses it does not document (found by SME's contract
    test, S1.12)."""
    from smo_shared.mns import provmns_router
    from smo_shared.mns.router import ERROR_BODY
    from smo_shared.openapi_security import apply_r1_gateway_security

    app = FastAPI()
    apply_r1_gateway_security(app)
    app.include_router(provmns_router(ProvMnS(source="/smo/test", system_dn="ManagementNode=test", classes=["SubNetwork"]), get_session=lambda: None))
    operations = app.openapi()["paths"]["/ProvMnS/{version}/{ldn}"]
    assert sorted(operations) == ["delete", "get", "patch", "put"]
    for method, operation in operations.items():
        assert operation["x-smo-error-body"] == "TS 28.532", method
        for status in ("400", "403", "404", "409", "412", "415", "422"):
            assert operation["responses"][status]["content"]["application/json"]["schema"] == ERROR_BODY, (method, status)
        assert operation["responses"]["401"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert operations["put"]["responses"]["201"] and operations["delete"]["responses"]["204"]
    assert ERROR_BODY["required"] == ["status", "type"] and ERROR_BODY["properties"]["status"] == {"type": "string"}


def test_an_operation_without_its_own_error_bodies_still_gets_the_envelope_for_422():
    """The hook still widens a documented 422 to the envelope on every other operation."""
    from smo_shared.openapi_security import apply_r1_gateway_security

    app = FastAPI()
    apply_r1_gateway_security(app)

    @app.post("/x", responses={422: {"description": "mine"}})
    def x() -> dict:
        """A route with a 422 of its own."""
        return {}

    response = app.openapi()["paths"]["/x"]["post"]["responses"]["422"]
    assert response == {"description": "mine", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}}


def test_reset_lets_the_next_start_register_again():
    """After `reset`, the registration starts from nothing: not registered, no attempt, a new thread on `start`."""
    registration = reg.SelfRegistration("echo", ["SubNetwork"], put=lambda path, json=None: _Page(201, {}), address=ECHO)
    registration.start()
    assert registration.registered.wait(5)
    registration.reset()
    assert not registration.registered.is_set() and registration.attempts == 0 and registration._thread is None
    assert not registration._stopping.is_set()
    registration.start()
    assert registration.registered.wait(5) and registration.attempts == 1
    reg.SelfRegistration("x", ["SubNetwork"]).reset()                  # never started: nothing to wait for
