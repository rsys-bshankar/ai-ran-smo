"""A rApp may report a fault for, or set the configuration of, its own instance only (SEC-15.9).

Covers `POST /instances/{id}/fault` and `PUT /instances/{id}/config`. Fixtures (`client`, `db_session_factory`, `fake_r1_delete`) and the `_create` helper come from
`test_main.py` and `test_operator_api.py`. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_own_instance_checks.py -q`.
"""

from test_main import client, db_session_factory, fake_r1_delete  # noqa: F401  (fixtures)
from test_operator_api import _create


def _rapp(instance: dict) -> dict:
    """The headers the gateway stamps on a call made with this instance's own token."""
    return {"X-R1-Role": "rapp", "X-R1-Invoker-Id": instance["oauthClientId"]}


def test_a_rapp_cannot_report_a_fault_for_another_instance(client, monkeypatch):
    """Another instance's token is refused with 403 `NOT_THIS_INSTANCE`, records nothing and does not change the target's state; the instance itself and an operator may report."""
    mine = _create(client, monkeypatch)
    other = _create(client, monkeypatch)
    url = f"/instances/{other['instanceId']}/fault"
    resp = client.post(url, params={"severity": "critical"}, headers=_rapp(mine))
    assert resp.status_code == 403 and "NOT_THIS_INSTANCE" in resp.text
    assert client.post(url, params={"severity": "minor"}, headers={"X-R1-Role": "rapp"}).status_code == 403    # no invoker id at all
    assert client.get(f"/instances/{other['instanceId']}/faults").json()["items"] == []
    assert client.post(url, params={"severity": "minor"}, headers=_rapp(other)).status_code == 200
    assert client.post(url, params={"severity": "minor"}, headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "smo-gui"}).status_code == 200
    assert client.post(url, params={"severity": "minor"}).status_code == 200      # a call that did not come through the gateway is trusted as elsewhere


def test_a_rapp_cannot_set_the_configuration_of_another_instance(client, monkeypatch):
    """Another instance's token cannot replace this instance's configuration (403); its own token and an operator can."""
    mine = _create(client, monkeypatch)
    other = _create(client, monkeypatch)
    url = f"/instances/{other['instanceId']}/config"
    assert client.put(url, json={"k": "evil"}, headers=_rapp(mine)).status_code == 403
    assert client.get(url).json() == {}
    assert client.put(url, json={"k": "v"}, headers=_rapp(other)).status_code == 200
    assert client.put(url, json={"k": "op"}, headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "smo-gui"}).status_code == 200
    assert client.get(url).json() == {"k": "op"}
