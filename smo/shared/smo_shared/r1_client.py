"""Thin HTTP client every module uses to call another module THROUGH R1
Termination, rather than service-to-service directly — matching the
topology every LLD assumes (rApp -> R1 Termination -> backend SMOS) and
extending it to SMO-internal cross-module calls (e.g. SO SMOS dispatching
a CONFIG step to RAN NF OAM, NFO calling FOCOM's QueryInventory).

Phase 1: R1 Termination's routing table (Foundational Platform LLD section 4.2)
maps path prefixes to backend services on the same Docker network, so this
client is a small wrapper over httpx rather than a real service mesh client.

Authentication: R1 Termination introspects a Bearer token against SME on
every proxied route (r1-termination/app/main.py's _authorized — "auth:
oauth2 on every backend route except /bootstrap"). SMO-internal callers go
through that same gate, so unless a caller passes its own bearer_token, the
client obtains one the way an rApp does: the token endpoint advertised by
R1's /bootstrap, a CAPIF API-invoker identity onboarded once at SME, and a
client_credentials grant. One identity and one cached token per process;
refreshed shortly before expiry, and once on a 401. Before this, every
internal call was sent without any Authorization header and R1 rejected it
(401), which silently failed e.g. Onboarding's NFO CreateDescriptor call and
rApp Management's package-status check on a real deployment. The
in-process integration mesh (tests_integration/mesh.py) bypasses R1's
gateway mechanics, so it never saw this.

PR-SEC-9.3: when the gateway is run with `R1_BOOTSTRAP_KEY[_FILE]`, `GET /bootstrap` needs the header `X-Bootstrap-Key`; this client
sends it when `SMO_BOOTSTRAP_KEY[_FILE]` is set (the SDK and the sample rApps reach /bootstrap only through this client).

SMO_INVOKER_ID / SMO_INVOKER_SECRET pin a pre-provisioned invoker instead
of onboarding a fresh one at first use.

PR-SEC-2: with `SMO_MTLS=on` every call below presents this module's client certificate and verifies the server against the CA
(`smo_shared/mtls.py`); the gateway and the token endpoint it advertises are `https://` then.

PR-SEC-14, who the caller is: an SMO module presents the enrollment secret
(`SMO_ENROLLMENT_SECRET[_FILE]`, a compose secret) when it registers and asks for
the `smo-internal` scope; SME records the invoker as `internal`. A process that
is an rApp (`SMO_IDENTITY_KIND=rapp`, the sample rApps' compose entries) presents
none, is recorded as `rapp`, asks for `smo-rapp`, and keeps its identity under its
own key in the module identity store. R1 Termination applies its role policy on
that record (`roles.py`).
"""

import contextlib
import contextvars
from collections.abc import Callable
import logging
import os
import secrets
import threading
import time

import httpx

from .correlation import HEADER_NAME as CORRELATION_ID_HEADER
from .correlation import get_correlation_id
from .identity_secrets import IdentityKeyError
from .invoker import ON_BEHALF_OF_HEADER, get_originator
from .scope import ON_BEHALF_SCOPE_HEADER, encode as encode_scope, get_originator_scope
from . import metrics, mtls, tracing
from .roles import BOOTSTRAP_KEY_HEADER, ENROLLMENT_HEADER, RAPP_SCOPE
from .secretfile import read_secret
from .timeouts import call_timeout

R1_GATEWAY_URL = mtls.http_url(os.environ.get("R1_GATEWAY_URL", "http://r1-termination:8000"))   # PR-SEC-2: https:// when SMO_MTLS=on

log = logging.getLogger(__name__)

_EXPIRY_MARGIN_SECONDS = 30

# SDK-PY-RETRY: who sends the token flow's own three calls (GET /bootstrap, POST /invoker-registrations, the client_credentials grant). None,
# the default and what every SMO module has, sends each once, as this client always has. The Python SDK sets a sender around the calls it
# makes (`smo_sdk/_retry.py`), so an rApp's token flow is repeated by the SDK's retry policy and no module's behaviour changes. A context
# variable, not a process setting, because the identity is per process: a sample rApp's own direct R1Client calls keep the old behaviour too.
TokenFlowSender = Callable[[Callable[[], httpx.Response], bool], httpx.Response]
_token_flow_sender: contextvars.ContextVar[TokenFlowSender | None] = contextvars.ContextVar("smo_token_flow_sender", default=None)


@contextlib.contextmanager
def token_flow_sender(sender: TokenFlowSender | None):
    """`with token_flow_sender(s):` the token flow's calls made inside the block go through `s(send, safe)` instead of `send()`.

    `send` makes the one HTTP call and returns its response (or raises httpx's transport error); `safe` is False only for the invoker
    registration, after which a lost answer may have left an invoker at SME (every registration mints a new one), so a sender must not repeat
    it after a transport error or a 502/504. The SDK is the only caller; None restores the default inside the block.
    """
    token = _token_flow_sender.set(sender)
    try:
        yield
    finally:
        _token_flow_sender.reset(token)


def _send_token_flow(send: Callable[[], httpx.Response], safe: bool) -> httpx.Response:
    """One call of the token flow: through the sender `token_flow_sender` set for this context, else sent once."""
    sender = _token_flow_sender.get()
    return send() if sender is None else sender(send, safe)


def identity_kind() -> str:
    """`rapp` for a process that is an rApp, else `module` (an SMO service)."""
    return "rapp" if os.environ.get("SMO_IDENTITY_KIND", "module").strip().lower() == "rapp" else "module"


class _ModuleIdentity:
    """This process's OAuth2 client identity at SME, and its cached token."""

    def __init__(self, store=None, invoker_id: str | None = None, invoker_secret: str | None = None):
        """Starts with the invoker id and secret given, else those pinned by `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` when both are set in the
        environment, else none (registered on first use). An identity given here is an rApp instance's (`bind_instance_identity`): it is never
        replaced by a registration of this process's own, so a refused one gives no token rather than another identity.

        `store` is the shared identity store; when None it is resolved on first use by `_identity_store`. No network or database access happens
        here.
        """
        self.lock = threading.Lock()
        self.pinned = invoker_id is not None
        # for an instance's identity: reads the stored credentials again (set by `_new_instance_identity`)
        self.reload: Callable[[], tuple[str, str] | None] | None = None
        self.invoker_id: str | None = invoker_id or os.environ.get("SMO_INVOKER_ID") or None
        self.invoker_secret: str | None = invoker_secret or os.environ.get("SMO_INVOKER_SECRET") or None
        # Where this module's invoker identity is shared with its other replicas (module_identity.py).
        # None = decide on first use: the database store when MODULE is set, else a per-process identity.
        self._store = store
        self._store_resolved = store is not None
        self.token_endpoint: str | None = None
        self.token: str | None = None
        self.expires_at = 0.0

    def _discover(self, base_url: str) -> str:
        """Returns the token endpoint URL, reading it once from the gateway's `GET /bootstrap` (first usable `tokenEndPoint.uri`) and caching it.

        Sends `X-Bootstrap-Key` when `SMO_BOOTSTRAP_KEY` is set. Raises httpx errors on a failed call and StopIteration when no endpoint is
        advertised; `token_for` catches both.
        """
        if self.token_endpoint is None:
            # PR-SEC-9.3: a gateway that sets R1_BOOTSTRAP_KEY asks for it here; unset (the default) nothing is sent
            key = read_secret("SMO_BOOTSTRAP_KEY")
            resp = _send_token_flow(lambda: httpx.get(f"{base_url}/bootstrap", headers={BOOTSTRAP_KEY_HEADER: key} if key else None, timeout=5.0,
                                                      **mtls.client_kwargs(base_url)), safe=True)
            resp.raise_for_status()
            uris = [(ep.get("tokenEndPoint") or {}).get("uri") for ep in resp.json().get("apiEndpoints", [])]
            self.token_endpoint = next(u for u in uris if u)
        return self.token_endpoint

    def _identity_store(self):
        """Returns the shared identity store, or None for a per-process identity.

        The database store is used when `MODULE` is set and `SMO_MODULE_IDENTITY_STORE` is not `off`; resolved once per process. A module that has no database
        cannot open it and gets None, as with the store off.
        """
        if not self._store_resolved:
            self._store_resolved = True
            if os.environ.get("MODULE") and os.environ.get("SMO_MODULE_IDENTITY_STORE", "db") != "off":
                try:
                    from .module_identity import DbIdentityStore
                    self._store = DbIdentityStore()
                except Exception as exc:  # noqa: BLE001  a module with no database has no store: per process, as with the store off (DOC-CF-2.4: this was a 500)
                    log.warning("module identity store not available, this process registers on its own: %r", exc)
        return self._store

    def _register_at_sme(self, token_endpoint: str) -> tuple[str, str]:
        # An opaque label, not a PEM key: this client authenticates with its onboarding
        # secret, so SME has no key to verify assertions with (an RFC 7523 client
        # assertion needs a PEM key, SA-SME-1-public-key).
        """Registers a new invoker at SME (`POST /invoker-registrations`) and returns (invoker id, onboarding secret).

        An SMO module sends the enrollment secret so SME records it as `internal`; an rApp sends none. The public key field carries an opaque label
        (`smo-<kind>:<MODULE>:<random>`) because this client authenticates with its onboarding secret and has no key. Raises httpx errors on
        failure.
        """
        sme = token_endpoint.rsplit("/oauth2/token", 1)[0]
        kind = identity_kind()
        label = f"smo-{'rapp' if kind == 'rapp' else 'module'}:{os.environ.get('MODULE', 'unknown')}:{secrets.token_urlsafe(8)}"
        headers = {}
        enrollment = read_secret("SMO_ENROLLMENT_SECRET") if kind == "module" else None
        if enrollment:
            headers[ENROLLMENT_HEADER] = enrollment
        # not safe to repeat after a lost answer: every registration mints a new invoker (the SDK's sender repeats it only on 429/503)
        resp = _send_token_flow(lambda: httpx.post(f"{sme}/invoker-registrations", json={"apiInvokerPublicKey": label}, headers=headers, timeout=5.0,
                                                   **mtls.client_kwargs(sme)), safe=False)
        resp.raise_for_status()
        body = resp.json()
        return body["apiInvokerId"], body["onboardingSecret"]

    def _onboard(self, token_endpoint: str, stale_invoker_id: str | None = None) -> None:
        """Obtain this module's invoker identity. With a shared store (PR-ST-4) all replicas of the
        module use one invoker: the stored identity is adopted if there is one (other than
        `stale_invoker_id`, which SME refused); otherwise this process registers one and stores it,
        and a replica that loses the race to store discards its own registration and adopts the winner's."""
        module = os.environ.get("MODULE", "unknown")
        if identity_kind() == "rapp":
            module = f"rapp:{module}"            # an rApp's identity is not the module's: they are registered differently
        store = self._identity_store()
        if store is not None:
            try:
                stored = store.load(module)
                if stored is not None and stored[0] != stale_invoker_id:
                    self.invoker_id, self.invoker_secret = stored
                    return
            except IdentityKeyError:
                raise                   # a key problem is not a store outage: another registration would not help and would only pile up at SME
            except Exception as exc:  # the store is an optimisation: without it, register per process as before
                log.warning("module identity store unavailable, registering this process on its own: %r", exc)
                store = None
        invoker_id, secret = self._register_at_sme(token_endpoint)
        if store is not None:
            try:
                stored_ok = (store.insert(module, invoker_id, secret) if stale_invoker_id is None
                             else store.replace(module, stale_invoker_id, invoker_id, secret))
                if not stored_ok:  # another replica stored its identity first: use that one, drop ours
                    self._offboard(token_endpoint, invoker_id)
                    winner = store.load(module)
                    if winner is not None:
                        self.invoker_id, self.invoker_secret = winner
                        return
            except IdentityKeyError:
                raise
            except Exception as exc:
                log.warning("could not store the module identity, using this process's own: %r", exc)
        self.invoker_id, self.invoker_secret = invoker_id, secret

    @staticmethod
    def _offboard(token_endpoint: str, invoker_id: str) -> None:
        """Best-effort delete of an invoker registration this process made but lost the race to store; a failure is logged and ignored (it is only an
        orphan registration).
        """
        sme = token_endpoint.rsplit("/oauth2/token", 1)[0]
        try:
            httpx.delete(f"{sme}/invoker-registrations/{invoker_id}", timeout=5.0, **mtls.client_kwargs(sme))
        except httpx.HTTPError as exc:  # an orphan registration is only clutter; SME's stale-invoker purge removes it
            log.warning("could not offboard the duplicate invoker %s: %r", invoker_id, exc)

    def _grant(self, token_endpoint: str) -> httpx.Response:
        """Asks SME's token endpoint for a client-credentials token for this identity and returns the raw answer.

        Sent through the token-flow sender, so the SDK may repeat it: a grant has no side effect worth guarding.
        """
        return _send_token_flow(lambda: httpx.post(token_endpoint, json={
            "grant_type": "client_credentials", "client_id": self.invoker_id,
            "client_secret": self.invoker_secret, "scope": RAPP_SCOPE if self.pinned or identity_kind() == "rapp" else "smo-internal",
        }, timeout=5.0, **mtls.client_kwargs(token_endpoint)), safe=True)

    def token_for(self, base_url: str, refresh: bool = False) -> str | None:
        """Returns a bearer token for SME's token endpoint, from the cache unless `refresh` is set or it is within 30 s of expiring; None when none can
        be obtained.

        Under the identity lock it discovers the endpoint, registers an identity if there is none, and requests a client-credentials token (scope
        `smo-rapp` for an rApp, `smo-internal` for a module). A 400 from the token endpoint means SME no longer knows the invoker: it registers
        again once and retries. Any failure is logged and gives None, so the caller sends the request without a token and R1 answers 401. The lock
        is held during the network calls, so concurrent callers wait for one refresh rather than each making their own.
        """
        with self.lock:
            if not refresh and self.token and time.time() < self.expires_at:
                return self.token
            try:
                token_endpoint = self._discover(base_url)
                if self.invoker_id is None:
                    self._onboard(token_endpoint)
                resp = self._grant(token_endpoint)
                # SME answers 400 when it no longer knows the invoker id (for example the registry was reset): register afresh, once, and ask again.
                # An rApp instance's identity is never replaced this way: its id is what rApp Management and the safeguards know it by.
                if resp.status_code == 400 and not self.pinned:   # SME no longer knows this invoker: onboard afresh, once
                    self._onboard(token_endpoint, stale_invoker_id=self.invoker_id)
                    resp = self._grant(token_endpoint)
                elif resp.status_code == 400 and self.reload is not None:
                    # an instance's secret renewed through another replica (call flow 01e): read the stored one, once, and ask again
                    fresh = self.reload()
                    if fresh is not None and fresh != (self.invoker_id, self.invoker_secret):
                        self.invoker_id, self.invoker_secret = fresh
                        resp = self._grant(token_endpoint)
                resp.raise_for_status()
            except (httpx.HTTPError, StopIteration, KeyError, ValueError, IdentityKeyError) as exc:
                # No token: the call still goes out and R1 answers 401, which
                # every caller already handles as an ordinary failed call.
                log.warning("could not obtain an SMO access token from SME: %r", exc)
                self.token = None
                return None
            body = resp.json()
            self.token = body["access_token"]
            self.expires_at = time.time() + max(int(body.get("expires_in", 60)) - _EXPIRY_MARGIN_SECONDS, 1)
            return self.token


_identity = _ModuleIdentity()

# docs/call-flows/01-rapp-onboarding-to-deployment.md (01c, 01d): one rApp process may serve several rApp instances, and each instance has its own
# identity at SME (made by rApp Management at CreateInstance, with its scope claim; its limits, kill switch and approval policy key on it). The
# identities this process was given are kept in the shared identity store (module_identity, key `rapp-instance:<id>`, the same table and trust
# as the process's own identity, PR-ST-4), so every replica and a restarted process have them; this dict is the per-process cache of the
# store. The instance the code running now acts for is a context variable: a call made while acting for an instance with credentials carries
# that instance's token, any other call this process's own.
_instance_identities: dict[str, _ModuleIdentity] = {}
_instances_lock = threading.Lock()
_acting_instance: contextvars.ContextVar[str | None] = contextvars.ContextVar("smo_acting_instance", default=None)


def _instance_key(instance_id: str) -> str:
    return f"rapp-instance:{instance_id}"


def _store():
    """The shared identity store this process uses (None: memory only, as in a test or a process without MODULE)."""
    return _identity._identity_store() or None


def bind_instance_identity(instance_id: str, invoker_id: str, invoker_secret: str) -> None:
    """Keeps the credentials rApp Management issued for an rApp instance: in the shared store (every replica, and after a restart) and in this
    process's cache; a later call replaces them (a rotation). A store that cannot be written is logged and the credentials kept in memory."""
    instance_id = str(instance_id)
    store = _store()
    if store is not None:
        try:
            store.put(_instance_key(instance_id), invoker_id, invoker_secret)
        except Exception as exc:  # the store is shared state, not the only copy: keep serving from memory
            log.warning("could not store the credentials of rApp instance %s, keeping them in memory: %r", instance_id, exc)
    with _instances_lock:
        _instance_identities[instance_id] = _new_instance_identity(instance_id, invoker_id, invoker_secret)


def _new_instance_identity(instance_id: str, invoker_id: str, invoker_secret: str) -> _ModuleIdentity:
    identity = _ModuleIdentity(store=False, invoker_id=invoker_id, invoker_secret=invoker_secret)
    identity.reload = lambda: _load_stored(instance_id)
    return identity


def _load_stored(instance_id: str) -> tuple[str, str] | None:
    """The credentials stored for the instance, or None (no store, nothing stored, or the store failed)."""
    store = _store()
    if store is None:
        return None
    try:
        return store.load(_instance_key(instance_id))
    except Exception as exc:
        log.warning("could not read the stored credentials of rApp instance %s: %r", instance_id, exc)
        return None


def _instance_identity_of(instance_id: str) -> _ModuleIdentity | None:
    """The identity of an instance: from this process's cache, else from the shared store (and then cached), else None."""
    with _instances_lock:
        found = _instance_identities.get(instance_id)
    if found is not None:
        return found
    stored = _load_stored(instance_id)
    if stored is None:
        return None
    with _instances_lock:
        return _instance_identities.setdefault(instance_id, _new_instance_identity(instance_id, *stored))


def forget_instance_identity(instance_id: str) -> None:
    """Drops an instance's credentials from this process's cache (nothing happens when there are none)."""
    with _instances_lock:
        _instance_identities.pop(str(instance_id), None)


def instance_identity(instance_id: str) -> str | None:
    """The invoker id held for an instance (cache or shared store), or None."""
    found = _instance_identity_of(str(instance_id))
    return found.invoker_id if found else None


def set_acting_instance(instance_id: str | None) -> contextvars.Token:
    """Makes the calls of the current context act for `instance_id` (None: this process's own identity); returns the token `reset_acting_instance` takes."""
    return _acting_instance.set(str(instance_id) if instance_id else None)


def reset_acting_instance(token: contextvars.Token) -> None:
    """Undoes a `set_acting_instance`."""
    _acting_instance.reset(token)


def acting_instance() -> str | None:
    """The id of the instance the current context acts for (`set_acting_instance`), or None when it acts as this process."""
    return _acting_instance.get()


@contextlib.contextmanager
def acting_for(instance_id: str | None):
    """`with acting_for(id):` the calls inside carry that instance's token when its identity is bound."""
    token = set_acting_instance(instance_id)
    try:
        yield
    finally:
        reset_acting_instance(token)


def _module_token(base_url: str, refresh: bool = False) -> str | None:
    """The token of the instance the current context acts for, when its identity is bound; else this process's own."""
    acting = _acting_instance.get()
    if acting:
        bound = _instance_identity_of(acting)
        if bound is not None:
            return bound.token_for(base_url, refresh)
    return _identity.token_for(base_url, refresh)


class R1Client:
    """Synchronous HTTP client for calling another module through R1 Termination (`base_url`, default `R1_GATEWAY_URL`).

    Each call adds the bearer token (the caller's own `bearer_token`, else this process's module token), the correlation id, the on-behalf-of rApp
    and its scope claim, and the trace headers, counts the call in the outbound metrics, and retries once with a fresh token on a 401 when it used
    the module token. It never raises for an HTTP status: the caller reads `status_code`. It raises httpx's own transport errors (timeouts,
    connection failures).
    """
    def __init__(self, base_url: str = R1_GATEWAY_URL, bearer_token: str | None = None):
        self.base_url = base_url
        self._bearer_token = bearer_token

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def acting_invoker_id(self) -> str | None:
        """The invoker id the calls made now carry (SEC-15.10b): that of the instance the current context acts for when its identity is bound, else this process's own.

        What a caller needs to name itself in a field the platform checks against its invoker id, such as a DME `producerId`. The identity is onboarded first if the process has none
        yet. None for a client given a `bearer_token` of its own (whose owner this process cannot tell) or when no identity could be obtained (SME unreachable).
        """
        if self._bearer_token or _module_token(self.base_url) is None:
            return None
        acting = _acting_instance.get()
        bound = _instance_identity_of(acting) if acting else None
        return (bound or _identity).invoker_id

    def _headers(self, refresh: bool = False) -> dict:
        """The headers every call carries: `Authorization` (omitted when no token could be obtained), and, when known, the correlation id and the
        on-behalf-of originator with its scope claim.

        `refresh=True` forces a new module token (used for the retry after a 401).
        """
        token = self._bearer_token or _module_token(self.base_url, refresh)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # Wave 3 cross-cutting standardization's Correlation-ID slice
        # (smo_shared/correlation.py): propagates the calling handler's
        # own correlation ID to this downstream call, so one inbound
        # request's whole cross-service fan-out shares one ID. None
        # outside any request context (e.g. a standalone script) — no
        # header added rather than fabricating one.
        correlation_id = get_correlation_id()
        if correlation_id:
            headers[CORRELATION_ID_HEADER] = correlation_id
        # Who this call is for (smo_shared/invoker.py): when the request being handled came from an rApp, the safeguards at the next module must
        # still see that rApp, not this module. Nothing is added when the module acts on its own account.
        originator = get_originator()
        if originator:
            headers[ON_BEHALF_OF_HEADER] = originator
            # ... and what that rApp may touch (PR-SEC-10): the module that owns the target decides, so the claim travels with the id
            claim = encode_scope(get_originator_scope())
            if claim:
                headers[ON_BEHALF_SCOPE_HEADER] = claim
        return headers

    def _call(self, send, method: str, path: str, headers: dict, **kwargs) -> httpx.Response:
        """One attempt, counted (PR-OBS-2.6): target module, method and outcome; a transport failure is counted and re-raised."""
        target, started = metrics.r1_target(path), time.perf_counter()
        # PR-OBS-3: one CLIENT span per attempt; the traceparent sent names that span (or, with spans off, the caller's own trace unchanged)
        with tracing.span(f"{method.upper()} {target}", "client", {"http.request.method": method.upper(), "smo.target": target,
                                                                  "smo.correlation_id": get_correlation_id() or ""}) as client_span:
            headers = {**headers, **tracing.inject_headers()}
            try:
                resp = send(self._url(path), headers=headers, **kwargs)
            except httpx.TimeoutException:
                metrics.record_outbound("r1", target, method, "timeout", time.perf_counter() - started)
                raise
            except httpx.HTTPError:
                metrics.record_outbound("r1", target, method, "error", time.perf_counter() - started)
                raise
            status = getattr(resp, "status_code", None)
            if status is not None:
                tracing.mark_status(client_span, status)
        metrics.record_outbound("r1", target, method, "error" if status is None else metrics.outcome_of(status), time.perf_counter() - started)
        return resp

    def _send(self, send, path: str, method: str = "other", **kwargs) -> httpx.Response:
        # A caller's own headers (for example `Idempotency-Key`, PR-ST-3) ride along with the
        # authorization and correlation headers; the client's own win on a clash.
        """Sends one request with `send` (an `httpx` function), applying the client's defaults, and retries once on a 401.

        The caller's own `headers` are merged under the client's (the client's win on a clash), `timeout` defaults to `timeouts.call_timeout()`, and
        the mTLS context is added when mTLS is on. The retry happens only when no `bearer_token` was given, since a caller-supplied token cannot be
        refreshed here.
        """
        extra = kwargs.pop("headers", None) or {}
        kwargs.setdefault("timeout", call_timeout())   # never httpx's implicit 5 s (timeouts.py)
        for name, value in mtls.client_kwargs(self.base_url).items():   # PR-SEC-2: this module's client certificate and the CA, when SMO_MTLS=on
            kwargs.setdefault(name, value)
        resp = self._call(send, method, path, {**extra, **self._headers()}, **kwargs)
        if resp.status_code == 401 and self._bearer_token is None:
            # expired or revoked at SME since it was cached: one fresh token, one retry
            resp = self._call(send, method, path, {**extra, **self._headers(refresh=True)}, **kwargs)
        return resp

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._send(httpx.get, path, method="get", **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> httpx.Response:
        return self._send(httpx.post, path, method="post", json=json, **kwargs)

    def put(self, path: str, json: dict | None = None, **kwargs) -> httpx.Response:
        return self._send(httpx.put, path, method="put", json=json, **kwargs)

    def patch(self, path: str, json: dict | None = None, **kwargs) -> httpx.Response:
        """PATCH through R1 (e.g. the SDK's Intent admin-state update)."""
        return self._send(httpx.patch, path, method="patch", json=json, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self._send(httpx.delete, path, method="delete", **kwargs)
