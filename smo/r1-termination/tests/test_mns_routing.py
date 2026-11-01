"""PR-CONV-S1.8: R1 Termination routes `/ProvMnS/{version}/{LDN}` by MnS registry lookup (`app/mns_routing.py`), with no static entry for it.

A fake SME answers the introspection and the registry read (`registry`: the flat Resources it returns, page by page), and a recorder stands in for every
producer, so what is asserted is where the gateway sent the request. The routing choice itself (`producer_for`) is tested with the registry module in
`shared/tests/test_mns_mnsregistry.py`; the deployed path end to end is `tests_integration/test_m1_echo_through_r1.py` (milestone M1).
Run: `cd smo/r1-termination && PYTHONPATH=.:../shared python -m pytest tests/test_mns_routing.py -q`.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as gateway
from app import mns_routing
from smo_shared.mns import mns_registry

client = TestClient(gateway.app)
SME = gateway.ROUTES["/sme"]
AUTH = {"Authorization": "Bearer t"}
ECHO = "http://echo:8000/ProvMnS/19.2.0"


def _registry(*producers: tuple[str, list[str], str], scope: dict | None = None) -> list[dict]:
    """The flat registry read for `producers` (module, classes, address), in DN order."""
    out = []
    for module, classes, address in producers:
        for dn, body in mns_registry.registration(module, classes, address, scope=(scope or {}).get(module, ())):
            out.append({**body, "objectInstance": dn})
    return sorted(out, key=lambda r: r["objectInstance"])


class FakeResponse:
    """What the fake network answers: a status, a JSON body and headers."""

    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code, self._body = status_code, body if body is not None else {"ok": True}
        self.content = json.dumps(self._body).encode()
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("refused", request=None, response=None)


@pytest.fixture
def net(monkeypatch):
    """The fake network: `role` SME reports for the token, `pages` the registry pages (a list of (resources, next link or None)), `down` makes the
    registry read fail; `forwards` and `reads` record what the gateway sent."""
    state = {"role": "internal", "pages": [(_registry(("echo", ["SubNetwork", "ManagedElement"], ECHO)), None)], "down": False,
             "forwards": [], "reads": [], "status": 200}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None, params=None, content=None, **kwargs):
            if url == f"{SME}/oauth2/introspect":
                return FakeResponse(body={"active": True, "client_id": "c", "role": state["role"]})
            state["forwards"].append({"method": method, "url": url, "params": params, "content": content, "headers": headers})
            return FakeResponse(status_code=state["status"])

        async def get(self, url, params=None, headers=None):
            state["reads"].append({"url": url, "params": params, "headers": headers})
            if state["down"]:
                raise httpx.ConnectError("SME is down")
            resources, following = state["pages"][len(state["reads"]) - 1]
            return FakeResponse(body=resources, headers={"link": f'<{following}>; rel="next"'} if following else {})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.delenv(mns_routing.CACHE_ENV, raising=False)
    mns_routing.CACHE.clear()
    yield state
    mns_routing.CACHE.clear()


def test_no_static_route_table_entry_is_a_provmns_path():
    """A module serving ProvMnS is reached by the classes it registers, never by a prefix of the static table: an entry for ProvMnS (any case) fails."""
    assert not [prefix for prefix in gateway.ROUTES if prefix.strip("/").lower().startswith("provmns")]
    assert mns_routing.PREFIX not in gateway.ROUTES and "/echo" not in gateway.ROUTES


def test_each_of_the_four_operations_goes_to_the_producer_the_registry_names(net):
    """createMOI, getMOIAttributes, modifyMOIAttributes and deleteMOI on Echo's classes go to `<mnsAddress>/<LDN>`, with the method, query and body."""
    assert client.put("/ProvMnS/19.2.0/SubNetwork=A", json={"id": "A"}, headers=AUTH).status_code == 200
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A", params={"scope": "x"}, headers=AUTH).status_code == 200
    assert client.patch("/ProvMnS/19.2.0/SubNetwork=A/ManagedElement=1", content=b"{}", headers=AUTH).status_code == 200
    assert client.delete("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH).status_code == 200
    sent = [(f["method"], f["url"]) for f in net["forwards"]]
    assert sent == [("PUT", f"{ECHO}/SubNetwork=A"), ("GET", f"{ECHO}/SubNetwork=A"), ("PATCH", f"{ECHO}/SubNetwork=A/ManagedElement=1"),
                    ("DELETE", f"{ECHO}/SubNetwork=A")]
    assert net["forwards"][0]["content"] == b'{"id":"A"}' and net["forwards"][1]["params"]["scope"] == "x"
    assert net["forwards"][0]["headers"]["X-R1-Role"] == "internal"


def test_the_registry_classes_go_to_sme_without_reading_the_registry(net):
    """MnsRegistry, MnsInfo and MgmtDataInfo are SME's: a registration is routed before anything is known about the module that sends it."""
    for path in ("SubNetwork=SMO/MnsRegistry=1", "SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", "SubNetwork=SMO/MnsRegistry=1/MgmtDataInfo=x"):
        client.get(f"/ProvMnS/19.2.0/{path}", headers=AUTH)
    assert [f["url"] for f in net["forwards"]] == [f"{SME}/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1",
                                                   f"{SME}/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x",
                                                   f"{SME}/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MgmtDataInfo=x"]
    assert net["reads"] == []


def test_the_registry_is_read_from_sme_directly_as_internal_and_every_page_is_followed(net):
    """The read is SME's flat getMOIAttributes of the whole registry, with the internal role, following `Link: rel="next"` to the end."""
    net["pages"] = [(_registry(("sme", ["MnsInfo"], f"{SME}/ProvMnS/19.2.0")), "/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1?cursor=abc"),
                    (_registry(("echo", ["SubNetwork"], ECHO)), None)]
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH).status_code == 200
    first, second = net["reads"]
    assert first["url"] == SME + mns_registry.REGISTRY_PATH and first["params"] == mns_registry.READ_PARAMS
    assert first["headers"]["X-R1-Role"] == "internal" and first["headers"]["Accept"] == mns_registry.READ_HEADERS["Accept"]
    assert second["url"] == f"{SME}/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1?cursor=abc" and second["params"] is None
    assert net["forwards"][0]["url"] == f"{ECHO}/SubNetwork=A"


def test_a_class_nobody_serves_and_a_path_that_is_no_provmns_uri_are_404_no_route(net):
    """No producer for the class (after one fresh read), no LDN, or an LDN that is not one: 404 NO_ROUTE, and nothing is forwarded."""
    nobody = client.get("/ProvMnS/19.2.0/SubNetwork=A/GNBDUFunction=1", headers=AUTH)
    assert nobody.status_code == 404 and nobody.json()["title"] == "NO_ROUTE"
    assert "GNBDUFunction" in nobody.json()["detail"] and "SubNetwork=A,GNBDUFunction=1" in nobody.json()["detail"]
    for path in ("/ProvMnS/19.2.0", "/ProvMnS", "/ProvMnS/19.2.0/not-an-rdn", "/ProvMnS/19.2.0/SubNetwork=A/=1"):
        answer = client.get(path, headers=AUTH)
        assert answer.status_code == 404 and answer.json()["title"] == "NO_ROUTE", path
    other_version = client.get("/ProvMnS/18.0.0/SubNetwork=A", headers=AUTH)
    assert other_version.status_code == 404 and "ProvMnS 18.0.0" in other_version.json()["detail"]
    assert net["forwards"] == []


def test_a_caller_with_no_token_is_refused_before_any_registry_read(net):
    """The gateway's checks come first: no token is a 401 and the registry is not read."""
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A").status_code == 401
    assert net["reads"] == [] and net["forwards"] == []


def test_an_rapp_may_read_but_not_change_a_provmns_resource(net):
    """The role policy applies as for any route: an rApp's getMOIAttributes is forwarded, its createMOI (a registration, say) is 403."""
    net["role"] = "rapp"
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH).status_code == 200
    refused = client.put("/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=evil", json={}, headers=AUTH)
    assert refused.status_code == 403 and refused.json()["title"] == "ROLE_NOT_PERMITTED"
    assert [f["method"] for f in net["forwards"]] == ["GET"]


def test_the_copy_is_reused_within_its_lifetime_and_read_again_after_it(net, monkeypatch):
    """Within R1_MNS_REGISTRY_CACHE_SECONDS the copy serves every request; after it, the registry is read again."""
    now = [100.0]
    monkeypatch.setattr(mns_routing.CACHE, "clock", lambda: now[0])
    net["pages"] = [(_registry(("echo", ["SubNetwork"], ECHO)), None)] * 3
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    now[0] += 9.9
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert len(net["reads"]) == 1
    now[0] += 0.2
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert len(net["reads"]) == 2


def test_a_class_not_found_reads_again_only_when_the_copy_is_more_than_a_second_old(net, monkeypatch):
    """A miss on a copy under a second old is a 404 at once; on an older copy the registry is read again, and a producer that registered since is
    found."""
    now = [0.0]
    monkeypatch.setattr(mns_routing.CACHE, "clock", lambda: now[0])
    net["pages"] = [(_registry(("echo", ["SubNetwork"], ECHO)), None), (_registry(("echo", ["SubNetwork", "ManagedElement"], ECHO)), None)]
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert client.get("/ProvMnS/19.2.0/ManagedElement=1", headers=AUTH).status_code == 404 and len(net["reads"]) == 1
    now[0] += 1.0
    assert client.get("/ProvMnS/19.2.0/ManagedElement=1", headers=AUTH).status_code == 200 and len(net["reads"]) == 2
    assert net["forwards"][-1]["url"] == f"{ECHO}/ManagedElement=1"


def test_a_registration_through_the_gateway_drops_the_copy(net):
    """A 2xx write of a registry class through this gateway clears the copy, so the next request reads the registry again; a read, a refused write
    and a write of another class do not."""
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert mns_routing.CACHE.entries is not None
    client.get("/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", headers=AUTH)
    client.put("/ProvMnS/19.2.0/SubNetwork=A", json={}, headers=AUTH)
    net["status"] = 422
    client.put("/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", json={}, headers=AUTH)
    assert mns_routing.CACHE.entries is not None
    net["status"] = 201
    client.put("/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", json={}, headers=AUTH)
    assert mns_routing.CACHE.entries is None


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_after_response_clears_on_each_kind_of_registry_write(method):
    """`after_response` clears on a 2xx PUT, PATCH or DELETE of a registry class, at either end of the 2xx range."""
    for status in (200, 299):
        cache = mns_routing.RegistryCache()
        cache.entries = []
        mns_routing.after_response(method, "19.2.0/SubNetwork=SMO/MnsRegistry=1/MgmtDataInfo=x", status, cache)
        assert cache.entries is None


@pytest.mark.parametrize("method, path, status", [
    ("GET", "19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", 200), ("POST", "19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", 200),
    ("PUT", "19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", 199), ("PUT", "19.2.0/SubNetwork=SMO/MnsRegistry=1/MnsInfo=x", 300),
    ("PUT", "19.2.0/SubNetwork=A", 201), ("PUT", "19.2.0", 201), ("PUT", "19.2.0/x", 201)])
def test_after_response_keeps_the_copy_otherwise(method, path, status):
    """A read, a status outside 2xx, another class or a path that is no ProvMnS URI leaves the copy as it is."""
    cache = mns_routing.RegistryCache()
    cache.entries = []
    mns_routing.after_response(method, path, status, cache)
    assert cache.entries == []


def test_an_unreadable_registry_is_503_with_retry_after_and_a_previous_copy_is_used_instead(net, monkeypatch):
    """SME down with no copy: 503 MNS_REGISTRY_UNAVAILABLE and Retry-After; with a copy (even an old one), routing goes on with it."""
    net["down"] = True
    refused = client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert refused.status_code == 503 and refused.json()["title"] == "MNS_REGISTRY_UNAVAILABLE" and refused.headers["Retry-After"] == "5"
    net["down"], net["reads"] = False, []
    now = [0.0]
    monkeypatch.setattr(mns_routing.CACHE, "clock", lambda: now[0])
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    net["down"] = True
    now[0] += 3600
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH).status_code == 200
    assert net["forwards"][-1]["url"] == f"{ECHO}/SubNetwork=A"


def test_a_registry_page_that_is_not_a_200_counts_as_unreadable(net, monkeypatch):
    """A refused registry read (SME's 4xx or 5xx) is an unreadable registry, not an empty one."""
    async def refused(sme_url):
        raise httpx.HTTPStatusError("503", request=None, response=None)
    monkeypatch.setattr(mns_routing.CACHE, "reader", refused)
    assert client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH).status_code == 503


@pytest.mark.parametrize("raw, seconds", [(None, 10.0), ("", 10.0), ("  ", 10.0), ("2.5", 2.5), ("0", 0.0), ("-4", 0.0), ("soon", 10.0)])
def test_the_copy_lifetime_setting(monkeypatch, raw, seconds):
    """R1_MNS_REGISTRY_CACHE_SECONDS: unset, empty or not a number is the default 10; a negative value is 0."""
    if raw is None:
        monkeypatch.delenv(mns_routing.CACHE_ENV, raising=False)
    else:
        monkeypatch.setenv(mns_routing.CACHE_ENV, raw)
    assert mns_routing.cache_seconds() == seconds


def test_with_mtls_the_producer_is_reached_over_https(net, monkeypatch):
    """With SMO_MTLS=on an http:// mnsAddress is called as https://, as every route of the table is."""
    monkeypatch.setattr(mns_routing.mtls, "enabled", lambda: True)
    monkeypatch.setattr(mns_routing.mtls, "client_kwargs", lambda url: {})          # the fake network needs no certificate
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert net["forwards"][0]["url"] == "https://echo:8000/ProvMnS/19.2.0/SubNetwork=A"


def test_a_trailing_slash_on_the_address_is_not_doubled(net):
    """An mnsAddress registered with a trailing slash gives one `/` before the LDN."""
    net["pages"] = [(_registry(("echo", ["SubNetwork"], ECHO + "/")), None)]
    client.get("/ProvMnS/19.2.0/SubNetwork=A", headers=AUTH)
    assert net["forwards"][0]["url"] == f"{ECHO}/SubNetwork=A"
