"""A short-lived cache of SME's token introspection answers (PR-SEC-5.4).

R1 Termination asks SME whether the bearer token is active on every proxied request (`main._introspect_token`). With `R1_INTROSPECTION_CACHE_SECONDS` above 0 it
remembers an answer for that many seconds, so a burst of calls with one token costs SME one lookup. Off (0) by default: then nothing here is ever consulted and the
gateway behaves exactly as before.

What the cache promises, and what it does not:

  - A token SME said was active is honoured from the cache for at most the TTL, and never past the token's own `exp`. A token revoked at SME (its invoker offboarded, or its secret renewed) is
    therefore honoured for at most the TTL after the revocation, by any replica of the gateway.
  - A revocation made through this gateway (`DELETE /sme/invoker-registrations/{id}`, `POST .../{id}/secret`, a real `purge-stale`) evicts the entries of that invoker (or all of them) at once, in
    this process, so *this* replica stops honouring the token on the next request. Other replicas do not hear about it: their bound is the TTL.
  - A token SME said was NOT active is remembered for `min(TTL, NEGATIVE_SECONDS)`: enough to take a flood of wrong tokens off SME, short enough that nothing waits long.
  - Only SME's answer is cached. "SME did not answer" (a transport error or a 5xx) is never stored, so an outage is a 503 as before, and a stored answer is never served
    past its TTL to ride one out.
  - The key is the SHA-256 of the token: the raw token is never held. The size is bounded; the oldest entry goes first when it is full.
  - A change to the role or the scope claim SME records for an invoker is seen after at most the TTL, like a revocation that does not go through this gateway.

The lookup-then-store race with a revocation is closed by a generation number: an answer fetched while a revocation was evicting is not stored.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import NamedTuple

from smo_shared.scope import Scope

NEGATIVE_SECONDS = 5.0                   # the longest an "inactive" answer is remembered, whatever the TTL: a wrong token must not be locked in for long
DEFAULT_MAX_ENTRIES = 10000


class Caller(NamedTuple):
    """Who an active token is for, as SME introspected it: the invoker id, its role and its scope claim (PR-SEC-10; None: unscoped)."""
    invoker_id: str
    role: str
    scope: Scope | None = None


Answer = Caller | None                   # the caller of an active token, None for an inactive one


class _Entry(NamedTuple):
    """One cached answer: the clock time it expires at and the answer (None: SME said inactive)."""

    expires: float
    answer: Answer


def token_key(token: str) -> str:
    """The cache key for a bearer token: its SHA-256 hex digest, so the raw token is never held in memory by the cache."""
    return hashlib.sha256(token.encode()).hexdigest()


class IntrospectionCache:
    """A bounded, thread-safe map from token hash to SME's introspection answer, each entry with its own expiry.

    Used only by `main._introspect_token` when `R1_INTROSPECTION_CACHE_SECONDS` is above 0. `generation` counts revocations: an answer fetched before the
    latest eviction is refused by `put`. Entries are kept in insertion order, so the oldest goes first when the cache is full.
    """
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, clock: Callable[[], float] = time.monotonic) -> None:
        """Builds an empty cache of at most `max_entries` entries (at least 1). `clock` returns seconds, monotonic; tests pass a fake one to step time exactly."""
        self.max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.generation = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, token: str) -> tuple[bool, Answer]:
        """Returns `(True, answer)` for a live entry (the answer is None for a remembered "inactive"), `(False, None)` for a miss. An expired entry is deleted on the way."""
        key = token_key(token)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            if entry.expires <= self._clock():
                del self._entries[key]
                return False, None
            return True, entry.answer

    def put(self, token: str, answer: Answer, ttl: float, generation: int) -> bool:
        """Remembers `answer` for `ttl` seconds; True when stored.

        The caller passes the smaller of the configured TTL and the token's remaining life. Nothing is stored when `ttl` is not positive, or when `generation` (read
        before SME was asked) differs from the current one: a revocation evicted meanwhile and this answer may predate it. When the cache is full it drops expired
        entries first and then the oldest live ones, so a flood of distinct tokens cannot grow it.
        """
        if ttl <= 0:
            return False
        key = token_key(token)
        with self._lock:
            if generation != self.generation:
                return False
            self._entries.pop(key, None)
            self._entries[key] = _Entry(self._clock() + ttl, answer)
            if len(self._entries) > self.max_entries:
                self._drop_expired()
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            return True

    def _drop_expired(self) -> None:
        """Deletes every expired entry. The caller holds the lock."""
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires <= now]:
            del self._entries[key]

    def evict_invoker(self, invoker_id: str) -> int:
        """A revocation of `invoker_id`: deletes every positive entry for it and returns how many went.

        Also bumps `generation`, so an introspection that is in flight is not stored afterwards. Negative entries ("inactive" answers, which name no invoker) stay.
        """
        with self._lock:
            self.generation += 1
            gone = [k for k, e in self._entries.items() if e.answer is not None and e.answer.invoker_id == invoker_id]
            for key in gone:
                del self._entries[key]
            return len(gone)

    def clear(self) -> int:
        """Deletes every entry and returns how many went. Bumps `generation` like `evict_invoker`; used after a real `purge-stale`, which offboards invokers the gateway is not told by name."""
        with self._lock:
            self.generation += 1
            count = len(self._entries)
            self._entries.clear()
            return count
