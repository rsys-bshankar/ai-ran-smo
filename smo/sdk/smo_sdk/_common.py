"""The plumbing every namespace client shares: `SdkError`, `ensure_ok`, the retrying wrapper and `BaseClient`.

Golden rule 6 (`docs/ARCHITECTURE.md`): the SDK is a thin client over the same R1 Termination path as every other cross-module call. Each client class wraps `R1Client` with one typed
method per platform route; this file is the one place where a response becomes either a return value or an `SdkError`, where a call is repeated per the retry policy (`_retry.py`,
SDK-PY-RETRY) and where a write that lost a race is repeated. A transport error (`httpx.HTTPError`) that is left when the retries are spent is not caught: only an HTTP answer becomes
`SdkError`. Change `ensure_ok` or `_RetryOnConflict` with care, because every method of all six clients goes through them; `tests/test_retry_on_conflict.py`, `tests/test_retry.py` and
the per-client tests pin their behaviour.
"""

import uuid
from typing import Any

from smo_shared.r1_client import R1Client
from smo_shared.r1_client import token_flow_sender as r1_token_flow_sender

from ._retry import RetryPolicy, is_safe, send_with_retry, token_flow_sender


class SdkError(Exception):
    """A platform service answered with an error status (4xx or 5xx).

    Carries the real `status_code` and the parsed `body` (the JSON, or the text when the body is not JSON), so an rApp author gets a clean exception instead of a silent success or a bare
    transport exception. Same shape as `so-smos/app/dispatch.py`'s `DownstreamError`.
    """

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"{status_code}: {body}")


def ensure_ok(resp) -> Any:
    """The body of a successful response, or `SdkError` for a status of 400 or more.

    Returns None for a 204 or an empty body. A JSON object whose `items` is a list is unwrapped to that list, so every `list_*`, `query_*` and `discover_*` method keeps returning a plain list;
    `total`, `limit` and `offset` of the page are discarded (the caller cannot see that a list was cut at the route's default limit). Any other JSON body is returned as it is.
    """
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        raise SdkError(resp.status_code, body)
    if resp.status_code == 204 or not resp.content:
        return None
    body = resp.json()
    # Every list-returning route answers {items, total, limit, offset} (limit/offset
    # pagination). No other route shapes a response as {"items": [...]}, so
    # unwrapping it here, once, at the one response boundary every sdk.* method goes
    # through, keeps each list_*/query_*/discover_* method's `-> list[dict]` return
    # type. The price: total, limit and offset are dropped, so a caller cannot see
    # that a list was cut at the route's default limit.
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    return body


MSAC_CREDENTIAL_HEADER = "X-MSAC-Credential"   # the header RAN NF OAM reads (`ran-nf-oam/app/msac.py` CREDENTIAL_HEADER, SEC-15.6)


def credential_kwargs(msac_credential: str | None) -> dict[str, dict[str, str]]:
    """The extra keyword arguments of an R1 call that carry an MSAC Identity's credential: `{"headers": {"X-MSAC-Credential": value}}`, or `{}` when none is given.

    The value goes into that header and nowhere else: not into the body, a log line, an exception or a `repr`, and the SDK reads no environment variable for it (the rApp passes in the secret it
    holds). An empty `{}` for None, so a call without a credential is made exactly as it was before.
    """
    return {} if msac_credential is None else {"headers": {MSAC_CREDENTIAL_HEADER: msac_credential}}


def _is_concurrent_modification(resp) -> bool:
    """409 whose ProblemDetails title is CONCURRENT_MODIFICATION (smo_shared/versioning.py), as
    opposed to the other 409s (an illegal transition, a name conflict), which are final answers."""
    if resp.status_code != 409:
        return False
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("title") == "CONCURRENT_MODIFICATION"


class _RetryOnConflict:
    """Wraps an R1 client so every SDK call follows the retry policy (`_retry.py`, SDK-PY-RETRY) and a mutating call that lost a write race
    (409 CONCURRENT_MODIFICATION, PR-ST-2) is sent once more.

    The layers, outermost first: the 409 repeat (once, so a call is sent at most twice for it); under each of those sends, the retry policy
    (up to `max_attempts` sends on a transport error or 429/502/503/504, see `send_with_retry` for which calls); under each of those, `R1Client`'s
    own one repeat with a fresh token on a 401. A lost write race gets a fresh retry budget, as in the Go SDK, and the 401 renewal is not counted
    as an attempt. The token flow `R1Client` runs inside a call (bootstrap, invoker registration, grant) uses the same policy through
    `smo_shared.r1_client.token_flow_sender`; the registration is repeated only on 429/503.

    The 409 repeat: the platform rolled the first attempt back and reloads the row for the second, so the repeat either succeeds or is refused
    for what it now is (for example an illegal transition because the other writer already made the same move); a second conflict is returned as
    is. Reads are never repeated for a 409. A call that carries `files` is never repeated at all (a consumed stream cannot be resent). Side effects
    a route performs before its commit, such as a call to another module, can run twice; the remedy for that is an idempotency key (PR-ST-3),
    which every POST made through this wrapper carries: the first attempt's key is reused by every repeat, so the platform answers a repeat from
    the stored first answer if the first attempt did complete.
    """

    _VERBS = ("get", "post", "put", "patch", "delete")
    _MUTATING = ("post", "put", "patch", "delete")

    def __init__(self, r1, retry: RetryPolicy | None = None):
        """Wrap `r1`, the R1 client whose verbs (get, post, put, patch, delete) the namespace clients call; `retry` is the policy (None: `RetryPolicy.from_env()`)."""
        self._r1 = r1
        self._retry = retry if retry is not None else RetryPolicy.from_env()

    def __getattr__(self, name):
        """The R1 client's attribute `name`; for a verb, a function that sends the call per the retry policy and, for a mutating verb, repeats it
        once on a 409 CONCURRENT_MODIFICATION.

        A POST without `files` gets an `Idempotency-Key` header (a caller's own key is kept) that every repeat reuses; PUT, PATCH and DELETE carry
        none. A call with `files` is neither keyed nor repeated, because the uploaded stream is consumed by the first attempt. Every other
        attribute is passed through untouched.
        """
        attr = getattr(self._r1, name)
        if name not in self._VERBS:
            return attr
        policy = self._retry

        def call(path, *args, **kwargs):
            if kwargs.get("files") is not None:
                return attr(path, *args, **kwargs)        # a consumed upload stream cannot be sent again: one send, no repeat of any kind
            if name == "post":
                # one key per SDK call, reused by every repeat below (PR-ST-3); a caller's own key wins
                headers = dict(kwargs.get("headers") or {})
                headers.setdefault("Idempotency-Key", uuid.uuid4().hex)
                kwargs["headers"] = headers
            safe = is_safe(name, kwargs.get("headers"))

            def send():
                # one attempt; the token flow R1Client may run inside it is repeated by the same policy (SDK only, modules unchanged)
                with r1_token_flow_sender(token_flow_sender(policy)):
                    return attr(path, *args, **kwargs)

            resp = send_with_retry(send, policy, safe=safe, what=f"{name.upper()} {path}")
            # Repeated once, not in a loop: a second conflict is returned to the caller as it is (PR-ST-2).
            if name in self._MUTATING and _is_concurrent_modification(resp):
                resp = send_with_retry(send, policy, safe=safe, what=f"{name.upper()} {path}")
            return resp

        return call


class BaseClient:
    """Base of the six namespace clients: holds `self._r1`, the given `R1Client` (or a new one) wrapped by `_RetryOnConflict`, so every call follows
    the retry policy and a write that lost a race is repeated once."""
    def __init__(self, r1: R1Client | None = None, retry: RetryPolicy | None = None):
        """Wrap `r1` (None: a new `R1Client()`) with `retry` (None: `RetryPolicy.from_env()`); an already wrapped client is kept as it is, so its
        calls are not retried twice over."""
        r1 = r1 or R1Client()
        self._r1 = r1 if isinstance(r1, _RetryOnConflict) else _RetryOnConflict(r1, retry)
