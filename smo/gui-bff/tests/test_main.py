"""Tests for the SMO Operator GUI BFF: login/session/CSRF, the RBAC-gated
proxy to R1 Termination, the BFF's own OAuth2 client flow against SME,
hop-by-hop header stripping, health aggregation, and user/audit admin.

R1 Termination and SME are faked with one httpx.MockTransport, so the
BFF's real R1Gateway runs unmodified: bootstrap discovery, invoker
onboarding, client_credentials, and the 401 refresh are all exercised.
Run with: cd smo/gui-bff && PYTHONPATH=. python -m pytest tests -q
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import AuditEntry, Database, GuiUser, LoginFailure, RevokedSession
from app.main import CSRF_COOKIE, SESSION_COOKIE, STATUS_MODULES, create_app, seed_users
from app.smo_client import R1Gateway

R1 = "http://r1-termination:8000"
SME = "http://sme:8000"
PASSWORDS = {"admin": "admin-pass-1", "operator": "operator-pass-1", "viewer": "viewer-pass-1"}


class FakeSmo:
    """R1 Termination + SME, just enough of each: R1's /bootstrap and
    /health, SME's invoker onboarding + token endpoint, and R1's proxy,
    which (like the real one) 401s anything without a live Bearer token.
    """

    def __init__(self):
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self.invokers = 0
        self.proxied: list[httpx.Request] = []
        self.down_modules: set[str] = set()
        self.not_ready: set[str] = set()
        self.without_version: set[str] = set()
        self.next_response: httpx.Response | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{R1}/bootstrap":
            return httpx.Response(200, json={"apiEndpoints": [{"apiName": "service-apis", "tokenEndPoint": {"uri": f"{SME}/oauth2/token"}}]})
        if url == f"{R1}/health":
            return httpx.Response(200, json={"status": "healthy"})
        if url == f"{R1}/ready":
            return httpx.Response(200, json={"status": "ready", "checks": {}})
        if url == f"{R1}/version":
            return httpx.Response(200, json={"module": "r1-termination", "version": "1.4.0", "buildSha": "abc1234", "builtAt": "2026-10-06T08:00:00Z"})
        if url == f"{SME}/invoker-registrations":
            self.invokers += 1
            return httpx.Response(201, json={"apiInvokerId": f"api-invoker-{self.invokers}", "onboardingSecret": "s3cret"})
        if url == f"{SME}/oauth2/token":
            body = json.loads(request.content)
            assert body["grant_type"] == "client_credentials" and body["client_secret"] == "s3cret"
            token = f"tok-{len(self.issued) + 1}"
            self.issued.append(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600, "token_type": "Bearer"})
        # everything else is R1's token-gated proxy
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if token not in self.issued or token in self.revoked:
            return httpx.Response(401, json={"title": "UNAUTHORIZED"})
        module = request.url.path.split("/")[1]
        if request.url.path.endswith("/health"):
            if module in self.down_modules:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path.endswith("/ready"):
            return httpx.Response(503 if module in self.not_ready else 200, json={"status": "ready", "checks": {}})
        if request.url.path.endswith("/version"):
            if module in self.without_version:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"module": module, "version": "1.4.0", "buildSha": "abc1234", "builtAt": "2026-10-06T08:00:00Z"})
        self.proxied.append(request)
        if self.next_response is not None:
            resp, self.next_response = self.next_response, None
            return resp
        return httpx.Response(200, json={"echo": request.url.path}, headers={
            "Connection": "close", "Keep-Alive": "timeout=5", "Set-Cookie": "upstream=1", "X-Upstream": "yes",
        })


@pytest.fixture
def smo():
    return FakeSmo()


@pytest.fixture
def db():
    return Database("sqlite://")


@pytest.fixture
def cfg():
    return Settings(r1_url=R1, jwt_secret="test-secret", cookie_secure=False, admin_password=PASSWORDS["admin"],
                    operator_password=PASSWORDS["operator"], viewer_password=PASSWORDS["viewer"])


@pytest.fixture
def app(cfg, db, smo):
    """The BFF under test: the seeded users on an in-memory database and the real `R1Gateway` talking to `FakeSmo` through a mock transport.
    """
    seed_users(db, cfg)
    return create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))


def login(app, username) -> TestClient:
    """Signs `username` in with its test password and returns a client that carries the session cookies and the `X-CSRF-Token` header, so its unsafe calls pass the CSRF check.
    """
    client = TestClient(app)
    resp = client.post("/api/login", json={"username": username, "password": PASSWORDS[username]})
    assert resp.status_code == 200, resp.text
    client.headers["X-CSRF-Token"] = resp.json()["csrfToken"]
    return client


def audit_rows(db, action=None):
    """Every audit row, or only those with `action`, read straight from the database."""
    with db.session() as s:
        rows = s.query(AuditEntry).all()
    return [r for r in rows if action is None or r.action == action]


# ---------------------------------------------------------------- login / session

def test_login_sets_httponly_session_and_readable_csrf_cookie(app):
    """The session cookie must be httpOnly, SameSite=strict and scoped to /api, while the CSRF cookie stays readable by the SPA (double-submit).
    """
    resp = TestClient(app).post("/api/login", json={"username": "operator", "password": PASSWORDS["operator"]})
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"
    set_cookies = resp.headers.get_list("set-cookie")
    session = next(c for c in set_cookies if c.startswith(f"{SESSION_COOKIE}="))
    csrf = next(c for c in set_cookies if c.startswith(f"{CSRF_COOKIE}="))
    assert "HttpOnly" in session and "SameSite=strict" in session and "Path=/api" in session
    assert "HttpOnly" not in csrf


def test_cookies_are_secure_by_default(cfg, db, smo):
    """With `cookie_secure` on (the default), every cookie the login sets carries the Secure flag."""
    cfg.cookie_secure = True
    seed_users(db, cfg)
    app = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))
    resp = TestClient(app).post("/api/login", json={"username": "admin", "password": PASSWORDS["admin"]})
    assert all("Secure" in c for c in resp.headers.get_list("set-cookie"))


def test_wrong_password_is_rejected_and_audited(app, db):
    """A wrong password is 401 and leaves one LOGIN_FAILED audit row naming the user."""
    resp = TestClient(app).post("/api/login", json={"username": "viewer", "password": "nope"})
    assert resp.status_code == 401
    assert [r.username for r in audit_rows(db, "LOGIN_FAILED")] == ["viewer"]


def test_repeated_failures_lock_the_account(app):
    """Five wrong passwords lock the account: the sixth attempt, even with the right password, is 429."""
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/api/login", json={"username": "viewer", "password": "nope"}).status_code == 401
    # even the right password is refused while locked
    assert client.post("/api/login", json={"username": "viewer", "password": PASSWORDS["viewer"]}).status_code == 429


def test_me_requires_a_session(app):
    """`/api/me` is 401 without a session and answers the caller's role with one."""
    assert TestClient(app).get("/api/me").status_code == 401
    assert login(app, "viewer").get("/api/me").json()["role"] == "viewer"


def test_a_tampered_session_token_is_rejected(app):
    """A session token whose signature was altered is refused (the alteration is a whole character in the middle, so it can never leave the signature unchanged).
    """
    client = login(app, "viewer")
    token = client.cookies.get(SESSION_COOKIE)
    header, payload, sig = token.split(".")
    # Change one whole character in the middle of the signature, to a different one, so the signature bytes
    # always change. (Overwriting the last two characters is not enough: the last of a 43-character base64url
    # signature carries only 4 data bits, so about one token in a thousand would come out unchanged and valid.)
    middle = len(sig) // 2
    tampered = sig[:middle] + ("B" if sig[middle] == "A" else "A") + sig[middle + 1:]
    assert tampered != sig
    client.cookies.set(SESSION_COOKIE, f"{header}.{payload}.{tampered}", path="/api")
    assert client.get("/api/me").status_code == 401


def test_logout_clears_the_session(app):
    """After logout the same client is no longer signed in."""
    client = login(app, "viewer")
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_logout_ends_the_session_itself_so_a_copied_cookie_stops_working(app):
    """Logout revokes the token id server side, so a copy of the cookie is refused with SESSION_REVOKED while a second session of the same user keeps working.
    """
    client = login(app, "viewer")
    copied = client.cookies.get(SESSION_COOKIE, path="/api")
    other = login(app, "viewer")                                          # a second session of the same user
    assert client.post("/api/logout").status_code == 200
    thief = TestClient(app)
    thief.cookies.set(SESSION_COOKIE, copied, path="/api")
    resp = thief.get("/api/me")
    assert resp.status_code == 401 and resp.json()["detail"]["title"] == "SESSION_REVOKED"
    assert other.get("/api/me").status_code == 200                       # only that session ended


def test_a_bearer_token_is_revoked_by_logging_out_with_it(app):
    """A token from `/api/token` stops working once it was used to log out."""
    token = TestClient(app).post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"]}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert TestClient(app).get("/api/me", headers=headers).status_code == 200
    assert TestClient(app).post("/api/logout", headers=headers).status_code == 200
    assert TestClient(app).get("/api/me", headers=headers).status_code == 401


def test_revocations_are_shared_through_the_database_and_expired_ones_are_forgotten(db):
    """Recording the same revocation twice is harmless, and a revocation whose token has expired by itself is pruned the next time one is written.
    """
    db.revoke_session("a", expires_at=2_000.0, now=1_000.0)
    db.revoke_session("a", expires_at=2_000.0, now=1_000.0)               # twice: no error
    assert db.session_revoked("a") and not db.session_revoked("b")
    db.revoke_session("b", expires_at=5_000.0, now=3_000.0)               # "a" has expired by itself by now
    assert not db.session_revoked("a") and db.session_revoked("b")


def test_seed_needs_no_password_in_git(db, tmp_path, caplog):
    """No GUI_ADMIN_PASSWORD set: admin is still seeded, with a random
    password written to an owner-only file and never logged; operator/viewer
    are not seeded."""
    password_file = tmp_path / "initial-admin-password"
    cfg = Settings(r1_url=R1, jwt_secret="x", cookie_secure=False, initial_password_file=str(password_file))
    with caplog.at_level("DEBUG"):
        seed_users(db, cfg)
    with db.session() as s:
        assert [u.username for u in s.query(GuiUser).all()] == ["admin"]
    generated = password_file.read_text().strip()
    assert oct(password_file.stat().st_mode & 0o777) == "0o600"
    assert generated not in caplog.text and str(password_file) in caplog.text
    app = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(FakeSmo().handler)))
    assert TestClient(app).post("/api/login", json={"username": "admin", "password": generated}).status_code == 200


def test_seeding_never_touches_an_existing_user_table(db, cfg):
    """A second `seed_users` call, even with a changed admin password in the environment, leaves the existing users as they are.
    """
    seed_users(db, cfg)
    cfg.admin_password = "changed-in-env"
    seed_users(db, cfg)
    client = TestClient(create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(FakeSmo().handler))))
    assert client.post("/api/login", json={"username": "admin", "password": PASSWORDS["admin"]}).status_code == 200


# ---------------------------------------------------------------- RBAC through the proxy

def test_viewer_can_read(app, smo):
    """A viewer's GET through the proxy reaches R1 with its query string intact."""
    resp = login(app, "viewer").get("/api/smo/rapp-mgmt/instances", params={"state": "RUNNING"})
    assert resp.status_code == 200
    [req] = smo.proxied
    assert req.url.path == "/rapp-mgmt/instances" and req.url.params["state"] == "RUNNING"


def test_viewer_is_blocked_on_post_and_nothing_reaches_r1(app, smo, db):
    """A viewer's POST is 403 naming the needed role, is audited as DENIED, and is never sent to R1."""
    resp = login(app, "viewer").post("/api/smo/aimgf/training-jobs", json={"modelId": "m", "producerId": "gui"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "requires role operator"
    assert smo.proxied == []
    [denied] = audit_rows(db, "DENIED")
    assert (denied.username, denied.method, denied.path) == ("viewer", "POST", "/aimgf/training-jobs")


def test_operator_can_train_and_ack(app, smo):
    """An operator may start a training job and acknowledge an alarm, and the request body is forwarded unchanged."""
    client = login(app, "operator")
    assert client.post("/api/smo/aimgf/training-jobs", json={"modelId": "m", "producerId": "gui"}).status_code == 200
    assert client.patch("/api/smo/ran-nf-oam/alarms/a-1/ack", params={"new_state": "ACKNOWLEDGED"}).status_code == 200
    assert [r.url.path for r in smo.proxied] == ["/aimgf/training-jobs", "/ran-nf-oam/alarms/a-1/ack"]
    assert json.loads(smo.proxied[0].content) == {"modelId": "m", "producerId": "gui"}


def test_alarm_ack_user_is_the_gui_user_not_whatever_the_browser_sent(app, smo):
    """The `ack_user_id` of an alarm acknowledgement is forced to the signed-in user, replacing a value the browser sent."""
    login(app, "operator").patch("/api/smo/ran-nf-oam/alarms/a-1/ack",
                                 params={"new_state": "ACKNOWLEDGED", "ack_user_id": "someone-else"})
    params = smo.proxied[0].url.params
    assert params.get_list("ack_user_id") == ["operator"]
    assert params["new_state"] == "ACKNOWLEDGED"


def test_operator_cannot_terminate_but_admin_can(app, smo):
    """Terminating an rApp instance is admin only: the operator's attempt is 403 and never forwarded."""
    assert login(app, "operator").post("/api/smo/rapp-mgmt/instances/i-1/terminate").status_code == 403
    assert smo.proxied == []
    assert login(app, "admin").post("/api/smo/rapp-mgmt/instances/i-1/terminate").status_code == 200
    assert [r.url.path for r in smo.proxied] == ["/rapp-mgmt/instances/i-1/terminate"]


def test_model_deprecation_is_admin_only_even_with_a_duplicated_param(app, smo):
    """APPROVE_TRAINING (HISTORY.md OI-6.1's operator gate) is
    operator-level; DEPRECATE/RETIRE and the six Wave 2 governance
    decisions (SUBMIT_FOR_APPROVAL/APPROVE/REJECT/CERTIFY/PROMOTE/ROLLBACK)
    are admin-only, the same elevated stakes as the DELETEs elsewhere.
    (AIMgF itself refuses job-driven events such as TRAINING_COMPLETE on
    advance — the BFF only decides the role.)
    """
    operator = login(app, "operator")
    assert operator.post("/api/smo/aimgf/models/m-1/advance?event=APPROVE_TRAINING").status_code == 200
    assert operator.post("/api/smo/aimgf/models/m-1/advance?event=DEPRECATE").status_code == 403
    assert operator.post("/api/smo/aimgf/models/m-1/advance?event=CERTIFY").status_code == 403
    assert operator.post("/api/smo/aimgf/models/m-1/advance?event=APPROVE_TRAINING&event=DEPRECATE").status_code == 403
    assert login(app, "admin").post("/api/smo/aimgf/models/m-1/advance?event=DEPRECATE").status_code == 200
    assert login(app, "admin").post("/api/smo/aimgf/models/m-1/advance?event=CERTIFY").status_code == 200


def test_package_delete_is_admin_only(app):
    """Deleting an onboarded package needs admin; an operator gets 403."""
    assert login(app, "operator").delete("/api/smo/onboarding/packages/p-1").status_code == 403
    assert login(app, "admin").delete("/api/smo/onboarding/packages/p-1").status_code == 200


def test_routes_not_in_the_table_are_refused_even_for_admin(app, smo):
    """A route no rule names (SME token endpoint, NFO instantiation, an unknown module) is 403 even for an admin and never reaches R1.
    """
    admin = login(app, "admin")
    assert admin.post("/api/smo/sme/oauth2/token", json={}).status_code == 403
    assert admin.post("/api/smo/nfo/deployments", json={}).status_code == 403
    assert admin.get("/api/smo/not-a-module/x").status_code == 403
    assert smo.proxied == []


def test_remedial_action_admin_flag_is_derived_from_the_gui_role(app, smo):
    """`requester_is_admin` on a remedial action is set from the signed-in role, whatever the browser claimed."""
    login(app, "operator").post("/api/smo/sa-smos/monitors/m-1/remedial-actions",
                                params={"action_type": "SCALE", "requester_is_admin": "true"})
    login(app, "admin").post("/api/smo/sa-smos/monitors/m-1/remedial-actions", params={"action_type": "SCALE"})
    assert [r.url.params.get_list("requester_is_admin") for r in smo.proxied] == [["false"], ["true"]]


def test_gui_created_intents_carry_the_gui_rmio_identity(app, smo):
    """Intents created or changed through the GUI carry the fixed `smo-gui` RMIO identity, so a spoofed one in the body is replaced.
    """
    client = login(app, "operator")
    client.post("/api/smo/intent-service/intents", json={"expectations": [], "rmioId": "spoofed-rapp"})
    client.patch("/api/smo/intent-service/intents/i-1/admin-state", json={"newState": "DEACTIVATED", "requesterId": "spoofed-rapp"})
    assert json.loads(smo.proxied[0].content)["rmioId"] == "smo-gui"
    assert json.loads(smo.proxied[1].content) == {"newState": "DEACTIVATED", "requesterId": "smo-gui"}


def test_cm_write_identity_and_msac_tier_come_from_the_gui_role(app, smo):
    """A configuration job is attributed to `smo-gui:<user>` and carries the MSAC admin tier only for an admin, whatever the body said.
    """
    body = {"scope": "entire-RAN", "changes": [], "requestedBy": "someone", "msacRole": "admin"}
    login(app, "operator").post("/api/smo/ran-nf-oam/config-jobs", json=body)
    login(app, "admin").post("/api/smo/ran-nf-oam/config-jobs", json=body)
    sent = [json.loads(r.content) for r in smo.proxied]
    assert [(b["requestedBy"], b["msacRole"]) for b in sent] == [("smo-gui:operator", None), ("smo-gui:admin", "admin")]


def test_assist_rejection_is_attributed_to_the_gui_user(app, smo):
    """Who rejected an ASSIST dispatch is the signed-in user, and a viewer cannot reject."""
    login(app, "operator").post("/api/smo/intent-service/autonomy-dispatches/d-1/reject",
                                json={"rejectedBy": "someone-else", "reason": "wrong cells"})
    assert json.loads(smo.proxied[0].content) == {"rejectedBy": "smo-gui:operator", "reason": "wrong cells"}
    assert login(app, "viewer").post("/api/smo/intent-service/autonomy-dispatches/d-1/reject", json={}).status_code == 403


def test_an_approval_is_attributed_to_the_signed_in_user_and_not_to_what_the_browser_sent(app, smo):
    """AI-11: who approved or rejected an rApp's action is the GUI user; a viewer cannot decide; the sweep and a hand-made path are not exposed."""
    login(app, "operator").post("/api/smo/ran-nf-oam/rapp-approvals/a-1/approve", json={"decidedBy": "smo-gui:admin", "reason": "ok"})
    login(app, "operator").post("/api/smo/ran-nf-oam/rapp-approvals/a-2/reject", json={"decidedBy": "someone-else", "reason": "no"})
    assert [json.loads(r.content) for r in smo.proxied] == [{"decidedBy": "smo-gui:operator", "reason": "ok"}, {"decidedBy": "smo-gui:operator", "reason": "no"}]
    assert login(app, "viewer").post("/api/smo/ran-nf-oam/rapp-approvals/a-1/approve", json={}).status_code == 403
    assert login(app, "admin").post("/api/smo/ran-nf-oam/rapp-approvals/expire-due").status_code == 403         # the sweep is for a scheduler, not the GUI
    assert len(smo.proxied) == 2


def test_every_proxied_call_names_the_signed_in_user_in_a_header_the_browser_cannot_choose(app, smo):
    """SEC-15.8: each call to the SMO carries `X-R1-Acting-User: smo-gui:<signed-in user>`, so a module can name the person behind the BFF's token; a value the browser sent is replaced."""
    login(app, "operator").post("/api/smo/ran-nf-oam/rapp-approvals/a-1/approve", json={"reason": "ok"}, headers={"X-R1-Acting-User": "smo-gui:admin"})
    login(app, "admin").get("/api/smo/ran-nf-oam/alarms", headers={"X-R1-Acting-User": "smo-gui:operator"})
    assert [r.headers["x-r1-acting-user"] for r in smo.proxied] == ["smo-gui:operator", "smo-gui:admin"]


def test_a_policy_asking_for_two_approvals_reaches_ran_nf_oam_from_an_admin_only_with_the_setter_pinned(app, smo):
    """Two-person approval is a field of the approval policy: the BFF passes it through, pins who set the policy, and leaves the policy to an admin."""
    body = {"timeoutSeconds": 600, "onTimeout": "EXPIRE", "requiredApprovals": 2, "requestedBy": "someone-else"}
    login(app, "admin").put("/api/smo/ran-nf-oam/rapp-approval-policy/es-client", json=body)
    assert json.loads(smo.proxied[0].content) == {"timeoutSeconds": 600, "onTimeout": "EXPIRE", "requiredApprovals": 2, "requestedBy": "smo-gui:admin"}
    assert login(app, "operator").put("/api/smo/ran-nf-oam/rapp-approval-policy/es-client", json=body).status_code == 403
    assert len(smo.proxied) == 1


def test_a_rapps_own_api_is_no_longer_a_module_of_the_proxy(app, smo):
    """PR-GUI-8: the four sample rApps' static rules are gone; their routes are reached through /api/rapps/<instance>/operator/..., allowed by the declaration."""
    for who in ("viewer", "operator", "admin"):
        c = login(app, who)
        assert c.get("/api/smo/energy-saving-rapp/instances/i-1/dashboard").status_code == 403
        assert c.post("/api/smo/energy-saving-rapp/instances/i-1/cells/101/override", json={"operator": "x"}).status_code == 403
    assert smo.proxied == []


def test_role_change_applies_on_the_next_request(app, db):
    """The role is read from the user table on every request, so demoting an operator takes effect without a new sign-in."""
    operator = login(app, "operator")
    assert operator.post("/api/smo/so-smos/orders", json={"scope": "s", "steps": []}).status_code == 200
    login(app, "admin").patch("/api/admin/users/operator", json={"role": "viewer"})
    assert operator.post("/api/smo/so-smos/orders", json={"scope": "s", "steps": []}).status_code == 403


# ---------------------------------------------------------------- CSRF / bearer

def test_cookie_session_mutation_without_csrf_header_is_refused(app, smo):
    """A cookie session's unsafe call is 403 when the CSRF header is missing or wrong, and nothing is forwarded."""
    client = login(app, "operator")
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/smo/so-smos/orders", json={}).status_code == 403
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.post("/api/smo/so-smos/orders", json={}).status_code == 403
    assert smo.proxied == []


def test_oauth2_password_grant_bearer_token_needs_no_csrf(app, smo):
    """A Bearer token from `/api/token` works on unsafe calls without a CSRF header, and a bad password is a 400 `invalid_grant`.
    """
    client = TestClient(app)
    resp = client.post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"]})
    assert resp.status_code == 200 and resp.json()["token_type"] == "Bearer"
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert client.post("/api/smo/so-smos/orders", json={"scope": "s", "steps": []}, headers=headers).status_code == 200
    bad = client.post("/api/token", data={"grant_type": "password", "username": "operator", "password": "x"})
    assert bad.status_code == 400 and bad.json() == {"error": "invalid_grant"}


# ---------------------------------------------------------------- proxy mechanics

def test_proxy_forwards_the_bff_token_never_the_browser_credentials(app, smo):
    """The upstream request carries the BFF's own SME token only: no cookie, CSRF header or custom header from the browser, and the invoker is registered once.
    """
    client = login(app, "viewer")
    client.get("/api/smo/onboarding/packages", headers={"Authorization-Hint": "x", "X-Custom": "y"})
    req = smo.proxied[0]
    assert req.headers["authorization"] == "Bearer tok-1"
    assert "cookie" not in req.headers and "x-csrf-token" not in req.headers and "x-custom" not in req.headers
    assert smo.invokers == 1   # onboarded once at SME, via the bootstrap-advertised endpoint


def test_proxy_strips_hop_by_hop_and_upstream_set_cookie(app):
    """Hop-by-hop headers and an upstream Set-Cookie are removed from the proxied answer while ordinary headers pass."""
    resp = login(app, "viewer").get("/api/smo/onboarding/packages")
    assert resp.headers["x-upstream"] == "yes"
    assert "keep-alive" not in resp.headers
    assert resp.headers.get("connection") != "close"
    assert "upstream=1" not in resp.headers.get("set-cookie", "")


def test_smo_auth_failure_detail_does_not_leak_exception_text(cfg, db):
    """When no SME token can be had the answer is 502 SMO_AUTH_FAILED with a fixed message, never the exception text or an internal host name.
    """
    def sme_down(request):
        raise httpx.ConnectError("refused: internal-host-10.0.0.7:8000")

    seed_users(db, cfg)
    app = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(sme_down)))
    resp = login(app, "viewer").get("/api/smo/onboarding/packages")
    assert resp.status_code == 502 and resp.json()["title"] == "SMO_AUTH_FAILED"
    assert "internal-host" not in resp.text and "ConnectError" not in resp.text


def test_proxy_passes_upstream_errors_through(app, smo):
    """An upstream error status and body (here a 409) reach the browser unchanged."""
    smo.next_response = httpx.Response(409, json={"title": "MODEL_NOT_CERTIFIED"})
    resp = login(app, "operator").post("/api/smo/rapp-mgmt/instances", json={"packageId": "p"})
    assert resp.status_code == 409 and resp.json() == {"title": "MODEL_NOT_CERTIFIED"}


def test_expired_smo_token_is_refreshed_once(app, smo):
    """When R1 refuses the cached token with 401 the BFF fetches a new one once and retries, reusing the stored invoker identity.
    """
    client = login(app, "viewer")
    client.get("/api/smo/onboarding/packages")
    smo.revoked.add("tok-1")
    assert client.get("/api/smo/onboarding/packages").status_code == 200
    assert smo.issued == ["tok-1", "tok-2"]
    assert smo.invokers == 1   # the stored invoker credential was reused


def test_mutations_are_audited_with_status(app, db):
    """A forwarded change writes one PROXY audit row with user, role, method, path and the upstream status."""
    login(app, "operator").post("/api/smo/aimgf/training-jobs", json={"modelId": "m", "producerId": "gui"})
    [row] = audit_rows(db, "PROXY")
    assert (row.username, row.role, row.method, row.path, row.status_code) == \
        ("operator", "operator", "POST", "/aimgf/training-jobs", 200)


def test_reads_are_not_audited(app, db):
    """A forwarded read writes no PROXY audit row."""
    login(app, "viewer").get("/api/smo/onboarding/packages")
    assert audit_rows(db, "PROXY") == []


def test_audit_log_is_append_only(app, db):
    """Changing an audit row through the ORM is refused at flush with PermissionError."""
    login(app, "viewer")
    with db.session() as s:
        row = s.query(AuditEntry).first()
        row.detail = "rewritten"
        with pytest.raises(PermissionError):
            s.commit()


def test_security_headers_on_every_bff_response(app):
    """Every BFF response carries nosniff, a frame-denying CSP and X-Frame-Options DENY, errors included."""
    resp = TestClient(app).get("/api/me")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


# ---------------------------------------------------------------- health

def test_modules_status_probes_every_module_via_r1(app, smo):
    """The health grid lists R1 and the 15 SMO modules in order, marks an unreachable one unhealthy with `unreachable`, and reports a latency for each.
    """
    smo.down_modules.add("nfo")
    body = login(app, "viewer").get("/api/modules/status").json()
    by_module = {m["module"]: m for m in body["modules"]}
    assert list(by_module) == STATUS_MODULES and len(STATUS_MODULES) == 16  # R1 and the 15 SMO modules; the sample rApps are not probed (their pages are reached by instance, PR-GUI-8)
    assert by_module["nfo"]["healthy"] is False and by_module["nfo"]["error"] == "unreachable"
    assert all(m["healthy"] for name, m in by_module.items() if name != "nfo")
    assert all(isinstance(m["latencyMs"], float) for m in body["modules"])


def test_modules_status_adds_readiness_and_the_build_each_module_runs(app, smo):
    """Readiness and build fields come from /ready and /version, and are null when a module has no /version or did not answer."""
    smo.not_ready.add("dme")
    smo.without_version.add("sme")      # an older release during a rolling upgrade has no /version
    smo.down_modules.add("nfo")
    by_module = {m["module"]: m for m in login(app, "viewer").get("/api/modules/status").json()["modules"]}
    assert by_module["r1-termination"]["version"] == "1.4.0" and by_module["r1-termination"]["ready"] is True   # R1's own public routes
    assert by_module["aimgf"]["ready"] is True and by_module["aimgf"]["buildSha"] == "abc1234"
    assert by_module["aimgf"]["builtAt"] == "2026-10-06T08:00:00Z" and by_module["aimgf"]["version"] == "1.4.0"
    assert by_module["dme"]["healthy"] is True and by_module["dme"]["ready"] is False and by_module["dme"]["buildSha"] == "abc1234"
    assert by_module["sme"]["healthy"] is True and by_module["sme"]["version"] is None and by_module["sme"]["buildSha"] is None and by_module["sme"]["builtAt"] is None
    assert by_module["nfo"]["healthy"] is False and by_module["nfo"]["ready"] is None and by_module["nfo"]["buildSha"] is None


def test_modules_status_reports_smo_auth_failure_without_crashing(cfg, db):
    """With no SME token the module entries say `auth: no SMO access token` instead of failing the request, and R1's own entry is still healthy.
    """
    def sme_down(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        raise httpx.ConnectError("refused")

    seed_users(db, cfg)
    app = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(sme_down)))
    body = login(app, "viewer").get("/api/modules/status").json()
    by_module = {m["module"]: m for m in body["modules"]}
    assert by_module["r1-termination"]["healthy"] is True
    assert by_module["sme"]["healthy"] is False and by_module["sme"]["error"] == "auth: no SMO access token"
    assert by_module["sme"]["ready"] is None and by_module["sme"]["version"] is None
    assert by_module["r1-termination"]["ready"] is None and by_module["r1-termination"]["buildSha"] is None   # its /ready and /version are unreachable here


# ---------------------------------------------------------------- admin

def test_user_admin_is_admin_only(app):
    """The user list and the audit log are 403 for an operator."""
    assert login(app, "operator").get("/api/admin/users").status_code == 403
    assert login(app, "operator").get("/api/admin/audit").status_code == 403


def test_admin_creates_updates_and_deletes_a_user(app):
    """The admin user life cycle: create (201), duplicate (409), bad name (400), short password (422), role change and delete."""
    admin = login(app, "admin")
    created = admin.post("/api/admin/users", json={"username": "noc1", "password": "long-enough", "role": "viewer"})
    assert created.status_code == 201 and created.json()["role"] == "viewer"
    assert admin.post("/api/admin/users", json={"username": "noc1", "password": "long-enough", "role": "viewer"}).status_code == 409
    assert admin.post("/api/admin/users", json={"username": "Bad Name", "password": "long-enough", "role": "viewer"}).status_code == 400
    assert admin.post("/api/admin/users", json={"username": "noc2", "password": "short", "role": "viewer"}).status_code == 422

    assert admin.patch("/api/admin/users/noc1", json={"role": "operator"}).json()["role"] == "operator"
    assert admin.delete("/api/admin/users/noc1").status_code == 204
    assert "noc1" not in [u["username"] for u in admin.get("/api/admin/users").json()]


def test_erasing_a_gui_user_end_to_end_and_what_it_leaves_behind(app, db):
    """STD-4.3, the procedure of docs/PRIVACY.md section 4, tried once: what deleting a user removes, and what stays and why."""
    admin = login(app, "admin")
    password = "noc9-long-pass"
    assert admin.post("/api/admin/users", json={"username": "noc9", "password": password, "role": "operator"}).status_code == 201

    # the user signs in twice (a browser session and a script token) and does something that is audited and attributed to them
    browser = TestClient(app)
    resp = browser.post("/api/login", json={"username": "noc9", "password": password})
    assert resp.status_code == 200
    browser.headers["X-CSRF-Token"] = resp.json()["csrfToken"]
    token = TestClient(app).post("/api/token", data={"grant_type": "password", "username": "noc9", "password": password}).json()["access_token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert browser.patch("/api/smo/ran-nf-oam/alarms/a-1/ack").status_code == 200
    ended = TestClient(app)                                               # a third session that the user ended themselves: a revocation row
    ended_resp = ended.post("/api/login", json={"username": "noc9", "password": password})
    ended.headers["X-CSRF-Token"] = ended_resp.json()["csrfToken"]
    assert ended.post("/api/logout").status_code == 200
    for _ in range(2):                                                    # failed sign-ins after the last success: the counter row is there
        assert TestClient(app).post("/api/login", json={"username": "noc9", "password": "wrong"}).status_code == 401
    with db.session() as s:
        assert s.get(GuiUser, "noc9") is not None and s.get(LoginFailure, "noc9").count == 2
        assert s.query(RevokedSession).count() == 1
    assert browser.get("/api/me").status_code == 200 and TestClient(app).get("/api/me", headers=bearer).status_code == 200
    audited_before = [r.id for r in audit_rows(db) if r.username == "noc9"]
    assert {r.action for r in audit_rows(db) if r.username == "noc9"} >= {"LOGIN", "TOKEN", "PROXY", "LOGOUT", "LOGIN_FAILED"}

    # the procedure: one admin call
    assert admin.delete("/api/admin/users/noc9").status_code == 204

    # removed: the account, the failed-login counter, and every session (a token naming a user that no longer exists is refused)
    with db.session() as s:
        assert s.get(GuiUser, "noc9") is None
        assert s.get(LoginFailure, "noc9") is None
    assert "noc9" not in [u["username"] for u in admin.get("/api/admin/users").json()]
    assert browser.get("/api/me").status_code == 401
    assert TestClient(app).get("/api/me", headers=bearer).json()["detail"]["title"] == "SESSION_REVOKED"
    assert TestClient(app).post("/api/login", json={"username": "noc9", "password": password}).status_code == 401

    # a new account under the same name does not revive the old person's unexpired tokens
    assert admin.post("/api/admin/users", json={"username": "noc9", "password": password, "role": "operator"}).status_code == 201
    assert browser.get("/api/me").status_code == 401
    assert TestClient(app).get("/api/me", headers=bearer).status_code == 401

    # what stays, deliberately: the audit rows that name the user (the log is append-only), and the revocation row, which holds a token id and an expiry only
    assert [r.id for r in audit_rows(db) if r.username == "noc9"][:len(audited_before)] == audited_before
    assert any(r.action == "USER_DELETED" and r.detail == "noc9" for r in audit_rows(db))
    assert {c.name for c in RevokedSession.__table__.columns} == {"jti", "expires_at"}
    with db.session() as s, pytest.raises(PermissionError):
        row = s.query(AuditEntry).filter(AuditEntry.username == "noc9").first()
        row.username = "erased"                                           # the ORM refuses an edit: pseudonymising in place is not a BFF operation
        s.commit()


def test_password_reset_and_deactivation_revoke_existing_sessions(app):
    """An admin's password reset ends the user's existing sessions at once."""
    viewer = login(app, "viewer")
    login(app, "admin").patch("/api/admin/users/viewer", json={"password": "a-new-password"})
    assert viewer.get("/api/me").status_code == 401


def test_the_last_active_admin_cannot_be_removed_or_demoted(app):
    """The only active admin can be neither demoted nor deleted (409), but a second admin can be deleted."""
    admin = login(app, "admin")
    assert admin.patch("/api/admin/users/admin", json={"role": "operator"}).status_code == 409
    assert admin.delete("/api/admin/users/admin").status_code == 409
    admin.post("/api/admin/users", json={"username": "admin2", "password": "long-enough", "role": "admin"})
    assert admin.delete("/api/admin/users/admin2").status_code == 204


def test_change_own_password_keeps_the_current_session(app):
    """Changing one's own password ends other sessions but hands the caller a fresh CSRF token so the current one continues, and the new password signs in.
    """
    client = login(app, "operator")
    resp = client.post("/api/me/password", json={"currentPassword": PASSWORDS["operator"], "newPassword": "brand-new-pass"})
    assert resp.status_code == 200
    client.headers["X-CSRF-Token"] = resp.json()["csrfToken"]
    assert client.get("/api/me").status_code == 200
    assert TestClient(app).post("/api/login", json={"username": "operator", "password": "brand-new-pass"}).status_code == 200


def test_audit_endpoint_lists_newest_first_and_filters(app):
    """The audit endpoint returns newest rows first and filters by action."""
    admin = login(app, "admin")
    admin.post("/api/admin/users", json={"username": "noc1", "password": "long-enough", "role": "viewer"})
    entries = admin.get("/api/admin/audit").json()["items"]
    assert [e["action"] for e in entries][:2] == ["USER_CREATED", "LOGIN"]
    assert {e["action"] for e in admin.get("/api/admin/audit", params={"action": "LOGIN"}).json()["items"]} == {"LOGIN"}


def _written_audit_actions() -> set[str]:
    """The action names the BFF's code writes: the first argument of every `audit(...)` call in app/ that is a string literal."""
    import ast
    from pathlib import Path
    found: set[str] = set()
    for source in (Path(__file__).resolve().parent.parent / "app").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            name = node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None)
            if (name in ("audit", "_audit") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
    return found


def test_the_served_audit_actions_are_exactly_the_ones_the_code_writes(app):
    """GUI-10.4: `GET /api/admin/audit/actions` feeds the console's action filter; it fails here when an `audit(...)` call writes an action the
    list lacks, or the list keeps one nothing writes any more. Admin only."""
    from app.main import AUDIT_ACTIONS
    assert set(AUDIT_ACTIONS) == _written_audit_actions()
    body = login(app, "admin").get("/api/admin/audit/actions").json()
    assert body["actions"] == sorted(AUDIT_ACTIONS)
    assert login(app, "operator").get("/api/admin/audit/actions").status_code == 403


def test_permissions_endpoint_exposes_the_rbac_table(app):
    """`/api/permissions` returns the caller's role and the rule table the SPA evaluates."""
    body = login(app, "viewer").get("/api/permissions").json()
    assert body["role"] == "viewer"
    assert {"method": "POST", "pattern": "^/rapp-mgmt/instances/[^/]+/terminate$", "role": "admin", "queryMatch": {}} in body["rules"]


def test_rotating_the_signing_key_ends_every_session_and_a_new_login_works(cfg, db, smo):
    """PR-SEC-4.8: change GUI_JWT_SECRET and restart: sessions signed with the old key are refused, signing in again works."""
    def start(secret):
        rotated = Settings(r1_url=R1, jwt_secret=secret, cookie_secure=False, admin_password=PASSWORDS["admin"],
                           operator_password=PASSWORDS["operator"], viewer_password=PASSWORDS["viewer"])
        seed_users(db, rotated)
        return create_app(rotated, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))

    old = login(start("the-old-key"), "viewer")
    assert old.get("/api/me").status_code == 200
    new_app = start("the-new-key")
    carried = TestClient(new_app)
    carried.cookies.update(old.cookies)
    assert carried.get("/api/me").status_code == 401                          # the old session does not survive the rotation
    assert login(new_app, "viewer").get("/api/me").json()["role"] == "viewer"


def test_audit_endpoint_total_false_has_no_total_and_a_has_more_flag(app):
    """With `total=false` the audit page has no `total` and a correct `hasMore`."""
    admin = login(app, "admin")
    admin.post("/api/admin/users", json={"username": "noc2", "password": "long-enough", "role": "viewer"})
    assert admin.get("/api/admin/audit", params={"limit": 1}).json()["total"] >= 2
    page = admin.get("/api/admin/audit", params={"limit": 1, "total": "false"}).json()
    assert "total" not in page and len(page["items"]) == 1 and page["hasMore"] is True
    assert admin.get("/api/admin/audit", params={"limit": 500, "total": "false"}).json()["hasMore"] is False


def test_a_list_read_through_the_proxy_decides_the_same_with_total_false():
    """The `total=false` query parameter does not change the RBAC decision for a list read."""
    from app.rbac import Role, decide
    for role in (Role.VIEWER, Role.OPERATOR, Role.ADMIN):
        assert decide("GET", "/aimgf/models", {"limit": ["5"], "total": ["false"]}, role).allowed


# ---------------------------------------------------------------- sign-in history, last active, audit paging and export (GUI-9.5, GUI-9.8)

def test_my_sign_ins_are_mine_only_newest_first(app):
    """`GET /api/me/sign-ins` lists the caller's own sign-ins, failures and sign-outs, never another user's, and never the other audit rows."""
    TestClient(app).post("/api/login", json={"username": "viewer", "password": "wrong-one"})
    login(app, "operator")
    viewer = login(app, "viewer")
    viewer.post("/api/me/password", json={"currentPassword": PASSWORDS["viewer"], "newPassword": "viewer-pass-2"})   # audited, not a sign-in
    rows = viewer.get("/api/me/sign-ins").json()
    assert [r["action"] for r in rows] == ["LOGIN", "LOGIN_FAILED"]
    assert set(rows[0]) == {"at", "action", "detail"} and rows[0]["at"].endswith("+00:00")
    assert len(viewer.get("/api/me/sign-ins", params={"limit": 1}).json()) == 1


def test_the_user_list_says_when_each_user_was_last_active_and_signed_in(app):
    """`lastActiveAt` is the user's newest audit row and `lastSignInAt` the newest sign-in, from one grouped read; null for a user never seen."""
    admin = login(app, "admin")
    admin.post("/api/admin/users", json={"username": "fresh", "password": "long-enough", "role": "viewer"})
    viewer = login(app, "viewer")
    viewer.post("/api/smo/aimgf/training-jobs", json={})        # refused (DENIED) for a viewer: audited, so it is activity
    users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
    assert users["fresh"]["lastActiveAt"] is None and users["fresh"]["lastSignInAt"] is None
    assert users["viewer"]["lastSignInAt"] and users["viewer"]["lastActiveAt"] >= users["viewer"]["lastSignInAt"]
    assert users["operator"]["lastActiveAt"] is None


def test_audit_keyset_paging_walks_the_log_without_offsets(app):
    """`after_id` returns rows below that id, newest first, and `nextAfterId` chains the pages until a short page ends the walk."""
    admin = login(app, "admin")
    for i in range(5):
        admin.post("/api/admin/users", json={"username": f"k{i}", "password": "long-enough", "role": "viewer"})
    everything = [e["id"] for e in admin.get("/api/admin/audit", params={"limit": 500}).json()["items"]]
    walked, after = [], None
    while True:
        page = admin.get("/api/admin/audit", params={"limit": 2, "total": "false", **({"after_id": after} if after else {})}).json()
        walked += [e["id"] for e in page["items"]]
        after = page["nextAfterId"]
        if after is None:
            break
    assert walked == everything and everything == sorted(everything, reverse=True)


def test_audit_since_and_until_bound_the_rows_by_time(app, db):
    """`since` is inclusive and `until` exclusive; a time without a zone is UTC."""
    import datetime
    with db.session() as s:
        s.add_all([AuditEntry(action="OLD", at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)),
                   AuditEntry(action="MID", at=datetime.datetime(2020, 6, 1, tzinfo=datetime.UTC))])
        s.commit()
    admin = login(app, "admin")
    actions = lambda **p: [e["action"] for e in admin.get("/api/admin/audit", params=p).json()["items"]]  # noqa: E731
    assert actions(until="2020-06-01T00:00:00Z") == ["OLD"]
    assert actions(since="2020-06-01T00:00:00", until="2021-01-01T00:00:00+00:00") == ["MID"]
    assert "OLD" not in actions(since="2020-02-01T00:00:00Z")


def test_the_audit_csv_export_is_admin_only_streamed_and_safe_to_open(app, db, monkeypatch):
    """An attachment with a header row and every matching row (read in batches), a formula-looking name defused, and the export itself audited."""
    import csv as csv_module
    import io
    from app import main as main_module
    monkeypatch.setattr(main_module, "AUDIT_CSV_BATCH", 2)
    TestClient(app).post("/api/login", json={"username": "=HYPERLINK(1)", "password": "x"})
    assert login(app, "operator").get("/api/admin/audit.csv").status_code == 403
    admin = login(app, "admin")
    resp = admin.get("/api/admin/audit.csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith('attachment; filename="smo-gui-audit-')
    rows = list(csv_module.reader(io.StringIO(resp.text)))
    assert rows[0] == ["id", "at", "username", "role", "action", "method", "path", "statusCode", "detail"]
    assert len(rows) - 1 == len(audit_rows(db)) and "'=HYPERLINK(1)" in [r[2] for r in rows[1:]]
    ids = [int(r[0]) for r in rows[1:]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == len(ids)
    assert audit_rows(db, "AUDIT_EXPORTED")[-1].username == "admin"
    only_logins = list(csv_module.reader(io.StringIO(admin.get("/api/admin/audit.csv", params={"action": "LOGIN"}).text)))[1:]
    assert only_logins and {r[4] for r in only_logins} == {"LOGIN"}


def test_the_audit_csv_export_stops_at_its_row_limit(app, monkeypatch):
    """The export never runs past AUDIT_CSV_MAX_ROWS rows, whatever the log holds."""
    from app import main as main_module
    monkeypatch.setattr(main_module, "AUDIT_CSV_MAX_ROWS", 3)
    monkeypatch.setattr(main_module, "AUDIT_CSV_BATCH", 2)
    admin = login(app, "admin")
    for i in range(4):
        admin.post("/api/admin/users", json={"username": f"m{i}", "password": "long-enough", "role": "viewer"})
    assert len(admin.get("/api/admin/audit.csv").text.strip().splitlines()) == 1 + 3
