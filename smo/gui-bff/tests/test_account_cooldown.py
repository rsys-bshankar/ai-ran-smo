"""SEC-15.11b: one account guessed from many addresses gets a short cool-down, and the expired failure counters are purged.

`SEC-15.11` gives each (address, name) pair its own five tries, so a name tried once from each of a thousand addresses was never slowed. These tests cover the third counter
(`app/throttle.py`, the key `a<US>name`): the rules as pure functions, the counter in the database (including two instances racing), the sign-in routes with a controlled clock,
the settings, and the purge of expired rows of all three kinds that the retention command now does. Fixtures (`app`, `cfg`, `db`, `smo`) and `PASSWORDS` come from `test_main.py`.
No network. Run with `cd smo/gui-bff && PYTHONPATH=.:../shared python -m pytest tests/test_account_cooldown.py -q`.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as gui_main
from app import retention, source
from app.config import Settings
from app.db import Database, LoginFailure
from app.main import create_app, seed_users
from app.smo_client import R1Gateway
from app.throttle import LOCKOUT_SECONDS, AccountPolicy

from test_main import PASSWORDS, R1, app, cfg, db, smo  # noqa: F401  (pytest fixtures and constants)

POLICY = AccountPolicy(max_failures=20, window=600, cooldown=60, cooldown_max=300)


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves: `clock.now` is what `time.time()` returns inside the application (a list of one so the lambda sees changes), starting at a fixed second."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(gui_main.time, "time", lambda: now[0])
    return now


def _login(app, username, password, ip):
    """One sign-in attempt from `ip` (as the console's nginx sends it), returning the response."""
    return TestClient(app).post("/api/login", json={"username": username, "password": password}, headers={"X-Forwarded-For": ip})


def _guess_from_many_addresses(app, username, times, first=1):
    """`times` wrong passwords for `username`, each from an address of its own, so no (address, name) pair and no source gets near its own limit; every one must be a plain 401."""
    for n in range(first, first + times):
        assert _login(app, username, "guess", f"198.51.{n // 250}.{n % 250 + 1}").status_code == 401


# ---------------------------------------------------------------- the rules

def test_the_defaults_and_the_levels():
    """20 failures in 600 s start a cool-down of 60 s, doubling per repeat and never above 300 s: 60, 120, 240, 300, and the fourth level is the last."""
    assert (POLICY.max_failures, POLICY.window, POLICY.cooldown, POLICY.cooldown_max) == (20, 600, 60, 300)
    assert [POLICY.cooldown_for(level) for level in (1, 2, 3, 4, 5, 9)] == [60, 120, 240, 300, 300, 300]
    assert POLICY.max_level == 4
    assert AccountPolicy(5, 100, 300, 300).max_level == 1


@pytest.mark.parametrize("args", [(-1, 600, 60, 300), (5, 0, 60, 300), (5, 600, 0, 300), (5, 600, 60, 30)])
def test_a_policy_that_makes_no_sense_is_refused(args):
    """A negative threshold, a zero window or cool-down, or a ceiling under the cool-down raises ValueError (so the start stops); a threshold of 0 is 'off' and checks nothing else."""
    with pytest.raises(ValueError):
        AccountPolicy(*args)
    assert AccountPolicy(0, 0, 0, 0).enabled is False


def test_failures_below_the_threshold_do_not_cool_down_and_the_threshold_does():
    """The 19th failure leaves the name open; the 20th trips it at that moment, and the row then stands for 'tripped since'."""
    state, now = None, 100.0
    for n in range(1, 20):
        state = POLICY.after_failure(state, now + n)
        assert state == (n, 101.0) and not POLICY.cooling(*state, now + n)
    state = POLICY.after_failure(state, 120.0)
    assert state == (20, 120.0) and POLICY.tripped(20)
    assert POLICY.cooling(*state, 120.0) and POLICY.cooling(*state, 179.9) and not POLICY.cooling(*state, 180.0)


def test_a_failure_during_a_cool_down_changes_nothing():
    """A failure counted while cooling (a path that did not check first, or two requests at once) must not lengthen the cool-down: nothing is stored."""
    assert POLICY.after_failure((20, 120.0), 130.0) is None


def test_the_next_cool_down_needs_another_threshold_of_failures_and_is_longer():
    """After a cool-down ends one failure does not start another: it takes 20 more; each repeat is twice as long (60, 120, 240, 300) and the ceiling holds."""
    state = (20, 120.0)
    now = 200.0
    for n in range(1, 20):
        state = POLICY.after_failure(state, now)
        assert state == (20 + n, 120.0) and not POLICY.cooling(*state, now)
    for trips, length in ((2, 120), (3, 240), (4, 300), (4, 300)):
        for _ in range(20 - state[0] % 20):
            state = POLICY.after_failure(state, now) or state
        assert state == (20 * trips, now)                                  # the fifth trip is held at four levels
        assert POLICY.cooling(*state, now + length - 1) and not POLICY.cooling(*state, now + length)
        now += length
        for _ in range(19):
            state = POLICY.after_failure(state, now) or state


def test_a_quiet_name_starts_again_from_nothing():
    """A counting row older than the window, or a tripped one older than its longest cool-down plus the window, restarts at one failure; younger ones keep counting."""
    assert POLICY.after_failure((7, 100.0), 100.0 + 599) == (8, 100.0)
    assert POLICY.after_failure((7, 100.0), 100.0 + 600) == (1, 700.0)
    assert POLICY.after_failure((40, 100.0), 100.0 + 899) == (41, 100.0)
    assert POLICY.after_failure((40, 100.0), 100.0 + 900) == (1, 1000.0)
    assert POLICY.stale(5, 0.0, 600.0) and not POLICY.stale(5, 0.0, 599.0) and not POLICY.stale(20, 0.0, 899.0) and POLICY.stale(20, 0.0, 900.0)


def test_a_threshold_of_one_trips_at_once_and_zero_is_off():
    """With one failure allowed the first failure trips; with 0 nothing ever cools or is counted."""
    one = AccountPolicy(1, 600, 60, 60)
    assert one.after_failure(None, 10.0) == (1, 10.0) and one.cooling(1, 10.0, 11.0)
    off = AccountPolicy(0, 600, 60, 300)
    assert not off.enabled and not off.tripped(20) and off.cooling(20, 0.0, 1.0) is False


# ---------------------------------------------------------------- the counter in the database

def test_the_counter_counts_trips_and_restarts(db):
    """`record_account_failure` stores the policy's state; `account_cooling` reads it; an unknown name or a policy that is off is never cooling and writes nothing."""
    key = source.account_key("admin")
    assert key == "a\x1fadmin" and not db.account_cooling(key, POLICY, 10.0)
    for n in range(20):
        db.record_account_failure(key, POLICY, 100.0 + n)
    assert db.account_cooling(key, POLICY, 119.0) and not db.account_cooling(key, POLICY, 179.0 + 1)
    db.record_account_failure(key, POLICY, 130.0)                          # during the cool-down: ignored
    with db.session() as s:
        assert (s.get(LoginFailure, key).count, s.get(LoginFailure, key).first_failed_at) == (20, 119.0)
    off = AccountPolicy(0)
    db.record_account_failure("a\x1fother", off, 1.0)
    assert not db.account_cooling("a\x1fother", off, 1.0)
    with db.session() as s:
        assert s.get(LoginFailure, "a\x1fother") is None


class _Racing:
    """Stands in for the policy: `after_failure` is the real one, but first runs `rival` (another instance writing) after the row was read and before this one writes, `times` times."""
    enabled = True

    def __init__(self, rival, times=1):
        self.rival, self.times = rival, times

    def after_failure(self, row, now):
        result = POLICY.after_failure(row, now)
        if self.times > 0:
            self.times -= 1
            self.rival()
        return result


def test_two_instances_counting_at_once_lose_no_failure(db):
    """A write that finds the row changed under it reads again and counts on top of the other instance's value (compare and swap), for both an existing row and a first row."""
    key = source.account_key("racer")

    def rival_adds_first_row():
        with db.session() as s:
            s.add(LoginFailure(username=key, count=3, first_failed_at=50.0))
            s.commit()
    policy = _Racing(rival_adds_first_row)
    db.record_account_failure(key, policy, 60.0)                           # the first row appears under it: read again, counted on top
    with db.session() as s:
        assert s.get(LoginFailure, key).count == 4

    def rival_counts_too():
        with db.session() as s:
            row = s.get(LoginFailure, key)
            row.count += 1
            s.commit()
    db.record_account_failure(key, _Racing(rival_counts_too), 61.0)
    with db.session() as s:
        assert s.get(LoginFailure, key).count == 6                          # 4, the rival's 1, ours 1


def test_a_counter_that_never_settles_raises(db):
    """If every write loses its race the call raises RuntimeError rather than looping or dropping the failure silently."""
    key = source.account_key("busy")

    def rival_counts_too():
        with db.session() as s:
            row = s.get(LoginFailure, key)
            if row is None:
                s.add(LoginFailure(username=key, count=1, first_failed_at=1.0))
            else:
                row.count += 1
            s.commit()
    with pytest.raises(RuntimeError):
        db.record_account_failure(key, _Racing(rival_counts_too, times=99), 5.0)


# ---------------------------------------------------------------- the sign-in routes

def test_one_name_from_many_addresses_gets_a_cool_down(app, clock):
    """The finding: 20 wrong passwords for `admin`, each from a different address, and the next try for `admin`, from a new address with the RIGHT password, is a 429 until 60 s pass.
    Another name from the same new address is untouched. Without the per-account counter every one of these sign-ins is a plain 200 or 401."""
    _guess_from_many_addresses(app, "admin", 20)
    refused = _login(app, "admin", PASSWORDS["admin"], "192.0.2.50")
    assert refused.status_code == 429 and refused.json()["title"] == "TOO_MANY_ATTEMPTS"
    assert _login(app, "operator", PASSWORDS["operator"], "192.0.2.50").status_code == 200
    clock[0] += 59
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.51").status_code == 429
    clock[0] += 2
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.52").status_code == 200


def test_nineteen_failures_are_not_enough(app, clock):
    """One short of the threshold: the name still signs in, from anywhere."""
    _guess_from_many_addresses(app, "viewer", 19)
    assert _login(app, "viewer", PASSWORDS["viewer"], "192.0.2.60").status_code == 200


def test_refused_attempts_do_not_extend_the_cool_down(app, clock):
    """Hammering a cooling name does not lengthen it: the 429s are not counted, so 60 s after the trip the name is open again."""
    _guess_from_many_addresses(app, "admin", 20)
    for n in range(30):
        assert _login(app, "admin", "guess", f"192.0.2.{n + 70}").status_code == 429
    clock[0] += 61
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.150").status_code == 200


def test_a_successful_sign_in_does_not_reset_the_account_counter(app, clock):
    """19 failures from many addresses, then the account's owner signs in (the pair is cleared, the account counter is not), then ONE more failure trips it: 20, not 1. An attacker who
    holds an account of their own therefore cannot reset the count of the account they guess by signing in."""
    _guess_from_many_addresses(app, "admin", 19)
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.80").status_code == 200
    with app.state.db.session() as s:
        assert s.get(LoginFailure, source.account_key("admin")).count == 19
    assert _login(app, "admin", "guess", "192.0.2.81").status_code == 401
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.82").status_code == 429


def test_the_counter_decays_with_time_only(app, clock):
    """19 failures, then 11 minutes of quiet: the counter has expired, so 19 more do not trip it (20 would have if it had been kept)."""
    _guess_from_many_addresses(app, "admin", 19)
    clock[0] += 601
    _guess_from_many_addresses(app, "admin", 19, first=100)
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.90").status_code == 200


def test_the_cool_down_grows_but_stays_short(app, clock):
    """A second trip right after the first one ends lasts 120 s, and the answer is the same 429 each time."""
    _guess_from_many_addresses(app, "admin", 20)
    clock[0] += 61
    _guess_from_many_addresses(app, "admin", 20, first=300)
    clock[0] += 119
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.91").status_code == 429
    clock[0] += 2
    assert _login(app, "admin", PASSWORDS["admin"], "192.0.2.92").status_code == 200


def test_an_unknown_name_cools_down_exactly_like_a_real_one(app, clock):
    """The answer never says whether the account exists: a made-up name that has failed 20 times gets the same status and body as `admin` does, and a failure before it the same 401."""
    _guess_from_many_addresses(app, "admin", 20)
    _guess_from_many_addresses(app, "nobody", 20)
    real = _login(app, "admin", "guess", "192.0.2.100")
    ghost = _login(app, "nobody", "guess", "192.0.2.100")
    assert (real.status_code, real.json()) == (ghost.status_code, ghost.json()) and real.status_code == 429


def test_the_token_grant_shares_the_cool_down(app, clock):
    """`POST /api/token` counts and is refused on the same account counter as the form."""
    _guess_from_many_addresses(app, "admin", 20)
    resp = TestClient(app).post("/api/token", data={"grant_type": "password", "username": "admin", "password": PASSWORDS["admin"]}, headers={"X-Forwarded-For": "192.0.2.110"})
    assert resp.status_code == 429


def test_with_the_cool_down_off_nothing_changes(cfg, db, smo, clock):
    """`GUI_ACCOUNT_MAX_FAILURES=0`: 25 failures of one name from 25 addresses never refuse the right password, and no account row is written."""
    cfg.account_max_failures = 0
    seed_users(db, cfg)
    off = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))
    _guess_from_many_addresses(off, "admin", 25)
    assert _login(off, "admin", PASSWORDS["admin"], "192.0.2.120").status_code == 200
    with db.session() as s:
        assert s.get(LoginFailure, source.account_key("admin")) is None


def test_the_pair_lock_and_the_source_throttle_are_unchanged(app, clock):
    """The two older counters still lock at their own numbers: five failures from one address lock that pair, well before the account cool-down."""
    for _ in range(5):
        assert _login(app, "admin", "guess", "203.0.113.7").status_code == 401
    assert _login(app, "admin", PASSWORDS["admin"], "203.0.113.7").status_code == 429
    assert _login(app, "admin", PASSWORDS["admin"], "203.0.113.8").status_code == 200


# ---------------------------------------------------------------- the settings

def test_the_account_settings_have_defaults_and_read_the_environment(monkeypatch):
    """Defaults 20, 600, 60 and 300; each variable is read as a whole number; a value that is not one stops the start naming the variable; a ceiling under the cool-down is refused."""
    names = ("GUI_ACCOUNT_MAX_FAILURES", "GUI_ACCOUNT_WINDOW_SECONDS", "GUI_ACCOUNT_COOLDOWN_SECONDS", "GUI_ACCOUNT_COOLDOWN_MAX_SECONDS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert Settings().account_policy() == POLICY
    for name, value in zip(names, ("7", "120", "30", "90")):
        monkeypatch.setenv(name, value)
    assert Settings().account_policy() == AccountPolicy(7, 120, 30, 90)
    for name in names:
        monkeypatch.setenv(name, "ten")
        with pytest.raises(ValueError, match=name):
            Settings()
        monkeypatch.setenv(name, "-1")
        with pytest.raises(ValueError, match=name):
            Settings()
        monkeypatch.setenv(name, {"GUI_ACCOUNT_MAX_FAILURES": "7", "GUI_ACCOUNT_WINDOW_SECONDS": "120", "GUI_ACCOUNT_COOLDOWN_SECONDS": "30", "GUI_ACCOUNT_COOLDOWN_MAX_SECONDS": "90"}[name])
    monkeypatch.setenv("GUI_ACCOUNT_COOLDOWN_MAX_SECONDS", "10")
    with pytest.raises(ValueError, match="ceiling"):
        Settings().account_policy()


def test_a_nonsense_combination_stops_the_application_start(cfg, db, smo):
    """`create_app` raises for a ceiling under the cool-down, so a bad value is found at start, not at the first sign-in."""
    cfg.account_cooldown_max_seconds = 1
    with pytest.raises(ValueError):
        create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))


# ---------------------------------------------------------------- the purge

def _rows(db):
    """The keys of all counter rows, sorted."""
    with db.session() as s:
        return sorted(row.username for row in s.query(LoginFailure))


def _seed_counters(db, now):
    """One expired and one live row of each kind, plus an old name-only row and a tripped account row at three ages. Returns the keys that must survive a purge at `now`."""
    rows = {
        source.pair_key("1.1.1.1", "old"): (3, now - LOCKOUT_SECONDS),                  # expired
        source.pair_key("1.1.1.1", "live"): (3, now - LOCKOUT_SECONDS + 1),             # live
        source.source_key("1.1.1.1"): (9, now - LOCKOUT_SECONDS - 5),                   # expired
        source.source_key("2.2.2.2"): (9, now - 5),                                      # live
        "legacy-name": (4, now - 10_000),                                               # an old name-only row: expired
        "legacy-fresh": (4, now - 10),                                                  # an old name-only row still inside the window: kept
        source.account_key("counting-old"): (7, now - 600),                             # a counting account row past its window: expired
        source.account_key("counting-live"): (7, now - 599),
        source.account_key("tripped-live"): (20, now - 899),                            # tripped: kept until window + longest cool-down
        source.account_key("tripped-old"): (40, now - 900),
    }
    with db.session() as s:
        for key, (count, first) in rows.items():
            s.add(LoginFailure(username=key, count=count, first_failed_at=first))
        s.commit()
    return sorted([source.pair_key("1.1.1.1", "live"), source.source_key("2.2.2.2"), "legacy-fresh", source.account_key("counting-live"), source.account_key("tripped-live")])


def test_the_purge_removes_expired_rows_of_every_kind_and_keeps_live_ones(db):
    """Pair, source, name-only and account rows past their window go; every row still counting or locking stays; the count returned is the number removed. Run twice, the second removes none."""
    now = 1_800_000_000.0
    keep = _seed_counters(db, now)
    assert db.purge_login_failures(POLICY, LOCKOUT_SECONDS, now) == 5
    assert _rows(db) == keep
    assert db.purge_login_failures(POLICY, LOCKOUT_SECONDS, now) == 0


def test_the_purge_with_the_cool_down_off_still_clears_account_rows(db):
    """A deployment that switched the cool-down off: its old account rows go once they are older than the longest window, however the count stood."""
    now = 1_800_000_000.0
    with db.session() as s:
        s.add(LoginFailure(username=source.account_key("a"), count=30, first_failed_at=now - 901))
        s.add(LoginFailure(username=source.account_key("b"), count=30, first_failed_at=now - 10))
        s.commit()
    assert db.purge_login_failures(AccountPolicy(0, 600, 60, 300), LOCKOUT_SECONDS, now) == 1
    assert _rows(db) == [source.account_key("b")]


def test_the_retention_function_uses_the_clock_unless_given_one(db, monkeypatch):
    """`retention.purge_login_failures` passes the current time when none is given."""
    now = 1_800_000_000.0
    _seed_counters(db, now)
    monkeypatch.setattr(retention.time, "time", lambda: now)
    assert retention.purge_login_failures(db, POLICY) == 5
    assert retention.purge_login_failures(db, POLICY, now=now + 10_000) == 5      # and the given time wins: everything left is then expired


def test_the_retention_command_purges_counters_even_with_audit_retention_off(db, monkeypatch, capsys, tmp_path):
    """`python -m app.retention` removes the expired counters whatever `GUI_AUDIT_RETENTION_DAYS` says (unset here), says how many, and still exits 0."""
    import time
    _seed_counters(db, time.time())
    monkeypatch.delenv("GUI_AUDIT_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("GUI_DATABASE_URL", f"sqlite:///{tmp_path / 'gui.db'}")
    monkeypatch.setattr(retention, "Database", lambda _url: db)
    assert retention.main() == 0
    assert "deleted 5 expired failed-login counter(s)" in capsys.readouterr().out
    assert len(_rows(db)) == 5


def test_the_retention_command_purges_counters_with_audit_retention_on(db, monkeypatch, capsys, tmp_path):
    """With an audit retention set the same command does both: the counters first, then the audit rows."""
    import time
    _seed_counters(db, time.time())
    monkeypatch.setenv("GUI_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("GUI_DATABASE_URL", f"sqlite:///{tmp_path / 'gui.db'}")
    monkeypatch.setattr(retention, "Database", lambda _url: db)
    assert retention.main() == 0
    out = capsys.readouterr().out
    assert "expired failed-login counter(s)" in out and "audit row(s) older than 30 day(s)" in out


def test_the_purge_runs_on_a_database_object_of_its_own(tmp_path):
    """The same purge on a file-backed database opened fresh, as the command does."""
    database = Database(f"sqlite:///{tmp_path / 'gui.db'}")
    _seed_counters(database, 1_800_000_000.0)
    assert retention.purge_login_failures(database, POLICY, now=1_800_000_000.0) == 5
