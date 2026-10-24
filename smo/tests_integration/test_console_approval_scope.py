"""SEC-10.12 through the real R1 gateway: the operator console's call for a person limited to one region (`X-R1-Acting-User` and `X-R1-Acting-User-Scope`, sent
by an `internal` caller with a real token) reaches RAN NF OAM's approval inbox for rApp actions, which lists only that person's requests and answers a request of
another region exactly as an unknown id (404 `APPROVAL_NOT_FOUND`, ADR 0005 sections 5 and 11). The gateway introspects the token at the real SME and forwards to
the real RAN NF OAM of the in-process mesh, so the test proves the person's claim survives the hop and is applied where the request lives.
"""

import json
import uuid
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


class _Upstream:
    """Stands in for httpx.AsyncClient inside the gateway: introspection goes to the real SME, a call to RAN NF OAM to the mesh's RAN NF OAM with the headers
    the gateway stamped."""

    def __init__(self, mesh, **_):
        self.mesh = mesh

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if parsed.path == "/oauth2/introspect":
            answer = self.mesh["sme"].post("/oauth2/introspect", json=kwargs["json"])
            return httpx.Response(answer.status_code, json=answer.json())
        assert parsed.hostname == "ran-nf-oam", url
        answer = self.mesh["ran-nf-oam"].request(method, parsed.path, headers=kwargs.get("headers"), params=kwargs.get("params"),
                                                 content=kwargs.get("content"))
        return httpx.Response(answer.status_code, content=answer.content, headers={"content-type": answer.headers.get("content-type", "application/json")})


@pytest.fixture
def console(mesh, loaded_apps, db_connection, monkeypatch):
    """The real gateway in front of SME and RAN NF OAM, an `internal` invoker's token (the console's), an eu and a us element and an unscoped rApp's pending
    request on each; returns (gateway client, the console's Authorization header, {region: approval id})."""
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")
    monkeypatch.delenv("SMO_ROLE_ENFORCEMENT", raising=False)
    r1 = loaded_apps["r1-termination"]
    monkeypatch.setattr(r1.httpx, "AsyncClient", lambda **kwargs: _Upstream(mesh, **kwargs))
    for ref, region in (("ME-EU", "eu"), ("ME-US", "us")):
        db_connection.execute(text("INSERT INTO managed_entity (managed_element_ref, entity_type, o1_protocol, cell_guards, region, tenant) "
                                   "VALUES (:ref, 'O-DU', 'NETCONF', '{}', :region, NULL)"), {"ref": ref, "region": region})
    oam = mesh["ran-nf-oam"]
    assert oam.put("/rapp-approval-policy/es-client", json={"requestedBy": "admin"}).status_code == 200
    parked = {}
    for region, ref in (("eu", "ME-EU"), ("us", "ME-US")):
        resp = oam.post("/config-jobs", headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"},
                        json={"requestedBy": "es-rapp", "scope": "cell", "changes": [{"managedElementRef": ref, "attributeChanges": {"txPower": 12}}]})
        assert resp.status_code == 202 and resp.json()["status"] == "PENDING_APPROVAL", resp.text
        parked[region] = resp.json()["approvalId"]
    invoker = mesh["sme"].post("/invoker-registrations", json={"apiInvokerPublicKey": "console-key"}).json()
    token = mesh["sme"].post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": invoker["apiInvokerId"],
                                                    "client_secret": invoker["onboardingSecret"]}).json()["access_token"]
    return TestClient(r1.app, raise_server_exceptions=False), {"Authorization": f"Bearer {token}"}, parked


def test_a_person_limited_to_one_region_sees_and_decides_only_that_regions_requests_through_the_gateway(console):
    """Through the gateway, a person limited to eu lists only the eu request (total 1), gets 404 APPROVAL_NOT_FOUND for the us request on read, approve and reject
    exactly as for an unknown id, and the us request is still PENDING for an unscoped person afterwards."""
    gateway, auth, parked = console
    eu_person = {**auth, "X-R1-Acting-User": "smo-gui:alice", "X-R1-Acting-User-Scope": json.dumps({"regions": ["eu"]})}
    page = gateway.get("/ran-nf-oam/rapp-approvals", headers=eu_person, params={"status": "PENDING"})
    assert page.status_code == 200, page.text
    assert [i["approvalId"] for i in page.json()["items"]] == [parked["eu"]] and page.json()["total"] == 1
    for method, suffix, body in (("GET", "", None), ("POST", "/approve", {}), ("POST", "/reject", {"reason": "no"})):
        hidden = gateway.request(method, f"/ran-nf-oam/rapp-approvals/{parked['us']}{suffix}", headers=eu_person, json=body)
        absent = gateway.request(method, f"/ran-nf-oam/rapp-approvals/{uuid.uuid4()}{suffix}", headers=eu_person, json=body)
        assert hidden.status_code == absent.status_code == 404 and hidden.json()["detail"]["title"] == absent.json()["detail"]["title"] == "APPROVAL_NOT_FOUND"
    unscoped = {**auth, "X-R1-Acting-User": "smo-gui:alice"}
    assert gateway.get(f"/ran-nf-oam/rapp-approvals/{parked['us']}", headers=unscoped).json()["status"] == "PENDING"
    assert gateway.get("/ran-nf-oam/rapp-approvals", headers=unscoped).json()["total"] == 2
