"""PR-SEC-7: one-time codes (TOTP) for local accounts, recovery codes, the login second step, GUI_LOGIN_MODE, break-glass, GUI_ADMIN_MFA_REQUIRED,
and the admin actions (revoke a user's sessions, reset a user's one-time code).

Time is a fixture here: a code is good for one 30 s step and once, so a test that enrols and signs in straight away would be refused as a replay
unless it lets the clock move.
"""

import base64
import logging
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app import totp
from app.config import Settings
from app.db import AuditEntry, Database, GuiRecoveryCode, GuiUser, GuiUserTotp, LoginChallenge
from app.main import CHALLENGE_TTL_SECONDS, create_app, seed_users
from app.smo_client import R1Gateway
from test_main import PASSWORDS, R1, FakeSmo, audit_rows
from test_oidc import FakeIdp, make_cfg

KEY = "k" * 40


# ---------------------------------------------------------------- the algorithm (RFC 4226 / 6238)

RFC6238_SECRET = b"12345678901234567890"      # the SHA-1 test key of RFC 6238 appendix B


# The table is the SHA-1 test vectors of RFC 6238 appendix B: a time (seconds) and the 8-digit code the standard gives for it.
@pytest.mark.parametrize("at, expected", [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
                                          (2000000000, "69279037"), (20000000000, "65353130")])
def test_rfc6238_sha1_test_vectors(at, expected):
    assert totp.hotp(RFC6238_SECRET, at // 30, digits=8) == expected


def test_rfc4226_appendix_d_vectors():
    """The HOTP implementation reproduces the first four 6-digit values of RFC 4226 appendix D, so the code every authenticator app computes matches ours.
    """
    assert [totp.hotp(RFC6238_SECRET, n) for n in range(4)] == ["755224", "287082", "359152", "969429"]


SECRET = base64.b32encode(RFC6238_SECRET).decode().rstrip("=")


def test_window_is_one_step_either_side_of_now():
    """A code is accepted for the previous, current and next 30 s step and refused two steps away (clock drift is tolerated, but only a little).
    """
    now = 1_700_000_000.0
    for offset, accepted in [(-2, False), (-1, True), (0, True), (1, True), (2, False)]:
        code = totp.code_at(SECRET, now + offset * 30)
        assert (totp.verify(SECRET, code, now, None) is not None) is accepted, offset


def test_a_step_is_accepted_once():
    """Once a step has been used, the same code, or an older one, is refused, while the next step's code is still accepted (replay protection).
    """
    now = 1_700_000_000.0
    code = totp.code_at(SECRET, now)
    step = totp.verify(SECRET, code, now, None)
    assert step == totp.time_step(now)
    assert totp.verify(SECRET, code, now, step) is None                       # replay of the same code
    assert totp.verify(SECRET, totp.code_at(SECRET, now - 30), now, step) is None   # an older step than the last used one
    assert totp.verify(SECRET, totp.code_at(SECRET, now + 30), now, step) == step + 1


def test_malformed_codes_are_refused_without_error():
    """Wrong-length, non-digit, non-ASCII-digit and embedded-newline codes give None rather than an exception, and surrounding spaces are tolerated.
    """
    for code in ["", "12345", "1234567", "abcdef", "12 34 5", "１２３４５６", "123456\n7"]:
        assert totp.verify(SECRET, code, 1_700_000_000.0, None) is None
    assert totp.verify(SECRET, " " + totp.code_at(SECRET, 1_700_000_000.0) + " ", 1_700_000_000.0, None) is not None


def test_secret_is_160_bits_of_base32():
    """Each new secret is 32 base32 characters (160 bits) and different from the others."""
    secrets_seen = {totp.new_secret() for _ in range(20)}
    assert len(secrets_seen) == 20 and all(re.fullmatch(r"[A-Z2-7]{32}", s) for s in secrets_seen)


def test_provisioning_uri_follows_the_key_uri_format():
    """The otpauth:// URI percent-encodes the label and carries secret, issuer, digits, period and algorithm as the authenticator apps expect.
    """
    uri = totp.provisioning_uri("SMO Console", "ana@x", "ABCDEFGH")
    assert uri.startswith("otpauth://totp/SMO%20Console%3Aana%40x?")
    assert "secret=ABCDEFGH" in uri and "issuer=SMO%20Console" in uri and "digits=6" in uri and "period=30" in uri and "algorithm=SHA1" in uri


def test_the_secret_is_encrypted_and_bound_to_its_user_and_key():
    """A stored secret does not contain the plain secret, differs on every encryption, and decrypts only for the same user and key; tampering and unknown formats raise TotpKeyError.
    """
    sealed = totp.encrypt_secret(KEY, "ana", SECRET)
    assert SECRET not in sealed and sealed.startswith("v1:")
    assert totp.encrypt_secret(KEY, "ana", SECRET) != sealed                  # a fresh nonce each time
    assert totp.decrypt_secret(KEY, "ana", sealed) == SECRET
    for wrong_user, wrong_key, stored in [("bob", KEY, sealed), ("ana", "j" * 40, sealed), ("ana", KEY, sealed[:-4] + "AAAA"), ("ana", KEY, "plain"), ("ana", KEY, "v1:%%%")]:
        with pytest.raises(totp.TotpKeyError):
            totp.decrypt_secret(wrong_key, wrong_user, stored)
    with pytest.raises(totp.TotpKeyError):
        totp.encrypt_secret("", "ana", SECRET)


def test_recovery_codes_are_distinct_unambiguous_and_hash_by_user_and_key():
    """Recovery codes are ten distinct strings without look-alike characters, recognised however they are typed, and their hash depends on the user and the key and does not contain the code.
    """
    codes = totp.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r"[a-hj-km-np-z2-9]{4}(-[a-hj-km-np-z2-9]{4}){3}", c) for c in codes)
    code = codes[0]
    assert totp.looks_like_recovery_code(code) and totp.looks_like_recovery_code(code.upper().replace("-", " "))
    assert not totp.looks_like_recovery_code("123456")
    assert totp.hash_recovery_code(KEY, "ana", code) == totp.hash_recovery_code(KEY, "ana", code.upper().replace("-", ""))
    assert totp.hash_recovery_code(KEY, "ana", code) != totp.hash_recovery_code(KEY, "bob", code)
    assert totp.hash_recovery_code(KEY, "ana", code) != totp.hash_recovery_code("j" * 40, "ana", code)
    assert code.replace("-", "") not in totp.hash_recovery_code(KEY, "ana", code)


# ---------------------------------------------------------------- settings

def test_settings_read_the_mode_and_the_key(monkeypatch, tmp_path):
    """`Settings` reads the login mode, the admin-MFA switch and the one-time-code key (inline or from a file) and refuses both forms at once, an unreadable file and an unknown mode.
    """
    for var in ("GUI_LOGIN_MODE", "GUI_TOTP_KEY", "GUI_TOTP_KEY_FILE", "GUI_ADMIN_MFA_REQUIRED", "GUI_TOTP_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    cfg = Settings()
    assert (cfg.login_mode, cfg.admin_mfa_required, cfg.totp_key, cfg.totp_issuer) == ("both", False, "", "SMO Operator Console")
    keyfile = tmp_path / "key"
    keyfile.write_text(KEY + "\n")
    monkeypatch.setenv("GUI_TOTP_KEY_FILE", str(keyfile))
    monkeypatch.setenv("GUI_LOGIN_MODE", " OIDC ")
    monkeypatch.setenv("GUI_ADMIN_MFA_REQUIRED", "true")
    cfg = Settings()
    assert (cfg.login_mode, cfg.admin_mfa_required, cfg.totp_key) == ("oidc", True, KEY)
    monkeypatch.setenv("GUI_TOTP_KEY", KEY)
    with pytest.raises(ValueError, match="both GUI_TOTP_KEY and GUI_TOTP_KEY_FILE"):
        Settings()
    monkeypatch.delenv("GUI_TOTP_KEY")
    monkeypatch.setenv("GUI_TOTP_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="cannot be read"):
        Settings()
    monkeypatch.delenv("GUI_TOTP_KEY_FILE")
    monkeypatch.setenv("GUI_LOGIN_MODE", "sometimes")
    with pytest.raises(ValueError, match="not one of both, oidc, local"):
        Settings()


def build(db, smo=None, **over):
    """Builds a BFF app on `db` with the usual test settings plus the one-time-code key, and any `Settings` field in `over`; the users are seeded first.
    """
    base = dict(r1_url=R1, jwt_secret="test-secret", cookie_secure=False, admin_password=PASSWORDS["admin"], operator_password=PASSWORDS["operator"],
                viewer_password=PASSWORDS["viewer"], totp_key=KEY)
    base.update(over)
    cfg = Settings(**base)
    seed_users(db, cfg)
    return create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport((smo or FakeSmo()).handler)))


def test_start_up_refuses_what_would_lock_everyone_out_or_weaken_the_key(db):
    """`create_app` raises for the configurations that would leave nobody able to sign in or would accept a one-time-code key shorter than 32 characters.
    """
    with pytest.raises(ValueError, match="GUI_LOGIN_MODE=oidc needs GUI_OIDC_ENABLED"):
        build(db, login_mode="oidc")
    with pytest.raises(ValueError, match="GUI_ADMIN_MFA_REQUIRED=true needs GUI_TOTP_KEY"):
        build(db, admin_mfa_required=True, totp_key="")
    with pytest.raises(ValueError, match="at least 32 characters"):
        build(db, totp_key="short")
    with pytest.raises(ValueError, match="GUI_LOCAL_LOGIN_ENABLED=false needs"):
        build(db, login_mode="local", local_login_enabled=False)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def clock(monkeypatch):
    """A movable clock: patches `time.time` so a test can advance past a 30 s step (a code works once) or past the challenge lifetime; `advance(seconds)` moves it.
    """
    class Clock:
        offset = 0.0

        def advance(self, seconds):
            self.offset += seconds

    state = Clock()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + state.offset)
    return state


@pytest.fixture
def db():
    return Database("sqlite://")


@pytest.fixture
def app(db, clock):
    return build(db)


def password_login(app, username, **extra):
    """Posts the user's password and returns the client and the raw response, without asserting a status, so a test can see a challenge or a refusal.
    """
    client = TestClient(app)
    resp = client.post("/api/login", json={"username": username, "password": PASSWORDS[username]})
    return client, resp


def session(app, username):
    """Signs `username` in with the password (the account must have no one-time code) and returns a client with the CSRF header set.
    """
    client, resp = password_login(app, username)
    assert resp.status_code == 200 and "csrfToken" in resp.json(), resp.text
    client.headers["X-CSRF-Token"] = resp.json()["csrfToken"]
    return client


def current_code(secret):
    return totp.code_at(secret, time.time())


def enrol(app, clock, username):
    """Enrol `username` through the routes; returns (secret, recovery codes). The clock moves on, so the confirming code is behind us."""
    client = session(app, username)
    begun = client.post("/api/me/totp/begin")
    assert begun.status_code == 200, begun.text
    secret = begun.json()["secret"]
    confirmed = client.post("/api/me/totp/confirm", json={"code": current_code(secret)})
    assert confirmed.status_code == 200, confirmed.text
    clock.advance(30)
    return secret, confirmed.json()["recoveryCodes"]


def sign_in_with_code(app, clock, username, secret):
    """Signs in an enrolled account through both steps with the current code, returns the client with the CSRF header set, and moves the clock on so the next code is a new step.
    """
    client, resp = password_login(app, username)
    assert resp.json().get("mfaRequired") is True, resp.text
    done = client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)})
    assert done.status_code == 200, done.text
    client.headers["X-CSRF-Token"] = done.json()["csrfToken"]
    clock.advance(30)
    return client


def actions(db):
    return [r.action for r in audit_rows(db)]


# ---------------------------------------------------------------- enrolment (SEC-7.1)

def test_enrolment_is_refused_without_a_key(db, clock):
    """Without `GUI_TOTP_KEY` enrolment is 503 TOTP_UNAVAILABLE and the status says one-time codes are not available."""
    app = build(db, totp_key="")
    client = session(app, "viewer")
    resp = client.post("/api/me/totp/begin")
    assert resp.status_code == 503 and resp.json()["title"] == "TOTP_UNAVAILABLE"
    assert client.get("/api/me/totp").json() == {"available": False, "enrolled": False, "pending": False, "recoveryCodesLeft": 0, "recoveryCodes": []}


def test_a_secret_is_not_active_until_a_first_valid_code(app, db, clock):
    """A new secret stays pending (a password alone still signs in) until the user types one valid code, and only then are the recovery codes issued.
    """
    client = session(app, "viewer")
    begun = client.post("/api/me/totp/begin").json()
    assert begun["account"] == "viewer" and begun["issuer"] == "SMO Operator Console"
    assert begun["otpauthUri"].startswith("otpauth://totp/") and f"secret={begun['secret']}" in begun["otpauthUri"]
    assert client.get("/api/me/totp").json() == {"available": True, "enrolled": False, "pending": True, "recoveryCodesLeft": 0, "recoveryCodes": []}
    # still pending: a password alone signs in
    _, resp = password_login(app, "viewer")
    assert resp.status_code == 200 and "mfaRequired" not in resp.json()
    # a wrong code does not activate it
    assert client.post("/api/me/totp/confirm", json={"code": "000000"}).status_code == 400
    assert client.get("/api/me/totp").json()["enrolled"] is False
    ok = client.post("/api/me/totp/confirm", json={"code": current_code(begun["secret"])})
    assert ok.status_code == 200
    body = ok.json()
    assert len(body["recoveryCodes"]) == 10 and body["recoveryCodesLeft"] == 10
    assert client.get("/api/me/totp").json() == {"available": True, "enrolled": True, "pending": False, "recoveryCodesLeft": 10,
                                                "recoveryCodes": [{"slot": i, "used": False, "usedAt": None} for i in range(1, 11)]}
    assert client.get("/api/me").json()["totpEnrolled"] is True


def test_the_secret_is_stored_encrypted_and_the_recovery_codes_hashed(app, db, clock):
    """In the database the secret is ciphertext that decrypts under the key and the recovery codes are only their keyed hashes.
    """
    secret, codes = enrol(app, clock, "operator")
    with db.session() as s:
        row = s.get(GuiUserTotp, "operator")
        stored_codes = [r.code_hash for r in s.scalars(select(GuiRecoveryCode)).all()]
    assert row is not None and row.confirmed and secret not in row.secret_enc and secret.lower() not in row.secret_enc.lower()
    assert totp.decrypt_secret(KEY, "operator", row.secret_enc) == secret
    assert len(stored_codes) == 10
    for code in codes:
        assert not any(code.replace("-", "") in h or code in h for h in stored_codes)
        assert totp.hash_recovery_code(KEY, "operator", code) in stored_codes


def test_a_second_enrolment_needs_a_reset_and_a_new_begin_replaces_a_pending_one(app, clock):
    """Starting again replaces a pending secret (the old one no longer confirms), but an enrolled user must be reset by an admin first (409).
    """
    client = session(app, "viewer")
    first = client.post("/api/me/totp/begin").json()["secret"]
    second = client.post("/api/me/totp/begin").json()["secret"]
    assert first != second
    assert client.post("/api/me/totp/confirm", json={"code": totp.code_at(first, time.time())}).status_code == 400       # the replaced secret is gone
    assert client.post("/api/me/totp/confirm", json={"code": current_code(second)}).status_code == 200
    again = client.post("/api/me/totp/begin")
    assert again.status_code == 409 and again.json()["title"] == "TOTP_ALREADY_ENROLLED"


def test_confirm_without_begin_and_twice(app, clock):
    """Confirming with nothing pending, or a second time, is 409 NO_ENROLMENT_IN_PROGRESS."""
    client = session(app, "viewer")
    assert client.post("/api/me/totp/confirm", json={"code": "123456"}).json()["title"] == "NO_ENROLMENT_IN_PROGRESS"
    secret = client.post("/api/me/totp/begin").json()["secret"]
    client.post("/api/me/totp/confirm", json={"code": current_code(secret)})
    clock.advance(30)
    assert client.post("/api/me/totp/confirm", json={"code": current_code(secret)}).json()["title"] == "NO_ENROLMENT_IN_PROGRESS"


def test_wrong_confirm_codes_count_towards_the_lockout(app, db):
    """Wrong codes typed during enrolment count towards the same lockout as wrong passwords: the sixth try is 429 and the password login is locked too.
    """
    client = session(app, "viewer")
    client.post("/api/me/totp/begin")
    assert [client.post("/api/me/totp/confirm", json={"code": "000000"}).status_code for _ in range(6)] == [400] * 5 + [429]
    assert TestClient(app).post("/api/login", json={"username": "viewer", "password": PASSWORDS["viewer"]}).status_code == 429


def test_wrong_confirm_codes_lock_the_user_for_that_source_only(app, db):
    """SEC-15.11: wrong codes typed during enrolment count under the (source, user) pair: after five the source is refused (429), and the same user's password sign-in from another source is not locked."""
    client = session(app, "viewer")
    client.post("/api/me/totp/begin")
    client.headers["X-Forwarded-For"] = "203.0.113.7"
    assert [client.post("/api/me/totp/confirm", json={"code": "000000"}).status_code for _ in range(6)] == [400] * 5 + [429]
    attacker = TestClient(app).post("/api/login", json={"username": "viewer", "password": PASSWORDS["viewer"]}, headers={"X-Forwarded-For": "203.0.113.7"})
    elsewhere = TestClient(app).post("/api/login", json={"username": "viewer", "password": PASSWORDS["viewer"]}, headers={"X-Forwarded-For": "198.51.100.20"})
    assert (attacker.status_code, elsewhere.status_code) == (429, 200)


def test_enrolment_routes_need_a_session(app):
    """Status, begin and confirm are 401 without a session."""
    client = TestClient(app)
    assert client.get("/api/me/totp").status_code == 401
    assert client.post("/api/me/totp/begin").status_code == 401
    assert client.post("/api/me/totp/confirm", json={"code": "1"}).status_code == 401


def test_an_oidc_user_has_no_local_second_factor(app, db):
    """An identity-provider user is told one-time codes are not available, cannot start an enrolment (409) and is never asked to enrol.
    """
    from app.main import UNUSABLE_HASH
    from app.security import issue_jwt
    with db.session() as s:
        s.add(GuiUser(username="oidc:abc", password_hash=UNUSABLE_HASH, role="viewer", token_version=7))
        s.commit()
    token = issue_jwt({"sub": "oidc:abc", "ver": 7, "csrf": "c", "jti": "j"}, "test-secret", 300)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    assert client.get("/api/me/totp", headers=headers).json()["available"] is False
    assert client.post("/api/me/totp/begin", headers=headers).status_code == 409
    me = client.get("/api/me", headers=headers).json()
    assert me["local"] is False and me["totpEnrolled"] is False and me["mfaEnrolmentRequired"] is False


# ---------------------------------------------------------------- the second step (SEC-7.2)

def test_password_alone_makes_no_session_for_an_enrolled_account(app, db, clock):
    """For an enrolled account the password step returns only a challenge: no cookie, and the challenge is not accepted as a session, as cookie or Bearer token.
    """
    secret, _ = enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    body = resp.json()
    assert resp.status_code == 200 and body["mfaRequired"] is True and body["expiresIn"] == CHALLENGE_TTL_SECONDS == 300
    assert set(body) == {"mfaRequired", "challenge", "expiresIn"} and not resp.headers.get_list("set-cookie")
    assert client.get("/api/me").status_code == 401
    # the challenge is not a session, as a cookie or as a Bearer token
    assert TestClient(app).get("/api/me", headers={"Authorization": f"Bearer {body['challenge']}"}).status_code == 401
    forged = TestClient(app)
    forged.cookies.set("smo_session", body["challenge"], path="/api")
    assert forged.get("/api/me").status_code == 401
    assert actions(db).count("MFA_CHALLENGE") == 1 and actions(db).count("LOGIN") == 1       # the enrolment's own sign-in; the second one has not finished
    client.post("/api/login/totp", json={"challenge": body["challenge"], "code": current_code(secret)})


def test_the_right_code_completes_the_sign_in(app, db, clock):
    """The correct code with the challenge gives a working session and an audit row saying the sign-in used password and code."""
    secret, _ = enrol(app, clock, "operator")
    client = sign_in_with_code(app, clock, "operator", secret)
    assert client.get("/api/me").json()["username"] == "operator"
    assert client.get("/api/permissions").status_code == 200
    login_rows = [r for r in audit_rows(db, "LOGIN") if r.username == "operator"]
    assert login_rows and login_rows[-1].detail == "password+code"


def test_a_wrong_code_is_refused_and_counts_with_the_wrong_passwords(app, db, clock):
    """Wrong passwords and wrong codes share one budget of five: after them even the right code is 429, and the account unlocks after the lockout period.
    """
    secret, _ = enrol(app, clock, "operator")
    client = TestClient(app)
    for _ in range(3):
        assert client.post("/api/login", json={"username": "operator", "password": "nope"}).status_code == 401
    challenge = client.post("/api/login", json={"username": "operator", "password": PASSWORDS["operator"]}).json()["challenge"]
    wrong = {"challenge": challenge, "code": "000000" if current_code(secret) != "000000" else "111111"}
    assert client.post("/api/login/totp", json=wrong).status_code == 401
    assert client.post("/api/login/totp", json=wrong).status_code == 401
    # 3 wrong passwords + 2 wrong codes = 5: the account is locked, even for the right code and the right password
    resp = client.post("/api/login/totp", json={"challenge": challenge, "code": current_code(secret)})
    assert resp.status_code == 429 and resp.json()["title"] == "TOO_MANY_ATTEMPTS"
    assert client.post("/api/login", json={"username": "operator", "password": PASSWORDS["operator"]}).status_code == 429
    assert len(audit_rows(db, "LOGIN_FAILED")) == 5
    clock.advance(301)
    assert client.post("/api/login", json={"username": "operator", "password": PASSWORDS["operator"]}).json()["mfaRequired"] is True


def test_a_guesser_who_knows_the_password_does_not_get_a_fresh_budget_per_password_round(app, clock):
    """A correct password does not reset the failure counter, so repeating the password step does not give a new set of code guesses.
    """
    secret, _ = enrol(app, clock, "operator")
    client = TestClient(app)
    statuses = []
    for _ in range(4):
        challenge = client.post("/api/login", json={"username": "operator", "password": PASSWORDS["operator"]}).json().get("challenge")
        for _ in range(2):
            statuses.append(client.post("/api/login/totp", json={"challenge": challenge, "code": "000000"}).status_code
                            if challenge else 429)
    assert statuses.count(401) == 5 and 429 in statuses[5:]


def test_a_code_works_once(app, clock):
    """A one-time code that signed one session in is refused (INVALID_CODE) for a second sign-in in the same time step."""
    secret, _ = enrol(app, clock, "operator")
    code = current_code(secret)
    first, resp = password_login(app, "operator")
    assert first.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": code}).status_code == 200
    second, resp = password_login(app, "operator")
    refused = second.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": code})
    assert refused.status_code == 401 and refused.json()["title"] == "INVALID_CODE"


def test_the_challenge_expires_after_five_minutes(app, clock):
    """A challenge older than its five-minute lifetime is refused with CHALLENGE_INVALID even with a right code."""
    secret, _ = enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    clock.advance(CHALLENGE_TTL_SECONDS + 1)
    late = client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)})
    assert late.status_code == 401 and late.json()["title"] == "CHALLENGE_INVALID"


def test_the_challenge_is_single_use(app, clock):
    """Once a challenge has completed a sign-in it cannot be used again."""
    secret, _ = enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    challenge = resp.json()["challenge"]
    assert client.post("/api/login/totp", json={"challenge": challenge, "code": current_code(secret)}).status_code == 200
    clock.advance(30)
    again = client.post("/api/login/totp", json={"challenge": challenge, "code": current_code(secret)})
    assert again.status_code == 401 and again.json()["title"] == "CHALLENGE_INVALID"


def test_the_challenge_is_bound_to_its_user(app, db, clock):
    """A challenge whose payload was edited to name another user, garbage, or a challenge whose stored row belongs to someone else, is refused.
    """
    secret, _ = enrol(app, clock, "operator")
    other_secret, _ = enrol(app, clock, "viewer")
    _, resp = password_login(app, "operator")
    challenge = resp.json()["challenge"]
    header, payload, signature = challenge.split(".")
    swapped = base64.urlsafe_b64encode(base64.urlsafe_b64decode(payload + "==").replace(b'"operator"', b'"viewer"')).rstrip(b"=").decode()
    for bad in [f"{header}.{swapped}.{signature}", "garbage", "", challenge[:-3] + "AAA"]:
        refused = TestClient(app).post("/api/login/totp", json={"challenge": bad, "code": current_code(other_secret)})
        assert refused.status_code == 401 and refused.json()["title"] == "CHALLENGE_INVALID"
    # the row says whose it is, too
    with db.session() as s:
        row = s.scalars(select(LoginChallenge)).one()
        assert row.username == "operator"
        row.username = "viewer"
        s.commit()
    assert TestClient(app).post("/api/login/totp", json={"challenge": challenge, "code": current_code(secret)}).status_code == 401


def test_a_session_token_is_not_a_challenge(app, clock):
    """A session token presented as a login challenge is refused: the two are signed with different keys."""
    secret, _ = enrol(app, clock, "operator")
    client = sign_in_with_code(app, clock, "operator", secret)
    session_cookie = client.cookies.get("smo_session")
    refused = TestClient(app).post("/api/login/totp", json={"challenge": session_cookie, "code": "123456"})
    assert refused.status_code == 401 and refused.json()["title"] == "CHALLENGE_INVALID"


def test_a_password_change_or_a_deactivation_between_the_steps_ends_the_sign_in(app, clock):
    """Resetting the password or deactivating the account between the two steps invalidates the challenge in flight."""
    secret, _ = enrol(app, clock, "operator")
    admin = session(app, "admin")
    _, resp = password_login(app, "operator")
    admin.patch("/api/admin/users/operator", json={"password": "another-pass-1"})
    assert TestClient(app).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)}).status_code == 401
    admin.patch("/api/admin/users/operator", json={"active": False})
    admin.patch("/api/admin/users/operator", json={"active": True})
    again = TestClient(app).post("/api/login", json={"username": "operator", "password": "another-pass-1"})
    assert again.json()["mfaRequired"] is True


def test_a_wrong_code_on_a_sign_in_that_never_started(app):
    """A made-up challenge is 401 and a body without a code is 422."""
    assert TestClient(app).post("/api/login/totp", json={"challenge": "x.y.z", "code": "123456"}).status_code == 401
    assert TestClient(app).post("/api/login/totp", json={"challenge": "x"}).status_code == 422


def test_a_stored_secret_that_cannot_be_read_fails_closed(db, clock):
    """When the key is wrong or missing, an enrolled account gets 503 and no session, by form login and by token grant, rather than falling back to the password alone.
    """
    app = build(db)
    secret, _ = enrol(app, clock, "operator")
    other = build(db, totp_key="j" * 40)
    _, resp = password_login(other, "operator")
    assert resp.status_code == 200 and resp.json()["mfaRequired"] is True
    refused = TestClient(other).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)})
    assert refused.status_code == 503 and refused.json()["title"] == "TOTP_KEY_UNAVAILABLE"
    keyless = build(db, totp_key="")
    assert password_login(keyless, "operator")[1].status_code == 503      # no session without the second factor, whatever the reason
    assert TestClient(keyless).post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"], "otp": "123456"}).status_code == 503


# ---------------------------------------------------------------- recovery codes (SEC-7.3)

def test_a_recovery_code_signs_in_once_and_says_how_many_are_left(app, db, clock):
    """A recovery code (in any casing or spacing) signs in once, reports how many are left, is audited, and cannot be used again.
    """
    secret, codes = enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    done = client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": codes[0].upper()})
    assert done.status_code == 200 and done.json()["recoveryCodesLeft"] == 9
    assert [r.detail for r in audit_rows(db, "RECOVERY_CODE_USED")] == ["9 left"]
    again, resp = password_login(app, "operator")
    refused = again.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": codes[0]})
    assert refused.status_code == 401
    second = again.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": codes[1].replace("-", " ")})
    assert second.status_code == 200 and second.json()["recoveryCodesLeft"] == 8
    client.headers["X-CSRF-Token"] = done.json()["csrfToken"]
    assert client.get("/api/me/totp").json()["recoveryCodesLeft"] == 8
    assert secret


def test_the_status_says_which_recovery_code_slots_are_used_and_never_a_code(app, clock):
    """GUI-9.8: `recoveryCodes` lists slots 1..10 in the order the codes were shown, the spent one marked with its time; no code or hash is in it."""
    _, codes = enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    done = client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": codes[2]})
    assert done.status_code == 200
    client.headers["X-CSRF-Token"] = done.json()["csrfToken"]
    status = client.get("/api/me/totp")
    slots = status.json()["recoveryCodes"]
    assert [s["slot"] for s in slots] == list(range(1, 11)) and [s["used"] for s in slots] == [False, False, True] + [False] * 7
    assert slots[2]["usedAt"] and all(s["usedAt"] is None for i, s in enumerate(slots) if i != 2)
    assert not any(c.replace("-", "") in status.text.replace("-", "") for c in codes)


def test_a_wrong_recovery_code_counts_as_a_failure(app, clock):
    """Wrong recovery codes count towards the lockout like wrong one-time codes."""
    enrol(app, clock, "operator")
    client, resp = password_login(app, "operator")
    wrong = "abcd-efgh-jkmn-pqrs"
    results = [client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": wrong}).status_code for _ in range(6)]
    assert results == [401] * 5 + [429]


def test_new_recovery_codes_replace_the_old_ones_and_need_a_current_code(app, db, clock):
    """Regenerating recovery codes needs a current one-time code (not a recovery code), replaces all old codes, and is audited; a user without a secret gets 409.
    """
    secret, old = enrol(app, clock, "operator")
    client = sign_in_with_code(app, clock, "operator", secret)
    assert client.post("/api/me/totp/recovery-codes", json={"code": "000000"}).status_code == 400
    assert client.post("/api/me/totp/recovery-codes", json={"code": old[0]}).status_code == 400          # a recovery code does not authorise new ones
    fresh = client.post("/api/me/totp/recovery-codes", json={"code": current_code(secret)})
    assert fresh.status_code == 200 and len(fresh.json()["recoveryCodes"]) == 10
    new = fresh.json()["recoveryCodes"]
    _, resp = password_login(app, "operator")
    assert TestClient(app).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": old[1]}).status_code == 401
    assert TestClient(app).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": new[0]}).status_code == 200
    assert "RECOVERY_CODES_REGENERATED" in actions(db)
    assert session(app, "viewer").post("/api/me/totp/recovery-codes", json={"code": "123456"}).status_code == 409    # not enrolled


# ---------------------------------------------------------------- the password grant is not a way round

def test_the_token_grant_asks_an_enrolled_account_for_its_code(app, clock):
    """The password grant for an enrolled account needs the `otp` field (a one-time or recovery code), so it is not a way round the second factor; accounts without a code are unchanged.
    """
    secret, codes = enrol(app, clock, "operator")
    form = {"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"]}
    client = TestClient(app)
    missing = client.post("/api/token", data=form)
    assert missing.status_code == 400 and "otp" in missing.json()["error_description"]
    assert client.post("/api/token", data={**form, "otp": "000000"}).status_code == 400
    ok = client.post("/api/token", data={**form, "otp": current_code(secret)})
    assert ok.status_code == 200
    bearer = {"Authorization": f"Bearer {ok.json()['access_token']}"}
    assert client.get("/api/me", headers=bearer).json()["username"] == "operator"
    assert client.post("/api/token", data={**form, "otp": codes[0]}).status_code == 200
    assert client.post("/api/token", data={**form, "otp": codes[0]}).status_code == 400
    # an account without a code is unchanged
    assert client.post("/api/token", data={**form, "username": "viewer", "password": PASSWORDS["viewer"]}).status_code == 200


# ---------------------------------------------------------------- GUI_LOGIN_MODE (SEC-7.6)

@pytest.fixture
def oidc_app(db, clock):
    """A factory for a BFF app with OIDC enabled against `FakeIdp` and the one-time-code key set; `make(**over)` changes settings such as the login mode.
    """
    def make(**over):
        cfg = make_cfg(totp_key=KEY, **over)
        seed_users(db, cfg)
        idp = FakeIdp()
        return create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(FakeSmo().handler)), oidc_transport=httpx.MockTransport(idp.handler))
    return make


def test_mode_oidc_closes_the_password_login_to_every_account_but_break_glass(oidc_app, db):
    """With `GUI_LOGIN_MODE=oidc` a correct password is refused with 403 LOGIN_MODE_OIDC_ONLY (form and token grant), wrong passwords still give 401, and OIDC itself still works.
    """
    app = oidc_app(login_mode="oidc")
    config = TestClient(app).get("/api/auth/config").json()
    assert config == {"localLogin": False, "loginMode": "oidc", "breakGlass": True, "oidc": {"enabled": True, "providerName": "Keycloak", "loginUrl": "/api/oidc/login"}}
    refused = TestClient(app).post("/api/login", json={"username": "admin", "password": PASSWORDS["admin"]})
    assert refused.status_code == 403 and refused.json()["title"] == "LOGIN_MODE_OIDC_ONLY" and "Keycloak" in refused.json()["detail"]
    assert not refused.headers.get_list("set-cookie")
    assert TestClient(app).post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401      # same answer as ever for a wrong password
    assert TestClient(app).post("/api/login", json={"username": "nobody", "password": "wrong"}).status_code == 401
    token = TestClient(app).post("/api/token", data={"grant_type": "password", "username": "admin", "password": PASSWORDS["admin"]})
    assert token.status_code == 403
    assert [r.detail for r in audit_rows(db, "LOGIN_REFUSED")][0].startswith("GUI_LOGIN_MODE=oidc")
    assert TestClient(app, follow_redirects=False).get("/api/oidc/login").status_code == 302       # OIDC itself works


def test_mode_oidc_without_local_login_has_no_break_glass(oidc_app):
    """With the local form switched off as well, the sign-in page is told there is no break-glass way in and every password login is refused.
    """
    app = oidc_app(login_mode="oidc", local_login_enabled=False)
    config = TestClient(app).get("/api/auth/config").json()
    assert config["localLogin"] is False and config["breakGlass"] is False
    assert TestClient(app).post("/api/login", json={"username": "admin", "password": PASSWORDS["admin"]}).status_code == 403


def test_mode_local_does_not_offer_oidc_even_when_it_is_configured(oidc_app):
    """With `GUI_LOGIN_MODE=local` OIDC is not offered and its login route is 404 although the provider is configured."""
    app = oidc_app(login_mode="local")
    config = TestClient(app).get("/api/auth/config").json()
    assert config == {"localLogin": True, "loginMode": "local", "breakGlass": False, "oidc": {"enabled": False}}
    assert TestClient(app, follow_redirects=False).get("/api/oidc/login").status_code == 404
    assert TestClient(app).post("/api/login", json={"username": "admin", "password": PASSWORDS["admin"]}).status_code == 200


def test_mode_local_ignores_a_provider_that_is_half_configured(db):
    """In local mode an incomplete provider setting cannot stop the start, because the OIDC client is not built."""
    cfg = make_cfg(login_mode="local", oidc_issuer="")
    seed_users(db, cfg)
    create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(FakeSmo().handler)))


def test_mode_both_is_the_default_and_changes_nothing(app):
    """With no mode set the sign-in page offers the local form and no provider."""
    assert TestClient(app).get("/api/auth/config").json() == {"localLogin": True, "loginMode": "both", "breakGlass": False, "oidc": {"enabled": False}}


# ---------------------------------------------------------------- break-glass (SEC-7.7)

def flag_break_glass(app, username="admin", on=True):
    """Has the admin set or clear the break-glass flag on `username` through the admin route and returns the user view."""
    admin = session(app, "admin")
    resp = admin.patch(f"/api/admin/users/{username}", json={"breakGlass": on})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_break_glass_account_without_a_code_cannot_sign_in(app, db, clock):
    """A break-glass account that has no one-time code enrolled is refused (403 BREAK_GLASS_NEEDS_TOTP) by form login and token grant, and the refusal is audited.
    """
    view = flag_break_glass(app, "operator")
    assert view["breakGlass"] is True and view["totpEnrolled"] is False
    _, resp = password_login(app, "operator")
    assert resp.status_code == 403 and resp.json()["title"] == "BREAK_GLASS_NEEDS_TOTP" and not resp.headers.get_list("set-cookie")
    assert TestClient(app).post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"]}).status_code == 403
    assert [r.detail for r in audit_rows(db, "LOGIN_REFUSED")][0] == "break-glass account without an enrolled one-time code"


def test_break_glass_signs_in_with_password_and_code_when_the_mode_is_oidc(oidc_app, db, clock, caplog):
    """A flagged account signs in with password and code even when the mode is oidc; the sign-in is audited as BREAK_GLASS_LOGIN and logged as a warning, and unflagged accounts stay refused.
    """
    app = oidc_app(login_mode="oidc")
    # the flag is set by an admin: the admin's own session comes from the token grant of a break-glass admin, so set the flag directly the first time
    with db.session() as s:
        s.get(GuiUser, "operator").break_glass = True
        s.commit()
    secret, _ = enrol_direct(app, db, "operator", clock)
    caplog.set_level(logging.WARNING, logger="smo-gui-bff")
    client, resp = password_login(app, "operator")
    assert resp.json()["mfaRequired"] is True
    done = client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)})
    assert done.status_code == 200
    client.headers["X-CSRF-Token"] = done.json()["csrfToken"]
    assert client.get("/api/me").json()["username"] == "operator"
    rows = audit_rows(db, "BREAK_GLASS_LOGIN")
    assert [(r.username, r.detail) for r in rows] == [("operator", "password+code")]
    assert audit_rows(db, "LOGIN") == []                                   # not an ordinary sign-in
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "break-glass sign-in" in r.getMessage()]
    assert len(warnings) == 1 and "operator" in warnings[0].getMessage()
    # the same account, another operator who is not flagged, stays refused
    assert TestClient(app).post("/api/login", json={"username": "viewer", "password": PASSWORDS["viewer"]}).status_code == 403


def enrol_direct(app, db, username, clock):
    """Enrol through the model, for an account the mode does not let sign in (its session comes from a signed token instead)."""
    from app.security import issue_jwt
    with db.session() as s:
        user = s.get(GuiUser, username)
        token = issue_jwt({"sub": username, "ver": user.token_version, "csrf": "c", "jti": f"enrol-{username}"}, "test-secret", 300)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    secret = client.post("/api/me/totp/begin", headers=headers).json()["secret"]
    codes = client.post("/api/me/totp/confirm", json={"code": current_code(secret)}, headers=headers).json()["recoveryCodes"]
    clock.advance(30)
    return secret, codes


def test_a_break_glass_sign_in_by_recovery_code_and_by_token_grant_is_audited_too(oidc_app, db, clock):
    """Break-glass sign-ins by token grant and by recovery code are audited as BREAK_GLASS_LOGIN, and the recovery code use is audited.
    """
    app = oidc_app(login_mode="oidc")
    with db.session() as s:
        s.get(GuiUser, "admin").break_glass = True
        s.commit()
    secret, codes = enrol_direct(app, db, "admin", clock)
    form = {"grant_type": "password", "username": "admin", "password": PASSWORDS["admin"]}
    assert TestClient(app).post("/api/token", data=form).status_code == 400
    assert TestClient(app).post("/api/token", data={**form, "otp": current_code(secret)}).status_code == 200
    client, resp = password_login(app, "admin")
    assert client.post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": codes[0]}).status_code == 200
    assert [r.detail for r in audit_rows(db, "BREAK_GLASS_LOGIN")] == ["token grant", "recovery code"]
    assert [r.detail for r in audit_rows(db, "RECOVERY_CODE_USED")] == ["9 left"]


def test_the_flag_is_dropped_between_the_steps(oidc_app, db, clock):
    """If the break-glass flag is removed between the password and the code step, the second step is refused under the oidc mode.
    """
    app = oidc_app(login_mode="oidc")
    with db.session() as s:
        s.get(GuiUser, "admin").break_glass = True
        s.commit()
    secret, _ = enrol_direct(app, db, "admin", clock)
    _, resp = password_login(app, "admin")
    with db.session() as s:
        s.get(GuiUser, "admin").break_glass = False
        s.commit()
    refused = TestClient(app).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)})
    assert refused.status_code == 403 and refused.json()["title"] == "LOGIN_MODE_OIDC_ONLY"


def test_the_break_glass_flag_belongs_to_local_accounts_and_shows_in_the_user_list(app, db):
    """The flag cannot be set on an identity-provider user, shows in the user list, and each change is audited."""
    from app.main import UNUSABLE_HASH
    with db.session() as s:
        s.add(GuiUser(username="oidc:abc", password_hash=UNUSABLE_HASH, role="viewer"))
        s.commit()
    admin = session(app, "admin")
    assert admin.patch("/api/admin/users/oidc:abc", json={"breakGlass": True}).json()["title"] == "OIDC_USER"
    assert admin.patch("/api/admin/users/viewer", json={"breakGlass": True}).json()["breakGlass"] is True
    users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
    assert users["viewer"]["breakGlass"] is True and users["admin"]["breakGlass"] is False and users["admin"]["totpEnrolled"] is False
    assert admin.patch("/api/admin/users/viewer", json={"breakGlass": False}).json()["breakGlass"] is False
    assert [r.detail for r in audit_rows(db, "USER_UPDATED")] == ["viewer: break-glass on", "viewer: break-glass off"]


# ---------------------------------------------------------------- GUI_ADMIN_MFA_REQUIRED (SEC-7.8)

def test_an_admin_without_a_code_reaches_only_enrolment(db, clock):
    """With `GUI_ADMIN_MFA_REQUIRED` an admin with no one-time code gets 403 MFA_ENROLMENT_REQUIRED on every route except the enrolment ones, and is let in after enrolling.
    """
    app = build(db, admin_mfa_required=True)
    admin = session(app, "admin")
    me = admin.get("/api/me")
    assert me.status_code == 200 and me.json()["mfaEnrolmentRequired"] is True
    assert admin.get("/api/me/totp").status_code == 200
    for method, path in [("GET", "/api/admin/users"), ("GET", "/api/admin/audit"), ("GET", "/api/permissions"), ("GET", "/api/modules/status"),
                         ("GET", "/api/smo/sme/health"), ("POST", "/api/me/password"), ("POST", "/api/me/totp/recovery-codes")]:
        resp = admin.request(method, path, json={} if method == "POST" else None)
        assert resp.status_code == 403 and resp.json()["detail"]["title"] == "MFA_ENROLMENT_REQUIRED", (method, path)
    secret = admin.post("/api/me/totp/begin").json()["secret"]
    assert admin.post("/api/me/totp/confirm", json={"code": current_code(secret)}).status_code == 200
    assert admin.get("/api/admin/users").status_code == 200
    assert admin.get("/api/me").json()["mfaEnrolmentRequired"] is False


def test_the_gate_is_for_local_admins_only(db, clock):
    """The enrolment gate does not apply to operators or to identity-provider users, whose provider asks for the second factor.
    """
    app = build(db, admin_mfa_required=True)
    # the other roles are not asked ...
    operator = session(app, "operator")
    assert operator.get("/api/me").json()["mfaEnrolmentRequired"] is False
    assert operator.get("/api/permissions").status_code == 200
    # ... and neither is a user of the identity provider, who has no local factor at all
    from app.main import UNUSABLE_HASH
    from app.security import issue_jwt
    with db.session() as s:
        s.add(GuiUser(username="oidc:boss", password_hash=UNUSABLE_HASH, role="admin", token_version=3))
        s.commit()
    token = issue_jwt({"sub": "oidc:boss", "ver": 3, "csrf": "c", "jti": "j"}, "test-secret", 300)
    assert TestClient(app).get("/api/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_the_gate_is_off_by_default(app):
    """Without the switch an admin needs no one-time code."""
    admin = session(app, "admin")
    assert admin.get("/api/me").json()["mfaEnrolmentRequired"] is False
    assert admin.get("/api/admin/users").status_code == 200


# ---------------------------------------------------------------- admin actions (SEC-7.5)

def test_an_admin_ends_every_session_of_a_user(app, db):
    """Revoking a user's sessions ends all their cookie sessions and tokens at once, leaves other users alone, lets the user sign in again, is audited, and is 404 for an unknown user.
    """
    first, second = session(app, "operator"), session(app, "operator")
    bearer = TestClient(app).post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"]}).json()["access_token"]
    other = session(app, "viewer")
    admin = session(app, "admin")
    resp = admin.post("/api/admin/users/operator/revoke-sessions")
    assert resp.status_code == 200 and resp.json() == {"status": "sessions revoked", "username": "operator"}
    for client in (first, second):
        gone = client.get("/api/me")
        assert gone.status_code == 401 and gone.json()["detail"]["title"] == "SESSION_REVOKED"
    assert TestClient(app).get("/api/me", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401
    assert other.get("/api/me").status_code == 200 and admin.get("/api/me").status_code == 200       # nobody else
    assert session(app, "operator").get("/api/me").status_code == 200                                  # the user can sign in again
    assert [(r.username, r.detail) for r in audit_rows(db, "USER_SESSIONS_REVOKED")] == [("admin", "operator")]
    assert admin.post("/api/admin/users/nobody/revoke-sessions").status_code == 404


def test_revoking_ends_a_sign_in_half_done(app, clock):
    """A challenge issued before the revocation can no longer finish a sign-in."""
    secret, _ = enrol(app, clock, "operator")
    _, resp = password_login(app, "operator")
    session(app, "admin").post("/api/admin/users/operator/revoke-sessions")
    assert TestClient(app).post("/api/login/totp", json={"challenge": resp.json()["challenge"], "code": current_code(secret)}).status_code == 401


def test_an_admin_resets_a_lost_device(app, db, clock):
    """Resetting removes the secret, recovery codes and open challenges, the user then signs in with the password alone, the reset is audited and idempotent, and a new enrolment is possible.
    """
    secret, codes = enrol(app, clock, "operator")
    assert password_login(app, "operator")[1].json()["mfaRequired"] is True
    admin = session(app, "admin")
    resp = admin.post("/api/admin/users/operator/reset-totp")
    assert resp.status_code == 200 and resp.json()["status"] == "one-time code removed"
    with db.session() as s:
        assert s.get(GuiUserTotp, "operator") is None and s.scalars(select(GuiRecoveryCode)).all() == [] and s.scalars(select(LoginChallenge)).all() == []
    assert "mfaRequired" not in password_login(app, "operator")[1].json()
    assert admin.get("/api/admin/users").json()[1]["totpEnrolled"] is False
    assert [r.detail for r in audit_rows(db, "TOTP_RESET")] == ["operator: removed"]
    assert admin.post("/api/admin/users/operator/reset-totp").json()["status"] == "no one-time code was set"
    assert admin.post("/api/admin/users/nobody/reset-totp").status_code == 404
    # and a new one can be enrolled
    new_secret, _ = enrol(app, clock, "operator")
    assert new_secret != secret and codes


def test_deleting_a_user_deletes_the_secret_and_the_recovery_codes(app, db, clock):
    """Deleting a user also deletes their one-time-code secret and recovery codes."""
    enrol(app, clock, "operator")
    assert session(app, "admin").delete("/api/admin/users/operator").status_code == 204
    with db.session() as s:
        assert s.get(GuiUserTotp, "operator") is None and s.scalars(select(GuiRecoveryCode)).all() == []


def test_every_admin_route_is_admin_only(app):
    """The generated check (as test_rbac_matrix does for the proxy table): each route under /api/admin, whatever its method, refuses no session and every lower role."""
    routes = [(m, r.path) for r in app.routes if getattr(r, "path", "").startswith("/api/admin") for m in sorted(r.methods - {"HEAD", "OPTIONS"})]
    paths = {p for _, p in routes}
    assert {"/api/admin/users/{username}/revoke-sessions", "/api/admin/users/{username}/reset-totp", "/api/admin/users", "/api/admin/audit"} <= paths
    clients = {role: session(app, role) for role in ("viewer", "operator")}
    for method, path in routes:
        concrete = re.sub(r"\{[^}]+\}", "x1", path)
        assert TestClient(app).request(method, concrete, json={}).status_code == 401, (method, path)
        for role, client in clients.items():
            resp = client.request(method, concrete, json={})
            assert resp.status_code == 403, (role, method, path, resp.text)


def test_the_enrolment_routes_are_for_any_signed_in_user_and_only_them(app):
    """The one-time-code routes are exactly status, begin, confirm and new recovery codes, so a new route there must be a deliberate change (they are open to any signed-in role).
    """
    paths = {(m, r.path) for r in app.routes if getattr(r, "path", "").startswith("/api/me/totp") for m in r.methods}
    assert paths == {("GET", "/api/me/totp"), ("POST", "/api/me/totp/begin"), ("POST", "/api/me/totp/confirm"), ("POST", "/api/me/totp/recovery-codes")}


# ---------------------------------------------------------------- nothing secret is written down

def test_no_secret_code_or_recovery_code_reaches_the_log_or_the_audit_trail(db, clock, caplog):
    """After enrolment, sign-ins with a code, a recovery code and the token grant, neither the log nor the audit rows contain any secret, code, challenge, key or password.
    """
    caplog.set_level(logging.DEBUG)
    app = build(db)
    client = session(app, "operator")
    begun = client.post("/api/me/totp/begin").json()
    secret = begun["secret"]
    code = current_code(secret)
    confirmed = client.post("/api/me/totp/confirm", json={"code": code}).json()
    codes = confirmed["recoveryCodes"]
    clock.advance(30)
    client2, resp = password_login(app, "operator")
    challenge = resp.json()["challenge"]
    login_code = current_code(secret)
    client2.post("/api/login/totp", json={"challenge": challenge, "code": "000000"})
    client2.post("/api/login/totp", json={"challenge": challenge, "code": login_code})
    again, resp2 = password_login(app, "operator")
    again.post("/api/login/totp", json={"challenge": resp2.json()["challenge"], "code": codes[0]})
    TestClient(app).post("/api/token", data={"grant_type": "password", "username": "operator", "password": PASSWORDS["operator"], "otp": codes[1]})
    session(app, "admin").post("/api/admin/users/operator/reset-totp")
    forbidden = [secret, code, login_code, codes[0], codes[1], codes[0].replace("-", ""), challenge, begun["otpauthUri"], KEY, PASSWORDS["operator"]]
    log_text = caplog.text
    with db.session() as s:
        audit_text = "\n".join(" ".join(str(v) for v in (r.username, r.role, r.action, r.method, r.path, r.detail)) for r in s.scalars(select(AuditEntry)).all())
    assert "RECOVERY_CODE_USED" in audit_text and "MFA_CHALLENGE" in audit_text
    for value in forbidden:
        assert value not in log_text, value
        assert value not in audit_text, value


# ---------------------------------------------------------------- the BFF's own database gains the column

def test_a_database_made_before_gets_the_break_glass_column(tmp_path):
    """An older BFF database without `break_glass` gets the column added at start (defaulting to false), a second start is harmless, and the previous release's insert still works.
    """
    url = f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE gui_user (username VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL, role VARCHAR NOT NULL, "
                          "active BOOLEAN NOT NULL, token_version INTEGER NOT NULL, created_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO gui_user VALUES ('old', 'x', 'viewer', 1, 0, '2026-01-01 00:00:00')"))
    engine.dispose()
    db = Database(url)
    with db.session() as s:
        assert s.get(GuiUser, "old").break_glass is False
    Database(url)                                  # a second start, or a second instance, finds it there
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT break_glass FROM gui_user")).scalar() in (0, False)
    # the previous release's insert (no such column) still works on the upgraded table
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO gui_user (username, password_hash, role, active, token_version, created_at) VALUES ('older', 'x', 'viewer', 1, 0, '2026-01-01 00:00:00')"))


def test_challenges_are_cleaned_up_and_spent_once(db):
    """Writing a challenge removes expired ones, a challenge is pending only for its user and until it expires, and consuming it works exactly once.
    """
    db.create_challenge("a", "ana", 100.0, 0.0)
    db.create_challenge("b", "bob", 500.0, 200.0)         # writing one removes the expired
    assert not db.challenge_pending("a", "ana", 50.0)
    assert db.challenge_pending("b", "bob", 300.0) and not db.challenge_pending("b", "ana", 300.0) and not db.challenge_pending("b", "bob", 600.0)
    assert db.consume_challenge("b", "bob", 300.0) and not db.consume_challenge("b", "bob", 300.0)
