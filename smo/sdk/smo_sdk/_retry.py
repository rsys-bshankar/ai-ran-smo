"""The SDK's retry policy (SDK-PY-RETRY): how a call that failed for a transient reason is repeated, the same policy as the Go and Java SDKs.

`_common._RetryOnConflict` sends every SDK call through `send_with_retry`, and sets `token_flow_sender` (via `smo_shared.r1_client.token_flow_sender`)
around it, so the token flow `R1Client` runs inside an SDK call (bootstrap, invoker registration, grant) is repeated by the same policy. SMO modules
never import this file and their `R1Client` calls are unchanged. It mirrors `sdk-go/retry.go` (`RetryPolicy`, `retryableStatus`, `parseRetryAfter`)
and `sdk-java/.../RetryPolicy.java`; where those two differ (the jitter, how `Retry-After` combines with the backoff) this file follows Go, as
`HISTORY.md` SDK-PY-RETRY records. The defaults (4 attempts, 200 ms, 5 s, `Retry-After` capped at 30 s) are repeated in `sdk/README.md` and the tests.

Owns no I/O except the sleep between attempts. `_sleep` and `_uniform` are module attributes so a test replaces them and never really waits.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
# The answers that prove the server did not run the call, so even a call that is not safe to repeat may be sent again. After a 502 or 504 the
# gateway lost the upstream's answer and the call may have run.
NOT_RUN_STATUSES = frozenset({429, 503})
MAX_RETRY_AFTER_SECONDS = 30.0
# The methods the Go and Java SDKs repeat after a transport error or a 502/504. PATCH is not among them (not idempotent by RFC 9110, and the
# SDK gives it no Idempotency-Key); a POST is safe only with an Idempotency-Key, which `_RetryOnConflict` adds to every POST without files.
SAFE_METHODS = frozenset({"get", "head", "options", "put", "delete"})

_sleep = time.sleep
_uniform = random.uniform


@dataclass(frozen=True)
class RetryPolicy:
    """How many times a call is sent and how long to wait between sends.

    `max_attempts` counts the first send (1 = never repeat). The wait after failed attempt n is `base_delay * 2**(n-1)` seconds, at most
    `max_delay`, with "equal jitter" (a random value between half of it and all of it) unless `jitter` is False; a `Retry-After` in seconds
    raises it, up to 30 s.
    """

    max_attempts: int = 4
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: bool = True

    def __post_init__(self):
        """Refuses a policy that cannot work: fewer than one attempt, or a delay that is not positive or a cap below the base."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= 0 or self.max_delay < self.base_delay:
            raise ValueError("base_delay must be positive and max_delay at least base_delay")

    @classmethod
    def off(cls) -> RetryPolicy:
        """A policy that sends every call once."""
        return cls(max_attempts=1)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> RetryPolicy:
        """The policy the environment asks for, the defaults where it says nothing.

        `SMO_SDK_RETRY=off` turns retries off (`on`, the default, keeps them); `SMO_SDK_RETRY_MAX_ATTEMPTS` (default 4, at least 1),
        `SMO_SDK_RETRY_BASE_DELAY_MS` (default 200) and `SMO_SDK_RETRY_MAX_DELAY_MS` (default 5000, at least the base) tune it. Raises
        `ValueError` naming the variable when a value cannot be used, so a mistyped setting stops the rApp at start instead of being ignored.
        """
        switch = environ.get("SMO_SDK_RETRY", "on").strip().lower()
        if switch not in ("on", "off"):
            raise ValueError(f"SMO_SDK_RETRY must be on or off, not {switch!r}")
        if switch == "off":
            return cls.off()
        attempts = _positive_int(environ, "SMO_SDK_RETRY_MAX_ATTEMPTS", 4)
        base_ms = _positive_int(environ, "SMO_SDK_RETRY_BASE_DELAY_MS", 200)
        max_ms = _positive_int(environ, "SMO_SDK_RETRY_MAX_DELAY_MS", 5000)
        if max_ms < base_ms:
            raise ValueError("SMO_SDK_RETRY_MAX_DELAY_MS must be at least SMO_SDK_RETRY_BASE_DELAY_MS")
        return cls(max_attempts=attempts, base_delay=base_ms / 1000, max_delay=max_ms / 1000)

    def wait(self, attempt: int, retry_after: float = 0.0) -> float:
        """Seconds to wait after the failed attempt number `attempt` (1-based), given the server's `Retry-After` in seconds (0: none)."""
        delay = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
        if self.jitter:
            delay = _uniform(delay / 2, delay)
        # as Go does: the server's hint can lengthen the wait, never shorten it, and never past 30 s
        return max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    """The integer value of `environ[name]` (the default when unset or empty); `ValueError` naming the variable when it is not a whole number of at least 1."""
    raw = str(environ.get(name, default)).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1, not {value}")
    return value


def retry_after_seconds(resp) -> float:
    """The answer's `Retry-After` in whole seconds, or 0 when it is absent, not positive or in the HTTP-date form (ignored, as Go and Java do)."""
    headers = getattr(resp, "headers", None) or {}
    try:
        value = int(str(headers.get("Retry-After", "")).strip())
    except ValueError:
        return 0.0
    return float(value) if value > 0 else 0.0


def is_safe(method: str, headers: Mapping[str, str] | None) -> bool:
    """Whether a call may be repeated after a transport error or a 502/504: GET, HEAD, OPTIONS, PUT and DELETE, and a POST that carries a
    non-empty `Idempotency-Key` (the platform answers its repeat from the stored first answer, PR-ST-3)."""
    method = method.lower()
    if method in SAFE_METHODS:
        return True
    if method == "post":
        return any(k.lower() == "idempotency-key" and v for k, v in (headers or {}).items())
    return False


def send_with_retry(send: Callable[[], httpx.Response], policy: RetryPolicy, *, safe: bool, what: str = "call") -> httpx.Response:
    """Sends with `send()` and repeats it per `policy`; returns the last answer, whatever its status.

    Repeated: an answer 429 or 503 (the server did not run it) for every call; a 502, a 504 or an `httpx.TransportError` (timeout, refused or
    dropped connection) only when `safe`. Any other answer, a 4xx in particular, is returned at once. When the attempts run out the last answer
    is returned, or the last transport error raised. Sleeps between attempts (`_sleep`) and logs each repeat at INFO with `what`.
    """
    attempt = 1
    while True:
        try:
            resp = send()
        except httpx.TransportError as exc:
            if not safe or attempt >= policy.max_attempts:
                raise
            retry_after, reason = 0.0, type(exc).__name__
        else:
            status = resp.status_code
            if status not in RETRY_STATUSES or attempt >= policy.max_attempts or (status not in NOT_RUN_STATUSES and not safe):
                return resp
            retry_after, reason = retry_after_seconds(resp), str(status)
        delay = policy.wait(attempt, retry_after)
        log.info("smo_sdk: %s failed (%s), attempt %d of %d, repeating in %.2f s", what, reason, attempt, policy.max_attempts, delay)
        _sleep(delay)
        attempt += 1


def token_flow_sender(policy: RetryPolicy) -> Callable[[Callable[[], httpx.Response], bool], httpx.Response]:
    """The sender `R1Client`'s token flow uses inside an SDK call (`smo_shared.r1_client.token_flow_sender`): bootstrap and grant are repeated as
    safe calls, the invoker registration (`safe=False`) only on 429/503, so a lost answer never mints a second invoker."""
    def send(call, safe):
        # one HTTP call of the token flow, through the SDK's policy
        return send_with_retry(call, policy, safe=safe, what="token flow call")
    return send
