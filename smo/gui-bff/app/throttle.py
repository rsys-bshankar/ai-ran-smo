"""The per-account cool-down of the sign-in lockout (SEC-15.11b), as pure rules; `db.py` stores the counter and `main.py` asks.

`SEC-15.11` counts failures per (source address, user name) and per source. Neither slows ONE account guessed from MANY addresses: each address has its own five tries. This adds a
third counter, keyed by the user name alone (`source.account_key`), that does not care where a failure came from. It is deliberately mild, because anyone can make a name fail:
a hard lock keyed by name alone would let an attacker keep a real person out for as long as they like. So it is a short cool-down, not a lock.

The rule, with `max_failures` N, `window`, `cooldown` and `cooldown_max` from `AccountPolicy`:

* N failures of one name, from anywhere, inside `window` seconds start a cool-down of `cooldown` seconds, during which every sign-in for that name, a right password included, is
  refused with the same 429 as the other locks. Refused attempts are not counted, so the cool-down does not feed itself.
* After it ends, N more failures start the next, twice as long, up to `cooldown_max`. Quiet for `window` after the last
  cool-down ended, and the counter starts again from nothing.
* A successful sign-in does NOT touch this counter (it only decays with time): otherwise an attacker who owns one account could reset the counter of the account being guessed.
  It also means a person who signs in in the middle of an attack is not helped: that is the price of the mild choice.
* The counter is counted for names that do not exist exactly as for real ones, so nothing here reveals which accounts exist.

The state is one row of `gui_login_failure` (the existing columns): `count` and `first_failed_at`. A row whose `count` is a multiple of N (and N or more) means "tripped", and
`first_failed_at` is then the moment of the trip. No clock or database is read here: callers pass `now`.
"""

from dataclasses import dataclass

# The window of the per-pair and per-source counters of SEC-15.11 (seconds). Here so that the retention command can use it without importing the application.
LOCKOUT_SECONDS = 300


@dataclass(frozen=True)
class AccountPolicy:
    """The per-account cool-down's settings. `max_failures` 0 switches it off. Raises ValueError for a setting that makes no sense, so a bad value stops the start."""
    max_failures: int = 20
    window: int = 600
    cooldown: int = 60
    cooldown_max: int = 300

    def __post_init__(self) -> None:
        if self.max_failures < 0:
            raise ValueError("the account failure threshold must be 0 (off) or more")
        if self.max_failures and (self.window < 1 or self.cooldown < 1):
            raise ValueError("the account failure window and cool-down must be at least 1 second")
        if self.max_failures and self.cooldown_max < self.cooldown:
            raise ValueError("the account cool-down ceiling must not be shorter than the cool-down")

    @property
    def enabled(self) -> bool:
        """True when the cool-down is on."""
        return self.max_failures > 0

    @property
    def max_level(self) -> int:
        """The number of consecutive trips after which the cool-down stops growing (the first level whose length reaches `cooldown_max`)."""
        level = 1
        while self.cooldown * 2 ** (level - 1) < self.cooldown_max:
            level += 1
        return level

    def cooldown_for(self, level: int) -> int:
        """How long the cool-down of the `level`-th trip (1 first) lasts: `cooldown` doubled per level, never above `cooldown_max`."""
        return min(self.cooldown * 2 ** (max(level, 1) - 1), self.cooldown_max)

    def tripped(self, count: int) -> bool:
        """True when `count` is a trip count: a multiple of the threshold, and at least the threshold."""
        return self.enabled and count >= self.max_failures and count % self.max_failures == 0

    def cooling(self, count: int, first_failed_at: float, now: float) -> bool:
        """True when a row with this state refuses sign-ins now: it was tripped and its cool-down has not ended."""
        return self.tripped(count) and now - first_failed_at < self.cooldown_for(min(count // self.max_failures, self.max_level))

    def stale(self, count: int, first_failed_at: float, now: float) -> bool:
        """True when the row has no effect any more and a failure may start it again: a counting row after `window`, a tripped one after its longest cool-down plus `window`."""
        horizon = self.window + (self.cooldown_max if count >= self.max_failures else 0)
        return now - first_failed_at >= horizon

    def after_failure(self, row: tuple[int, float] | None, now: float) -> tuple[int, float] | None:
        """The state to store after one more failure at `now`, given the stored `(count, first_failed_at)` or None; None means "store nothing" (a failure during a cool-down:
        a path that did not check first, or two requests at once, must not lengthen it)."""
        if row is None or self.stale(row[0], row[1], now):
            row = (0, now)
        count, first = row
        if self.cooling(count, first, now):
            return None
        count += 1
        if count % self.max_failures == 0:
            return min(count, self.max_level * self.max_failures), now
        return count, first
