"""Fixtures of the SDK unit tests: a recording fake of `R1Client`.

The `r1` fixture records every call the code under test makes (verb, path, params, json, files) and answers from a scripted response, the same "replace the R1 client" shape other modules'
tests use for their service doubles. Every recorded call is also checked against the gateway's role policy: the SDK must not make a change an rApp is not allowed to make
(`smo_shared.roles.rapp_may_change`, PR-SEC-14). Needs `smo_shared` on the path (`PYTHONPATH=.:../shared`); no network and no database.
"""

import os

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")

import pytest

from smo_shared import roles


class FakeResponse:
    """A scripted response with the attributes `ensure_ok` reads: `status_code`, `json()`, `content` (b"{}" when a payload is given and no content) and `text`."""
    def __init__(self, status_code=200, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content or (b"{}" if payload is not None else b"")
        self.text = text

    def json(self):
        return self._payload


class RecordingR1Client:
    """A fake `R1Client`: `get`, `post`, `put`, `patch` and `delete` record the call in `calls` and return the response set by `script()`, `200` with `{}` until then."""
    def __init__(self):
        self.calls = []
        self._next_response = FakeResponse(200, {})

    def script(self, status_code=200, payload=None, content=b"", text=""):
        """Set the response every following call returns (until the next `script`)."""
        self._next_response = FakeResponse(status_code, payload, content, text)

    def _record(self, verb, path, **kwargs):
        """Check that an rApp may make this change, append the call to `calls` and return the scripted response; the assertion fails the test when the SDK calls a route the gateway refuses an rApp."""
        # PR-SEC-14: every change the SDK makes must be one the gateway lets an rApp make (roles.RAPP_MAY_CHANGE)
        # The gateway's role policy is keyed by the first path segment (the module prefix) and the rest; reads are not restricted, so only the changing verbs are asserted by `rapp_may_change`.
        prefix, _, rest = "/" + path.lstrip("/").partition("/")[0], "/", path.lstrip("/").partition("/")[2]
        assert roles.rapp_may_change(prefix, verb, rest), f"the SDK calls {verb.upper()} {path}, which the gateway refuses an rApp"
        self.calls.append({"verb": verb, "path": path, **kwargs})
        return self._next_response

    def get(self, path, params=None, **kw):
        return self._record("get", path, params=params)

    def post(self, path, json=None, params=None, files=None, headers=None, **kw):
        """Record a POST; the headers a call adds beyond the `Idempotency-Key` the retry wrapper puts on every POST are recorded, and only when there are some, so the exact-dict assertions of the other tests are unchanged."""
        extra = {k: v for k, v in (headers or {}).items() if k != "Idempotency-Key"}
        return self._record("post", path, json=json, params=params, files=files, **({"headers": extra} if extra else {}))

    def put(self, path, json=None, params=None, **kw):
        return self._record("put", path, json=json, params=params)

    def patch(self, path, json=None, params=None, **kw):
        return self._record("patch", path, json=json, params=params)

    def delete(self, path, params=None, **kw):
        return self._record("delete", path, params=params)


@pytest.fixture
def r1():
    """A fresh `RecordingR1Client` for each test; the tests read `r1.calls`."""
    return RecordingR1Client()
