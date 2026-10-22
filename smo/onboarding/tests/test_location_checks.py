"""The two checks `POST /packages` makes on a location before it fetches anything: the `.csar` name and the SSRF guard (`app/main.py`, `_validate_package`).

The validation module repeats the name check on the bytes, so a package at a wrong name fails either way; what only these tests show is that nothing is fetched first, and what the
failure reason says. Fixtures (`client`) come from `test_main.py`. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_location_checks.py -q`.
"""

import pytest

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)


@pytest.fixture
def fetched(monkeypatch):
    """Replaces the package fetch with a recorder that fails the test's expectations by being called; returns the list of fetched locations."""
    calls = []

    def fake_get(location, timeout=None, **kw):
        calls.append(location)
        raise AssertionError("nothing may be fetched for this location")
    monkeypatch.setattr("app.main.httpx.get", fake_get)
    return calls


def test_a_location_without_the_csar_name_is_refused_before_any_fetch(client, fetched):
    """A location that does not end in `.csar` ends the package FAILED with a reason naming the location, and the fetch is never made."""
    answer = client.post("/packages", json={"location": "http://example.test/pkg.zip"}).json()
    assert fetched == []
    assert answer["failureReason"] == "package location 'http://example.test/pkg.zip' does not end with .csar"


@pytest.mark.parametrize("location", ["http://127.0.0.1:8000/pkg.csar", "http://169.254.169.254/pkg.csar", "file:///etc/pkg.csar"])
def test_an_address_the_guard_refuses_is_not_fetched(client, fetched, location):
    """A `.csar` location that is a loopback or link-local address, or not http(s), is refused before the fetch with a reason that says the destination is not allowed."""
    answer = client.post("/packages", json={"location": location}).json()
    assert fetched == []
    assert answer["failureReason"] == f"package location {location!r} is not an allowed http(s) destination"
