"""GUI-5: a console user's region/tenant scope claim (app/scoping.py). The claim follows the rules of smo_shared/scope.py; an admin sets it (or the identity provider's
token does); every call the BFF makes for the user carries it beside the person (`X-R1-Acting-User-Scope`), and what the BFF computes for many users at once (the
summary counts, the event stream's pages, the search, a background export) is asked with it and never shared across claims.

R1 Termination and SME are test_main.py's FakeSmo, which records every proxied request. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_scope_claims.py -q`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from smo_shared import scope as shared_scope

from app import scoping
from app.db import GuiUser
from test_main import PASSWORDS, app, cfg, db, login, smo  # noqa: F401  (pytest fixtures and the sign-in helper)
from test_oidc import idp, make_app, make_cfg, sign_in, failure  # noqa: F401  (the fake identity provider and its helpers)
from test_exports_scope import sign_in as sign_in_for_export, wait as wait_for_export

EU = {"regions": ["eu-west"]}
EU_HEADER = '{"regions":["eu-west"]}'


def _scoped(db, username, claim):  # noqa: F811
    """Sets `username`'s stored claim straight in the table (as an admin's PATCH would)."""
    with db.session() as s:
        s.get(GuiUser, username).scope = scoping.encode(scoping.parse_claim(claim))
        s.commit()


@pytest.mark.parametrize("claim", [None, {}, EU, {"tenants": ["acme", "beta"]}, {"regions": ["b", "a"], "tenants": ["t"]}, "eu", ["eu"], {"regions": []},
                                   {"regions": "eu"}, {"cells": ["c"]}, {"regions": ["a b"]}, {"regions": ["eu", "eu"]}, {"regions": ["x"] * 101}, {"regions": [7]}])
# The BFF does not install smo_shared, so it repeats the rules; each claim must get the same answer from both (the same normalised claim, or a refusal from both).
def test_the_claim_rules_are_smo_shareds(claim):
    """`scoping.parse_claim` accepts and normalises exactly what `smo_shared.scope.from_claim` accepts, and refuses the rest."""
    try:
        expected = shared_scope.to_claim(shared_scope.from_claim(claim))
    except ValueError:
        with pytest.raises(ValueError):
            scoping.parse_claim(claim)
        return
    assert scoping.parse_claim(claim) == expected
    assert scoping.encode(scoping.parse_claim(claim)) == shared_scope.encode(shared_scope.from_claim(claim))


def test_an_admin_sets_and_clears_a_users_claim_and_a_bad_one_is_refused(app, db):  # noqa: F811
    """A create with `scope` stores it normalised; a PATCH replaces it, `null` removes it and leaving it out keeps it; the list and `/api/me` show it; a claim that
    breaks the rules is 422 INVALID_SCOPE and changes nothing."""
    admin = login(app, "admin")
    made = admin.post("/api/admin/users", json={"username": "kim", "password": "a-long-password", "role": "viewer", "scope": {"regions": ["eu-west", "ap"]}})
    assert made.status_code == 201 and made.json()["scope"] == {"regions": ["ap", "eu-west"]}
    assert admin.patch("/api/admin/users/kim", json={"role": "operator"}).json()["scope"] == {"regions": ["ap", "eu-west"]}
    assert admin.patch("/api/admin/users/kim", json={"scope": {"tenants": ["acme"]}}).json()["scope"] == {"tenants": ["acme"]}
    bad = admin.patch("/api/admin/users/kim", json={"scope": {"regions": []}})
    assert bad.status_code == 422 and bad.json()["title"] == "INVALID_SCOPE"
    assert admin.post("/api/admin/users", json={"username": "lee", "password": "a-long-password", "role": "viewer", "scope": {"zones": ["z"]}}).status_code == 422
    kim = TestClient(app)
    kim.post("/api/login", json={"username": "kim", "password": "a-long-password"})
    assert kim.get("/api/me").json()["scope"] == {"tenants": ["acme"]}
    assert admin.patch("/api/admin/users/kim", json={"scope": None}).json()["scope"] is None
    assert kim.get("/api/me").json()["scope"] is None                       # read from the table on every request: no new sign-in needed
    assert login(app, "operator").get("/api/me").json()["scope"] is None   # every existing user stays unscoped


def test_every_proxied_call_carries_the_persons_claim_and_the_browser_cannot_set_it(app, db, smo):  # noqa: F811
    """A scoped user's proxied call carries `X-R1-Acting-User-Scope` with the stored claim beside the person; an unscoped user's carries none; a value the browser sent
    is never forwarded."""
    _scoped(db, "viewer", EU)
    viewer = login(app, "viewer")
    viewer.get("/api/smo/ran-nf-oam/alarms", headers={"X-R1-Acting-User-Scope": '{"regions":["everywhere"]}'})
    sent = smo.proxied[-1].headers
    assert sent["x-r1-acting-user-scope"] == EU_HEADER and sent["x-r1-acting-user"] == "smo-gui:viewer"
    login(app, "operator").get("/api/smo/ran-nf-oam/alarms", headers={"X-R1-Acting-User-Scope": EU_HEADER})
    assert "x-r1-acting-user-scope" not in smo.proxied[-1].headers


def test_summary_counts_are_asked_and_cached_per_claim(app, db, smo):  # noqa: F811
    """Two users with different claims each get counts asked with their own claim (a shared person, never the other's claim), and the cache does not give one the
    other's answer; an unscoped user's counts are asked as before, with no person and no claim."""
    _scoped(db, "viewer", EU)
    _scoped(db, "operator", {"regions": ["us-east"]})
    login(app, "viewer").get("/api/summary/alarms")
    eu = [r for r in smo.proxied if r.url.path == "/ran-nf-oam/alarms"]
    assert eu and all(r.headers.get("x-r1-acting-user-scope") == EU_HEADER and r.headers.get("x-r1-acting-user") == scoping.SHARED_PERSON for r in eu)
    before = len(smo.proxied)
    login(app, "operator").get("/api/summary/alarms")
    us = [r for r in smo.proxied[before:] if r.url.path == "/ran-nf-oam/alarms"]
    assert us and all(r.headers.get("x-r1-acting-user-scope") == '{"regions":["us-east"]}' for r in us)     # not served from the EU answer
    before = len(smo.proxied)
    login(app, "admin").get("/api/summary/alarms")
    unscoped = [r for r in smo.proxied[before:] if r.url.path == "/ran-nf-oam/alarms"]
    assert unscoped and all("x-r1-acting-user-scope" not in r.headers and "x-r1-acting-user" not in r.headers for r in unscoped)


def test_the_search_is_asked_and_cached_per_claim(app, db, smo):  # noqa: F811
    """The typeahead's element search goes out with the asking user's claim, and the same text from a user with another claim is asked again."""
    _scoped(db, "viewer", EU)
    login(app, "viewer").get("/api/search", params={"q": "du-1"})
    asked = [r for r in smo.proxied if r.url.path == "/ran-nf-oam/managed-entities"]
    assert asked and asked[-1].headers.get("x-r1-acting-user-scope") == EU_HEADER
    login(app, "operator").get("/api/search", params={"q": "du-1"})
    again = [r for r in smo.proxied if r.url.path == "/ran-nf-oam/managed-entities"]
    assert len(again) == len(asked) + 1 and "x-r1-acting-user-scope" not in again[-1].headers


def test_a_users_event_topics_are_their_own_and_the_claim_stays_on_the_server(app, db):  # noqa: F811
    """The hub keys a scoped user's topic with the claim, so two claims never share a computation, and what the browser is told names the topic it asked for."""
    from app.events import Topic, _Subscriber, parse_topic
    from dataclasses import replace
    topic = replace(parse_topic("summary:nav@eu-west"), claim=EU_HEADER)
    assert topic.hub_key == f"nav@eu-west|{EU_HEADER}" and replace(topic, claim=None).hub_key == "nav@eu-west"
    sub = _Subscriber("viewer", (topic.hub_key,))
    sub.offer({"page": "nav", "counts": {"alarms.critical": 1}, "scope": {"region": "eu-west"}}, topic.hub_key)
    [event] = sub.drain()
    assert event["topic"] == "summary:nav@eu-west" and EU_HEADER not in json.dumps(event)
    assert isinstance(Topic("nav", "nav"), Topic)


def test_an_export_reads_with_the_askers_claim(app, db, smo):  # noqa: F811
    """An alarm export written in the background asks every page as the person who asked, with their claim."""
    _scoped(db, "operator", EU)
    smo.next_response = None
    with TestClient(app) as operator:  # the event loop lives for the whole test, so the export task runs to its end
        sign_in_for_export(operator, "operator")
        made = operator.post("/api/exports", json={"kind": "alarms", "since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z"})
        assert made.status_code in (201, 202), made.text
        wait_for_export(operator, made.json()["id"])
    pages = [r for r in smo.proxied if r.url.path == "/ran-nf-oam/alarms"]
    assert pages and pages[0].headers.get("x-r1-acting-user") == "smo-gui:operator" and pages[0].headers.get("x-r1-acting-user-scope") == EU_HEADER


def test_the_identity_providers_claim_scopes_the_user_and_a_bad_one_refuses_the_sign_in(idp, db):  # noqa: F811
    """With `GUI_OIDC_SCOPE_CLAIM` set, the token's claim is the user's scope at every sign-in (none: unscoped); a claim that breaks the rules refuses the sign-in
    `invalid_scope`. Without the setting, an admin's claim is kept."""
    client = TestClient(make_app(idp, db, make_cfg(oidc_scope_claim="smo_scope")), follow_redirects=False)
    assert sign_in(client, idp, smo_scope=EU).status_code == 303
    assert client.get("/api/me").json()["scope"] == EU
    sign_in(client, idp)
    assert client.get("/api/me").json()["scope"] is None
    assert failure(sign_in(client, idp, smo_scope={"regions": []})) == "invalid_scope"
    with db.session() as s:
        s.get(GuiUser, "oidc:user-1").scope = EU_HEADER
        s.commit()
    plain = TestClient(make_app(idp, db), follow_redirects=False)
    sign_in(plain, idp, smo_scope={"regions": ["us-east"]})
    assert plain.get("/api/me").json()["scope"] == EU                     # no scope claim configured: the admin's claim stays


def test_a_stored_claim_that_no_longer_reads_permits_nothing(app, db, smo):  # noqa: F811
    """A damaged stored claim is shown as INVALID and still forwarded as it is, which the modules read as permitting nothing."""
    with db.session() as s:
        s.get(GuiUser, "viewer").scope = "not json"
        s.commit()
    viewer = login(app, "viewer")
    assert viewer.get("/api/me").json()["scope"] == "INVALID"
    viewer.get("/api/smo/ran-nf-oam/alarms")
    assert smo.proxied[-1].headers["x-r1-acting-user-scope"] == "not json"
    assert shared_scope.decode("not json") == shared_scope.DENY_ALL
    assert PASSWORDS["viewer"]
