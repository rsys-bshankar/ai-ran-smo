"""Tests of call flow 08a (DOC-CF-1.8): an rApp registers and deregisters only itself as an analytics producer, SME is asked before the producer is stored, and a
deregistration withdraws the producer's SME service.

Fixtures come from `test_main.py` (the app on SQLite, SME answering 201 / 204). Run: `cd smo/ran-analytics && PYTHONPATH=.:../shared python -m pytest
tests/test_producer_identity.py -q`.
"""

import httpx
import pytest

from test_main import FakeSme, client, db_session_factory  # noqa: F401  (pytest fixtures)

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "kpi-rapp"}
BODY = {"dme_input_types": [], "output_schema": {}}


def _register(client, producer="kpi-rapp", headers=RAPP, analytics_type="cell-kpi"):
    """Registers `producer` for `analytics_type` as the caller in `headers`."""
    return client.post("/producers", params={"producer_id": producer, "analytics_type": analytics_type}, json=BODY, headers=headers)


def test_an_rapp_registers_only_itself(client):
    """Call flow 08a: an rApp naming another producer is 403 PRODUCER_IDENTITY_MISMATCH and nothing is stored; itself, 201; the console or a module may name any."""
    other = _register(client, producer="someone-else")
    assert other.status_code == 403 and other.json()["detail"]["title"] == "PRODUCER_IDENTITY_MISMATCH"
    assert client.get("/producers").json()["items"] == []
    assert _register(client).status_code == 201
    assert _register(client, producer="someone-else", headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff"}).status_code == 201


@pytest.mark.parametrize("answers", [
    {"/sme/provider-registrations": FakeSme(403, {})},
    {"/sme/published-apis/v1/kpi-rapp/service-apis": FakeSme(409, {"detail": {"title": "SERVICE_NAME_CONFLICT"}})},
    {"/sme/provider-registrations": httpx.ConnectError("refused")},
])
def test_a_registration_sme_refuses_stores_nothing(client, monkeypatch, answers):
    """Call flow 08a: SME refusing the enrolment or the service, or not answering, is 502 UPSTREAM_FAILED and the producer is not stored (before: stored, and
    silently undiscoverable)."""
    def post(self, path, json=None, **kw):
        """SME's scripted answer."""
        answer = answers.get(path, FakeSme())
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr("app.main.R1Client.post", post)
    resp = _register(client)
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert client.get("/producers").json()["items"] == []


def test_two_producers_of_one_type_each_get_their_own_service(client, monkeypatch):
    """Call flow 08a: the SME service is named per producer and type, so a second producer of a type no longer collides with the first."""
    names = []
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: names.append((json or {}).get("serviceName")) or FakeSme())
    _register(client)
    _register(client, producer="other-rapp", headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-rapp"})
    assert [n for n in names if n] == ["mdaf.cell-kpi.kpi-rapp", "mdaf.cell-kpi.other-rapp"]


def test_a_deregistration_withdraws_the_sme_service(client, monkeypatch):
    """Call flow 08a: DELETE /producers/{id}?analytics_type withdraws the SME service the registration made and removes the producer; again is 404."""
    deleted = []
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: deleted.append(path) or FakeSme(204, {}))
    _register(client)
    resp = client.delete("/producers/kpi-rapp", params={"analytics_type": "cell-kpi"}, headers=RAPP)
    assert resp.status_code == 204 and client.get("/producers").json()["items"] == []
    assert deleted == ["/sme/published-apis/v1/kpi-rapp/service-apis/11111111-1111-1111-1111-111111111111"]
    again = client.delete("/producers/kpi-rapp", params={"analytics_type": "cell-kpi"}, headers=RAPP)
    assert again.status_code == 404 and again.json()["detail"]["title"] == "PRODUCER_NOT_FOUND"


def test_an_rapp_deregisters_only_itself_and_sme_must_answer(client, monkeypatch):
    """Call flow 08a: another rApp's deregistration is 403; SME not answering is 502 and the producer is kept."""
    _register(client)
    other = client.delete("/producers/kpi-rapp", params={"analytics_type": "cell-kpi"}, headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-rapp"})
    assert other.status_code == 403

    def down(self, path, **kw):
        """SME does not answer."""
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr("app.main.R1Client.delete", down)
    assert client.delete("/producers/kpi-rapp", params={"analytics_type": "cell-kpi"}, headers=RAPP).status_code == 502
    assert len(client.get("/producers").json()["items"]) == 1
