"""The BFF's southbound side: an OAuth2 client of the SMO, reaching every
module only through R1 Termination.

R1 Termination introspects a Bearer token against SME on every proxied
request (r1-termination/app/main.py's _authorized). The BFF gets its token
exactly the way an rApp does:

  1. GET {R1}/bootstrap -> the tokenEndPoint URI (SME's /oauth2/token);
  2. onboard once as a CAPIF API invoker at SME (/invoker-registrations),
     which mints an apiInvokerId + onboardingSecret — persisted, reused;
  3. client_credentials grant at the token endpoint; the access token is
     cached until shortly before expires_in, and refreshed once on a 401.

Operators never see this token and the browser never talks to R1: GUI
users authenticate to the BFF, the BFF authenticates to the SMO.
"""

import asyncio
import os
import secrets
import ssl
import time
from contextlib import suppress

import httpx

from .db import Database, SmoCredential

# smo_shared/roles.py's header, and `read_secret` for this one value: the BFF's image does not install smo_shared (see main.py).
ENROLLMENT_HEADER = "X-SMO-Enrollment"
ACTING_USER_HEADER = "X-R1-Acting-User"      # smo_shared/invoker.py's header: who is signed in, for the modules that must name a person (SEC-15.8)


def _enrollment_secret() -> str | None:
    """PR-SEC-14: the secret every SMO module mounts, from `SMO_ENROLLMENT_SECRET` or the file named by `SMO_ENROLLMENT_SECRET_FILE`."""
    value, path = os.environ.get("SMO_ENROLLMENT_SECRET", ""), os.environ.get("SMO_ENROLLMENT_SECRET_FILE", "")
    if path and not value:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().removesuffix("\n")
    return value or None


def _mtls_client_args() -> dict:
    """PR-SEC-2: with `SMO_MTLS=on`, the client certificate and the CA of this backend (`SMO_MTLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE`, default
    /run/mtls/tls.crt, tls.key, ca.crt) for every call to R1 and SME. Read once at start (a renewed certificate takes a restart of this one pod). The files
    must load: with mTLS on and no certificate this fails at start rather than calling without one."""
    if os.environ.get("SMO_MTLS", "off").strip().lower() not in ("on", "1", "true", "yes", "require"):
        return {}
    cert = os.environ.get("SMO_MTLS_CERT_FILE") or "/run/mtls/tls.crt"
    key = os.environ.get("SMO_MTLS_KEY_FILE") or "/run/mtls/tls.key"
    ca = os.environ.get("SMO_MTLS_CA_FILE") or "/run/mtls/ca.crt"
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return {"verify": context}


_EXPIRY_MARGIN_SECONDS = 30


class SmoAuthError(RuntimeError):
    pass


class R1Gateway:
    """The BFF's HTTP client for the SMO: it holds the BFF's own OAuth2 access token (obtained from SME with a CAPIF invoker identity stored in the shared database) and sends
    every call to R1 Termination with it. One instance per process; the token cache and the lock that serialises refreshes are per process, while the invoker identity is shared by
    every instance through the database. Failures to obtain a token are raised as `SmoAuthError`; transport failures are left as httpx errors for the caller to turn into 502s.
    """
    def __init__(self, r1_url: str, db: Database, sme_url: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        """Builds the gateway for `r1_url` (the SME token endpoint is taken from `sme_url` when given, else discovered from R1's /bootstrap on first use) with its httpx client.
        With `SMO_MTLS` on the client presents the backend's certificate and trusts the configured CA, and fails here if those files cannot be loaded. `transport` is for tests.
        """
        self.r1_url = r1_url.rstrip("/")
        self._sme_url_override = sme_url
        self._db = db
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, **_mtls_client_args())
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_endpoint: str | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ token

    async def _discover_token_endpoint(self) -> str:
        """The SME token endpoint URL: the `sme_url` override plus /oauth2/token when set, else the first `tokenEndPoint` that R1's /bootstrap advertises. Cached for the
        life of the process once found. Raises SmoAuthError when R1 answers anything but 200 or advertises no token endpoint.
        """
        if self._token_endpoint:
            return self._token_endpoint
        if self._sme_url_override:
            self._token_endpoint = f"{self._sme_url_override}/oauth2/token"
            return self._token_endpoint
        resp = await self._client.get(f"{self.r1_url}/bootstrap")
        if resp.status_code != 200:
            raise SmoAuthError(f"R1 /bootstrap returned {resp.status_code}")
        for ep in resp.json().get("apiEndpoints", []):
            uri = (ep.get("tokenEndPoint") or {}).get("uri")
            if uri:
                self._token_endpoint = uri
                return uri
        raise SmoAuthError("R1 /bootstrap advertised no tokenEndPoint")

    def _sme_base(self, token_endpoint: str) -> str:
        return self._sme_url_override or token_endpoint.rsplit("/oauth2/token", 1)[0]

    async def _onboard_invoker(self, token_endpoint: str, stale: SmoCredential | None = None) -> SmoCredential:
        """Register at SME and store the identity for every instance of this database (PR-ST-5). If another
        instance stored its own first (or, with `stale`, replaced the refused identity first), this instance
        offboards the duplicate it just registered and adopts the stored one."""
        # An opaque per-BFF label, not a PEM key: the BFF authenticates with
        # its onboarding secret, so SME has no key to verify assertions with
        # (an RFC 7523 client assertion needs a PEM key, SA-SME-1-public-key).
        sme = self._sme_base(token_endpoint)
        # PR-SEC-14: the BFF is an SMO module, so it presents the enrollment secret and SME records it as internal; the operator's authority
        # is the BFF's own login and RBAC (rbac.py), and R1 sees the BFF.
        enrollment = _enrollment_secret()
        resp = await self._client.post(f"{sme}/invoker-registrations",
                                       json={"apiInvokerPublicKey": f"smo-gui-bff:{secrets.token_urlsafe(16)}"},
                                       headers={ENROLLMENT_HEADER: enrollment} if enrollment else {})
        if resp.status_code != 201:
            raise SmoAuthError(f"SME invoker onboarding returned {resp.status_code}")
        body = resp.json()
        stale_id = stale.api_invoker_id if stale is not None else None
        if self._db.store_smo_credential(body["apiInvokerId"], body["onboardingSecret"], stale_invoker_id=stale_id):
            return SmoCredential(id=1, api_invoker_id=body["apiInvokerId"], onboarding_secret=body["onboardingSecret"])
        with suppress(httpx.HTTPError):   # an orphan registration is only clutter; SME's stale-invoker purge removes it
            await self._client.delete(f"{sme}/invoker-registrations/{body['apiInvokerId']}")
        return self._stored_credential() or await self._onboard_invoker(token_endpoint)

    def _stored_credential(self) -> SmoCredential | None:
        with self._db.session() as s:
            return s.get(SmoCredential, 1)

    async def _request_token(self, token_endpoint: str, cred: SmoCredential) -> httpx.Response:
        return await self._client.post(token_endpoint, json={
            "grant_type": "client_credentials", "client_id": cred.api_invoker_id,
            "client_secret": cred.onboarding_secret, "scope": "smo-gui",
        })

    async def token(self, force_refresh: bool = False) -> str:
        """The BFF's access token for R1, from the cache while it is valid (expiry less a 30 s margin) unless `force_refresh`. Otherwise: discover the endpoint, take the stored invoker
        identity (registering at SME when there is none), and request a client-credentials token; a 400 means SME no longer knows the invoker, so it is registered again once and the
        request repeated. Holds the lock throughout, so concurrent callers wait for one refresh instead of each doing their own. Raises SmoAuthError when SME is unreachable or does
        not answer 200.
        """
        async with self._lock:
            if not force_refresh and self._token and time.time() < self._token_expires_at:
                return self._token
            try:
                token_endpoint = await self._discover_token_endpoint()
                cred = self._stored_credential() or await self._onboard_invoker(token_endpoint)
                resp = await self._request_token(token_endpoint, cred)
                if resp.status_code == 400:
                    # SME no longer knows this invoker (e.g. its DB was
                    # reset): onboard afresh, once.
                    cred = await self._onboard_invoker(token_endpoint, stale=cred)
                    resp = await self._request_token(token_endpoint, cred)
            except httpx.HTTPError as exc:
                raise SmoAuthError(f"SME unreachable: {exc.__class__.__name__}") from exc
            if resp.status_code != 200:
                raise SmoAuthError(f"SME token endpoint returned {resp.status_code}")
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires_at = time.time() + max(int(body.get("expires_in", 60)) - _EXPIRY_MARGIN_SECONDS, 1)
            return self._token

    # ------------------------------------------------------------ calls

    async def request(self, method: str, path: str, *, params=None, content: bytes | None = None,
                      headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
        """One call to R1 Termination at `path` (already /<module>/...),
        with the BFF's Bearer token. Retried once with a fresh token if R1
        answers 401 (token expired or revoked at SME).
        """
        url = f"{self.r1_url}{path}"
        kwargs = {"params": params, "content": content}
        if timeout is not None:
            kwargs["timeout"] = timeout
        for attempt in (0, 1):
            token = await self.token(force_refresh=attempt == 1)
            resp = await self._client.request(method, url, headers={**(headers or {}), "Authorization": f"Bearer {token}"}, **kwargs)
            if resp.status_code != 401:
                return resp
        return resp

    async def r1_health(self, timeout: float) -> httpx.Response:
        return await self.r1_get("/health", timeout)

    async def r1_get(self, path: str, timeout: float) -> httpx.Response:
        """One of R1 Termination's own unauthenticated routes (`/health`, `/ready`, `/version`): no token."""
        return await self._client.get(f"{self.r1_url}{path}", timeout=timeout)
