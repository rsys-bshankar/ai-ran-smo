"""Tests of MLLF's placeholder (`app/main.py`): it answers its health probes and serves no route of the module it was (S2.1, S2.2; removed by S2.8).

Run: `cd smo/mllf && PYTHONPATH=.:../shared python -m pytest tests -q`. No database, no network.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_the_placeholder_answers_its_health_probes():
    """`/live`, `/ready` and `/health` answer 200, so the compose and Helm health checks of the service still pass."""
    for path in ("/live", "/ready", "/health"):
        assert client.get(path).status_code == 200, path


def test_the_placeholder_serves_no_route_of_its_own():
    """Only the probes, `/metrics` and FastAPI's own documents are routes: the module's routes are served by AIMgF."""
    paths = {route.path for route in app.routes}
    assert paths <= {"/live", "/ready", "/health", "/metrics", "/version", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}, paths
    assert client.get("/models").status_code == 404
