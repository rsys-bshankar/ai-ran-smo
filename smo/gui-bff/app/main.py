"""SMO Operator GUI — Backend-for-Frontend.

The only API the browser ever talks to. It holds GUI users and roles, issues
the session JWT, checks every call against the RBAC table (rbac.py), and
forwards allowed calls to R1 Termination with the BFF's own SME-issued
OAuth2 token (smo_client.py). The browser never calls R1 or any module port
directly: that would need CORS on every module and bypass the role checks.

Routes (all under /api, which nginx forwards here unchanged, except /.well-known/jwks.json, which nginx forwards as one exact path):
  GET  /.well-known/jwks.json   the public keys that verify session tokens (empty under HS256); no session needed          (PR-SEC-5)
  GET  /api/auth/config         what the sign-in page may offer: local login, OIDC and the provider's name (no session needed)
  POST /api/login               username/password -> httpOnly session cookie; for an account with a one-time code, a challenge instead (PR-SEC-7)
  POST /api/login/totp          the challenge and a one-time code (or a recovery code) -> the session cookie
  GET  /api/oidc/login          start an OIDC sign-in (authorization code + PKCE): redirect to the provider        (PR-SEC-6)
  GET  /api/oidc/callback       finish it: validate the ID token, create/update the user, set the session cookie, redirect to the GUI
  POST /api/token               OAuth2 password grant -> Bearer JWT (scripts/CLI)
  POST /api/logout              ends the session; an OIDC user also gets the provider's end-session URL when it has one
  GET  /api/me                  current user, role, CSRF token
  POST /api/me/password
  GET|POST /api/me/totp[...]    one-time-code enrolment: status (with which recovery-code slots are used), begin, confirm (recovery codes shown once), new recovery codes (PR-SEC-7)
  GET  /api/me/sign-ins         the caller's own recent sign-ins, failed sign-ins and sign-outs, from the audit log (GUI-9.8)
  GET  /api/permissions         the RBAC table, so the SPA gates on the same rules
  GET  /api/modules/status      every module's health, readiness and build version via R1, probed in parallel
  *    /api/smo/{module}/...    RBAC-checked proxy to R1 Termination
  GET  /api/rapps[/{instance}]  the rApp directory, and one rApp with the operator page its package declares (rapps.py)
  *    /api/rapps/{instance}/operator/...  a call to the rApp's operator API, only for the routes that declaration lists; changes audited
  GET|PUT|DELETE /api/me/pins   the rApps the user pinned to the sidebar (at most 5)
  GET|PUT /api/me/preferences   the user's console preferences: theme, text size, accent, start page, rows per page… (preferences.py)
  GET  /api/summary/{page}      the true counts behind one console page's tiles and badges, cached 5 s and shared, optionally scoped to a region / site cluster (summary.py, GUI-9.3)
  GET  /api/summary/attention   the Dashboard's "Needs your attention" groups in one call (summary.py, GUI-9.8b)
  GET  /api/events              Server-Sent Events: the summary counts of up to four pages (or the attention groups), pushed when they change (events.py, GUI-9.1)
  POST|GET|DELETE /api/exports[/{id}[/file]]  asynchronous CSV export jobs of decision records (operator) and the audit log (admin) (exports.py, GUI-9.5b)
  GET  /api/search              the ⌘K typeahead over elements, rApps, alarms, models and decisions (search.py, GUI-9.2)
  /api/admin/users[...]         user + role CRUD, break-glass flag, revoke a user's sessions, reset a user's one-time code (admin); the list has last-active times
  GET  /api/admin/audit         the append-only audit log, offset or keyset (`after_id`) pages, `since`/`until` (admin)
  GET  /api/admin/audit.csv     the same rows as a streamed CSV download, at most 1,000,000 (admin, GUI-9.5)
"""

import asyncio
import csv
import datetime
import hashlib
import io
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Iterator, cast
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .config import Settings, settings as default_settings
from . import events, exports, preferences, rapps, search, summary, totp
from .db import AuditEntry, Database, GuiUser, LoginFailure
from .oidc import LOGIN_TTL_SECONDS, MAX_PENDING_LOGINS, OidcClient, OidcConfig, OidcError
from .rbac import MODULES, RULES, Role, Rule, User, decide
from .security import decode_jwt, hash_password, issue_jwt, verify_password
from .signing import build_signer
from .smo_client import ACTING_USER_HEADER, R1Gateway, SmoAuthError

log = logging.getLogger("smo-gui-bff")

SESSION_COOKIE = "smo_session"
CSRF_COOKIE = "smo_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")

# PR-SEC-6: a user the identity provider vouches for is the row `oidc:<subject>`. USERNAME_RE has no ':', so no local account can take such a name,
# and the stored "hash" can never verify (no salt:digest in it), so the row has no password at all.
OIDC_PREFIX = "oidc:"
UNUSABLE_HASH = "!"
# PR-SEC-7: the second step of a sign-in. The challenge is a token signed with a key derived from the session key (so it is never a session), valid five
# minutes, and spent by the first correct code.
CHALLENGE_TTL_SECONDS = 300
CHALLENGE_USE = "login-totp"
MIN_TOTP_KEY_LENGTH = 32
# With GUI_ADMIN_MFA_REQUIRED, a local admin without an enrolled one-time code reaches only these (the SPA sends them to enrolment).
MFA_OPEN_PATHS = frozenset({"/api/me", "/api/me/totp", "/api/me/totp/begin", "/api/me/totp/confirm"})
OIDC_COOKIE = "smo_oidc"      # the browser binding of a sign-in in flight; Lax because the provider's redirect back is a cross-site navigation

# GUI-9.8: the audit actions that are a sign-in, a failed one or a sign-out (what `GET /api/me/sign-ins` lists), and of those the successful
# sign-ins (what `lastSignInAt` in the user list is the newest of). A token grant (`TOKEN`) is a sign-in by a script.
SIGN_IN_ACTIONS = ("LOGIN", "OIDC_LOGIN", "BREAK_GLASS_LOGIN", "TOKEN")
SIGN_IN_HISTORY_ACTIONS = (*SIGN_IN_ACTIONS, "LOGIN_FAILED", "OIDC_LOGIN_FAILED", "LOGIN_LOCKED", "LOGOUT")
# GUI-9.5: the CSV export of the audit log stops after this many rows, read from the database this many at a time.
AUDIT_CSV_MAX_ROWS = 1_000_000
AUDIT_CSV_BATCH = 1000
AUDIT_CSV_COLUMNS = ("id", "at", "username", "role", "action", "method", "path", "statusCode", "detail")
# GUI-10.4: every audit action the BFF writes (the first argument of each `audit(...)` call in app/), served by `GET /api/admin/audit/actions`
# for the console's action filter. tests/test_main.py scans the source and fails when a call writes an action missing here, or one here is no
# longer written.
AUDIT_ACTIONS = ("AUDIT_EXPORTED", "BREAK_GLASS_LOGIN", "DENIED", "EXPORT_DELETED", "EXPORT_DOWNLOADED", "EXPORT_REQUESTED", "LOGIN", "LOGIN_FAILED",
                 "LOGIN_LOCKED", "LOGIN_REFUSED", "LOGOUT", "MFA_CHALLENGE", "OIDC_LOGIN", "OIDC_LOGIN_FAILED", "PASSWORD_CHANGED", "PROXY", "RAPP_ACTION",
                 "RECOVERY_CODES_REGENERATED", "RECOVERY_CODE_USED", "TOKEN", "TOTP_ENROLLED", "TOTP_ENROL_STARTED", "TOTP_RESET", "USER_CREATED",
                 "USER_DELETED", "USER_SESSIONS_REVOKED", "USER_UPDATED")

# RFC 7230 section 6.1 hop-by-hop headers, plus headers the BFF must own
# itself: lengths/encodings are recomputed (httpx has already decoded the
# body), and neither the browser's GUI credentials nor an upstream
# Set-Cookie may ever cross the proxy. The same lesson R1 Termination's own
# proxy learned with Host.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade"}
_NEVER_FORWARD_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding", "set-cookie", "server", "date"}
_FORWARD_REQUEST = {"content-type", "accept"}

# Everything the BFF itself serves is JSON: nothing may frame, sniff or run it.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# Display order of the health grid: R1 first (every other probe goes through it).
STATUS_MODULES = ["r1-termination", *MODULES]

# Wave 3 (cross-cutting standardization) — Pagination. Every SMO backend
# module shares shared/smo_shared/pagination.py's paginate() for this same
# {items, total, limit, offset} shape; gui-bff can't import it — its own
# CI job (.github/workflows/smo-tests.yml's "Operator GUI BFF tests")
# deliberately never installs smo_shared, unlike every backend module's
# job, so gui-bff keeps its own small local conventions instead (same
# reason it already has its own _problem()/_problem_exception() rather
# than smo_shared.errors). One route here needs it, so it's inlined
# rather than requiring smo_shared just for this.
class _PageSize(int):
    """`limit` that also remembers whether `?total=false` asked to skip the count (smo_shared.pagination.PageSize, kept local for the reason above)."""

    with_total: bool = True

    def __new__(cls, limit: int, with_total: bool = True):
        self = super().__new__(cls, limit)
        self.with_total = with_total
        return self


def _page_limit(limit: int = Query(100, ge=1, le=500, description="Max rows to return (1-500)."),
                total: bool = Query(True, description="`false` skips the count of the whole result: the response then has no `total` and "
                                    "a `hasMore` flag instead. Default `true`.")) -> _PageSize:
    return _PageSize(limit, total)


PageLimit: Any = Depends(_page_limit)
PageOffset = Query(0, ge=0, description="Rows to skip before the first one returned.")


def _paginate(db, stmt, limit: int, offset: int) -> dict:
    # `db` is a real sqlalchemy.orm.Session (db.py's own Database.session()) —
    # left untyped here since this module's own `Session` name (below) is a
    # different, unrelated RBAC dataclass.
    """Runs `stmt` as one page of `{items, total, limit, offset}` (or `{items, limit, offset, hasMore}` when the caller sent `?total=false`).
    `limit` is the `_PageSize` the `PageLimit` dependency produced; a plain int means "with total". Without a total the query reads one row more than `limit`,
    which is how `hasMore` is known without a count. Returns ORM rows in `items`; the caller maps them to JSON.
    """
    n = int(limit)
    if getattr(limit, "with_total", True):
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = db.scalars(stmt.limit(n).offset(offset)).all()
        return {"items": rows, "total": total, "limit": n, "offset": offset}
    rows = db.scalars(stmt.limit(n + 1).offset(offset)).all()
    return {"items": rows[:n], "limit": n, "offset": offset, "hasMore": len(rows) > n}


def _problem_body(status: int, title: str, detail: str | None = None) -> dict:
    body = {"title": title, "status": status}
    if detail:
        body["detail"] = detail
    return body


def _problem(status: int, title: str, detail: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=_problem_body(status, title, detail))


def _problem_exception(status: int, title: str, detail: str | None = None) -> HTTPException:
    """Wave 3 (cross-cutting standardization) — Error Schema: the same
    {title, status, detail} shape as _problem() above, for the four spots
    that must `raise` rather than `return` (FastAPI dependencies, which
    resolve to their return value rather than short-circuiting the
    response) — current_session/require_admin below.
    """
    return HTTPException(status_code=status, detail=_problem_body(status, title, detail))


def _iso(at: datetime.datetime | None) -> str | None:
    """ISO 8601 with the UTC offset, or None. SQLite hands timestamps back without a zone; every one the BFF writes is UTC."""
    if at is None:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=datetime.UTC)).isoformat()


def _utc(at: datetime.datetime) -> datetime.datetime:
    """A caller's `since`/`until` as an aware UTC time (one without a zone is taken as UTC), the form the audit times are compared in."""
    return at.replace(tzinfo=datetime.UTC) if at.tzinfo is None else at.astimezone(datetime.UTC)


def _csv_cell(value: Any) -> str:
    """One CSV cell. A text that a spreadsheet would run as a formula (it starts with = + - @ or a tab or carriage return) gets a leading
    apostrophe: a username typed at the sign-in page lands in the audit log as it was typed (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def seed_users(db: Database, cfg: Settings) -> None:
    """First boot only: creates admin (always) plus operator/viewer (when
    their passwords are set). Never touches an existing user table.
    """
    with db.session() as s:
        if s.scalar(select(GuiUser).limit(1)) is not None:
            return
        admin_password = cfg.admin_password
        wrote_password_file = False
        if not admin_password:
            admin_password = secrets.token_urlsafe(12)
            _write_initial_password(cfg.initial_password_file, admin_password)
            wrote_password_file = True
            log.warning("GUI_ADMIN_PASSWORD not set: seeded user 'admin' with a generated password, written to %s "
                        "(mode 0600). Sign in, change it under Admin > Users, then delete the file.",
                        cfg.initial_password_file)
        seeds = [("admin", admin_password, Role.ADMIN), ("operator", cfg.operator_password, Role.OPERATOR),
                 ("viewer", cfg.viewer_password, Role.VIEWER)]
        for username, password, role in seeds:
            if password:
                s.add(GuiUser(username=username, password_hash=hash_password(password), role=role))
        try:
            s.commit()
        except IntegrityError:
            # Another instance seeded the same database a moment earlier (PR-ST-5): its users stand, and the
            # password this instance generated would not match them, so it must not leave its file behind.
            s.rollback()
            if wrote_password_file:
                with suppress(OSError):
                    os.remove(cfg.initial_password_file)
            log.info("another instance seeded the GUI users first; keeping its users")


def _write_initial_password(path: str, password: str) -> None:
    """Owner-only file on the BFF's own volume. The log and stdout never
    carry the password (CodeQL py/clear-text-logging-sensitive-data).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password + "\n")
    os.chmod(path, 0o600)   # O_CREAT's mode doesn't apply to an existing file


@dataclass
class Session:
    """The authenticated caller of one request, as `current_session` builds it: the user (role read from the user table on this request), the CSRF value
    inside the token, and whether the credential came in a cookie (and so needs the CSRF header on unsafe methods) or as a Bearer token.
    """
    user: User
    csrf: str | None
    via_cookie: bool


def create_app(cfg: Settings = default_settings, db: Database | None = None, gateway: R1Gateway | None = None,
               oidc_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """Builds the FastAPI application: validates the sign-in configuration, then defines every route as a closure over `cfg`, the database and the gateway.
    Raises ValueError, so the process stops at start, for a configuration that would lock everybody out or cannot work: `GUI_LOGIN_MODE=oidc` without OIDC enabled,
    local login off without OIDC, a one-time-code key shorter than 32 characters, `GUI_ADMIN_MFA_REQUIRED` without a key; `build_signer` and `OidcConfig.from_settings` raise for a
    bad signing key or provider. `db`, `gateway` and `oidc_transport` are injected by tests; in production they are None and the lifespan creates the database and the
    R1 gateway on start-up, seeds the first users and, when `GUI_JWT_SECRET` was not set, adopts the signing key stored in the database so every instance agrees.
    Nothing is written at import of this function; the module-level `app` below is the one that is served.
    """
    if cfg.login_mode == "oidc" and not cfg.oidc_enabled:
        raise ValueError("GUI_LOGIN_MODE=oidc needs GUI_OIDC_ENABLED=true (and the provider's settings): with the local form closed and no provider, nobody could sign in")
    # GUI_LOGIN_MODE=local: OIDC is not offered even when it is configured (and not even built, so a stale provider setting cannot stop the start)
    oidc: OidcClient | None = OidcClient(OidcConfig.from_settings(cfg), transport=oidc_transport) if cfg.oidc_enabled and cfg.login_mode != "local" else None
    if not cfg.local_login_enabled and oidc is None:
        raise ValueError("GUI_LOCAL_LOGIN_ENABLED=false needs GUI_OIDC_ENABLED=true (and GUI_LOGIN_MODE other than local): with neither, nobody could sign in")
    if cfg.totp_key and len(cfg.totp_key) < MIN_TOTP_KEY_LENGTH:
        raise ValueError(f"GUI_TOTP_KEY must be at least {MIN_TOTP_KEY_LENGTH} characters (for example `openssl rand -base64 32`)")
    if cfg.admin_mfa_required and not cfg.totp_key:
        raise ValueError("GUI_ADMIN_MFA_REQUIRED=true needs GUI_TOTP_KEY or GUI_TOTP_KEY_FILE: without a key no admin could enrol")

    signer = build_signer(cfg)           # PR-SEC-5: HS256 with GUI_JWT_SECRET (default) or RS256 / ES256 with a key file; a bad combination or key stops the start

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.db is None:
            app.state.db = Database(cfg.database_url)
        seed_users(app.state.db, cfg)
        if cfg.jwt_secret_generated:
            # Not a per-process random value: every instance of this database must sign with the same key.
            cfg.jwt_secret = app.state.db.shared_setting("jwt_secret", cfg.jwt_secret)
            log.warning("GUI_JWT_SECRET not set: using the session signing key stored in the BFF database "
                        "(shared by every instance of this database; set GUI_JWT_SECRET to manage it yourself)")
        if app.state.gateway is None:
            app.state.gateway = R1Gateway(cfg.r1_url, app.state.db, sme_url=cfg.sme_url, timeout=cfg.upstream_timeout_seconds)
        yield
        runner = getattr(app.state, "exports", None)
        if runner is not None:
            await runner.shutdown()        # GUI-9.5b: this instance's export tasks end, their jobs FAILED "interrupted" (another instance can serve the rest)
        await app.state.gateway.aclose()

    app = FastAPI(title="SMO Operator GUI BFF", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.state.db, app.state.gateway, app.state.cfg = db, gateway, cfg
    app.state.oidc = oidc

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    # ------------------------------------------------------------ helpers

    def audit(action: str, user: User | None = None, *, username: str | None = None, method: str | None = None,
              path: str | None = None, status_code: int | None = None, detail: str | None = None) -> None:
        with app.state.db.session() as s:
            s.add(AuditEntry(username=user.username if user else username, role=user.role if user else None,
                             action=action, method=method, path=path, status_code=status_code, detail=detail))
            s.commit()

    def issue_session(user: GuiUser) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        token = signer.issue({"sub": user.username, "ver": user.token_version, "csrf": csrf, "jti": secrets.token_urlsafe(16)}, cfg.session_ttl_seconds)
        return token, csrf

    def set_session_cookies(response: Response, token: str, csrf: str) -> None:
        response.set_cookie(SESSION_COOKIE, token, httponly=True, path="/api", secure=cfg.cookie_secure, samesite="strict", max_age=cfg.session_ttl_seconds)
        # Readable by the SPA on purpose: double-submit CSRF token, echoed back
        # as X-CSRF-Token and checked against the claim inside the session JWT.
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, path="/", secure=cfg.cookie_secure, samesite="strict", max_age=cfg.session_ttl_seconds)

    def check_credentials(username: str, password: str) -> GuiUser | JSONResponse:
        if app.state.db.login_locked(username, MAX_LOGIN_FAILURES, LOCKOUT_SECONDS, time.time()):
            audit("LOGIN_LOCKED", username=username)
            return _problem(429, "TOO_MANY_ATTEMPTS", "account temporarily locked after repeated failures")
        with app.state.db.session() as s:
            user = s.get(GuiUser, username)
        # Verify against a dummy hash for unknown users, so response time
        # doesn't reveal which usernames exist.
        ok = verify_password(password, user.password_hash if user else _DUMMY_HASH) and user is not None and user.active
        if not ok:
            app.state.db.record_login_failure(username, LOCKOUT_SECONDS, time.time())
            audit("LOGIN_FAILED", username=username)
            return _problem(401, "INVALID_CREDENTIALS")
        # The failure counter is not cleared here: with a one-time code the sign-in is not over yet, and a guesser who knows the password must not get a
        # fresh budget of code guesses from every password round. complete_login (and the token grant) clear it.
        return user

    def current_session(request: Request) -> Session:
        auth = request.headers.get("authorization", "")
        via_cookie = not auth.lower().startswith("bearer ")
        token = request.cookies.get(SESSION_COOKIE) if via_cookie else auth[7:].strip()
        claims = signer.decode(token or "")
        if claims is None:
            raise _problem_exception(401, "UNAUTHENTICATED", "not authenticated")
        with app.state.db.session() as s:
            user = s.get(GuiUser, claims.get("sub"))
        if user is None or not user.active or user.token_version != claims.get("ver"):
            raise _problem_exception(401, "SESSION_REVOKED", "session revoked")
        if claims.get("jti") and app.state.db.session_revoked(claims["jti"]):      # ended by its owner (logout)
            raise _problem_exception(401, "SESSION_REVOKED", "session revoked")
        if via_cookie and request.method in UNSAFE_METHODS:
            sent = request.headers.get(CSRF_HEADER, "")
            if not sent or not hmac.compare_digest(sent, str(claims.get("csrf", ""))):
                raise _problem_exception(403, "CSRF_TOKEN_INVALID", "missing or invalid CSRF token")
        # PR-SEC-7.8: a local admin who has no one-time code yet may only enrol one. A user of the identity provider is not asked: its provider does that.
        if (cfg.admin_mfa_required and user.role == Role.ADMIN and user.password_hash != UNUSABLE_HASH and request.url.path not in MFA_OPEN_PATHS
                and not app.state.db.totp_state(user.username)[0]):
            raise _problem_exception(403, "MFA_ENROLMENT_REQUIRED", "an admin must enrol a one-time code before using the console: open Account security")
        # Role always read from the user table, never from the token: a role
        # change or demotion applies on the very next request.
        return Session(user=User(user.username, Role(user.role)), csrf=claims.get("csrf"), via_cookie=via_cookie)

    def require_admin(session: Session = Depends(current_session)) -> Session:
        if session.user.role != Role.ADMIN:
            raise _problem_exception(403, "FORBIDDEN", "requires role admin")
        return session

    # ------------------------------------------------------------ auth

    class LoginRequest(BaseModel):
        username: str
        password: str

    class TotpLoginRequest(BaseModel):
        challenge: str
        code: str = Field(min_length=1, max_length=64)

    @app.get("/.well-known/jwks.json", responses={200: {"description": "The public keys that verify session tokens (RFC 7517), `{\"keys\": []}` under HS256; cacheable for five minutes", "content": {"application/json": {"schema": {"type": "object"}}}}})
    def jwks():
        """PR-SEC-5.3. Unauthenticated by design: it is the public half of the signing keys (the current one first, then the previous rotations that still verify), nothing else.
        Under HS256 (the default) the set is empty: a shared secret has no public half and is never published. Answers 200 with `application/json` and nothing else."""
        return JSONResponse(signer.jwks(), headers={"Cache-Control": "public, max-age=300"})

    @app.get("/api/auth/config")
    def auth_config():
        """Unauthenticated: what the sign-in page may offer. Nothing here is secret (the provider's display name and where to start).
        `localLogin` is whether the password form is the normal way in; with GUI_LOGIN_MODE=oidc it is false and `breakGlass` says that a
        break-glass account can still use the form (the page keeps it behind a link)."""
        return {"localLogin": cfg.local_login_enabled and cfg.login_mode != "oidc", "loginMode": cfg.login_mode,
                "breakGlass": cfg.local_login_enabled and cfg.login_mode == "oidc",
                "oidc": ({"enabled": True, "providerName": cfg.oidc_provider_name, "loginUrl": "/api/oidc/login"} if oidc else {"enabled": False})}

    # ------------------------------------------------------------ one-time codes: the helpers (PR-SEC-7)

    def challenge_key() -> str:
        """The key the challenge is signed with: derived from the session key, so a challenge is never a valid session token (and the other way round).
        Read when used, because an unset GUI_JWT_SECRET is replaced by the shared one at start-up."""
        return hmac.new(cfg.jwt_secret.encode(), b"smo-gui-login-challenge", hashlib.sha256).hexdigest()

    def issue_challenge(user: GuiUser) -> str:
        jti = secrets.token_urlsafe(24)
        app.state.db.create_challenge(jti, user.username, time.time() + CHALLENGE_TTL_SECONDS, time.time())
        return issue_jwt({"use": CHALLENGE_USE, "sub": user.username, "ver": user.token_version, "jti": jti}, challenge_key(), CHALLENGE_TTL_SECONDS)

    def mode_refusal(user: GuiUser, enrolled: bool) -> JSONResponse | None:
        """What GUI_LOGIN_MODE and the break-glass rule say about an account whose password was right (PR-SEC-7.6, 7.7): nothing for most, else the refusal."""
        if cfg.login_mode == "oidc" and not user.break_glass:
            audit("LOGIN_REFUSED", username=user.username, detail="GUI_LOGIN_MODE=oidc: not a break-glass account")
            return _problem(403, "LOGIN_MODE_OIDC_ONLY", f"local sign-in is closed: sign in through {cfg.oidc_provider_name}. Only a break-glass account can use a password.")
        if user.break_glass and not enrolled:
            audit("LOGIN_REFUSED", username=user.username, detail="break-glass account without an enrolled one-time code")
            return _problem(403, "BREAK_GLASS_NEEDS_TOTP", "a break-glass account signs in with a one-time code and has none enrolled: ask another admin to reset it, then enrol")
        return None

    def check_second_factor(user: GuiUser, code: str) -> tuple[str, int | None]:
        """Test a one-time code or a recovery code for `user`: ("ok", recovery codes left or None), ("bad", None), or ("key", None) when the stored secret cannot
        be read (no key, or a different one). A failure counts towards the lockout. The code is never logged or audited."""
        key = cfg.totp_key
        stored = app.state.db.totp_secret(user.username)
        if stored is None or not stored[1]:
            return "bad", None
        if not key:
            return "key", None
        if totp.looks_like_recovery_code(code):
            left = app.state.db.use_recovery_code(user.username, totp.hash_recovery_code(key, user.username, code))
            if left is not None:
                return "ok", left
        else:
            try:
                secret = totp.decrypt_secret(key, user.username, stored[0])
            except totp.TotpKeyError:
                return "key", None
            step = totp.verify(secret, code, time.time(), stored[2])
            if step is not None and app.state.db.use_totp_step(user.username, step):
                return "ok", None
        app.state.db.record_login_failure(user.username, LOCKOUT_SECONDS, time.time())
        audit("LOGIN_FAILED", username=user.username, detail="one-time code")
        return "bad", None

    def key_problem(username: str) -> JSONResponse:
        log.error("the one-time-code secret of %s cannot be read: GUI_TOTP_KEY is missing or is not the key it was stored with", username)
        return _problem(503, "TOTP_KEY_UNAVAILABLE", "the one-time code cannot be checked: ask an administrator (GUI_TOTP_KEY)")

    def mfa_view(username: str, role: str) -> dict:
        """What the SPA needs to know about the second factor of the signed-in user: local or not, enrolled or not, and whether it must enrol now (PR-SEC-7.8)."""
        local = not username.startswith(OIDC_PREFIX)
        enrolled = local and app.state.db.totp_state(username)[0]
        return {"local": local, "totpEnrolled": enrolled, "mfaEnrolmentRequired": bool(cfg.admin_mfa_required and local and role == Role.ADMIN and not enrolled)}

    def complete_login(user: GuiUser, response: Response, *, how: str, recovery_left: int | None = None) -> dict:
        """The session after every check has passed: cookies, the audit row (a break-glass sign-in has its own action and a warning in the log)."""
        app.state.db.clear_login_failures(user.username)
        token, csrf = issue_session(user)
        set_session_cookies(response, token, csrf)
        who = User(user.username, Role(user.role))
        if user.break_glass:
            log.warning("break-glass sign-in: %s (role %s)", user.username, user.role)
            audit("BREAK_GLASS_LOGIN", who, detail=how)
        else:
            audit("LOGIN", who, detail=how if how != "password" else None)
        if recovery_left is not None:
            audit("RECOVERY_CODE_USED", who, detail=f"{recovery_left} left")
        body: dict = {"username": user.username, "role": user.role, "csrfToken": csrf, **mfa_view(user.username, user.role)}
        if recovery_left is not None:
            body["recoveryCodesLeft"] = recovery_left
        return body

    @app.post("/api/login")
    def login(body: LoginRequest, response: Response):
        # Status codes: 200 with the session cookies and `{username, role, csrfToken, ...}`; 200 with `{mfaRequired, challenge, expiresIn}` when the account has an enrolled
        # one-time code (no session yet; the challenge goes to POST /api/login/totp); 401 INVALID_CREDENTIALS (an unknown, inactive or wrong-password account looks the same);
        # 429 TOO_MANY_ATTEMPTS while the account is locked; 403 LOCAL_LOGIN_DISABLED, LOGIN_MODE_OIDC_ONLY or BREAK_GLASS_NEEDS_TOTP; 503 TOTP_KEY_UNAVAILABLE when the account
        # has a one-time code and the server has no key to check it. Failures are counted per username; the counter is cleared only when the sign-in completes.
        if not cfg.local_login_enabled:
            return _problem(403, "LOCAL_LOGIN_DISABLED", "sign in through the identity provider")
        user = check_credentials(body.username, body.password)
        if isinstance(user, JSONResponse):
            return user
        enrolled = app.state.db.totp_state(user.username)[0]
        refusal = mode_refusal(user, enrolled)
        if refusal is not None:
            return refusal
        if enrolled:
            # The password alone does not make a session: a challenge, spent by the second step (POST /api/login/totp).
            if not cfg.totp_key:
                return key_problem(user.username)
            challenge = issue_challenge(user)
            audit("MFA_CHALLENGE", User(user.username, Role(user.role)))
            return {"mfaRequired": True, "challenge": challenge, "expiresIn": CHALLENGE_TTL_SECONDS}
        return complete_login(user, response, how="password")

    @app.post("/api/login/totp")
    def login_totp(body: TotpLoginRequest, response: Response):
        # The second step of POST /api/login. The challenge is checked before anything else (signature, purpose, expiry), then the lockout, then that the user is still
        # active with the same token version and that the challenge row is still unspent; the code is checked next and the challenge is spent last, so a wrong code does not
        # burn it. Status codes: 200 as for /api/login (plus `recoveryCodesLeft` after a recovery code); 401 CHALLENGE_INVALID or INVALID_CODE; 429 TOO_MANY_ATTEMPTS;
        # 403 for the login-mode refusals; 503 TOTP_KEY_UNAVAILABLE.
        claims = decode_jwt(body.challenge, challenge_key())
        if claims is None or claims.get("use") != CHALLENGE_USE or not isinstance(claims.get("jti"), str) or not isinstance(claims.get("sub"), str):
            return _problem(401, "CHALLENGE_INVALID", "the sign-in took too long or did not start here: enter the password again")
        username, jti = claims["sub"], claims["jti"]
        # the same lockout as a wrong password: failures of either step are counted under the user name
        if app.state.db.login_locked(username, MAX_LOGIN_FAILURES, LOCKOUT_SECONDS, time.time()):
            audit("LOGIN_LOCKED", username=username)
            return _problem(429, "TOO_MANY_ATTEMPTS", "account temporarily locked after repeated failures")
        with app.state.db.session() as s:
            user = s.get(GuiUser, username)
        if (user is None or not user.active or user.token_version != claims.get("ver") or not cfg.local_login_enabled
                or not app.state.db.challenge_pending(jti, username, time.time())):
            return _problem(401, "CHALLENGE_INVALID", "the sign-in took too long or did not start here: enter the password again")
        refusal = mode_refusal(user, enrolled=True)
        if refusal is not None:
            return refusal
        outcome, left = check_second_factor(user, body.code)
        if outcome == "key":
            return key_problem(username)
        if outcome != "ok":
            return _problem(401, "INVALID_CODE", "the code is wrong, or was already used")
        if not app.state.db.consume_challenge(jti, username, time.time()):     # spent by a request that got here first
            return _problem(401, "CHALLENGE_INVALID", "the sign-in took too long or did not start here: enter the password again")
        return complete_login(user, response, how="recovery code" if left is not None else "password+code", recovery_left=left)

    @app.post("/api/token")
    def oauth2_password_grant(grant_type: str = Form(...), username: str = Form(...), password: str = Form(...), otp: str = Form("", max_length=64)):
        """RFC 6749 section 4.3 resource-owner password grant, for scripts
        and CLI use: the same users and roles as the GUI, sent as
        `Authorization: Bearer`. No CSRF check applies to Bearer calls
        (a browser never attaches them on its own). An account with a one-time
        code sends it as the extra form field `otp` (a recovery code works too):
        the grant must not be a way round the second factor.
        """
        if grant_type != "password":
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
        if not cfg.local_login_enabled:
            return JSONResponse(status_code=403, content={"error": "unauthorized_client", "error_description": "local login is disabled"})
        user = check_credentials(username, password)
        if isinstance(user, JSONResponse):
            return JSONResponse(status_code=400 if user.status_code == 401 else user.status_code,
                                content={"error": "invalid_grant"})
        enrolled = app.state.db.totp_state(user.username)[0]
        refusal = mode_refusal(user, enrolled)
        if refusal is not None:
            return JSONResponse(status_code=403, content={"error": "unauthorized_client", "error_description": "local sign-in is not open to this account"})
        left = None
        if enrolled:
            if not otp:
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "this account needs a one-time code: send it as the form field otp"})
            outcome, left = check_second_factor(user, otp)
            if outcome == "key":
                return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})
            if outcome != "ok":
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
        app.state.db.clear_login_failures(user.username)
        token, _ = issue_session(user)
        who = User(user.username, Role(user.role))
        if user.break_glass:
            log.warning("break-glass sign-in (token grant): %s (role %s)", user.username, user.role)
            audit("BREAK_GLASS_LOGIN", who, detail="token grant")
        else:
            audit("TOKEN", who)
        if left is not None:
            audit("RECOVERY_CODE_USED", who, detail=f"{left} left")
        return {"access_token": token, "token_type": "Bearer", "expires_in": cfg.session_ttl_seconds}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        # Needs no valid session on purpose: it ends whatever token it is given (cookie or Bearer) and always clears both cookies, answering 200 `{status}`. When the token
        # decodes and carries a `jti`, the session is recorded as revoked in the shared database, so a copy of the token stops working on every instance. For an OIDC user whose
        # provider publishes an end-session endpoint the answer also carries `endSessionUrl`. A token that does not decode writes no audit row.
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get(SESSION_COOKIE, "")
        claims = signer.decode(token)
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=cfg.cookie_secure, samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=cfg.cookie_secure, samesite="strict")
        if claims:
            if claims.get("jti"):
                # the cookie is cleared in the browser, but a copy of it (or the token) would stay good until it expires: the session itself is ended here
                app.state.db.revoke_session(claims["jti"], claims["exp"], time.time())
            audit("LOGOUT", username=claims.get("sub"))
        result: dict[str, str] = {"status": "logged out"}
        if oidc is not None and claims and str(claims.get("sub", "")).startswith(OIDC_PREFIX):
            end_session = oidc.end_session_url()        # RP-initiated logout: the SPA sends the browser there to end the provider's session too
            if end_session:
                result["endSessionUrl"] = end_session
        return result

    # ------------------------------------------------------------ OIDC login (PR-SEC-6)

    def oidc_failure(code: str, *, subject: str | None = None, detail: str | None = None) -> RedirectResponse:
        """Back to the sign-in page with a reason code (never the provider's text), the binding cookie cleared, and an audit row."""
        audit("OIDC_LOGIN_FAILED", username=subject, detail=f"{code}: {detail}" if detail else code)
        response = RedirectResponse("/login?" + urlencode({"oidc_error": code}), status_code=303)
        response.delete_cookie(OIDC_COOKIE, path="/api/oidc", secure=cfg.cookie_secure, samesite="lax")
        return response

    @app.get("/api/oidc/login")
    def oidc_login():
        # Starts an OIDC sign-in: 404 OIDC_DISABLED when OIDC is not configured; otherwise 302 to the provider with state, nonce and a PKCE challenge, the pending sign-in stored
        # in the database and a random binding value set in the httpOnly `smo_oidc` cookie. A discovery failure, or too many sign-ins already pending, redirects to /login with
        # an `oidc_error` code instead (303) and writes an OIDC_LOGIN_FAILED audit row.
        if oidc is None:
            return _problem(404, "OIDC_DISABLED")
        state, nonce, verifier, binding = (secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_urlsafe(24))
        try:
            url = oidc.authorization_url(state, nonce, verifier)
        except OidcError as exc:
            return oidc_failure(exc.code, detail=exc.detail)
        if not app.state.db.start_oidc_login(state, nonce, verifier, hashlib.sha256(binding.encode()).hexdigest(), time.time(),
                                             LOGIN_TTL_SECONDS, MAX_PENDING_LOGINS):
            return oidc_failure("too_many_logins")
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(OIDC_COOKIE, binding, httponly=True, path="/api/oidc", secure=cfg.cookie_secure, samesite="lax", max_age=LOGIN_TTL_SECONDS)
        return response

    def provision_oidc_user(subject: str, role: Role) -> tuple[GuiUser | None, str]:
        """The user row for `subject`, created on first sign-in (no password) and given `role` at every sign-in; None for a disabled one."""
        username = OIDC_PREFIX + subject
        for _ in range(2):
            with app.state.db.session() as s:
                user = s.get(GuiUser, username)
                if user is None:
                    # a random starting token version, as for any new user: see create_user
                    user = GuiUser(username=username, password_hash=UNUSABLE_HASH, role=role, token_version=secrets.randbits(30))
                    s.add(user)
                    try:
                        s.commit()
                    except IntegrityError:      # another instance created the same user a moment earlier: use its row
                        s.rollback()
                        continue
                    return user, "created"
                if not user.active:
                    return None, "disabled"
                note = "existing"
                if user.role != role:
                    note = f"role {user.role}->{role}"
                    user.role = role
                    s.commit()
                return user, note
        raise RuntimeError("could not create or read the OIDC user")

    @app.get("/api/oidc/callback")
    def oidc_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
        # Finishes an OIDC sign-in. Order: the pending sign-in is consumed first (deleted, so a replayed callback finds nothing), then the binding cookie is compared in
        # constant time with the stored hash (a callback opened in another browser fails), then a provider error, then the code exchange and the ID-token checks, then the
        # group-to-role mapping. Every failure is a 303 redirect to /login?oidc_error=<code> (never the provider's own text) with an audit row; success is a 303 to / with the
        # session cookies. A person whose groups no longer map to any role has their existing sessions ended (token version bumped) before the refusal.
        if oidc is None:
            return _problem(404, "OIDC_DISABLED")
        pending = app.state.db.consume_oidc_login(state, time.time()) if state else None
        if pending is None:
            return oidc_failure("invalid_state")
        binding = request.cookies.get(OIDC_COOKIE, "")
        if not hmac.compare_digest(hashlib.sha256(binding.encode()).hexdigest(), pending.binding_hash):
            return oidc_failure("invalid_state", detail="the browser that started the sign-in is not the one that came back")
        if error:
            return oidc_failure("access_denied" if error == "access_denied" else "idp_error", detail=error if re.fullmatch(r"[a-z_]{1,40}", error) else None)
        if not code:
            return oidc_failure("idp_error", detail="no code")
        subject: str | None = None
        try:
            tokens = oidc.exchange_code(code, pending.verifier)
            access = tokens.get("access_token")
            claims = oidc.validate_id_token(tokens["id_token"], pending.nonce, access if isinstance(access, str) else None)
            subject = OIDC_PREFIX + claims["sub"]
            role = oidc.role_for(claims)
            if role is None:
                # removed from every mapped group: an earlier session of this person ends too (a role is otherwise re-read on every request)
                with app.state.db.session() as s:
                    existing = s.get(GuiUser, subject)
                    if existing is not None:
                        existing.token_version += 1
                        s.commit()
                raise OidcError("no_role", "no group of the user maps to a role")
            user, note = provision_oidc_user(claims["sub"], role)
            if user is None:
                raise OidcError("account_disabled")
        except OidcError as exc:
            return oidc_failure(exc.code, subject=subject, detail=exc.detail)
        token, csrf = issue_session(user)
        response = RedirectResponse("/", status_code=303)
        set_session_cookies(response, token, csrf)
        response.delete_cookie(OIDC_COOKIE, path="/api/oidc", secure=cfg.cookie_secure, samesite="lax")
        audit("OIDC_LOGIN", User(user.username, Role(user.role)), detail=f"iss={oidc.cfg.issuer} {note}")
        return response

    @app.get("/api/me")
    def me(session: Session = Depends(current_session)):
        # mfaEnrolmentRequired: the SPA sends such an admin to the enrolment page, because every other route answers 403 MFA_ENROLMENT_REQUIRED (PR-SEC-7.8)
        return {"username": session.user.username, "role": session.user.role, "csrfToken": session.csrf, **mfa_view(session.user.username, session.user.role)}

    # ------------------------------------------------------------ one-time code enrolment (PR-SEC-7.1, 7.3)

    class CodeRequest(BaseModel):
        code: str = Field(min_length=1, max_length=64)

    def enrolment_refusal(session: Session) -> JSONResponse | None:
        if session.user.username.startswith(OIDC_PREFIX):
            return _problem(409, "OIDC_USER", "this user signs in through the identity provider, which asks for the second factor")
        if not cfg.totp_key:
            return _problem(503, "TOTP_UNAVAILABLE", "one-time codes are not set up on this server: an administrator must set GUI_TOTP_KEY (or GUI_TOTP_KEY_FILE)")
        return None

    def code_attempt_refusal(username: str) -> JSONResponse | None:
        """A code typed in a signed-in session is guessable too: the wrong ones count towards the same lockout as at sign-in."""
        if app.state.db.login_locked(username, MAX_LOGIN_FAILURES, LOCKOUT_SECONDS, time.time()):
            audit("LOGIN_LOCKED", username=username)
            return _problem(429, "TOO_MANY_ATTEMPTS", "too many wrong codes: try again in a few minutes")
        return None

    def check_own_code(username: str, secret_b32: str, code: str, last_step: int | None) -> int | None:
        step = totp.verify(secret_b32, code, time.time(), last_step)
        if step is None:
            app.state.db.record_login_failure(username, LOCKOUT_SECONDS, time.time())
            audit("LOGIN_FAILED", username=username, detail="one-time code (enrolment)")
        return step

    @app.get("/api/me/totp")
    def totp_status(session: Session = Depends(current_session)):
        # The one-time-code state of the signed-in user for the account page: `available` (the server has a key), `enrolled`, `pending` and `recoveryCodesLeft`.
        # An OIDC user is reported as not available with the reason "identity provider", because the provider asks for the second factor. Read only; 200.
        username = session.user.username
        if username.startswith(OIDC_PREFIX):
            return {"available": False, "enrolled": False, "pending": False, "recoveryCodesLeft": 0, "recoveryCodes": [], "reason": "identity provider"}
        enrolled, pending = app.state.db.totp_state(username)
        # GUI-9.8: which of the recovery codes are spent, by the slot they were shown in, never the codes themselves
        slots = [{"slot": slot, "used": used_at is not None, "usedAt": _iso(used_at)} for slot, used_at in app.state.db.recovery_code_slots(username)] if enrolled else []
        return {"available": bool(cfg.totp_key), "enrolled": enrolled, "pending": pending,
                "recoveryCodesLeft": app.state.db.recovery_codes_left(username) if enrolled else 0, "recoveryCodes": slots}

    @app.post("/api/me/totp/begin")
    def totp_begin(session: Session = Depends(current_session)):
        """Generate a secret and keep it, encrypted and not yet active. The secret and the otpauth:// URI are in this answer and nowhere else; there is no QR image."""
        refusal = enrolment_refusal(session)
        if refusal is not None:
            return refusal
        username = session.user.username
        secret = totp.new_secret()
        if not app.state.db.begin_totp(username, totp.encrypt_secret(cfg.totp_key, username, secret)):
            return _problem(409, "TOTP_ALREADY_ENROLLED", "a one-time code is already set up: an admin must reset it before another can be enrolled")
        audit("TOTP_ENROL_STARTED", session.user)
        return {"secret": secret, "otpauthUri": totp.provisioning_uri(cfg.totp_issuer, username, secret), "issuer": cfg.totp_issuer, "account": username}

    @app.post("/api/me/totp/confirm")
    def totp_confirm(body: CodeRequest, session: Session = Depends(current_session)):
        """The first valid code from the new secret makes it active, and returns the recovery codes: the only time they are shown."""
        refusal = enrolment_refusal(session) or code_attempt_refusal(session.user.username)
        if refusal is not None:
            return refusal
        username = session.user.username
        stored = app.state.db.totp_secret(username)
        if stored is None or stored[1]:
            return _problem(409, "NO_ENROLMENT_IN_PROGRESS", "start the enrolment first" if stored is None else "a one-time code is already set up")
        try:
            secret = totp.decrypt_secret(cfg.totp_key, username, stored[0])
        except totp.TotpKeyError:
            return key_problem(username)
        step = check_own_code(username, secret, body.code, None)
        if step is None:
            return _problem(400, "INVALID_CODE", "the code does not match: check the device clock and try the next code")
        codes = totp.new_recovery_codes()
        if not app.state.db.confirm_totp(username, step, [totp.hash_recovery_code(cfg.totp_key, username, c) for c in codes]):
            return _problem(409, "NO_ENROLMENT_IN_PROGRESS", "the enrolment was already confirmed")
        app.state.db.clear_login_failures(username)
        audit("TOTP_ENROLLED", session.user, detail=f"{len(codes)} recovery codes issued")
        return {"status": "enrolled", "recoveryCodes": codes, "recoveryCodesLeft": len(codes)}

    @app.post("/api/me/totp/recovery-codes")
    def totp_new_recovery_codes(body: CodeRequest, session: Session = Depends(current_session)):
        """Ten new recovery codes, replacing all the old ones; asks for a current one-time code (not a recovery code)."""
        refusal = enrolment_refusal(session) or code_attempt_refusal(session.user.username)
        if refusal is not None:
            return refusal
        username = session.user.username
        stored = app.state.db.totp_secret(username)
        if stored is None or not stored[1]:
            return _problem(409, "NOT_ENROLLED", "no one-time code is set up")
        try:
            secret = totp.decrypt_secret(cfg.totp_key, username, stored[0])
        except totp.TotpKeyError:
            return key_problem(username)
        step = check_own_code(username, secret, body.code, stored[2])
        if step is None or not app.state.db.use_totp_step(username, step):
            return _problem(400, "INVALID_CODE", "the code is wrong, or was already used: wait for the next one")
        codes = totp.new_recovery_codes()
        app.state.db.replace_recovery_codes(username, [totp.hash_recovery_code(cfg.totp_key, username, c) for c in codes])
        audit("RECOVERY_CODES_REGENERATED", session.user, detail=f"{len(codes)} issued")
        return {"recoveryCodes": codes, "recoveryCodesLeft": len(codes)}

    @app.get("/api/me/sign-ins")
    def my_sign_ins(limit: int = Query(20, ge=1, le=100, description="At most this many rows, newest first."), session: Session = Depends(current_session)):
        """The caller's own recent sign-ins, failed sign-ins (also those typed under the caller's name by someone else) and sign-outs, newest first:
        `[{at, action, detail}]` from the audit log. Never another user's rows, whatever the role."""
        stmt = (select(AuditEntry).where(AuditEntry.username == session.user.username, AuditEntry.action.in_(SIGN_IN_HISTORY_ACTIONS))
                .order_by(AuditEntry.id.desc()).limit(limit))
        with app.state.db.session() as s:
            return [{"at": _iso(e.at), "action": e.action, "detail": e.detail} for e in s.scalars(stmt).all()]

    class ChangePasswordRequest(BaseModel):
        currentPassword: str
        newPassword: str = Field(min_length=MIN_PASSWORD_LENGTH)

    @app.post("/api/me/password")
    def change_own_password(body: ChangePasswordRequest, response: Response, session: Session = Depends(current_session)):
        # Changes the signed-in user's own password after checking the current one: 400 INVALID_CREDENTIALS when it is wrong. Bumping `token_version` ends every other session
        # of the user at once; the response carries a fresh CSRF token, and for a cookie session the new cookies replace the old. The new password length is validated by the
        # request model (422). There is no lockout on the current-password check here. Audit: PASSWORD_CHANGED.
        with app.state.db.session() as s:
            user = s.get(GuiUser, session.user.username)
            if not verify_password(body.currentPassword, user.password_hash):
                return _problem(400, "INVALID_CREDENTIALS", "current password is wrong")
            user.password_hash = hash_password(body.newPassword)
            user.token_version += 1
            s.commit()
            token, csrf = issue_session(user)
        if session.via_cookie:
            set_session_cookies(response, token, csrf)
        audit("PASSWORD_CHANGED", session.user)
        return {"status": "password changed", "csrfToken": csrf}

    @app.get("/api/permissions")
    def permissions(session: Session = Depends(current_session)):
        """The RBAC table itself — the SPA evaluates the same rules, first
        match wins, to decide which actions to show. Display only: the
        proxy below re-checks every call.
        """
        return {"role": session.user.role, "rules": [
            {"method": r.method, "pattern": r.pattern.pattern, "role": r.role, "queryMatch": r.query_match}
            for r in RULES
        ]}

    # ------------------------------------------------------------ health

    @app.get("/api/modules/status")
    async def modules_status(session: Session = Depends(current_session)):
        # Probes every module's /health through R1 in parallel (R1 itself directly), each with the short `GUI_HEALTH_TIMEOUT_SECONDS`, and for a module that is live also its
        # /ready and /version. Always 200: an unreachable module, a missing SMO token or an older build without /version shows up as `healthy: false`, `error` or null fields
        # in that module's entry, never as a failed request. Any signed-in role may call it. Writes nothing.
        gw: R1Gateway = app.state.gateway

        async def get(module: str, route: str) -> httpx.Response:
            # R1's own routes are public and answered by the gateway; a module's are reached through R1's token-gated proxy
            if module == "r1-termination":
                return await gw.r1_get(route, cfg.health_timeout_seconds)
            return await gw.request("GET", f"/{module}{route}", timeout=cfg.health_timeout_seconds)

        async def optional_json(module: str, route: str) -> tuple[int | None, dict]:
            """(status, body) of a route a module may not have (an older build has no /version): never an error."""
            try:
                resp = await get(module, route)
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return resp.status_code, body if isinstance(body, dict) else {}
            except (SmoAuthError, httpx.HTTPError, ValueError):
                return None, {}

        async def probe(module: str) -> dict:
            started = time.perf_counter()
            try:
                resp = await get(module, "/health")
                healthy, status_code, error = resp.status_code == 200, resp.status_code, None
            except SmoAuthError as exc:
                log.warning("health probe %s: SMO token unavailable: %s", module, exc)
                healthy, status_code, error = False, None, "auth: no SMO access token"
            except httpx.HTTPError as exc:
                log.warning("health probe %s failed: %r", module, exc)
                healthy, status_code, error = False, None, "unreachable"
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            if healthy:     # a module that is not live is not asked again
                (ready_status, _), (version_status, version) = await asyncio.gather(
                    optional_json(module, "/ready"), optional_json(module, "/version"))
            else:
                ready_status, version_status, version = None, None, {}
            known = version_status == 200
            return {"module": module, "healthy": healthy, "latencyMs": latency_ms,
                    "statusCode": status_code, "error": error,
                    # PR-OBS-8.2: readiness (null when the module did not answer /ready with 200 or 503) and the build it runs (null when
                    # it has no /version, e.g. an older release during a rolling upgrade)
                    "ready": ready_status == 200 if ready_status in (200, 503) else None,
                    "version": version.get("version") if known else None,
                    "buildSha": version.get("buildSha") if known else None,
                    "builtAt": version.get("builtAt") if known else None}

        results = await asyncio.gather(*(probe(m) for m in STATUS_MODULES))
        return {"checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "modules": list(results)}

    # ------------------------------------------------------------ proxy

    @app.api_route("/api/smo/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(full_path: str, request: Request, session: Session = Depends(current_session)):
        # The only road from the browser to the SMO. Order: `decide()` (rbac.py) checks the role against the first matching rule, and a call with no rule or too low a role is
        # 403 FORBIDDEN and audited as DENIED; then the rule's query and JSON overrides are applied (a caller cannot override a forced value; a body that must be rewritten has to
        # be a JSON object, else 400 INVALID_BODY); then the request is sent to R1 with the BFF's own token, forwarding only `content-type` and `accept`. Upstream answers are
        # returned as they are minus hop-by-hop, length, encoding, Set-Cookie and server headers. 502 SMO_AUTH_FAILED when no SME token could be obtained, 502 R1_UNREACHABLE when
        # R1 did not answer. Only mutating methods write an audit row (PROXY, with the upstream status); reads are not audited.
        path = "/" + full_path
        query: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            query.setdefault(k, []).append(v)
        decision = decide(request.method, path, query, session.user.role)
        mutating = request.method in UNSAFE_METHODS
        if not decision.allowed:
            detail = f"requires role {decision.required_role}" if decision.required_role else "not exposed through the GUI"
            audit("DENIED", session.user, method=request.method, path=path, status_code=403, detail=detail)
            return _problem(403, "FORBIDDEN", detail)

        rule = cast(Rule, decision.rule)         # allowed means a rule matched
        params = [(k, v) for k, v in request.query_params.multi_items()]
        if rule.query_overrides:
            forced = rule.query_overrides(session.user)
            params = [(k, v) for k, v in params if k not in forced] + list(forced.items())
        body = await request.body()
        if rule.json_overrides:
            try:
                payload = await request.json()
            except ValueError:
                return _problem(400, "INVALID_BODY", "expected a JSON object")
            if not isinstance(payload, dict):
                return _problem(400, "INVALID_BODY", "expected a JSON object")
            body = json.dumps({**payload, **rule.json_overrides(session.user)}).encode()

        headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST}
        # SEC-15.8: every module sees the BFF as the caller, so the signed-in person goes with each call in `X-R1-Acting-User` (the same `smo-gui:<username>` the rules write into
        # `requestedBy` and `decidedBy`). R1 Termination forwards it to a module because the BFF's token is `internal`; the two-person approval takes the decider from it instead
        # of from the body. Set after the browser's headers were filtered above, so the browser cannot choose it.
        headers[ACTING_USER_HEADER] = f"smo-gui:{session.user.username}"
        try:
            upstream = await app.state.gateway.request(request.method, path, params=params, content=body or None, headers=headers)
        except SmoAuthError as exc:
            if mutating:
                audit("PROXY", session.user, method=request.method, path=path, status_code=502, detail=f"auth: {exc}")
            log.warning("proxy %s %s: SMO token unavailable: %s", request.method, path, exc)
            return _problem(502, "SMO_AUTH_FAILED", "the BFF could not obtain an SMO access token from SME")
        except httpx.HTTPError as exc:
            if mutating:
                audit("PROXY", session.user, method=request.method, path=path, status_code=502, detail=exc.__class__.__name__)
            log.warning("proxy %s %s: R1 Termination unreachable: %r", request.method, path, exc)
            return _problem(502, "R1_UNREACHABLE", "R1 Termination did not answer")

        if mutating:
            query_text = "&".join(f"{k}={v}" for k, v in params)
            audit("PROXY", session.user, method=request.method, path=path + (f"?{query_text}" if query_text else ""),
                  status_code=upstream.status_code)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _NEVER_FORWARD_RESPONSE}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)

    # ------------------------------------------------------------ rApp directory, declared pages, pins (PR-GUI-8)

    rapps.install(app, current_session=current_session, audit=audit, problem=_problem)

    # ------------------------------------------------------------ console preferences and summary counts (GUI redesign)

    preferences.install(app, current_session=current_session)
    summary.install(app, current_session=current_session, problem=_problem)
    events.install(app, current_session=current_session, problem=_problem)       # GUI-9.1: needs summary's cached computation
    search.install(app, current_session=current_session, problem=_problem)       # GUI-9.2: needs rapps' directory

    # ------------------------------------------------------------ admin

    class CreateUserRequest(BaseModel):
        username: str
        password: str = Field(min_length=MIN_PASSWORD_LENGTH)
        role: Role

    class UpdateUserRequest(BaseModel):
        role: Role | None = None
        active: bool | None = None
        password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH)
        breakGlass: bool | None = None

    def _user_view(u: GuiUser, enrolled: set[str] | None = None, activity: dict | None = None) -> dict:
        """The admin's view of one user. `enrolled` and `activity` (Database.user_activity) are read for this user alone when not given; the list
        passes them for every user at once. `lastActiveAt` is the user's newest audit row (sign-ins, changes, refusals: plain reads are not
        audited), `lastSignInAt` the newest successful sign-in; null when there is none."""
        if enrolled is None:
            enrolled = {u.username} if app.state.db.totp_state(u.username)[0] else set()
        if activity is None:
            activity = app.state.db.user_activity(SIGN_IN_ACTIONS, u.username)
        last_active, last_sign_in = activity.get(u.username, (None, None))
        return {"username": u.username, "role": u.role, "active": u.active, "createdAt": u.created_at.isoformat(),
                "breakGlass": u.break_glass, "totpEnrolled": u.username in enrolled, "lastActiveAt": _iso(last_active), "lastSignInAt": _iso(last_sign_in)}

    def _active_admins(s) -> int:
        return len(s.scalars(select(GuiUser).where(GuiUser.role == Role.ADMIN, GuiUser.active.is_(True))).all())

    @app.get("/api/admin/users")
    def list_users(session: Session = Depends(require_admin)):
        # Admin only (403 for other roles). Every local and OIDC user ordered by username, with the one-time-code enrolment of all of them read in one query. Read only.
        with app.state.db.session() as s:
            enrolled = app.state.db.totp_enrolled_users()
            activity = app.state.db.user_activity(SIGN_IN_ACTIONS)        # one grouped query for every user
            return [_user_view(u, enrolled, activity) for u in s.scalars(select(GuiUser).order_by(GuiUser.username)).all()]

    @app.post("/api/admin/users", status_code=201)
    def create_user(body: CreateUserRequest, session: Session = Depends(require_admin)):
        # Admin only. 400 INVALID_USERNAME when the name does not match `USERNAME_RE` (which also keeps local names out of the `oidc:` namespace), 409 USER_EXISTS, 201 with the
        # user otherwise. The starting `token_version` is random on purpose, so a deleted and re-created name cannot revive the old person's unexpired token. Audit: USER_CREATED.
        if not USERNAME_RE.match(body.username):
            return _problem(400, "INVALID_USERNAME", "2-32 chars: lowercase letter first, then a-z 0-9 _ . -")
        with app.state.db.session() as s:
            if s.get(GuiUser, body.username) is not None:
                return _problem(409, "USER_EXISTS")
            # A random starting token version, not 0: a session token carries the version it was issued under, so a user deleted
            # and created again under the same name (STD-4.3) must not make the old person's unexpired token valid again.
            user = GuiUser(username=body.username, password_hash=hash_password(body.password), role=body.role,
                           token_version=secrets.randbits(30))
            s.add(user)
            s.commit()
            view = _user_view(user)
        audit("USER_CREATED", session.user, detail=f"{body.username} role={body.role}")
        return view

    @app.patch("/api/admin/users/{username}")
    def update_user(username: str, body: UpdateUserRequest, session: Session = Depends(require_admin)):
        # Admin only; changes the fields sent. 404 NO_SUCH_USER; 409 OIDC_USER for a password or break-glass change on an identity-provider user; 409 LAST_ADMIN when the change
        # would demote or deactivate the only active admin. Deactivating or resetting a password bumps `token_version` (every session of that user ends); a role change does not
        # need to, because the role is read from the table on each request. The whole change is one commit. Audit: USER_UPDATED with the list of changes.
        with app.state.db.session() as s:
            user = s.get(GuiUser, username)
            if user is None:
                return _problem(404, "NO_SUCH_USER")
            if body.password is not None and user.password_hash == UNUSABLE_HASH:
                return _problem(409, "OIDC_USER", "this user signs in through the identity provider and has no password")
            demoting = (body.role is not None and body.role != Role.ADMIN) or body.active is False
            if user.role == Role.ADMIN and demoting and _active_admins(s) <= 1:
                return _problem(409, "LAST_ADMIN", "at least one active admin must remain")
            changes = []
            if body.role is not None and body.role != user.role:
                changes.append(f"role {user.role}->{body.role}")
                user.role = body.role
            if body.active is not None and body.active != user.active:
                changes.append("activated" if body.active else "deactivated")
                user.active = body.active
                user.token_version += 1
            if body.breakGlass is not None and body.breakGlass != user.break_glass:
                if body.breakGlass and user.password_hash == UNUSABLE_HASH:
                    return _problem(409, "OIDC_USER", "a break-glass account is a local one: this user signs in through the identity provider")
                changes.append("break-glass on" if body.breakGlass else "break-glass off")
                user.break_glass = body.breakGlass
            if body.password is not None:
                changes.append("password reset")
                user.password_hash = hash_password(body.password)
                user.token_version += 1
            s.commit()
            view = _user_view(user)
        audit("USER_UPDATED", session.user, detail=f"{username}: {', '.join(changes) or 'no change'}")
        return view

    @app.delete("/api/admin/users/{username}", status_code=204)
    def delete_user(username: str, session: Session = Depends(require_admin)):
        # Admin only. 409 CANNOT_DELETE_SELF; 409 LAST_ADMIN for the only active admin; 204 also for a user that does not exist (the call is idempotent). The user row and its
        # failed-login counter go in one transaction; the one-time-code data and pins are removed right after, in their own transactions. Audit rows that name the user are kept.
        if username == session.user.username:
            return _problem(409, "CANNOT_DELETE_SELF")
        with app.state.db.session() as s:
            user = s.get(GuiUser, username)
            if user is None:
                return Response(status_code=204)
            if user.role == Role.ADMIN and user.active and _active_admins(s) <= 1:
                return _problem(409, "LAST_ADMIN", "at least one active admin must remain")
            # One transaction: the account and the failed-login counter kept under its name go together (STD-4.3). There is no
            # session row to remove: a session is a signed token, and with its user gone every token naming it is refused.
            # The audit rows that name the user stay (docs/PRIVACY.md), and so do the module tables that record `smo-gui:<name>`.
            s.execute(delete(LoginFailure).where(LoginFailure.username == username))
            s.delete(user)
            s.commit()
        app.state.db.reset_totp(username)       # the one-time-code secret, recovery codes and open challenges go with the account (docs/PRIVACY.md)
        app.state.db.remove_all_pins(username)  # so do the rApps the user pinned to the sidebar
        app.state.db.remove_preferences(username)  # and the console preferences
        audit("USER_DELETED", session.user, detail=username)
        return Response(status_code=204)

    @app.post("/api/admin/users/{username}/revoke-sessions")
    def revoke_user_sessions(username: str, session: Session = Depends(require_admin)):
        """End every session of a user at once, in every instance: a session token carries the user's token version, and bumping it makes all of them
        (cookie sessions and tokens from /api/token) answer 401 SESSION_REVOKED. A sign-in half done (a challenge) ends too. PR-SEC-7.5."""
        with app.state.db.session() as s:
            user = s.get(GuiUser, username)
            if user is None:
                return _problem(404, "NO_SUCH_USER")
            user.token_version += 1
            s.commit()
        audit("USER_SESSIONS_REVOKED", session.user, detail=username)
        return {"status": "sessions revoked", "username": username}

    @app.post("/api/admin/users/{username}/reset-totp")
    def reset_user_totp(username: str, session: Session = Depends(require_admin)):
        """Remove a user's one-time code and recovery codes (a lost device): the user signs in with the password alone until a new code is enrolled. For a
        break-glass account that means it cannot sign in at all until then. Existing sessions stay; revoke them as well if the device may be in other hands."""
        with app.state.db.session() as s:
            if s.get(GuiUser, username) is None:
                return _problem(404, "NO_SUCH_USER")
        had = app.state.db.reset_totp(username)
        audit("TOTP_RESET", session.user, detail=f"{username}: {'removed' if had else 'none was set'}")
        return {"status": "one-time code removed" if had else "no one-time code was set", "username": username}

    def audit_query(username: str | None, action: str | None, since: datetime.datetime | None, until: datetime.datetime | None):
        """The audit rows matching the filters, newest first (by id: rows are appended, so id order is time order)."""
        stmt = select(AuditEntry).order_by(AuditEntry.id.desc())
        if username:
            stmt = stmt.where(AuditEntry.username == username)
        if action:
            stmt = stmt.where(AuditEntry.action == action)
        if since is not None:
            stmt = stmt.where(AuditEntry.at >= _utc(since))
        if until is not None:
            stmt = stmt.where(AuditEntry.at < _utc(until))
        return stmt

    def audit_view(e: AuditEntry) -> dict:
        return {"id": e.id, "at": e.at.isoformat(), "username": e.username, "role": e.role, "action": e.action,
                "method": e.method, "path": e.path, "statusCode": e.status_code, "detail": e.detail}

    @app.get("/api/admin/audit")
    def list_audit(limit: int = PageLimit, offset: int = PageOffset, username: str | None = None, action: str | None = None,
                   after_id: int | None = Query(None, ge=1, description="Keyset paging: only rows with an id below this one (the `nextAfterId` of the "
                                                                         "previous page). Use it instead of `offset` on a long log."),
                   since: datetime.datetime | None = Query(None, description="Only rows at or after this time (ISO 8601; no zone means UTC)."),
                   until: datetime.datetime | None = Query(None, description="Only rows before this time."),
                   session: Session = Depends(require_admin)):
        """The audit log, newest first, by offset or by keyset (`after_id`; GUI-9.5). `nextAfterId` is the id to pass for the next page, null when
        this page was not full."""
        stmt = audit_query(username, action, since, until)
        if after_id is not None:
            stmt = stmt.where(AuditEntry.id < after_id)
        with app.state.db.session() as s:
            page = _paginate(s, stmt, limit, offset)
            items = [audit_view(e) for e in page["items"]]
        return {**page, "items": items, "nextAfterId": items[-1]["id"] if items and len(items) == int(limit) else None}

    @app.get("/api/admin/audit/actions")
    def list_audit_actions(session: Session = Depends(require_admin)):
        """GUI-10.4: the audit actions the BFF writes (`AUDIT_ACTIONS`), sorted, for the audit log's action filter: `{"actions": [...]}`."""
        return {"actions": sorted(AUDIT_ACTIONS)}

    @app.get("/api/admin/audit.csv", responses={200: {"description": "The audit rows as CSV, newest first", "content": {"text/csv": {}}}})
    def export_audit(username: str | None = None, action: str | None = None,
                     since: datetime.datetime | None = Query(None, description="Only rows at or after this time (ISO 8601; no zone means UTC)."),
                     until: datetime.datetime | None = Query(None, description="Only rows before this time."),
                     session: Session = Depends(require_admin)):
        """The audit rows matching the filters as a CSV download (GUI-9.5), newest first, streamed in batches so a long log never sits in memory;
        at most 1,000,000 rows. The export itself is audited (`AUDIT_EXPORTED`) before the first row is sent."""
        filters = " ".join(f"{k}={v}" for k, v in (("username", username), ("action", action), ("since", since and _iso(_utc(since))),
                                                    ("until", until and _iso(_utc(until)))) if v)
        audit("AUDIT_EXPORTED", session.user, detail=filters or "all")
        stmt = audit_query(username, action, since, until)

        def rows() -> Iterator[str]:
            """The CSV text, a header then batches of AUDIT_CSV_BATCH rows read by keyset (each batch its own short read)."""
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(AUDIT_CSV_COLUMNS)
            sent, below = 0, None
            while sent < AUDIT_CSV_MAX_ROWS:
                batch_stmt = stmt if below is None else stmt.where(AuditEntry.id < below)
                with app.state.db.session() as s:
                    batch = s.scalars(batch_stmt.limit(min(AUDIT_CSV_BATCH, AUDIT_CSV_MAX_ROWS - sent))).all()
                for e in batch:
                    writer.writerow([_csv_cell(v) for v in (e.id, _iso(e.at), e.username, e.role, e.action, e.method, e.path, e.status_code, e.detail)])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
                if len(batch) < AUDIT_CSV_BATCH:
                    return
                sent += len(batch)
                below = batch[-1].id

        name = f"smo-gui-audit-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.csv"
        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ asynchronous export jobs (GUI-9.5b): needs audit_query above

    exports.install(app, current_session=current_session, audit=audit, problem=_problem, audit_query=audit_query, csv_cell=_csv_cell,
                    audit_columns=AUDIT_CSV_COLUMNS, iso=_iso)

    return app


# Hashed once at import: `check_credentials` verifies against it for an unknown user name, so that branch costs one scrypt as a known user does.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))

app = create_app()
