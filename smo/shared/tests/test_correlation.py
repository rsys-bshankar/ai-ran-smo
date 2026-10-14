"""apply_correlation_id (smo_shared/correlation.py) — Wave 3 cross-cutting
standardization's Correlation-ID slice. Run with:
cd smo/shared && PYTHONPATH=. python -m pytest tests -q
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from smo_shared.correlation import HEADER_NAME, apply_correlation_id, get_correlation_id


def _app() -> TestClient:
    """Helper: a test client for an app with the correlation middleware and a route `/echo` that returns the id the handler sees."""
    app = FastAPI()
    apply_correlation_id(app)

    @app.get("/echo")
    def echo():
        # Test route returning the correlation id the handler sees; not part of any published API.
        return {"correlationId": get_correlation_id()}

    return TestClient(app)


def test_generates_a_correlation_id_when_the_caller_sends_none():
    """Without an X-Correlation-ID header the service makes one, uses it in the handler and returns it in the response header."""
    client = _app()
    resp = client.get("/echo")
    assert resp.status_code == 200
    generated = resp.json()["correlationId"]
    assert generated  # a real, non-empty id was generated
    assert resp.headers[HEADER_NAME] == generated


def test_propagates_and_echoes_the_callers_own_correlation_id():
    """A caller-supplied correlation id is kept for the handler and echoed back unchanged."""
    client = _app()
    resp = client.get("/echo", headers={HEADER_NAME: "caller-supplied-id"})
    assert resp.status_code == 200
    assert resp.json()["correlationId"] == "caller-supplied-id"
    assert resp.headers[HEADER_NAME] == "caller-supplied-id"


def test_get_correlation_id_is_none_outside_any_request():
    """Outside a request there is no correlation id, so a standalone script's outbound calls carry none."""
    assert get_correlation_id() is None


def test_two_requests_get_two_different_generated_ids():
    """Generated ids are per request: two requests never share one."""
    client = _app()
    first = client.get("/echo").json()["correlationId"]
    second = client.get("/echo").json()["correlationId"]
    assert first != second


def test_correlation_scope_sets_the_id_outside_a_request_and_restores_it():
    """Call flow 22e: inside `correlation_scope` the id is the current one (a worker's task has no request), and the previous value is back after it."""
    from smo_shared.correlation import correlation_scope, get_correlation_id
    assert get_correlation_id() is None
    with correlation_scope("pass-1") as cid:
        assert cid == get_correlation_id() == "pass-1"
    assert get_correlation_id() is None
