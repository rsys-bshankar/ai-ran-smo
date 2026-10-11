"""Exact-behaviour tests of the mock O1 adaptor's emit path (`_emit`), added because a mutation pass over it found changes no test noticed (PR-SEC-15 part 4).

`test_emit.py` covers the happy paths and the refusals' status codes; this file pins the guarded sender's behaviour for a caller-named `target` (the body is forwarded, an
unreachable target is a 502 that does not leak the address) and the words of the 409, 422 and 502 answers an operator reads. `Oam`, `oam` and `client` come from `test_emit.py`.
Run: `PYTHONPATH=.:../shared python -m pytest tests/test_emit_exact.py -q`.
"""

import httpx

from test_emit import OAM, Oam, client, oam  # noqa: F401  (oam: a pytest fixture)

MEASUREMENTS = [{"cellId": "101", "timestamp": "2026-10-01T12:00:00+00:00", "value": 40.0}]


def test_a_report_sent_to_a_named_target_carries_its_body(monkeypatch, oam):
    """With a caller-named `target` the JSON body still reaches RAN NF OAM (the guarded sender gets the body, not an empty object), at the target's origin with its trailing slash dropped."""
    monkeypatch.delenv("MOCK_O1_OAM_URL")
    resp = client.post("/emit/pm-report", json={"managedElementRef": "gnb-1", "counterType": "LOAD", "measurements": MEASUREMENTS, "target": "http://other-oam:9000/"})
    assert resp.status_code == 200 and resp.json()["target"] == "http://other-oam:9000"
    call = oam.calls[0]
    assert call["url"] == "http://other-oam:9000/pm-reports"
    assert call["json"]["managedElementRef"] == "gnb-1" and call["json"]["measurements"] == MEASUREMENTS


def test_an_unreachable_named_target_is_a_502_that_names_the_kind_not_the_address(monkeypatch):
    """When the guarded sender gets no answer from a caller-named target the answer is 502 `RAN NF OAM unreachable`, worded with the trigger kind and not the address the caller typed."""
    monkeypatch.delenv("MOCK_O1_OAM_URL", raising=False)
    monkeypatch.setattr(httpx, "post", Oam(raises=httpx.ConnectError("down")).post)
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR", "target": "http://other-oam:9000"})
    assert resp.status_code == 502 and resp.json()["title"] == "RAN NF OAM unreachable"
    assert resp.json()["detail"] == "POST to alarm target failed" and "other-oam" not in resp.text


def test_an_unreachable_configured_oam_is_a_502_that_names_the_path_and_the_error(monkeypatch):
    """When the configured RAN NF OAM cannot be reached the answer is 502 whose detail names the path and the exception class."""
    monkeypatch.setenv("MOCK_O1_OAM_URL", OAM)
    monkeypatch.setattr(httpx, "post", Oam(raises=httpx.ConnectError("down")).post)
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR"})
    assert resp.status_code == 502 and resp.json()["title"] == "RAN NF OAM unreachable"
    assert resp.json()["detail"] == "POST /alarms/ingest failed: ConnectError"


def test_the_refusals_are_worded_for_the_operator(monkeypatch, oam):
    """The 409 for an undeclared service names the service, the variable and the trigger; the 422 for a bad target says what a usable one is."""
    monkeypatch.setenv("MOCK_O1_SUPPORTED_SERVICES", "FM")
    undeclared = client.post("/emit/heartbeat", json={"endpointId": "e"})
    assert undeclared.status_code == 409 and undeclared.json()["title"] == "service not declared"
    assert "MOCK_O1_SUPPORTED_SERVICES" in undeclared.json()["detail"] and "supportedServices" in undeclared.json()["detail"]
    bad = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR", "target": "http://127.0.0.1:1"})
    assert bad.status_code == 422 and bad.json()["title"] == "unusable target"
    assert "http(s) origin" in bad.json()["detail"] and "loopback" in bad.json()["detail"]
