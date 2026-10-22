"""PR-OBS-2: request count and latency series, and /metrics.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_metrics.py -q
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from smo_shared.health import install_health
from smo_shared.metrics import install_metrics


def _app() -> FastAPI:
    """Helper: an app with metrics and health installed, a templated route (which can answer 409) and a route that crashes."""
    app = FastAPI()
    install_metrics(app)
    install_health(app)

    @app.get("/models/{model_id}")
    def model(model_id: str):
        # Test route that can answer 409; not part of any published API.
        if model_id == "boom":
            raise HTTPException(status_code=409, detail="x")
        return {"id": model_id}

    @app.get("/crash")
    def crash():
        # Test route that raises an unexpected error; not part of any published API.
        raise RuntimeError("no")

    return app


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_a_request_is_counted_under_its_route_template_and_status():
    """Requests are counted under the route template and status, so two different ids add two to one series."""
    client = TestClient(_app())
    before = _sample("smo_http_requests_total", method="GET", route="/models/{model_id}", status="200")
    client.get("/models/a")
    client.get("/models/b")
    assert _sample("smo_http_requests_total", method="GET", route="/models/{model_id}", status="200") == before + 2


def test_raw_ids_never_become_label_values():
    """A raw path id never appears in the scrape output, which would make the label set unbounded."""
    TestClient(_app()).get("/models/some-unique-id-123")
    assert "some-unique-id-123" not in TestClient(_app()).get("/metrics").text


def test_error_statuses_have_their_own_series_and_unmatched_paths_share_one():
    """A 409, a 500 and unmatched 404s are counted under their own status, with every unknown path sharing the `unmatched` series."""
    client = TestClient(_app(), raise_server_exceptions=False)
    b409 = _sample("smo_http_requests_total", method="GET", route="/models/{model_id}", status="409")
    b404 = _sample("smo_http_requests_total", method="GET", route="unmatched", status="404")
    b500 = _sample("smo_http_requests_total", method="GET", route="/crash", status="500")
    client.get("/models/boom")
    client.get("/nope/1")
    client.get("/nope/2")
    client.get("/crash")
    assert _sample("smo_http_requests_total", method="GET", route="/models/{model_id}", status="409") == b409 + 1
    assert _sample("smo_http_requests_total", method="GET", route="unmatched", status="404") == b404 + 2
    assert _sample("smo_http_requests_total", method="GET", route="/crash", status="500") == b500 + 1


def test_latency_is_observed_in_a_histogram():
    """Each request adds one observation and a positive duration to the latency histogram."""
    client = TestClient(_app())
    before = _sample("smo_http_request_duration_seconds_count", method="GET", route="/models/{model_id}", status="200")
    client.get("/models/a")
    assert _sample("smo_http_request_duration_seconds_count", method="GET", route="/models/{model_id}", status="200") == before + 1
    assert _sample("smo_http_request_duration_seconds_sum", method="GET", route="/models/{model_id}", status="200") > 0


def test_probes_and_the_scrape_itself_are_not_counted():
    """Probe routes and /metrics are not counted, or a scrape every few seconds would dominate the series."""
    client = TestClient(_app())
    client.get("/live")
    client.get("/health")
    client.get("/metrics")
    client.get("/metrics")
    text = client.get("/metrics").text
    for route in ("/live", "/health", "/metrics"):
        assert f'route="{route}"' not in text


def test_metrics_is_prometheus_text_and_not_in_the_openapi_spec():
    """/metrics answers Prometheus text format and is not published in the OpenAPI document."""
    client = TestClient(_app())
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE smo_http_requests_total counter" in response.text
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


# --- PR-OBS-2.5: state machine transitions ----------------------------------------------------------------------------------------

def test_every_transition_is_counted_by_machine_state_event_and_target_and_refusals_have_their_own_series():
    """A state machine counts each transition by machine, from-state, event and to-state, and each refused one in its own series."""
    import enum
    from smo_shared.statemachine import IllegalTransition, StateMachine

    class DemoState(enum.Enum):
        NEW = "NEW"
        DONE = "DONE"

    class DemoEvent(enum.Enum):
        FINISH = "FINISH"
        REOPEN = "REOPEN"

    fsm = StateMachine().add(DemoState.NEW, DemoEvent.FINISH, DemoState.DONE)
    labels = dict(machine="DemoState", from_state="NEW", event="FINISH", to_state="DONE")
    before = _sample("smo_fsm_transitions_total", **labels)
    assert fsm.fire(DemoState.NEW, DemoEvent.FINISH) is DemoState.DONE
    assert fsm.fire(DemoState.NEW, DemoEvent.FINISH) is DemoState.DONE
    assert _sample("smo_fsm_transitions_total", **labels) == before + 2

    refused = dict(machine="DemoState", from_state="DONE", event="REOPEN")
    before = _sample("smo_fsm_illegal_transitions_total", **refused)
    with pytest.raises(IllegalTransition):
        fsm.fire(DemoState.DONE, DemoEvent.REOPEN)
    assert _sample("smo_fsm_illegal_transitions_total", **refused) == before + 1


def test_a_guard_that_rejects_counts_as_a_refusal_not_a_transition():
    """A transition whose guard rejects is counted as a refusal, not as a taken transition."""
    import enum
    from smo_shared.statemachine import IllegalTransition, StateMachine

    class GuardedState(enum.Enum):
        A = "A"
        B = "B"

    fsm = StateMachine().add(GuardedState.A, "go", GuardedState.B, guard=lambda **ctx: ctx.get("ok", False))
    ok = dict(machine="GuardedState", from_state="A", event="go", to_state="B")
    no = dict(machine="GuardedState", from_state="A", event="go")
    t0, i0 = _sample("smo_fsm_transitions_total", **ok), _sample("smo_fsm_illegal_transitions_total", **no)
    with pytest.raises(IllegalTransition):
        fsm.fire(GuardedState.A, "go", ok=False)
    assert fsm.fire(GuardedState.A, "go", ok=True) is GuardedState.B
    assert _sample("smo_fsm_transitions_total", **ok) == t0 + 1 and _sample("smo_fsm_illegal_transitions_total", **no) == i0 + 1


# --- PR-OBS-2.4: database pool gauges ----------------------------------------------------------------------------------------------

def test_pool_gauges_follow_checkouts_and_returns(tmp_path):
    """The pool gauges show in-use, idle and overflow connections and the capacity as connections are checked out and returned."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import QueuePool
    from smo_shared.metrics import PoolCollector

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}", poolclass=QueuePool, pool_size=2, max_overflow=1)
    collector = PoolCollector(lambda: engine)

    def gauges():
        out = {}
        for family in collector.collect():
            for sample in family.samples:
                out[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
        return out

    def state(name):
        return gauges()[("smo_db_pool_connections", (("state", name),))]

    assert state("in_use") == 0 and gauges()[("smo_db_pool_capacity", ())] == 3
    held = [engine.connect() for _ in range(3)]                       # two in the pool plus one overflow
    assert state("in_use") == 3 and state("overflow") == 1 and state("idle") == 0
    for conn in held:
        conn.close()
    assert state("in_use") == 0 and state("idle") == 2 and state("overflow") == 0


def test_a_pool_that_is_not_a_queue_pool_reports_nothing():
    """A pool without the queue-pool counters (SQLite's test pool) yields no pool series instead of an error."""
    from smo_shared.metrics import PoolCollector
    from smo_shared.testing import make_test_engine

    assert list(PoolCollector(make_test_engine).collect()) == []


def test_the_scrape_carries_the_pool_and_fsm_families_for_the_modules_own_engine():
    """The scrape output includes the state-machine counter family registered by the shared library."""
    body = TestClient(_app()).get("/metrics").text
    assert "# TYPE smo_fsm_transitions_total counter" in body


def test_a_service_without_database_credentials_can_install_metrics():
    """Installing metrics in a process with no database settings (R1 Termination, the mocks) must not import smo_shared.db, which would refuse to start
    without a URL.
    """
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("SMO_DATABASE") and k != "SMO_ALLOW_INSECURE_DEFAULT_DB"}
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    code = ("import sys\nfrom fastapi import FastAPI\nfrom smo_shared.metrics import install_metrics\n"
            "app = FastAPI(); install_metrics(app)\nassert 'smo_shared.db' not in sys.modules\nprint('ok')")
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr


# --- PR-OBS-2.6: outbound calls ---------------------------------------------------------------------------------------------------------


def _calls(client, target, method, outcome) -> float:
    """Helper: the current value of smo_outbound_calls_total for one (client, target, method, outcome)."""
    return _sample("smo_outbound_calls_total", client=client, target=target, method=method, outcome=outcome)


def test_r1_target_is_the_module_in_the_path_and_bounded():
    """r1_target returns the module segment of a path (without query) and `other` for anything that is not module-shaped or too long, keeping the label
    bounded.
    """
    from smo_shared.metrics import r1_target
    assert r1_target("/sme/service-apis/v1/x") == "sme" and r1_target("/dme/data-jobs?x=1") == "dme" and r1_target("sme") == "sme"
    assert r1_target("/") == "other" and r1_target("/Not A Module/x") == "other" and r1_target("/" + "a" * 80) == "other"


# Table: the stubbed response (200, 404, 503) and the outcome class (2xx, 4xx, 5xx) it must be counted under; each call also records a duration.
@pytest.mark.parametrize("behaviour,outcome", [
    (lambda *a, **k: __import__("httpx").Response(200), "2xx"),
    (lambda *a, **k: __import__("httpx").Response(404), "4xx"),
    (lambda *a, **k: __import__("httpx").Response(503), "5xx"),
])
def test_r1_client_counts_each_call_by_target_method_and_outcome(monkeypatch, behaviour, outcome):
    from smo_shared.r1_client import R1Client
    monkeypatch.setattr("smo_shared.r1_client.httpx.get", behaviour)
    before = _calls("r1", "dme", "GET", outcome)
    R1Client(bearer_token="t").get("/dme/data-jobs")
    assert _calls("r1", "dme", "GET", outcome) == before + 1
    assert _sample("smo_outbound_call_duration_seconds_count", client="r1", target="dme") >= 1


def test_r1_client_counts_a_timeout_and_a_connection_error_and_still_raises(monkeypatch):
    """R1Client counts a timeout and a connection error under their outcomes and still re-raises the exception to the caller."""
    import httpx
    from smo_shared.r1_client import R1Client

    def timeout(*a, **k):
        raise httpx.ReadTimeout("slow")

    def refused(*a, **k):
        raise httpx.ConnectError("no")

    client = R1Client(bearer_token="t")
    t0, e0 = _calls("r1", "nfo", "POST", "timeout"), _calls("r1", "nfo", "POST", "error")
    monkeypatch.setattr("smo_shared.r1_client.httpx.post", timeout)
    with pytest.raises(httpx.ReadTimeout):
        client.post("/nfo/jobs", json={})
    monkeypatch.setattr("smo_shared.r1_client.httpx.post", refused)
    with pytest.raises(httpx.ConnectError):
        client.post("/nfo/jobs", json={})
    assert _calls("r1", "nfo", "POST", "timeout") == t0 + 1 and _calls("r1", "nfo", "POST", "error") == e0 + 1


def test_the_retry_after_a_401_is_a_second_counted_call(monkeypatch):
    """The automatic retry after a 401 is counted as a second call, so the 4xx and the 2xx each appear once."""
    import httpx
    from smo_shared import r1_client
    answers = iter([httpx.Response(401), httpx.Response(200)])
    monkeypatch.setattr(r1_client.httpx, "get", lambda *a, **k: next(answers))
    monkeypatch.setattr(r1_client, "_module_token", lambda base, refresh=False: "tok")
    f0, o0 = _calls("r1", "mlmr", "GET", "4xx"), _calls("r1", "mlmr", "GET", "2xx")
    assert r1_client.R1Client().get("/mlmr/models").status_code == 200
    assert _calls("r1", "mlmr", "GET", "4xx") == f0 + 1 and _calls("r1", "mlmr", "GET", "2xx") == o0 + 1


def test_webhooks_are_counted_under_one_constant_target_never_the_host(monkeypatch):
    """Callbacks are counted under the constant target `callback` (a refused destination as `blocked`) and no destination host appears in the metric
    output.
    """
    import httpx
    from smo_shared import webhook
    monkeypatch.setattr(webhook.httpx, "post", lambda url, json=None, timeout=None, **kw: httpx.Response(500))
    monkeypatch.setattr(webhook.httpx, "delete", lambda url, timeout=None, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
    p0, d0, b0 = _calls("webhook", "callback", "POST", "5xx"), _calls("webhook", "callback", "DELETE", "error"), _calls("webhook", "callback", "GET", "blocked")
    webhook.post_webhook("http://consumer.example/cb", {})
    webhook.delete_webhook("http://consumer.example/cb/1")
    webhook.get_webhook("http://localhost/cb")                      # the SSRF guard refuses it: nothing sent, counted as blocked
    assert _calls("webhook", "callback", "POST", "5xx") == p0 + 1
    assert _calls("webhook", "callback", "DELETE", "error") == d0 + 1
    assert _calls("webhook", "callback", "GET", "blocked") == b0 + 1
    exposed = "\n".join(line for line in __import__("prometheus_client").generate_latest(REGISTRY).decode().splitlines()
                        if line.startswith("smo_outbound_"))
    assert "consumer.example" not in exposed and "localhost" not in exposed
