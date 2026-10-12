"""GUI BFF settings — all from the environment, nothing secret in git.

GUI_JWT_SECRET / GUI_ADMIN_PASSWORD may be left unset for a throwaway demo.
An unset GUI_JWT_SECRET is not a per-process random value: the first instance
generates one and stores it in the BFF's database (`gui_setting`), and every
instance (and restart) using that database signs and verifies with the same key,
so run replicas against one shared GUI_DATABASE_URL. A generated admin password is
written to GUI_INITIAL_PASSWORD_FILE (mode 0600), never logged; the log only
says where it is. A real deployment sets both.

OIDC login (PR-SEC-6) is opt-in: GUI_OIDC_ENABLED=true plus the issuer, client and redirect URI. app/oidc.py validates
the combination when the app is built, so a half-configured provider stops the start instead of failing at the first
sign-in. The client's credential comes from GUI_OIDC_CLIENT_SECRET or, better, the file named by GUI_OIDC_CLIENT_SECRET_FILE.
"""

import os
import secrets
from dataclasses import dataclass, field


def mtls_on() -> bool:
    """PR-SEC-2: this backend's calls to R1 present its client certificate (the same `SMO_MTLS` switch as smo_shared/mtls.py, which this image does not install)."""
    return os.environ.get("SMO_MTLS", "off").strip().lower() in ("on", "1", "true", "yes", "require")


def _internal_url(url: str) -> str:
    return "https://" + url[len("http://"):] if mtls_on() and url.startswith("http://") else url


def _client_credential() -> str:
    """The OIDC client's credential: `GUI_OIDC_CLIENT_SECRET`, or the file named by `GUI_OIDC_CLIENT_SECRET_FILE` (a mounted secret).
    Both set is an error (which one wins is not something to guess about a credential); an unreadable file stops the start."""
    value, path = os.environ.get("GUI_OIDC_CLIENT_SECRET", ""), os.environ.get("GUI_OIDC_CLIENT_SECRET_FILE", "")
    if value and path:
        raise ValueError("both GUI_OIDC_CLIENT_SECRET and GUI_OIDC_CLIENT_SECRET_FILE are set: set only one")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().removesuffix("\n")
        except OSError as exc:
            raise ValueError(f"GUI_OIDC_CLIENT_SECRET_FILE={path} cannot be read: {exc.strerror or type(exc).__name__}") from exc
    return value


def _totp_key() -> str:
    """The key that encrypts one-time-code secrets at rest (PR-SEC-7): `GUI_TOTP_KEY`, or the file named by `GUI_TOTP_KEY_FILE` (a mounted secret). Same rules as the OIDC
    client credential: both set is an error, an unreadable file stops the start. Empty means no key, and enrolment is then refused."""
    value, path = os.environ.get("GUI_TOTP_KEY", ""), os.environ.get("GUI_TOTP_KEY_FILE", "")
    if value and path:
        raise ValueError("both GUI_TOTP_KEY and GUI_TOTP_KEY_FILE are set: set only one")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().removesuffix("\n")
        except OSError as exc:
            raise ValueError(f"GUI_TOTP_KEY_FILE={path} cannot be read: {exc.strerror or type(exc).__name__}") from exc
    return value


def _read_key_file(name: str, path: str) -> str:
    """Returns the text of the key file at `path`; `name` is the environment variable that named it, used only in the error.
    An unreadable file raises ValueError (naming the variable and the OS reason, never the contents), so a bad mount stops the start instead of failing at the first sign-in.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError(f"{name}={path} cannot be read: {exc.strerror or type(exc).__name__}") from exc


def _jwt_algorithm() -> str:
    """PR-SEC-5: the session token's algorithm, HS256 (the shared `GUI_JWT_SECRET`, the default) or RS256 / ES256 (a key pair). Anything else stops the start."""
    algorithm = os.environ.get("GUI_JWT_ALGORITHM", "").strip().upper() or "HS256"
    if algorithm not in ("HS256", "RS256", "ES256"):
        raise ValueError(f"GUI_JWT_ALGORITHM={algorithm!r} is not one of HS256, RS256, ES256")
    return algorithm


def _jwt_private_key() -> str:
    """The PEM private key that signs session tokens under RS256 / ES256: the file named by `GUI_JWT_PRIVATE_KEY_FILE` (a mounted secret; no inline form, a PEM in an
    environment variable is what the file convention exists to avoid). Empty when unset; an unreadable file stops the start."""
    path = os.environ.get("GUI_JWT_PRIVATE_KEY_FILE", "").strip()
    return _read_key_file("GUI_JWT_PRIVATE_KEY_FILE", path) if path else ""


def _jwt_previous_keys() -> list[tuple[str, str]]:
    """(label, PEM) of each file in `GUI_JWT_PREVIOUS_KEY_FILES` (comma separated; a public key is enough): the keys of earlier rotations, which still verify."""
    raw = os.environ.get("GUI_JWT_PREVIOUS_KEY_FILES", "")
    return [(f"GUI_JWT_PREVIOUS_KEY_FILES entry {path}", _read_key_file("GUI_JWT_PREVIOUS_KEY_FILES", path)) for path in (p.strip() for p in raw.split(",")) if path]


def _login_mode() -> str:
    mode = os.environ.get("GUI_LOGIN_MODE", "").strip().lower() or "both"
    if mode not in ("both", "oidc", "local"):
        raise ValueError(f"GUI_LOGIN_MODE={mode!r} is not one of both, oidc, local")
    return mode


def _non_negative_int(name: str, default: int) -> int:
    """The integer in the environment variable `name` (`default` when unset); ValueError, naming the variable, when it is not a whole number of 0 or more."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a whole number") from exc
    if value < 0:
        raise ValueError(f"{name}={raw!r} must be 0 or more")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Every environment setting of the BFF, read once when the object is built (the module-level `settings` below).
    Each field's default is a factory, so the environment is read at construction and a test can build a fresh `Settings()` after changing it. Nothing here is a secret
    in git: secrets come from the environment or a mounted file. An empty `jwt_secret` is replaced in `__post_init__`; app/signing.py and app/main.py then decide, with
    `jwt_secret_generated`, whether to adopt the database's shared key instead.
    """
    r1_url: str = field(default_factory=lambda: _internal_url(os.environ.get("R1_URL", "http://r1-termination:8000")).rstrip("/"))
    # Optional override. By default the SME token endpoint is discovered the
    # same way an rApp discovers it: from R1 Termination's own /bootstrap.
    sme_url: str | None = field(default_factory=lambda: _internal_url(os.environ.get("SME_URL") or "").rstrip("/") or None)
    database_url: str = field(default_factory=lambda: os.environ.get("GUI_DATABASE_URL", "sqlite:///./gui-bff.db"))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("GUI_JWT_SECRET", ""))
    # ---- PR-SEC-5: the session token's signing key. HS256 (default) signs with jwt_secret as always; RS256 / ES256 sign with the private key and
    # publish the public keys at /.well-known/jwks.json. app/signing.py checks the combination when the app is built.
    jwt_algorithm: str = field(default_factory=_jwt_algorithm)
    jwt_private_key: str = field(default_factory=_jwt_private_key)
    jwt_previous_keys: list[tuple[str, str]] = field(default_factory=_jwt_previous_keys)
    session_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("GUI_SESSION_TTL_SECONDS", str(8 * 3600))))
    # Secure cookies are the default. Browsers already treat http://localhost as
    # a secure context, so this only needs turning off for plain-http access by
    # a non-localhost hostname (e.g. a lab VM reached by IP).
    cookie_secure: bool = field(default_factory=lambda: _bool("GUI_COOKIE_SECURE", True))
    admin_password: str = field(default_factory=lambda: os.environ.get("GUI_ADMIN_PASSWORD", ""))
    # Where a generated admin password is written (mode 0600) when
    # GUI_ADMIN_PASSWORD is unset — never to the log.
    initial_password_file: str = field(default_factory=lambda: os.environ.get("GUI_INITIAL_PASSWORD_FILE", "./initial-admin-password"))
    operator_password: str = field(default_factory=lambda: os.environ.get("GUI_OPERATOR_PASSWORD", ""))
    viewer_password: str = field(default_factory=lambda: os.environ.get("GUI_VIEWER_PASSWORD", ""))
    # SEC-15.11: how many reverse proxies stand between a browser and this backend (the console's nginx is one). The sign-in lockout and throttle read the client's address from
    # the entry that many places from the right of X-Forwarded-For; 0 ignores the header (clients reach the backend directly). Negative values stop the start.
    trusted_proxy_hops: int = field(default_factory=lambda: _non_negative_int("GUI_TRUSTED_PROXY_HOPS", 1))
    health_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("GUI_HEALTH_TIMEOUT_SECONDS", "3")))
    upstream_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("GUI_UPSTREAM_TIMEOUT_SECONDS", "30")))
    # ---- PR-SEC-6: OIDC login (off by default). Local username/password login stays unless GUI_LOCAL_LOGIN_ENABLED=false.
    local_login_enabled: bool = field(default_factory=lambda: _bool("GUI_LOCAL_LOGIN_ENABLED", True))
    oidc_enabled: bool = field(default_factory=lambda: _bool("GUI_OIDC_ENABLED", False))
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_ISSUER", "").strip().rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_CLIENT_ID", "").strip())
    oidc_client_credential: str = field(default_factory=_client_credential)
    oidc_redirect_uri: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_REDIRECT_URI", "").strip())
    oidc_scopes: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_SCOPES", "") or "openid profile email")
    oidc_groups_claim: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_GROUPS_CLAIM", "").strip() or "groups")
    oidc_group_role_map: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_GROUP_ROLE_MAP", ""))
    oidc_default_role: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_DEFAULT_ROLE", "").strip().lower())
    # GUI-5: the ID-token claim that holds the user's region/tenant scope (`{"regions": [...], "tenants": [...]}`); empty (default): scopes are set by an admin
    oidc_scope_claim: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_SCOPE_CLAIM", "").strip())
    oidc_provider_name: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_PROVIDER_NAME", "SSO").strip() or "SSO")
    oidc_post_logout_redirect_uri: str = field(default_factory=lambda: os.environ.get("GUI_OIDC_POST_LOGOUT_REDIRECT_URI", "").strip())
    oidc_allow_http: bool = field(default_factory=lambda: _bool("GUI_OIDC_ALLOW_HTTP", False))
    oidc_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("GUI_OIDC_TIMEOUT_SECONDS", "10")))
    # ---- PR-SEC-7: multi-factor sign-in. `both` offers OIDC (when configured) and the local form; `oidc` closes the local form to every account but a break-glass one,
    # so the identity provider's second factor applies; `local` does not offer OIDC even when it is configured.
    login_mode: str = field(default_factory=_login_mode)
    # A local admin without an enrolled one-time code can reach only the enrolment routes (off by default).
    admin_mfa_required: bool = field(default_factory=lambda: _bool("GUI_ADMIN_MFA_REQUIRED", False))
    totp_key: str = field(default_factory=_totp_key)
    totp_issuer: str = field(default_factory=lambda: os.environ.get("GUI_TOTP_ISSUER", "").strip() or "SMO Operator Console")
    jwt_secret_generated: bool = False

    def __post_init__(self) -> None:
        if not self.jwt_secret:
            self.jwt_secret = secrets.token_urlsafe(48)
            self.jwt_secret_generated = True


settings = Settings()
