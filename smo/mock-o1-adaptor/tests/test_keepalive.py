"""The stub's periodic heartbeat (`app/keepalive.py`): which endpoints it keeps alive, that it pages RAN NF OAM's list, and that it is off unless configured.

RAN NF OAM is replaced by recording `httpx.get` / `httpx.post` fakes. Needs no network. Run: `cd smo/mock-o1-adaptor && PYTHONPATH=.:../shared python -m pytest tests/test_keepalive.py -q`.
"""

import httpx
import pytest

from app import keepalive

OAM, SELF = "http://ran-nf-oam:8000", "http://mock-o1-adaptor:8000"


def _ep(endpoint_id: str, uri: str, beat: bool = True) -> dict:
    return {"endpointId": endpoint_id, "adaptorUri": uri, "lastHeartbeatAt": "2026-10-13T10:00:00+00:00" if beat else None}


@pytest.fixture
def oam(monkeypatch):
    """Fixture: RAN NF OAM's endpoint list is `state["endpoints"]` (paged as the route pages it); heartbeat POSTs are recorded in `state["beats"]`."""
    state = {"endpoints": [], "beats": [], "gets": 0}

    def get(url, params=None, timeout=None):
        state["gets"] += 1
        items = state["endpoints"][params["offset"]:params["offset"] + params["limit"]]
        return httpx.Response(200, json={"items": items, "total": len(state["endpoints"]), "limit": params["limit"], "offset": params["offset"]})

    def post(url, timeout=None):
        state["beats"].append(url)
        return httpx.Response(200, json={"healthStatus": "ACTIVE"})

    monkeypatch.setattr(keepalive.httpx, "get", get)
    monkeypatch.setattr(keepalive.httpx, "post", post)
    return state


def test_only_this_adaptors_endpoints_that_had_a_first_heartbeat_are_kept_alive(oam):
    """An endpoint of this stub that had its first heartbeat gets one; one never heartbeaten (DISCOVERED, the operator's first press) and one of another
    adaptor (even with a prefix-sharing host) get none."""
    oam["endpoints"] = [_ep("a", f"{SELF}/edit-config"), _ep("b", f"{SELF}/restconf"), _ep("c", f"{SELF}/edit-config", beat=False),
                        _ep("d", "http://other-adaptor:8000/edit-config"), _ep("e", f"{SELF}0/edit-config")]
    assert keepalive.beat_once(OAM, SELF) == 2
    assert oam["beats"] == [f"{OAM}/o1-adaptor-endpoints/a/heartbeat", f"{OAM}/o1-adaptor-endpoints/b/heartbeat"]


def test_every_page_of_endpoints_is_read(oam, monkeypatch):
    """More endpoints than one page: each page is read and every endpoint of this stub gets its heartbeat."""
    monkeypatch.setattr(keepalive, "PAGE", 2)
    oam["endpoints"] = [_ep(str(i), f"{SELF}/edit-config") for i in range(5)]
    assert keepalive.beat_once(OAM, SELF) == 5 and oam["gets"] == 3


@pytest.mark.parametrize("seconds, oam_url, self_url", [(None, OAM, SELF), ("0", OAM, SELF), ("x", OAM, SELF), ("30", "", SELF), ("30", OAM, "")])
def test_it_is_off_unless_the_interval_and_both_urls_are_set(monkeypatch, seconds, oam_url, self_url):
    """No thread starts without a positive MOCK_O1_HEARTBEAT_SECONDS, MOCK_O1_OAM_URL and MOCK_O1_SELF_URL."""
    if seconds is None:
        monkeypatch.delenv("MOCK_O1_HEARTBEAT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MOCK_O1_HEARTBEAT_SECONDS", seconds)
    monkeypatch.setenv("MOCK_O1_OAM_URL", oam_url)
    monkeypatch.setenv("MOCK_O1_SELF_URL", self_url)
    assert keepalive.start() is None


def test_the_thread_beats_on_its_interval_and_stops(oam, monkeypatch):
    """With everything set the thread sends rounds every interval, and setting the returned event stops it."""
    import time
    monkeypatch.setenv("MOCK_O1_HEARTBEAT_SECONDS", "0.05")
    monkeypatch.setenv("MOCK_O1_OAM_URL", OAM)
    monkeypatch.setenv("MOCK_O1_SELF_URL", SELF)
    oam["endpoints"] = [_ep("a", f"{SELF}/edit-config")]
    stop = keepalive.start()
    try:
        deadline = time.monotonic() + 2
        while len(oam["beats"]) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(oam["beats"]) >= 2
    finally:
        stop.set()


def test_a_refused_heartbeat_is_logged_and_not_counted(oam, monkeypatch, caplog):
    """RAN NF OAM answering an endpoint's heartbeat with an error (the endpoint deleted meanwhile) is logged, not counted, and the others still go."""
    oam["endpoints"] = [_ep("gone", f"{SELF}/edit-config"), _ep("a", f"{SELF}/edit-config")]
    monkeypatch.setattr(keepalive.httpx, "post", lambda url, timeout=None: httpx.Response(404 if "/gone/" in url else 200, json={}))
    with caplog.at_level("WARNING", logger="app.keepalive"):
        assert keepalive.beat_once(OAM, SELF) == 1
    assert any("gone" in r.getMessage() and "404" in r.getMessage() for r in caplog.records)


def test_a_failed_round_is_logged_and_the_next_one_still_runs(monkeypatch, caplog):
    """RAN NF OAM unreachable: the round is logged and the thread goes on to the next one instead of dying."""
    import threading
    calls = []
    two = threading.Event()

    def flaky(oam_url, self_url):
        calls.append(1)
        if len(calls) >= 2:
            two.set()
        raise httpx.ConnectError("down")

    monkeypatch.setattr(keepalive, "beat_once", flaky)
    monkeypatch.setenv("MOCK_O1_HEARTBEAT_SECONDS", "0.02")
    monkeypatch.setenv("MOCK_O1_OAM_URL", OAM)
    monkeypatch.setenv("MOCK_O1_SELF_URL", SELF)
    with caplog.at_level("WARNING", logger="app.keepalive"):
        stop = keepalive.start()
        try:
            assert two.wait(2)
        finally:
            stop.set()
    assert any("heartbeat round" in r.getMessage() and "ConnectError" in r.getMessage() for r in caplog.records)


def test_the_app_starts_the_heartbeat_with_its_lifespan_and_stops_it_on_shutdown(monkeypatch):
    """The app's lifespan starts the thread (when configured) and sets its stop event when the app shuts down."""
    import threading
    from fastapi.testclient import TestClient
    from app.main import app
    stop = threading.Event()
    monkeypatch.setattr(keepalive, "start", lambda: stop)
    with TestClient(app):
        assert not stop.is_set()
    assert stop.is_set()
