"""TE&IV through R1 by class name (plan tasks S3.19 to S3.21, S1.8, S1.12; ADR 0013 section 7): TE&IV starts with no route of its own in R1
Termination's table for ProvMnS, registers its 15 TS 28.541 classes in SME's MnS registry under its scope (SubNetwork=SMO-RAN), and the five reads of
ADR 0013 section 11.4 (Q1 to Q5) go through R1 to it by class name and come back exactly as the tree it was loaded with holds them.

The set-up is test_m1_echo_through_r1.py's: SME, R1 Termination and TE&IV are in-process apps on the suite's database, the network between them
replaced by host name (`r1-termination`, `sme`, `teiv`), so registration and routing take the deployed path (token at SME, introspection, registry
lookup, the forward). smo-teiv itself is TE&IV's fake (`teiv/tests/fake_teiv.py`), loaded with TE&IV's 12-cell tree written by TE&IV's own encoder
(`teiv/app/ingest.py`), so what is read back is what TE&IV would have written. The latency of the same reads at 1,000 cells against the real smo-teiv
is `teiv/loadtest/queries.py` (the "TE&IV load" workflow).

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_teiv_through_r1.py -q
"""

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import r1_client
from smo_shared.db import get_session
from smo_shared.mns import mns_registry

from loader import load_app_module

TEIV = Path(__file__).resolve().parents[1] / "teiv"
BASE = "/ProvMnS/19.2.0/"
FLAT = {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
NTH1 = json.dumps({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1})
CELLS = 12


@pytest.fixture(scope="module")
def teiv_parts(loaded_apps):
    """TE&IV's `app.main`, its encoder and tree, and the fake smo-teiv class (loaded after the suite's modules: TE&IV is not one of them)."""
    main = load_app_module("teiv")
    import app.ingest as ingest                                  # TE&IV's package, still the `app` in sys.modules
    sys.path[:0] = [str(TEIV / "loadtest"), str(TEIV / "tests")]
    try:
        import fake_teiv
        import tree
    finally:
        del sys.path[:2]
    return main, ingest, tree, fake_teiv


class _Hosts(httpx.AsyncBaseTransport):
    """R1 Termination's outbound calls, by host name, to the in-process apps."""

    def __init__(self, apps: dict):
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transports[request.url.host].handle_async_request(request)


@pytest.fixture
def stack(loaded_apps, teiv_parts, db_connection, monkeypatch):
    """SME, R1 Termination and TE&IV wired by host name on the suite's database, TE&IV on the fake smo-teiv holding the tree; yields (r1 TestClient,
    TE&IV's app module, the tree module, the tree's objects)."""
    main, ingest, tree, fake_teiv = teiv_parts
    mois = tree.synthetic(CELLS)
    fake = fake_teiv.FakeTeiv().load(*ingest.encode(mois))
    monkeypatch.setattr(main.client, "http", httpx.Client(base_url="http://teiv-exposure:8080/topology-inventory/v1", transport=fake.transport()))
    sessions = sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)

    def session():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    r1, sme = loaded_apps["r1-termination"], loaded_apps["sme"]
    apps = {"r1-termination": r1.app, "sme": sme.app, "teiv": main.app}
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
    monkeypatch.setattr(r1_client, "_identity", r1_client._ModuleIdentity(store=False))
    monkeypatch.setenv("SMO_MNS_REGISTRATION", "on")
    r1.mns_routing.CACHE.clear()
    try:
        yield clients["r1-termination"], main, tree, mois
    finally:
        r1.mns_routing.CACHE.clear()
        for app in apps.values():
            app.dependency_overrides.pop(get_session, None)


def _path(dn: str) -> str:
    return BASE + dn.replace(",", "/")


def test_q1_to_q5_read_through_r1_by_class_name(stack, loaded_apps):
    """TE&IV registers; then Q1 (a cell), Q2 (its neighbours), Q3 (a GNBDUFunction's cells), Q4 (two hops, Q2 of each neighbour) and Q5 (a
    SubNetwork's cells by nRTAC) through R1 answer exactly what the tree holds; the registry, read through R1, lists TE&IV's 15 classes and scope."""
    gateway, teiv, tree, mois = stack
    assert not any(prefix.lower() == "/provmns" for prefix in loaded_apps["r1-termination"].ROUTES)
    token = r1_client._module_token(r1_client.R1_GATEWAY_URL)
    auth = {"Authorization": f"Bearer {token}", **FLAT}
    neighbours: dict[str, set[str]] = {}
    for m in mois:
        if m.cls == "NRCellRelation":
            neighbours.setdefault(m.dn.rsplit(",", 1)[0], set()).add(m.attributes["adjacentNRCellRef"])

    def q2(cu: str) -> set[str]:
        r = gateway.get(_path(cu), params={"scope": NTH1, "filter": "@.objectClass=='NRCellRelation'", "attributes": "adjacentNRCellRef"},
                        headers=auth)
        assert r.status_code == 200, r.text
        return {x["attributes"]["adjacentNRCellRef"] for x in r.json()}

    with TestClient(teiv.app):                                 # TE&IV starts: it loads its decorator schema and registers its classes
        assert teiv.REGISTRATION.registered.wait(30), "TE&IV did not register in the MnS registry"

        cell = tree.du_cell(1, 2)
        q1 = gateway.get(_path(cell), headers=auth)
        assert q1.status_code == 200, q1.text
        source = next(m for m in mois if m.dn == cell).attributes
        got = q1.json()[0]["attributes"]
        assert got["nrPci"] == source["nrPci"] and got["administrativeState"] == source["administrativeState"]
        assert got["nRSectorCarrierRef"] == source["nRSectorCarrierRef"]

        cu = tree.cu_cell(2, 1)
        assert q2(cu) == neighbours[cu]

        q3 = gateway.get(_path(f"{tree.me_dn(3)},GNBDUFunction=1"), params={"scope": NTH1, "filter": "@.objectClass=='NRCellDU'"}, headers=auth)
        assert [x["objectInstance"] for x in q3.json()] == [tree.du_cell(3, c) for c in (1, 2, 3)]

        second = set().union(*(q2(n) for n in sorted(neighbours[cu])))
        assert neighbours[cu] | second == neighbours[cu] | set().union(*(neighbours[n] for n in neighbours[cu]))

        tac = source["nRTAC"]
        q5 = gateway.get(_path(tree.SUBNETWORK), params={"scope": json.dumps({"scopeType": "BASE_ALL"}),
                                                          "filter": f"@.objectClass=='NRCellDU' && @.attributes.nRTAC=={tac}"}, headers=auth)
        assert q5.status_code == 200
        assert [x["objectInstance"] for x in q5.json()] == sorted(m.dn for m in mois if m.cls == "NRCellDU" and m.attributes["nRTAC"] == tac)

        listed = mns_registry.entries(mns_registry.read(lambda path, **kw: gateway.get(path, params=kw.get("params"),
                                                                                       headers={**kw["headers"], "Authorization": auth["Authorization"]})))
    by_label = {e.label: e for e in listed}
    assert by_label["teiv"].classes == set(teiv.CLASSES) and len(by_label["teiv"].classes) == 15
    assert by_label["teiv"].address == "http://teiv:8000/ProvMnS/19.2.0" and by_label["teiv"].mns_type == "PROV_MNS"


def test_teivs_committed_openapi_matches_its_live_schema(teiv_parts):
    """docs/openapi/teiv.json (what the contract-drift and interface gates read) is TE&IV's live OpenAPI document; regenerate it with
    scripts/generate_openapi_specs.py once TE&IV is in its module list, or as this test's message says until then."""
    main = teiv_parts[0]
    committed = json.loads((Path(__file__).resolve().parents[1] / "docs" / "openapi" / "teiv.json").read_text())
    assert committed == main.app.openapi(), "stale docs/openapi/teiv.json: regenerate it from teiv's app.openapi() (sorted keys, two-space indent)"
