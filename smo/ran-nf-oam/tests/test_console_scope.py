"""GUI-5: the operator console's call for a person with a scope claim (`X-R1-Acting-User` and `X-R1-Acting-User-Scope` from an `internal` caller, smo_shared/scope.py
`scope_of`) is narrowed exactly as a scoped rApp's: lists hold only that person's elements, a write outside them is refused. Run:
`PYTHONPATH=.:../shared python -m pytest tests/test_console_scope.py -q`."""

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_scope import PLACES, claim, places  # noqa: F401  (the fleet in its regions and tenants)
from test_waves import fleet  # noqa: F401

CONSOLE = {"X-R1-Invoker-Id": "gui-client", "X-R1-Role": "internal", "X-R1-Acting-User": "smo-gui:ana"}


def _region_of(region):
    """The fleet elements PLACES puts in `region`."""
    return sorted(ref for ref, (r, _) in PLACES.items() if r == region)


def test_a_person_with_a_claim_sees_only_their_elements_and_one_without_sees_all(client, places):
    """The managed elements of a person limited to one region are that region's; the console acting for an unscoped person sees every element, as before."""
    region = next(r for r, _ in PLACES.values() if r)
    scoped = {**CONSOLE, "X-R1-Acting-User-Scope": claim(regions=[region])}
    seen = sorted(e["managedElementRef"] for e in client.get("/managed-entities", headers=scoped).json()["items"])
    assert seen == _region_of(region)
    assert len(client.get("/managed-entities", headers=CONSOLE).json()["items"]) == len(PLACES)


def test_a_person_cannot_write_outside_their_claim(client, places):
    """A config job the console sends for a person limited to one region, naming an element of another, is refused SCOPE_DENIED and nothing is written."""
    region = next(r for r, _ in PLACES.values() if r)
    outside = next(ref for ref, (r, _) in PLACES.items() if r != region)
    resp = client.post("/config-jobs", headers={**CONSOLE, "X-R1-Acting-User-Scope": claim(regions=[region])},
                       json={"requestedBy": "smo-gui:ana", "scope": "cell", "changes": [{"managedElementRef": outside, "attributeChanges": {"txPower": 20}}]})
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "SCOPE_DENIED" and places["edits"] == []
