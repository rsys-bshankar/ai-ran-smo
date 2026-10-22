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


def test_a_correlation_id_that_is_not_short_printable_ascii_is_replaced():
    """Call flow 14a: an id with a space, a non-ASCII character, or more than 128 characters is replaced by a fresh UUID (seen by the handler and
    echoed), so it can never reach a downstream call; 128 printable characters are kept."""
    import uuid
    client = _app()
    for bad in ("has space", "x" * 129):
        resp = client.get("/echo", headers={HEADER_NAME: bad})
        assert resp.json()["correlationId"] == resp.headers[HEADER_NAME] != bad
        uuid.UUID(resp.headers[HEADER_NAME])
    resp = client.get("/echo", headers=[(HEADER_NAME.encode(), "café".encode("latin-1"))])
    uuid.UUID(resp.json()["correlationId"])
    assert client.get("/echo", headers={HEADER_NAME: "~" * 128}).json()["correlationId"] == "~" * 128


def test_an_unhandled_error_answers_with_the_correlation_id_in_the_header_and_the_body():
    """Call flow 14a: the 500 of an unhandled exception (handled outside the correlation middleware) carries the request's id in `X-Correlation-ID`
    and in the problem body's `correlationId`; a refusal raised by a route carries it in the body too."""
    from smo_shared.errors import FrameworkError, framework_error, install_integrity_handlers
    app = FastAPI()
    apply_correlation_id(app)
    install_integrity_handlers(app)

    @app.get("/boom")
    def boom():
        # Test route raising an unexpected error; not part of any published API.
        raise RuntimeError("boom")

    @app.get("/refused")
    def refused():
        # Test route raising a framework error; not part of any published API.
        raise framework_error(FrameworkError.INTERNAL_ERROR, detail="no")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom", headers={HEADER_NAME: "flow14-500"})
    assert resp.status_code == 500 and resp.headers[HEADER_NAME] == "flow14-500" and resp.json()["detail"]["correlationId"] == "flow14-500"
    assert client.get("/refused", headers={HEADER_NAME: "flow14-refused"}).json()["detail"]["correlationId"] == "flow14-refused"


def test_every_problem_answer_names_the_request_s_correlation_id():
    """Call flow 14a: the integrity, range, concurrency and body-size answers each carry the request's id in `correlationId`, so the mutation pilot's
    shared tests hold every place the id is put into a body."""
    from sqlalchemy.exc import DataError, IntegrityError
    from sqlalchemy.orm.exc import StaleDataError

    from smo_shared.bodylimit import BodySizeLimit
    from smo_shared.errors import install_integrity_handlers, install_out_of_range_handler
    from smo_shared.versioning import install_concurrency_handler
    app = FastAPI()
    install_integrity_handlers(app)
    install_out_of_range_handler(app)
    install_concurrency_handler(app)
    app.add_middleware(BodySizeLimit, settings=lambda: (10, {}))
    apply_correlation_id(app)
    errors = {"/integrity": IntegrityError("INSERT", {}, Exception("violates foreign key constraint")), "/overflow": OverflowError("big"),
              "/data": DataError("INSERT", {}, Exception("out of range")), "/stale": StaleDataError("stale")}
    for path, error in errors.items():
        app.add_api_route(path, lambda error=error: (_ for _ in ()).throw(error))
    app.add_api_route("/body", lambda: {}, methods=["POST"])
    client = TestClient(app, raise_server_exceptions=False)
    for path in errors:
        assert client.get(path, headers={HEADER_NAME: f"id{path}"}).json()["detail"]["correlationId"] == f"id{path}", path
    big = client.post("/body", content=b"x" * 11, headers={HEADER_NAME: "id-413"})
    assert big.status_code == 413 and big.json()["correlationId"] == "id-413" and big.headers[HEADER_NAME] == "id-413"


def test_a_callback_carries_the_current_correlation_id_and_none_outside_one(monkeypatch):
    """Call flow 14c: a webhook sent while an id is current carries it as X-Correlation-ID; one sent outside any carries no headers argument."""
    import httpx

    from smo_shared import webhook
    from smo_shared.correlation import correlation_scope
    sent = []
    monkeypatch.setattr(webhook, "_vet_resolution", lambda destination: type("V", (), {"blocked": False, "addresses": ()})())
    monkeypatch.setattr(webhook, "_pinned_to", lambda destination, vetting: __import__("contextlib").nullcontext())
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw.get("headers")) or httpx.Response(200))
    with correlation_scope("cb-14"):
        webhook.post_webhook("http://consumer/cb", {})
    webhook.post_webhook("http://consumer/cb", {})
    assert sent == [{HEADER_NAME: "cb-14"}, None]
