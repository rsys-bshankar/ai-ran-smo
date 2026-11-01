"""Milestone M1's exit test (plan tasks S1.7, S1.8, S1.12): Echo, started with no route of its own in R1 Termination's table, is reached through R1 by
class name for all four ProvMnS operations, and a registry read lists it.

Unlike the rest of this suite (`mesh.py`, which dispatches by prefix and skips the gateway), R1 Termination runs for real here: SME, R1 Termination
and Echo are three in-process apps on the suite's one database, and the network between them is replaced by host name (`r1-termination`, `sme`,
`echo`): the synchronous `httpx` calls of a module's R1Client go to the app's TestClient, the asynchronous ones R1 Termination makes (introspection,
the registry read, the forward) to the app through `httpx.ASGITransport`. So every step is the deployed path: Echo's start-up registers its MnsInfo
and MgmtDataInfo through R1 with a token it obtained at SME; R1 introspects that token, routes the registry's classes to SME, and routes Echo's
classes by registry lookup to the `mnsAddress` Echo registered.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_m1_echo_through_r1.py -q
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import r1_client
from smo_shared.db import Base, get_session
from smo_shared.mns import mns_registry

from loader import load_app_module

BASE = "/ProvMnS/19.2.0/"
MERGE = {"content-type": "application/3gpp-merge-patch+json"}
SUBTREE = {"scope": json.dumps({"scopeType": "BASE_ALL"})}


@pytest.fixture(scope="module")
def echo_module(loaded_apps):
    """Echo's `app.main`, loaded after the suite's modules (it is not one of them: it is off by default)."""
    return load_app_module("echo")


class _Hosts(httpx.AsyncBaseTransport):
    """R1 Termination's outbound calls, by host name, to the in-process apps."""

    def __init__(self, apps: dict):
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transports[request.url.host].handle_async_request(request)


@pytest.fixture
def stack(loaded_apps, echo_module, db_connection, monkeypatch):
    """SME, R1 Termination and Echo wired by host name on the suite's database; yields (r1 TestClient, echo app module)."""
    Base.metadata.tables["moi_store"].create(db_connection, checkfirst=True)        # Echo's store (in its own schema on Postgres)
    sessions = sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)

    def session():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    r1, sme = loaded_apps["r1-termination"], loaded_apps["sme"]
    apps = {"r1-termination": r1.app, "sme": sme.app, "echo": echo_module.app}
    for app in apps.values():
        app.dependency_overrides[get_session] = session
    clients = {host: TestClient(app) for host, app in apps.items()}

    def call(verb):
        def send(url, **kwargs):
            target = httpx.URL(url)
            options = {k: kwargs[k] for k in ("params", "headers", "json", "content") if kwargs.get(k) is not None}
            return getattr(clients[target.host], verb)(target.raw_path.decode(), **options)
        return send

    for verb in ("get", "post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, verb, call(verb))
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=_Hosts(apps), timeout=kwargs.get("timeout")))
    monkeypatch.setattr(r1_client, "_identity", r1_client._ModuleIdentity(store=False))       # this test's own invoker, through the real token flow
    monkeypatch.setenv("SMO_MNS_REGISTRATION", "on")
    r1.mns_routing.CACHE.clear()
    try:
        yield clients["r1-termination"], echo_module
    finally:
        r1.mns_routing.CACHE.clear()
        for app in apps.values():
            app.dependency_overrides.pop(get_session, None)


def test_m1_echo_is_reached_through_r1_by_class_name_for_all_four_operations_and_the_registry_lists_it(stack, loaded_apps):
    """M1: no static route; Echo registers at start-up; createMOI, getMOIAttributes, modifyMOIAttributes and deleteMOI through R1 reach it by class;
    the registry, read through R1, lists Echo (and SME) with the classes each serves."""
    gateway, echo = stack
    routes = loaded_apps["r1-termination"].ROUTES
    assert not any(prefix.lower() in ("/echo", "/provmns") for prefix in routes), "Echo must have no route of its own in R1's table"
    token = r1_client._module_token(r1_client.R1_GATEWAY_URL)                 # bootstrap, invoker registration at SME, client-credentials grant
    auth = {"Authorization": f"Bearer {token}"}

    before = gateway.get(BASE + "SubNetwork=M1", headers=auth)
    assert before.status_code == 404 and before.json()["title"] == "NO_ROUTE"       # nobody serves SubNetwork yet

    with TestClient(echo.app):                                                # Echo starts: its lifespan registers it in the MnS registry
        assert echo.REGISTRATION.registered.wait(30), "Echo did not register in the MnS registry"

        created = gateway.put(BASE + "SubNetwork=M1", json={"id": "M1", "attributes": {"userLabel": "milestone 1"}}, headers=auth)
        assert created.status_code == 201, created.text
        child = gateway.put(BASE + "SubNetwork=M1/ManagedElement=1", json={"id": "1", "attributes": {"userLabel": "me1"}}, headers=auth)
        assert child.status_code == 201, child.text

        read = gateway.get(BASE + "SubNetwork=M1", params=SUBTREE, headers=auth)
        assert read.status_code == 200
        tree = read.json()
        assert tree["objectInstance"] == "SubNetwork=M1" and tree["attributes"] == {"userLabel": "milestone 1"}
        assert [me["objectInstance"] for me in tree["ManagedElement"]] == ["SubNetwork=M1,ManagedElement=1"]

        changed = gateway.patch(BASE + "SubNetwork=M1/ManagedElement=1", content=json.dumps({"attributes": {"vendorName": "v1"}}),
                                headers={**auth, **MERGE})
        assert changed.status_code == 200, changed.text
        assert changed.json()["attributes"] == {"userLabel": "me1", "vendorName": "v1"}

        deleted = gateway.delete(BASE + "SubNetwork=M1", headers=auth)
        assert deleted.status_code == 204
        gone = gateway.get(BASE + "SubNetwork=M1", headers=auth)
        assert gone.status_code == 404 and gone.json()["type"] == "MOI_NOT_FOUND"          # Echo's own answer: routed, and the instance is gone

        listed = mns_registry.entries(mns_registry.read(lambda path, **kw: gateway.get(path, params=kw.get("params"),
                                                                                       headers={**kw["headers"], **auth})))
    by_label = {e.label: e for e in listed}
    assert by_label["echo"].classes == {"SubNetwork", "ManagedElement"}
    assert by_label["echo"].address == "http://echo:8000/ProvMnS/19.2.0" and by_label["echo"].mns_type == "PROV_MNS"
    assert by_label["sme"].classes == {"MnsRegistry", "MnsInfo", "MgmtDataInfo"}
