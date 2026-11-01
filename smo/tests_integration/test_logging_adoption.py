"""Every service logs structured JSON with one access line per request, and its probes stay out of the INFO log (PR-OBS-1.6)."""

import json
import logging

from fastapi.testclient import TestClient

from smo_shared.logconfig import AccessLogMiddleware


def test_every_loaded_service_has_the_access_log_middleware(loaded_apps):
    """Every service has the access-log middleware."""
    missing = [name for name, main in loaded_apps.items()
               if not any(m.cls is AccessLogMiddleware for m in main.app.user_middleware)]
    assert missing == [], f"services without structured logging: {missing}"


def test_a_request_to_any_service_logs_one_json_access_line_with_a_route_template_and_no_query(loaded_apps, caplog):
    """A request to a service logs exactly one access record at DEBUG for a probe, with the method, the route template and the status, and without
    the query string.
    """
    caplog.set_level(logging.DEBUG, logger="smo.access")
    for name in ("r1-termination", "sme", "nfo", "aimgf", "mock-o1-adaptor"):
        caplog.clear()
        TestClient(loaded_apps[name].app).get("/live?token=do-not-log-me")
        records = [r for r in caplog.records if r.name == "smo.access"]
        assert len(records) == 1, name
        record = records[0]
        assert (record.method, record.route, record.status) == ("GET", "/live", 200), name
        assert record.levelno == logging.DEBUG                    # a probe: hidden at the default INFO level
        assert "do-not-log-me" not in json.dumps({k: str(v) for k, v in vars(record).items()}), name
