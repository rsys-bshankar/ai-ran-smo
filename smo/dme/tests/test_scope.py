"""PR-SEC-10.7 (docs/adr/0005-tenant-region-authorization.md section 9): the one DME read that names a managed element, the list of actions (and an action by id), is limited to the
elements inside the caller's scope claim. DME has no region or tenant of its own, so it asks RAN NF OAM which elements the claim covers (`GET /ran-nf-oam/managed-entities`, to which
`R1Client` passes the claim on); the double below answers with a fixed set, as RAN NF OAM would for that claim. The same path through the real modules is
`tests_integration/test_tenant_region_scope.py`. Run with: pytest tests/test_scope.py -q
"""

import json
import uuid

import pytest
from sqlalchemy.orm import Session

from smo_shared.scope import SCOPE_HEADER

from app.models import DmeActionRecord

from test_main import client  # noqa: F401  (pytest fixture)

EU = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client", SCOPE_HEADER: json.dumps({"regions": ["eu"]}, separators=(",", ":"))}
UNSCOPED = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
INTERNAL = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


class RanNfOam:
    """What RAN NF OAM answers `GET /managed-entities` with for the caller's claim: `visible`, in pages of the `limit` asked for."""

    def __init__(self):
        self.visible: list[str] = []
        self.calls: list[dict] = []
        self.status = 200

    def get(self, path, params=None, **kw):
        assert path == "/ran-nf-oam/managed-entities"
        self.calls.append(params)
        if self.status != 200:
            return FakeResponse({}, self.status)
        page = self.visible[params["offset"]: params["offset"] + params["limit"]]
        return FakeResponse({"items": [{"managedElementRef": r} for r in page], "total": len(self.visible), "limit": params["limit"], "offset": params["offset"]})


@pytest.fixture
def oam(monkeypatch):
    """Replaces the R1 client's GET with the `RanNfOam` double so a test chooses which elements the claim covers and can read back the calls made."""
    fake = RanNfOam()
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, params=None, **kw: fake.get(path, params=params, **kw))
    return fake


def _action(client, *elements, requested_by="es-rapp"):
    """Stores a DME action row naming the given elements (none: an action naming no element) and returns its id as a string."""
    with Session(client.app.state.test_engine) as db:
        row = DmeActionRecord(requested_by=requested_by, managed_element_ref=elements[0] if elements else "", changes=[{"managedElementRef": e} for e in elements] or [{}])
        db.add(row)
        db.commit()
        return str(row.action_id)


def _listed(client, headers, **q):
    return client.get("/actions", headers=headers, params=q).json()


def test_an_unscoped_caller_asks_nobody_and_sees_every_action(client, oam):
    """Without a scope claim DME lists and reads every action and never calls RAN NF OAM, so unscoped callers behave as before."""
    ids = {_action(client, "ME-1"), _action(client, "ME-3"), _action(client)}
    for headers in ({}, UNSCOPED, INTERNAL):
        assert {a["actionId"] for a in _listed(client, headers)["items"]} == ids
        assert client.get(f"/actions/{next(iter(ids))}", headers=headers).status_code == 200
    assert oam.calls == []                                           # no claim: nothing is asked, as before


def test_a_scoped_caller_sees_the_actions_whose_elements_are_all_inside_its_claim(client, oam):
    """A scoped caller lists only the actions whose every element is inside its claim, with a matching total, and asking for another's element gives an empty page."""
    oam.visible = ["ME-1", "ME-2"]
    inside, also = _action(client, "ME-1"), _action(client, "ME-2", "ME-1")
    _action(client, "ME-1", "ME-3")                                  # one element outside: the whole action is
    _action(client, "ME-3")
    _action(client)                                                  # names no element: has no region or tenant to be inside
    page = _listed(client, EU)
    assert {a["actionId"] for a in page["items"]} == {inside, also} and page["total"] == 2
    assert _listed(client, EU, managed_element_ref="ME-3")["items"] == []                       # naming another's element: an empty page
    assert _listed(client, EU, requested_by="someone-else")["items"] == []
    assert len(_listed(client, UNSCOPED)["items"]) == 5
    assert len(_listed(client, EU, limit=1)["items"]) == 1 and _listed(client, EU, limit=1)["total"] == 2
    assert [a["actionId"] for a in _listed(client, EU, limit=1, offset=1)["items"]] != [a["actionId"] for a in _listed(client, EU, limit=1)["items"]]


def test_the_elements_are_asked_for_in_pages_until_all_are_known(client, oam):
    """DME pages through RAN NF OAM's element list (500 at a time) until it knows them all, so an element beyond the first page is still recognised."""
    oam.visible = [f"E{i}" for i in range(1100)]
    last = _action(client, "E1099")
    assert [a["actionId"] for a in _listed(client, EU)["items"]] == [last]
    assert [c["offset"] for c in oam.calls] == [0, 500, 1000] and {c["limit"] for c in oam.calls} == {500}


def test_an_action_by_id_outside_the_claim_is_a_404_like_one_that_is_not_there(client, oam):
    """An action outside the caller's claim answers exactly like a missing one (404, same body), so a scoped caller cannot tell which action ids exist."""
    oam.visible = ["ME-1"]
    mine, theirs = _action(client, "ME-1"), _action(client, "ME-3")
    assert client.get(f"/actions/{mine}", headers=EU).status_code == 200
    same = {**EU, "X-Correlation-ID": "one-id"}                 # the problem body names the request's id (call flow 14a): the same for both
    hidden, absent = client.get(f"/actions/{theirs}", headers=same), client.get(f"/actions/{uuid.uuid4()}", headers=same)
    assert hidden.status_code == absent.status_code == 404 and hidden.json() == absent.json()
    assert client.get(f"/actions/{theirs}", headers=UNSCOPED).status_code == 200


def test_if_the_elements_cannot_be_learned_nothing_is_shown(client, oam):
    """When RAN NF OAM cannot say which elements the claim covers, a scoped list or read fails with 502 instead of showing actions unfiltered."""
    action = _action(client, "ME-1")
    oam.visible, oam.status = ["ME-1"], 503
    assert client.get("/actions", headers=EU).status_code == 502
    assert client.get(f"/actions/{action}", headers=EU).status_code == 502


def test_a_claim_that_could_not_be_passed_on_shows_nothing_rather_than_everything(client, oam):
    """No invoker id: the claim cannot travel with the call (it would be read as no claim, and RAN NF OAM would answer with every element)."""
    oam.visible = ["ME-1"]
    _action(client, "ME-1")
    anonymous = {"X-R1-Role": "rapp", SCOPE_HEADER: EU[SCOPE_HEADER]}
    assert _listed(client, anonymous)["items"] == []
    internal_with_a_claim = {**INTERNAL, SCOPE_HEADER: EU[SCOPE_HEADER]}                  # an SMO module on its own account that was given a claim: acts for nobody
    assert _listed(client, internal_with_a_claim)["items"] == []
    assert oam.calls == []
