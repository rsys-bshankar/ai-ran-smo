"""SME, R1 Termination and Echo in one process, wired by host name: the stack of milestone M1's exit test (`test_m1_echo_through_r1.py`) and of the
subscription and access-rule tests (`test_subscriptions_and_access_rules.py`).

R1 Termination runs for real: the synchronous `httpx` calls of a module's R1Client go to the app's TestClient (by host: `r1-termination`, `sme`,
`echo`), the asynchronous ones R1 Termination makes (introspection, the registry read, the forward) to the app through `httpx.ASGITransport`. All three
use the suite's one database (`db_connection`). Echo is loaded once per process (it is not one of the suite's modules: it is off by default), whichever
test file asks first.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import r1_client
from smo_shared.db import Base, get_session

from loader import load_app_module

_ECHO = []


def load_echo():
    """Echo's `app.main`, loaded on the first call of the process."""
    if not _ECHO:
        _ECHO.append(load_app_module("echo"))
    return _ECHO[0]


@pytest.fixture
def echo_module(loaded_apps):
    """Echo's `app.main`, loaded after the suite's modules."""
    return load_echo()


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
    echo_module.REGISTRATION.reset()                                                    # each test's Echo registers in its own database
    try:
        yield clients["r1-termination"], echo_module
    finally:
        r1.mns_routing.CACHE.clear()
        for app in apps.values():
            app.dependency_overrides.pop(get_session, None)
