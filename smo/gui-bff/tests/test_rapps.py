"""The rApp directory, one rApp's declared page, the proxy for its declared routes and the pins (app/rapps.py, PR-GUI-8, GUI-8.3 and GUI-8.5).

R1 Termination, rApp Management and Onboarding are faked with one MockTransport (FakeSmo of test_main.py plus the routes below), so the BFF's real gateway
client runs unmodified."""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app, seed_users
from app.smo_client import R1Gateway
from test_main import PASSWORDS, R1, FakeSmo, audit_rows, login

EXAMPLE = json.loads(Path(__file__).with_name("operator_ui_example.json").read_text())
P_ES, P_PLAIN = str(uuid.uuid4()), str(uuid.uuid4())
I_RUN, I_FAULT, I_GONE, I_NOPAGE = (str(uuid.uuid4()) for _ in range(4))


class FakeRapps(FakeSmo):
    """FakeSmo plus rApp Management, Onboarding and the gateway's `/rapps/{id}/operator/...` prefix."""

    def __init__(self):
        super().__init__()
        self.packages = [
            {"packageId": P_ES, "name": "Energy Saving", "version": "1.0.0", "vendor": "Acme", "applicationType": "rApp", "state": "AVAILABLE",
             "aiCapabilities": {"operatorUi": EXAMPLE, "executionModes": ["batch"]}},
            {"packageId": P_PLAIN, "name": "Plain", "version": "2.0.0", "vendor": "Beta Networks", "applicationType": "rApp", "state": "AVAILABLE", "aiCapabilities": None},
        ]
        self.instances = [
            {"instanceId": I_RUN, "packageId": P_ES, "state": "RUNNING", "autonomyMode": "ASSIST", "operatorApiBase": "http://es:8000"},
            {"instanceId": I_FAULT, "packageId": P_PLAIN, "state": "FAULTED", "autonomyMode": "SHADOW", "operatorApiBase": None},
            {"instanceId": I_GONE, "packageId": P_ES, "state": "UNDEPLOYED", "autonomyMode": "SHADOW", "operatorApiBase": None},
            {"instanceId": I_NOPAGE, "packageId": P_ES, "state": "RUNNING", "autonomyMode": "SHADOW", "operatorApiBase": None},
        ]
        self.operator_calls: list[httpx.Request] = []
        self.operator_answer = httpx.Response(200, json={"cells": [], "done": True})
        self.rapp_mgmt_down = False
        self.operator_error: Exception | None = None
        self.status_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(("/rapp-mgmt/", "/onboarding/", "/rapps/")):
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.issued:
                return httpx.Response(401, json={"title": "UNAUTHORIZED"})
            return self.rapps(request, path)
        return super().handler(request)

    def rapps(self, request, path):
        if path.startswith("/rapp-mgmt/") and self.rapp_mgmt_down:
            raise httpx.ConnectError("down")
        if path == "/rapp-mgmt/instances":
            return httpx.Response(200, json={"items": self.instances, "limit": 500, "offset": 0, "hasMore": False})
        if path.startswith("/rapp-mgmt/instances/"):
            found = [i for i in self.instances if i["instanceId"] == path.rsplit("/", 1)[1]]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={"detail": {"title": "RAPP_INSTANCE_NOT_FOUND"}})
        if path == "/onboarding/packages":
            return httpx.Response(200, json={"items": self.packages, "limit": 500, "offset": 0, "hasMore": False})
        if path.startswith("/onboarding/packages/") and path.endswith("/onboarding-status"):
            self.status_calls += 1
            found = [p for p in self.packages if p["packageId"] == path.split("/")[3]]
            return httpx.Response(200, json={"packageId": found[0]["packageId"], "state": "AVAILABLE", "aiCapabilities": found[0]["aiCapabilities"]}) if found \
                else httpx.Response(404, json={"detail": "no"})
        self.operator_calls.append(request)
        if self.operator_error:
            raise self.operator_error
        return self.operator_answer


@pytest.fixture
def smo():
    return FakeRapps()


@pytest.fixture
def app(smo):
    """The BFF on an in-memory database with the real gateway talking to `FakeRapps`; the database is kept as `app.state.test_db` so tests can read the audit log and pins.
    """
    cfg = Settings(r1_url=R1, jwt_secret="test-secret", cookie_secure=False, admin_password=PASSWORDS["admin"],
                   operator_password=PASSWORDS["operator"], viewer_password=PASSWORDS["viewer"])
    db = Database("sqlite://")
    seed_users(db, cfg)
    application = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))
    application.state.test_db = db
    return application


def op_url(instance, route):
    return f"/api/rapps/{instance}/operator{route}"


# ------------------------------------------------------------------ the directory

def test_the_directory_needs_a_session(app):
    """The directory is 401 without a session."""
    assert TestClient(app).get("/api/rapps").status_code == 401


def test_the_directory_lists_every_instance_with_its_package_and_never_its_address(app):
    """The directory joins each instance with its package's name, version and vendor, says whether an operator API is registered but never shows its address or the declaration, and lists the owners and states present.
    """
    body = login(app, "viewer").get("/api/rapps").json()
    assert body["total"] == 4 and body["limit"] == 50 and body["offset"] == 0
    by_id = {r["instanceId"]: r for r in body["items"]}
    assert by_id[I_RUN] == {"instanceId": I_RUN, "packageId": P_ES, "name": "Energy Saving", "version": "1.0.0", "vendor": "Acme", "state": "RUNNING",
                            "autonomyMode": "ASSIST", "hasPage": True, "operatorApiRegistered": True, "pinned": False}
    assert by_id[I_FAULT]["hasPage"] is False and by_id[I_FAULT]["operatorApiRegistered"] is False
    assert "es:8000" not in json.dumps(body) and "operatorApiBase" not in json.dumps(body) and "declaration" not in body["items"][0]
    assert body["owners"] == ["Acme", "Beta Networks"] and body["states"] == ["FAULTED", "RUNNING", "UNDEPLOYED"]


def test_search_filters_by_name_version_owner_and_ids_without_regard_to_case(app):
    """`search` matches name, version, owner, instance id and package id case-insensitively, and a blank search matches everything.
    """
    c = login(app, "viewer")
    names = lambda **q: sorted(r["name"] + ":" + r["state"] for r in c.get("/api/rapps", params=q).json()["items"])   # noqa: E731
    assert names(search="energy") == ["Energy Saving:RUNNING"] * 2 + ["Energy Saving:UNDEPLOYED"]
    assert names(search="BETA") == ["Plain:FAULTED"]
    assert names(search="2.0.0") == ["Plain:FAULTED"]
    assert names(search=I_FAULT[:8]) == ["Plain:FAULTED"]
    assert names(search=P_ES[:8]) == names(search="energy")
    assert names(search="no such") == []
    assert names(search="  ") and len(names(search="  ")) == 4


def test_state_owner_has_page_and_pinned_filters_combine(app):
    """The state, owner, `hasPage` and `pinned` filters work alone and together, ignoring case and surrounding spaces in text."""
    c = login(app, "viewer")
    get = lambda **q: c.get("/api/rapps", params=q).json()   # noqa: E731
    assert [r["instanceId"] for r in get(state="faulted")["items"]] == [I_FAULT]
    assert get(owner="acme")["total"] == 3 and get(owner="Acme", state="RUNNING")["total"] == 2
    assert get(owner="Acme ")["total"] == 3
    assert get(hasPage="false")["total"] == 1 and get(hasPage="true")["total"] == 3
    c.put(f"/api/me/pins/{I_RUN}")
    assert [r["instanceId"] for r in get(pinned="true")["items"]] == [I_RUN] and get(pinned="false")["total"] == 3


def test_the_directory_pages_in_a_stable_order(app):
    """Pages follow a fixed order (name, version, id), so consecutive pages neither repeat nor miss an instance, and a limit outside 1-500 is 422.
    """
    c = login(app, "viewer")
    first = c.get("/api/rapps", params={"limit": 2}).json()
    second = c.get("/api/rapps", params={"limit": 2, "offset": 2}).json()
    ids = [r["instanceId"] for r in first["items"] + second["items"]]
    assert len(set(ids)) == 4 and first["total"] == 4 and len(second["items"]) == 2
    assert first["items"][0]["name"] == "Energy Saving" and ids[-1] == I_FAULT                  # Energy Saving x3, then Plain
    assert c.get("/api/rapps", params={"limit": 0}).status_code == 422 and c.get("/api/rapps", params={"limit": 501}).status_code == 422


def test_the_directory_says_so_when_the_smo_cannot_be_asked(app, smo):
    """When rApp Management is unreachable the directory is 502 R1_UNREACHABLE rather than an empty list."""
    smo.rapp_mgmt_down = True
    resp = login(app, "viewer").get("/api/rapps")
    assert resp.status_code == 502 and resp.json()["title"] == "R1_UNREACHABLE"


def test_pages_beyond_the_first_are_read(app, smo):
    """The directory keeps reading pages of 500 while the SMO says there are more."""
    many = [{"instanceId": str(uuid.uuid4()), "packageId": P_PLAIN, "state": "RUNNING", "autonomyMode": "SHADOW", "operatorApiBase": None} for _ in range(3)]
    calls = []
    original = smo.rapps

    def paged(request, path):
        if path == "/rapp-mgmt/instances":
            offset = int(request.url.params["offset"])
            calls.append(offset)
            return httpx.Response(200, json={"items": many[offset // 500:offset // 500 + 1], "limit": 500, "offset": offset, "hasMore": offset // 500 < 2})
        return original(request, path)

    smo.rapps = paged
    assert login(app, "viewer").get("/api/rapps").json()["total"] == 3 and calls == [0, 500, 1000]


def test_a_rapp_onboarded_after_the_index_was_read_shows_with_its_name_at_once(app, smo, monkeypatch):
    """A package missing from the cached index is read again (once the refresh interval has passed), so a rApp onboarded at run time shows with its name instead of a blank.
    """
    from app import rapps
    c = login(app, "viewer")
    assert c.get("/api/rapps").json()["total"] == 4                                    # the package index is now cached
    new_package = {"packageId": str(uuid.uuid4()), "name": "Fresh", "version": "9.0.0", "vendor": "Gamma", "applicationType": "rApp", "state": "AVAILABLE",
                   "aiCapabilities": {"operatorUi": EXAMPLE}}
    smo.packages.append(new_package)
    smo.instances.append({"instanceId": str(uuid.uuid4()), "packageId": new_package["packageId"], "state": "RUNNING", "autonomyMode": "SHADOW", "operatorApiBase": "http://x:8000"})
    named = lambda: sorted(r["name"] for r in c.get("/api/rapps").json()["items"] if r["version"] == "9.0.0" or r["name"] is None)   # noqa: E731
    assert named() == [None]                                                          # inside the refresh window the cached index is used, so no name yet
    monkeypatch.setattr(rapps, "PACKAGE_INDEX_REFRESH_SECONDS", 0.0)
    assert named() == ["Fresh"]                                                       # a package the index does not hold is read again
    hit = c.get("/api/rapps", params={"search": "fresh"}).json()
    assert hit["total"] == 1 and hit["items"][0]["hasPage"] is True and hit["items"][0]["vendor"] == "Gamma"


# ------------------------------------------------------------------ one rApp

def test_one_rapp_carries_its_declaration_and_what_the_user_may_do(app):
    """One rApp returns its declaration and `canChange`, which is true for operator and admin and false for a viewer."""
    viewer = login(app, "viewer").get(f"/api/rapps/{I_RUN}").json()
    assert viewer["declarationState"] == "declared" and viewer["declaration"] == EXAMPLE and viewer["readOnly"] is False
    assert viewer["canChange"] is False and viewer["name"] == "Energy Saving" and viewer["operatorApiRegistered"] is True
    assert login(app, "operator").get(f"/api/rapps/{I_RUN}").json()["canChange"] is True
    assert login(app, "admin").get(f"/api/rapps/{I_RUN}").json()["canChange"] is True


def test_a_rapp_without_a_declaration_has_a_generic_page_only(app):
    """A package with no operator page has `declarationState` none, no declaration and no change right."""
    body = login(app, "operator").get(f"/api/rapps/{I_FAULT}").json()
    assert body["declarationState"] == "none" and body["declaration"] is None and body["canChange"] is False and body["hasPage"] is False


def test_a_read_only_declaration_cannot_change(app, smo):
    """A read-only declaration reports `readOnly` and `canChange` false even for an admin."""
    smo.packages[0]["aiCapabilities"] = {"operatorUi": {"version": 1, "readOnly": True, "panels": [EXAMPLE["panels"][0]]}}
    body = login(app, "admin").get(f"/api/rapps/{I_RUN}").json()
    assert body["readOnly"] is True and body["canChange"] is False


# Each row is a stored `operatorUi` value that is not the validated shape; the rApp must be reported as `unreadable` with no declaration and no change right.
@pytest.mark.parametrize("stored", [{"operatorUi": "text"}, {"operatorUi": {"panels": "no"}}, {"operatorUi": {"panels": [1]}}, {"operatorUi": None}])
def test_a_stored_declaration_that_is_not_readable_is_reported_and_gives_no_page(app, smo, stored):
    smo.packages[0]["aiCapabilities"] = stored
    body = login(app, "operator").get(f"/api/rapps/{I_RUN}").json()
    assert body["declarationState"] == "unreadable" and body["declaration"] is None and body["canChange"] is False


def test_an_unknown_or_malformed_instance_is_404(app):
    """An unknown instance id, or a value that is not a UUID, is 404 NO_SUCH_RAPP."""
    c = login(app, "viewer")
    assert c.get(f"/api/rapps/{uuid.uuid4()}").status_code == 404
    assert c.get("/api/rapps/not-a-uuid").json()["title"] == "NO_SUCH_RAPP"


def test_the_declaration_is_cached_briefly(app, smo):
    """Repeated page and proxy calls for one package read its onboarding status from the SMO only once."""
    c = login(app, "viewer")
    for _ in range(3):
        c.get(f"/api/rapps/{I_RUN}")
        c.get(op_url(I_RUN, f"/instances/{I_RUN}/dashboard"))
    assert smo.status_calls == 1


# ------------------------------------------------------------------ the proxy

def test_a_declared_read_goes_through_the_gateway_with_only_the_declared_query(app, smo):
    """A declared read reaches the gateway's `/rapps/<id>/operator/...` path with the BFF's token, no cookie and only the declared query, with the fixed value replacing the browser's.
    """
    resp = login(app, "viewer").get(op_url(I_RUN, f"/instances/{I_RUN}/dashboard"), params={"points": "1", "x": "y"})
    assert resp.status_code == 200 and resp.json() == {"cells": [], "done": True}
    sent = smo.operator_calls[0]
    assert sent.method == "GET" and sent.url.path == f"/rapps/{I_RUN}/operator/instances/{I_RUN}/dashboard"
    assert dict(sent.url.params) == {"points": "48"}
    assert sent.headers["authorization"].startswith("Bearer tok-") and "cookie" not in sent.headers


def test_a_viewer_cannot_press_a_change_button_and_nothing_is_sent(app, smo):
    """A viewer's change calls are 403 FORBIDDEN and nothing reaches the gateway."""
    c = login(app, "viewer")
    resp = c.post(op_url(I_RUN, f"/instances/{I_RUN}/evaluate"))
    assert resp.status_code == 403 and resp.json()["title"] == "FORBIDDEN"
    assert c.delete(op_url(I_RUN, f"/instances/{I_RUN}/cells/C1/override")).status_code == 403
    assert smo.operator_calls == []


def test_an_undeclared_route_is_refused_for_every_role_and_audited(app, smo):
    """Undeclared routes, an instance that is not the open page and an encoded traversal are 403 UNDECLARED_ROUTE for every role, never sent, and each refusal is audited as DENIED.
    """
    for who in ("viewer", "operator", "admin"):
        c = login(app, who)
        for method, route in (("POST", f"/instances/{I_RUN}/lifecycle/train"), ("GET", f"/instances/{I_RUN}/lifecycle/train"), ("POST", f"/instances/{I_RUN}/start"),
                              ("GET", "/sim-producer/publish"), ("POST", f"/instances/{I_FAULT}/evaluate"), ("GET", f"/instances/{I_RUN}/cells/%2e%2e/dashboard")):
            resp = c.request(method, op_url(I_RUN, route))
            assert resp.status_code == 403 and resp.json()["title"] == "UNDECLARED_ROUTE", (who, method, route)
    assert smo.operator_calls == []
    assert len(audit_rows(app.state.test_db, "DENIED")) == 18


def test_a_declaration_the_rapp_does_not_have_means_no_route_at_all(app, smo):
    """A rApp with no declaration has no callable operator route, whatever the role."""
    resp = login(app, "admin").get(op_url(I_FAULT, f"/instances/{I_FAULT}/dashboard"))
    assert resp.status_code == 403 and resp.json()["title"] == "UNDECLARED_ROUTE" and smo.operator_calls == []


def test_a_change_by_an_operator_is_audited_before_and_after_with_the_session_user_filled_in(app, smo):
    """A change sends only the declared inputs and fixed values with `{user}` filled from the session, and writes a `requested` audit row before the call and a `done` row with the status after it.
    """
    resp = login(app, "operator").post(op_url(I_RUN, f"/instances/{I_RUN}/cells/C1/override"), json={"operator": "mallory", "reason": "evil", "x": 1})
    assert resp.status_code == 200
    sent = smo.operator_calls[0]
    assert sent.method == "POST" and sent.url.path == f"/rapps/{I_RUN}/operator/instances/{I_RUN}/cells/C1/override"
    assert json.loads(sent.content) == {"operator": "operator", "reason": "manual override"}
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-r1-acting-user"] == "smo-gui:operator"                              # SEC-15.8: the person behind the BFF's token
    rows = audit_rows(app.state.test_db, "RAPP_ACTION")
    assert [(r.username, r.role, r.status_code, r.detail.split(" phase=")[1]) for r in rows] == [("operator", "operator", None, "requested"), ("operator", "operator", 200, "done")]
    assert rows[0].path == f"/rapps/{I_RUN}/operator/instances/{I_RUN}/cells/C1/override" and "action=unlock-cell" in rows[0].detail and I_RUN in rows[0].detail


def test_the_outcome_of_a_refused_or_failed_change_is_audited_too(app, smo):
    """When the rApp refuses the change, its answer is relayed unchanged and the status appears in the second audit row."""
    smo.operator_answer = httpx.Response(409, json={"detail": "busy"})
    resp = login(app, "operator").post(op_url(I_RUN, f"/instances/{I_RUN}/evaluate"))
    assert resp.status_code == 409 and resp.json() == {"detail": "busy"}                       # the rApp's own answer, relayed
    assert [r.status_code for r in audit_rows(app.state.test_db, "RAPP_ACTION")] == [None, 409]


def test_an_unreachable_gateway_still_leaves_both_audit_entries_and_no_exception_text(app, smo):
    """If the gateway cannot be reached the answer is 502 R1_UNREACHABLE without the exception text, and both audit rows are still written.
    """
    smo.operator_error = httpx.ConnectError("secret internal detail")
    resp = login(app, "operator").post(op_url(I_RUN, f"/instances/{I_RUN}/evaluate"))
    assert resp.status_code == 502 and resp.json()["title"] == "R1_UNREACHABLE" and "secret" not in resp.text
    rows = audit_rows(app.state.test_db, "RAPP_ACTION")
    assert [r.status_code for r in rows] == [None, 502] and "R1_UNREACHABLE" in rows[1].detail and "secret" not in rows[1].detail


def test_the_gateways_not_registered_answer_is_passed_on_for_the_page_to_explain(app, smo):
    """The gateway's OPERATOR_API_NOT_REGISTERED answer is passed on so the page can explain that the rApp has no operator API.
    """
    smo.operator_answer = httpx.Response(404, json={"title": "OPERATOR_API_NOT_REGISTERED", "status": 404})
    resp = login(app, "viewer").get(op_url(I_RUN, f"/instances/{I_RUN}/dashboard"))
    assert resp.status_code == 404 and resp.json()["title"] == "OPERATOR_API_NOT_REGISTERED"


def test_a_body_is_checked_against_the_action_and_a_bad_one_is_never_sent(app, smo):
    """A body that breaks the declared inputs (range, missing, not JSON, not an object, over the size limit) is refused with 422, 400 or 413 and never forwarded; a good one is forwarded with only the declared fields.
    """
    smo.packages[0]["aiCapabilities"] = {"operatorUi": {"version": 1, "panels": [{
        "id": "a", "title": "A", "kind": "actions", "actions": [{"id": "set", "label": "Set", "method": "POST", "path": "/instances/{instanceId}/set", "success": "ok",
                                                                    "inputs": [{"name": "level", "label": "L", "type": "integer", "min": 1, "max": 5, "required": True}],
                                                                    "body": {"by": "{user}"}}]}]}}
    c = login(app, "operator")
    url = op_url(I_RUN, f"/instances/{I_RUN}/set")
    assert c.post(url, json={"level": 9}).status_code == 422 and c.post(url, json={}).status_code == 422 and c.post(url, content=b"{not json").status_code == 400
    assert c.post(url, json=[1]).status_code == 400
    assert c.post(url, content=b"x" * 70_000).status_code == 413
    assert smo.operator_calls == []
    ok = c.post(url, json={"level": 3, "extra": "dropped"})
    assert ok.status_code == 200 and json.loads(smo.operator_calls[0].content) == {"level": 3, "by": "operator"}


def test_a_read_only_rapp_refuses_every_change_even_for_an_admin(app, smo):
    """On a read-only rApp a change is 403 RAPP_READ_ONLY for an admin too, while reads still pass."""
    smo.packages[0]["aiCapabilities"] = {"operatorUi": {"version": 1, "readOnly": True, "panels": [EXAMPLE["panels"][0]]}}
    c = login(app, "admin")
    resp = c.post(op_url(I_RUN, f"/instances/{I_RUN}/evaluate"))
    assert resp.status_code == 403 and resp.json()["title"] == "RAPP_READ_ONLY"
    assert c.get(op_url(I_RUN, f"/instances/{I_RUN}")).status_code == 200
    assert len(smo.operator_calls) == 1


def test_the_instance_in_the_route_is_the_page_that_is_open(app, smo):
    """A call on the page of one instance cannot name another instance's routes (403 UNDECLARED_ROUTE)."""
    resp = login(app, "viewer").get(op_url(I_RUN, f"/instances/{I_NOPAGE}/dashboard"))
    assert resp.status_code == 403 and resp.json()["title"] == "UNDECLARED_ROUTE"


def test_a_change_needs_the_csrf_token_when_the_session_is_a_cookie(app, smo):
    """A change through a cookie session without the CSRF header is 403 and nothing is sent."""
    c = login(app, "operator")
    del c.headers["X-CSRF-Token"]
    assert c.post(op_url(I_RUN, f"/instances/{I_RUN}/evaluate")).status_code == 403
    assert smo.operator_calls == []


def test_the_rapps_response_headers_never_carry_cookies_or_hop_by_hop_fields(app, smo):
    """Set-Cookie and hop-by-hop headers from the rApp are removed from the answer, ordinary headers pass."""
    smo.operator_answer = httpx.Response(200, json={}, headers={"Set-Cookie": "x=1", "Connection": "close", "X-Rapp": "y"})
    resp = login(app, "viewer").get(op_url(I_RUN, f"/instances/{I_RUN}/dashboard"))
    assert "set-cookie" not in resp.headers and resp.headers["x-rapp"] == "y"


def test_an_unknown_instance_in_a_proxy_call_is_404(app, smo):
    """A proxy call naming an instance that does not exist is 404 and sends nothing."""
    resp = login(app, "operator").post(op_url(uuid.uuid4(), "/instances/x/evaluate"))
    assert resp.status_code == 404 and smo.operator_calls == []


# ------------------------------------------------------------------ pins

def test_pins_are_per_user_idempotent_and_limited_to_five(app, smo):
    """Pins are per user, pinning twice counts once, the sixth is 409 PIN_LIMIT, unpinning is idempotent and frees a place, and the pins list shows names with the oldest first.
    """
    extra = [{"instanceId": str(uuid.uuid4()), "packageId": P_PLAIN, "state": "RUNNING", "autonomyMode": "SHADOW", "operatorApiBase": None} for _ in range(3)]
    smo.instances += extra
    ids = [I_RUN, I_FAULT, I_GONE, I_NOPAGE, extra[0]["instanceId"]]
    c = login(app, "operator")
    assert c.get("/api/me/pins").json() == {"max": 5, "items": []}
    for i in ids:
        assert c.put(f"/api/me/pins/{i}").json() == {"instanceId": i, "pinned": True}
    assert c.put(f"/api/me/pins/{ids[0]}").status_code == 200                      # again: fine, not counted twice
    full = c.put(f"/api/me/pins/{extra[1]['instanceId']}")
    assert full.status_code == 409 and full.json()["title"] == "PIN_LIMIT"
    got = c.get("/api/me/pins").json()
    assert [p["instanceId"] for p in got["items"]] == ids and got["items"][0]["name"] == "Energy Saving" and got["items"][0]["pinned"] is True
    assert c.delete(f"/api/me/pins/{ids[0]}").status_code == 204 and c.delete(f"/api/me/pins/{ids[0]}").status_code == 204
    assert c.put(f"/api/me/pins/{extra[1]['instanceId']}").status_code == 200       # room again
    assert login(app, "viewer").get("/api/me/pins").json()["items"] == []             # another user's sidebar is their own


def test_pinning_an_unknown_or_malformed_instance_is_404(app):
    """A pin of an unknown instance or of a value that is not a UUID is 404 and stores nothing."""
    c = login(app, "viewer")
    assert c.put(f"/api/me/pins/{uuid.uuid4()}").status_code == 404 and c.put("/api/me/pins/x").status_code == 404
    assert c.get("/api/me/pins").json()["items"] == []


def test_a_pin_of_an_instance_that_was_deleted_is_dropped_when_the_pins_are_read(app, smo):
    """A pin of an instance that no longer exists is removed when the pins are read, and stays removed."""
    c = login(app, "viewer")
    c.put(f"/api/me/pins/{I_RUN}")
    c.put(f"/api/me/pins/{I_GONE}")
    smo.instances = [i for i in smo.instances if i["instanceId"] != I_GONE]
    assert [p["instanceId"] for p in c.get("/api/me/pins").json()["items"]] == [I_RUN]
    smo.instances.append({"instanceId": I_GONE, "packageId": P_ES, "state": "RUNNING", "autonomyMode": "SHADOW", "operatorApiBase": None})
    assert [p["instanceId"] for p in c.get("/api/me/pins").json()["items"]] == [I_RUN]       # it stays dropped


def test_pins_survive_the_smo_being_down_without_names(app, smo):
    """When the SMO cannot be asked the pins come back without names and none is dropped; the names return when it is back."""
    c = login(app, "viewer")
    c.put(f"/api/me/pins/{I_RUN}")
    smo.rapp_mgmt_down = True
    items = c.get("/api/me/pins").json()["items"]
    assert items == [{"instanceId": I_RUN, "name": None, "version": None, "state": None, "hasPage": False, "operatorApiRegistered": False, "pinned": True}]
    smo.rapp_mgmt_down = False
    assert [p["name"] for p in c.get("/api/me/pins").json()["items"]] == ["Energy Saving"]


def test_deleting_a_user_removes_the_pins(app, smo):
    """Deleting a user deletes their pins."""
    admin = login(app, "admin")
    assert admin.post("/api/admin/users", json={"username": "temp", "password": "temp-pass-1", "role": "viewer"}).status_code == 201
    temp = TestClient(app)
    resp = temp.post("/api/login", json={"username": "temp", "password": "temp-pass-1"})
    temp.headers["X-CSRF-Token"] = resp.json()["csrfToken"]
    temp.put(f"/api/me/pins/{I_RUN}")
    assert app.state.test_db.pins("temp") == [I_RUN]
    assert admin.delete("/api/admin/users/temp").status_code == 204
    assert app.state.test_db.pins("temp") == []
