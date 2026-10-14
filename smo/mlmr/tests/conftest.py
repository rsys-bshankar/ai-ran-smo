"""Pytest configuration of the mlmr unit tests: switches on the in-memory SQLite fallback of `smo_shared.db` before the application is imported, and
stands in for AIMgF's lifecycle read.

Without a database URL `smo_shared.db` refuses to start unless `SMO_ALLOW_SQLITE_FALLBACK` is set; this file sets it for the whole suite. The autouse fixture
`aimgf_runtimes` answers the lifecycle read that a model delete makes (call flow 26e): a model whose id a test puts in the dict answers that runtime state, any
other 404 (AIMgF does not know it). A test that replaces `R1Client.get` itself (the `nrm-refs` tests) overrides it.
Run the suite from `smo/mlmr` with `PYTHONPATH=.:../shared python -m pytest tests -q`.
"""

import os

import pytest

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")


class _Answer:
    """A minimal HTTP answer: `status_code` and `json()`."""

    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code, self._body = status_code, body

    def json(self):
        """The body."""
        return self._body


@pytest.fixture(autouse=True)
def aimgf_runtimes(monkeypatch) -> dict:
    """AIMgF's runtime state per model id (str), as `GET /aimgf/models/{id}/lifecycle` answers it; a model not in the dict is 404 at AIMgF."""
    states: dict[str, str] = {}

    def fake_get(self, path, **kw):
        """Answers the lifecycle read from `states`; any other path is 404."""
        if path.startswith("/aimgf/models/") and path.endswith("/lifecycle"):
            model_id = path.split("/")[3]
            if model_id in states:
                return _Answer(200, {"modelId": model_id, "runtimeLifecycleState": states[model_id]})
        return _Answer(404)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    return states
