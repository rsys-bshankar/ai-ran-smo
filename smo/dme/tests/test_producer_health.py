"""The boundary of a producer's health callback in `GET /dme-types` (`typeStatus`): which statuses of the callback count as healthy.

`test_main.py` covers a callback that answers 200, one that answers 503 and one that cannot be reached; this file pins the line between them (`_producer_is_healthy`: below 300).
Fixtures (`client`) and `FakeHealthResponse` come from `test_main.py`. Run with `PYTHONPATH=.:../shared python -m pytest tests/test_producer_health.py -q`.
"""

import pytest

from test_main import FakeHealthResponse, client, register_type_body  # noqa: F401  (client: a pytest fixture)


# status of the producer's health callback -> typeStatus; the line is 2xx healthy, so 299 still counts and 300 does not
@pytest.mark.parametrize("status, expected", [(200, "ENABLED"), (204, "ENABLED"), (299, "ENABLED"), (300, "DISABLED"), (301, "DISABLED"), (404, "DISABLED"), (500, "DISABLED")])
def test_type_status_follows_the_class_of_the_health_status(client, monkeypatch, status, expected):
    """A producer's health callback answering below 300 makes the type ENABLED and 300 or above DISABLED: the boundary of `_producer_is_healthy`."""
    monkeypatch.setattr("app.main.httpx.get", lambda url, timeout=None: FakeHealthResponse(status))
    client.post("/production-capabilities", json=register_type_body())
    assert client.get("/dme-types").json()[0]["typeStatus"] == expected
