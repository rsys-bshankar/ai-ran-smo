"""OpenID Connect login for the operator GUI (PR-SEC-6): authorization-code flow with PKCE, against one configured identity provider.

What this module is: the relying-party half that has no web framework in it. `OidcClient` talks to the provider (discovery, JWKS, the token
endpoint) and checks what comes back; `main.py` owns the routes, the database rows for sign-ins in flight, the user rows and the session.

  * Discovery (`{issuer}/.well-known/openid-configuration`) and the provider's signing keys (JWKS) are fetched over HTTPS with an explicit
    timeout and cached in the process for a while. A cache of public provider metadata is safe per replica: each instance may hold a
    different, equally valid copy. A key id the cache does not know makes one refetch (the provider rotated its keys), at most once per
    `JWKS_REFETCH_MIN_SECONDS`, so a forged `kid` cannot make the BFF hammer the provider. If the provider cannot be reached the last good copy keeps serving.
  * ID token validation: the signature against the JWKS key with the token's `kid`; the algorithm must be one of `ALLOWED_ALGORITHMS`
    (asymmetric only, so `none` and an HMAC keyed with the public key are refused before any key is looked at); `iss` equals the configured
    issuer; `aud` contains the client id (and `azp`, when present or when there are several audiences, equals it); `exp`, `iat`, `sub` are present and
    `exp`/`nbf`/`iat` are checked with a small leeway; `nonce` equals the one this sign-in started with; `at_hash`, when the token carries one, matches the
    access token that came with it.
  * Roles come from one claim of the ID token (a list, or a string split on spaces or commas; a dotted name reaches into an object, such as
    `realm_access.roles`), through an operator-configured group-to-role table. The highest role matched wins. No match: the configured default
    role, and by default none, which is a refusal.

No token, code or client credential is ever written to a log; errors carry a short reason code from `REASONS`, never the provider's text.
"""

import base64
import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
import jwt
from jwt import PyJWK

from .config import Settings
from .rbac import RANK, Role

# Why a sign-in was refused. The code is all the browser is told (as `?oidc_error=<code>` on the sign-in page) and what the audit row says.
REASONS = {
    "idp_unavailable": "the identity provider could not be reached",
    "idp_error": "the identity provider reported an error",
    "access_denied": "the identity provider did not grant the sign-in",
    "invalid_state": "the sign-in was not started here, or has expired or been used",
    "token_exchange_failed": "the identity provider refused the authorization code",
    "token_invalid": "the ID token did not pass validation",
    "no_role": "the account has no group that maps to a role",
    "account_disabled": "the account is disabled",
    "invalid_scope": "the token's scope claim is not a valid region/tenant scope",
    "too_many_logins": "too many sign-ins are waiting; try again shortly",
}

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
_HASH_BITS = {"256": hashlib.sha256, "384": hashlib.sha384, "512": hashlib.sha512}
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
JWKS_REFETCH_MIN_SECONDS = 30
CLOCK_LEEWAY_SECONDS = 30
LOGIN_TTL_SECONDS = 600
MAX_PENDING_LOGINS = 5000
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_SCOPE_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")      # RFC 6749 scope-token


class OidcError(Exception):
    """A sign-in that failed. `code` is one of `REASONS`; `detail` is for the audit row (a class name or a claim name, never a token)."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class OidcConfig:
    """The validated, immutable OIDC settings of one provider. Built only by `from_settings`, which refuses a half-configured combination, so a client that
    exists always has an issuer, client id, redirect URI, a scope list with `openid`, and a way to give a role (a group map, a default role, or both).
    """
    issuer: str
    client_id: str
    client_credential: str
    redirect_uri: str
    scopes: str
    groups_claim: str
    group_roles: dict[str, Role]
    default_role: Role | None
    provider_name: str
    post_logout_redirect_uri: str
    allow_http: bool
    timeout: float

    @classmethod
    def from_settings(cls, cfg: Settings) -> "OidcConfig":
        """Validate the OIDC settings; raises ValueError naming the variable at fault. Called when the app is built, so a bad combination stops the start."""
        problems = []
        if not cfg.oidc_issuer:
            problems.append("GUI_OIDC_ISSUER is required")
        if not cfg.oidc_client_id:
            problems.append("GUI_OIDC_CLIENT_ID is required")
        if not cfg.oidc_redirect_uri:
            problems.append("GUI_OIDC_REDIRECT_URI is required (the public URL of /api/oidc/callback, registered at the provider)")
        for name, value in (("GUI_OIDC_ISSUER", cfg.oidc_issuer), ("GUI_OIDC_REDIRECT_URI", cfg.oidc_redirect_uri),
                            ("GUI_OIDC_POST_LOGOUT_REDIRECT_URI", cfg.oidc_post_logout_redirect_uri)):
            if value and not _acceptable_url(value, cfg.oidc_allow_http):
                problems.append(f"{name} must be an absolute https URL (http only for localhost, or with GUI_OIDC_ALLOW_HTTP=true)")
        scopes = cfg.oidc_scopes.split()
        if "openid" not in scopes:
            problems.append("GUI_OIDC_SCOPES must include openid")
        if not all(_SCOPE_RE.match(s) for s in scopes):
            problems.append("GUI_OIDC_SCOPES holds an invalid scope")
        if not cfg.oidc_groups_claim:
            problems.append("GUI_OIDC_GROUPS_CLAIM must not be empty")
        group_roles = _parse_group_roles(cfg.oidc_group_role_map, problems)
        if not group_roles and not cfg.oidc_default_role:
            problems.append("GUI_OIDC_GROUP_ROLE_MAP is empty and GUI_OIDC_DEFAULT_ROLE is unset: nobody could sign in")
        default_role: Role | None = None
        if cfg.oidc_default_role:
            try:
                default_role = Role(cfg.oidc_default_role)
            except ValueError:
                problems.append("GUI_OIDC_DEFAULT_ROLE must be viewer, operator or admin (or empty to deny)")
        if cfg.oidc_timeout_seconds <= 0:
            problems.append("GUI_OIDC_TIMEOUT_SECONDS must be positive")
        if problems:
            raise ValueError("invalid OIDC configuration: " + "; ".join(problems))
        return cls(issuer=cfg.oidc_issuer, client_id=cfg.oidc_client_id, client_credential=cfg.oidc_client_credential,
                   redirect_uri=cfg.oidc_redirect_uri, scopes=" ".join(scopes), groups_claim=cfg.oidc_groups_claim,
                   group_roles=group_roles, default_role=default_role, provider_name=cfg.oidc_provider_name,
                   post_logout_redirect_uri=cfg.oidc_post_logout_redirect_uri, allow_http=cfg.oidc_allow_http, timeout=cfg.oidc_timeout_seconds)


def _acceptable_url(url: str, allow_http: bool) -> bool:
    parts = urlsplit(url)
    if not parts.netloc or parts.fragment:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and (allow_http or (parts.hostname or "") in _LOOPBACK))


def _parse_group_roles(text: str, problems: list[str]) -> dict[str, Role]:
    """`group=role,group2=role2` (the group may contain `=`: the role is what follows the last one)."""
    table: dict[str, Role] = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        group, _, role = item.rpartition("=")
        group = group.strip()
        try:
            table[group] = Role(role.strip().lower())
        except ValueError:
            problems.append(f"GUI_OIDC_GROUP_ROLE_MAP entry {item!r} is not group=viewer|operator|admin")
            continue
        if not group:
            problems.append("GUI_OIDC_GROUP_ROLE_MAP has an entry with no group name")
    return table


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)) without padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def claim_at(claims: dict, path: str) -> object:
    """The claim named `path`. An exact top-level name wins (a claim may legitimately contain dots); otherwise a dotted path reaches into objects."""
    if path in claims:
        return claims[path]
    node: object = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def groups_from(claims: dict, path: str) -> list[str]:
    """The groups named by the claim at `path` as a list of strings: a list claim keeps its string items, a string claim is split on spaces and commas, anything else
    (a missing claim, a number, an object) gives an empty list, which means no group and so, by `role_for`, the default role or a refusal.
    """
    value = claim_at(claims, path)
    if isinstance(value, str):
        return [g for g in re.split(r"[\s,]+", value) if g]
    if isinstance(value, list):
        return [g for g in value if isinstance(g, str)]
    return []


class OidcClient:
    """The relying-party client for the one configured provider. It owns the provider documents it fetched (discovery, signing keys), cached in memory with a lock,
    and the checks of the ID token; it holds no per-user state and writes nothing to the database. `transport` replaces the HTTP transport (tests use it to stand in for the
    provider) and `clock` the time source. Every failure is raised as `OidcError` with a code from `REASONS`.
    """
    def __init__(self, cfg: OidcConfig, transport: httpx.BaseTransport | None = None, clock=time.time):
        """Stores the configuration and the injected transport and clock, and starts with empty caches (no network call is made here: the first discovery happens on first use).
        """
        self.cfg = cfg
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._discovery: dict | None = None
        self._discovery_at = 0.0
        self._keys: dict[str, dict] = {}       # kid -> JWK
        self._keys_at = 0.0
        self._keys_attempt_at = 0.0

    # ------------------------------------------------------------ the provider's documents

    def _get_json(self, url: str) -> dict:
        """GETs `url` with the configured timeout and returns the JSON object. Any transport error, a non-JSON body, a status other than 200 or a body that is not an
        object is raised as `OidcError("idp_unavailable")` carrying only the exception class or the status, never the response text.
        """
        try:
            with httpx.Client(timeout=self.cfg.timeout, transport=self._transport) as client:
                resp = client.get(url, headers={"Accept": "application/json"})
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("idp_unavailable", type(exc).__name__) from exc
        if resp.status_code != 200 or not isinstance(body, dict):
            raise OidcError("idp_unavailable", f"HTTP {resp.status_code}")
        return body

    def discovery(self) -> dict:
        """The provider's discovery document, from the cache while it is younger than `DISCOVERY_TTL_SECONDS`, else fetched and validated: the `issuer` in it must equal the
        configured one (OIDC Discovery 4.3, so a document served for another issuer is refused), and the authorization, token and JWKS endpoints must be acceptable URLs (https,
        or http for loopback or with `GUI_OIDC_ALLOW_HTTP`). An unusable `end_session_endpoint` is dropped rather than refused. When a refresh fails the last good copy is returned
        instead; with none, the OidcError is raised. The network call is made outside the lock, so concurrent callers may both fetch (harmless: the copies are equally valid).
        """
        now = self._clock()
        with self._lock:
            if self._discovery is not None and now - self._discovery_at < DISCOVERY_TTL_SECONDS:
                return self._discovery
            stale = self._discovery
        try:
            doc = self._get_json(f"{self.cfg.issuer}/.well-known/openid-configuration")
            if doc.get("issuer") != self.cfg.issuer:      # OIDC Discovery 4.3: the issuer in the document is the one asked for
                raise OidcError("idp_error", "issuer mismatch in the discovery document")
            for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = doc.get(name)
                if not isinstance(value, str) or not _acceptable_url(value, self.cfg.allow_http):
                    raise OidcError("idp_error", f"discovery document has no usable {name}")
            end_session = doc.get("end_session_endpoint")
            if end_session is not None and not (isinstance(end_session, str) and _acceptable_url(end_session, self.cfg.allow_http)):
                doc.pop("end_session_endpoint")
        except OidcError:
            if stale is not None:
                return stale
            raise
        with self._lock:
            self._discovery, self._discovery_at = doc, now
        return doc

    def _load_keys(self, now: float) -> None:
        """Fetches the provider's JWKS and replaces the cached key table, keeping only RSA and EC signature keys that have a string `kid`.
        Raises OidcError when the fetch fails or the document has no `keys` list; the caller decides whether the old table may keep serving.
        """
        body = self._get_json(self.discovery()["jwks_uri"])
        keys = body.get("keys")
        if not isinstance(keys, list):
            raise OidcError("idp_error", "JWKS has no keys")
        with self._lock:
            self._keys = {k["kid"]: k for k in keys if isinstance(k, dict) and isinstance(k.get("kid"), str)
                          and k.get("kty") in ("RSA", "EC") and k.get("use", "sig") == "sig"}
            self._keys_at = now

    def _signing_key(self, kid: str | None) -> PyJWK:
        """The key for the ID token's `kid`, as a PyJWK. The JWKS is fetched when the cache is empty or older than `JWKS_TTL_SECONDS`, and refetched when the `kid` is unknown
        (the provider may have rotated), but at most once per `JWKS_REFETCH_MIN_SECONDS`, so a token with a forged `kid` cannot make the BFF hammer the provider. If a refetch fails and
        there is an older table it is used. Raises OidcError("token_invalid") for an unknown or unusable key; the attempt time is recorded under the lock before the network call.
        """
        now = self._clock()
        with self._lock:
            fresh = bool(self._keys) and now - self._keys_at < JWKS_TTL_SECONDS
            found = self._keys.get(kid) if kid and fresh else None
            may_refetch = now - self._keys_attempt_at >= JWKS_REFETCH_MIN_SECONDS
            if found is None and (not fresh or may_refetch):
                self._keys_attempt_at = now
                refetch = True
            else:
                refetch = False
        if found is None and refetch:      # first use, an expired cache, or a key id we do not know: the provider may have rotated
            try:
                self._load_keys(now)
            except OidcError:
                if not self._keys:
                    raise
            with self._lock:
                found = self._keys.get(kid) if kid else None
        if found is None:
            raise OidcError("token_invalid", "no signing key with this kid")
        try:
            return PyJWK(found)
        except jwt.PyJWTError as exc:
            raise OidcError("token_invalid", "unusable signing key") from exc

    # ------------------------------------------------------------ the flow

    def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        """The provider URL that starts the sign-in: the code flow with this client's id, redirect URI and scopes, the caller's `state` and `nonce`, and the S256
        PKCE challenge derived from `verifier` (the verifier itself is not sent). May fetch the discovery document, so it can raise OidcError("idp_unavailable" or "idp_error").
        """
        query = urlencode({"response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_uri,
                           "scope": self.cfg.scopes, "state": state, "nonce": nonce,
                           "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256"})
        endpoint = self.discovery()["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange_code(self, code: str, verifier: str) -> dict:
        """Exchanges the authorization `code` (with the PKCE `verifier`) at the provider's token endpoint and returns the token response, which is guaranteed to hold a
        non-empty `id_token`. The client authenticates with HTTP Basic (RFC 6749 2.3.1) when it has a credential, with a form post instead when the provider lists only
        `client_secret_post`, and as a public client (id only, PKCE as the proof) when it has no credential. Raises OidcError("idp_unavailable") for a transport failure and
        ("token_exchange_failed") for a refusal or a response with no ID token. The code, verifier and credential are never logged.
        """
        doc = self.discovery()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg.redirect_uri, "code_verifier": verifier}
        auth = None
        methods = doc.get("token_endpoint_auth_methods_supported")
        if self.cfg.client_credential:
            if isinstance(methods, list) and "client_secret_basic" not in methods and "client_secret_post" in methods:
                form.update(client_id=self.cfg.client_id, client_secret=self.cfg.client_credential)
            else:
                auth = httpx.BasicAuth(quote_plus(self.cfg.client_id), quote_plus(self.cfg.client_credential))   # RFC 6749 2.3.1
        else:
            form["client_id"] = self.cfg.client_id            # a public client: PKCE is its proof
        try:
            with httpx.Client(timeout=self.cfg.timeout, transport=self._transport) as client:
                resp = client.post(doc["token_endpoint"], data=form, auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                                   headers={"Accept": "application/json"})
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("idp_unavailable", type(exc).__name__) from exc
        if resp.status_code != 200 or not isinstance(body, dict):
            raise OidcError("token_exchange_failed", f"HTTP {resp.status_code}")
        if not isinstance(body.get("id_token"), str) or not body["id_token"]:
            raise OidcError("token_exchange_failed", "no id_token in the response")
        return body

    def validate_id_token(self, id_token: str, nonce: str, access_token: str | None = None) -> dict:
        """The claims of an ID token that passed every check in the module docstring; OidcError('token_invalid') otherwise."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError("token_invalid", "malformed token") from exc
        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            raise OidcError("token_invalid", f"algorithm {alg!r} is not accepted")
        key = self._signing_key(header.get("kid") if isinstance(header.get("kid"), str) else None)
        try:
            claims = jwt.decode(id_token, key=key, algorithms=[alg], audience=self.cfg.client_id, issuer=self.cfg.issuer,
                                leeway=CLOCK_LEEWAY_SECONDS, options={"require": ["exp", "iat", "sub", "iss", "aud"]}) 
        except jwt.PyJWTError as exc:
            raise OidcError("token_invalid", type(exc).__name__) from exc
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise OidcError("token_invalid", "bad sub")
        aud = claims.get("aud")
        if (claims.get("azp") is not None or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != self.cfg.client_id:
            raise OidcError("token_invalid", "azp is not this client")
        got = claims.get("nonce")
        if not isinstance(got, str) or not hmac.compare_digest(got.encode(), nonce.encode()):
            raise OidcError("token_invalid", "nonce mismatch")
        at_hash = claims.get("at_hash")
        if at_hash is not None and access_token:
            digest = _HASH_BITS[alg[-3:]](access_token.encode("ascii", "replace")).digest()
            expected = base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode()
            if not isinstance(at_hash, str) or not hmac.compare_digest(at_hash.encode(), expected.encode()):
                raise OidcError("token_invalid", "at_hash mismatch")
        return claims

    def role_for(self, claims: dict) -> Role | None:
        """The highest role any of the user's groups maps to; the default role when none does; None (deny) when there is no default."""
        matched = [self.cfg.group_roles[g] for g in groups_from(claims, self.cfg.groups_claim) if g in self.cfg.group_roles]
        if matched:
            return max(matched, key=lambda r: RANK[r])
        return self.cfg.default_role

    def end_session_url(self) -> str | None:
        """RP-initiated logout (OIDC RP-Initiated Logout 1.0), when the provider advertises an `end_session_endpoint`. No `id_token_hint` is sent
        (the BFF keeps no ID token): the client id identifies the client instead, and the provider may ask the person to confirm."""
        try:
            endpoint = self.discovery().get("end_session_endpoint")
        except OidcError:
            return None
        if not endpoint:
            return None
        params = {"client_id": self.cfg.client_id}
        if self.cfg.post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = self.cfg.post_logout_redirect_uri
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"
