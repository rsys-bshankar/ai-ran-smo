"""SEC-15.11: the sign-in lockout is keyed by (source address, user name) and a source is throttled on its own.

Before, failures were counted under the user name alone, so anyone could lock any user out from anywhere, and one source trying many names was not slowed. The tests send the address
the way the console's nginx does, in `X-Forwarded-For` (the test client's own peer is not an address, so it is the source `unknown`). Fixtures (`app`, `cfg`, `db`, `smo`) and `PASSWORDS`
come from `test_main.py`. No network. Run with `cd smo/gui-bff && PYTHONPATH=.:../shared python -m pytest tests/test_login_sources.py -q`.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as gui_main
from app import source
from app.config import Settings
from app.db import LoginFailure
from app.main import create_app, seed_users
from app.smo_client import R1Gateway

from test_main import PASSWORDS, R1, app, cfg, db, smo  # noqa: F401  (pytest fixtures and constants)

ATTACKER = "203.0.113.7"
HOME = "198.51.100.20"


def _login(app, username, password, ip=None, via="/api/login"):
    """One sign-in attempt from `ip` (as the console's nginx would send it), returning the response."""
    headers = {"X-Forwarded-For": ip} if ip else {}
    client = TestClient(app)
    if via == "/api/token":
        return client.post(via, data={"grant_type": "password", "username": username, "password": password}, headers=headers)
    return client.post(via, json={"username": username, "password": password}, headers=headers)


def test_the_existing_names_and_numbers_are_unchanged():
    """`MAX_LOGIN_FAILURES` and `LOCKOUT_SECONDS` keep their names and values: five failures in five minutes lock a user for the source that failed."""
    assert (gui_main.MAX_LOGIN_FAILURES, gui_main.LOCKOUT_SECONDS) == (5, 300)
    assert gui_main.MAX_SOURCE_FAILURES > gui_main.MAX_LOGIN_FAILURES


def test_an_attacker_cannot_lock_a_user_out_from_another_address(app):
    """Five wrong passwords for `admin` from one address lock `admin` for that address only: the real admin signs in from another one (the finding)."""
    for _ in range(5):
        assert _login(app, "admin", "guess", ATTACKER).status_code == 401
    assert _login(app, "admin", PASSWORDS["admin"], ATTACKER).status_code == 429          # locked for the attacker, even with the right password
    assert _login(app, "admin", PASSWORDS["admin"], HOME).status_code == 200              # not for anyone else


def test_the_lock_holds_for_the_source_that_failed_and_for_the_token_grant(app):
    """The pair lock is shared by the form and the `/api/token` grant (one counter per source and name), and does not lock another name from that source."""
    for _ in range(5):
        assert _login(app, "viewer", "guess", ATTACKER, via="/api/token").status_code == 400
    assert _login(app, "viewer", PASSWORDS["viewer"], ATTACKER).status_code == 429
    assert _login(app, "viewer", PASSWORDS["viewer"], HOME).status_code == 200
    assert _login(app, "operator", PASSWORDS["operator"], ATTACKER).status_code == 200


def test_one_source_trying_many_names_is_throttled(app):
    """A source that fails `MAX_SOURCE_FAILURES` times, whatever the names, is refused (429) for every name, a valid one with the right password included; other sources are not."""
    for n in range(gui_main.MAX_SOURCE_FAILURES):
        assert _login(app, f"nobody{n}", "guess", ATTACKER).status_code == 401
    assert _login(app, "operator", PASSWORDS["operator"], ATTACKER).status_code == 429
    assert _login(app, "operator", PASSWORDS["operator"], HOME).status_code == 200


def test_a_successful_sign_in_clears_the_pair_but_not_the_source_budget(app):
    """A sign-in clears the pair counter of that user for that source only; the source's own failure count is kept, so an attacker cannot reset it by signing in to an account of its own."""
    for _ in range(3):
        assert _login(app, "viewer", "guess", ATTACKER).status_code == 401
    assert _login(app, "operator", PASSWORDS["operator"], ATTACKER).status_code == 200
    assert _login(app, "viewer", PASSWORDS["viewer"], ATTACKER).status_code == 200       # the pair had 3 of 5: still allowed, and now cleared
    for _ in range(5):
        assert _login(app, "viewer", "guess", ATTACKER).status_code == 401              # a fresh budget of 5 for the pair...
    with app.state.db.session() as s:
        counts = {row.username: row.count for row in s.query(LoginFailure)}
    assert counts[source.source_key(ATTACKER)] == 8                                       # ...while the source counted all 3 + 5 failures


def test_a_spoofed_left_part_of_x_forwarded_for_does_not_change_the_source(app):
    """The nearest proxy appends the address it saw; whatever a client puts before it is ignored, so rotating the left part does not give a fresh lock budget."""
    for n in range(5):
        assert _login(app, "admin", "guess", f"10.0.0.{n}, {ATTACKER}").status_code == 401
    assert _login(app, "admin", PASSWORDS["admin"], f"10.9.9.9, {ATTACKER}").status_code == 429


def test_with_no_trusted_proxy_the_header_is_ignored(cfg, db, smo):
    """With `trusted_proxy_hops` 0 (clients reach the backend directly) `X-Forwarded-For` is not believed: a client cannot pick its source, and the source is the peer."""
    cfg.trusted_proxy_hops = 0
    seed_users(db, cfg)
    direct = create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))
    for n in range(5):
        assert _login(direct, "admin", "guess", f"192.0.2.{n}").status_code == 401
    assert _login(direct, "admin", PASSWORDS["admin"], "192.0.2.99").status_code == 429


# ---------------------------------------------------------------- the address

# Table: (peer, X-Forwarded-For, hops, the source). Direct, one proxy, two proxies, too few entries, junk, IPv6 as its /64, IPv4-mapped IPv6, and no peer at all.
@pytest.mark.parametrize("peer, forwarded, hops, expected", [
    ("192.0.2.1", None, 1, "192.0.2.1"),
    ("172.18.0.5", "198.51.100.9", 1, "198.51.100.9"),
    ("172.18.0.5", "1.1.1.1, 198.51.100.9", 1, "198.51.100.9"),
    ("172.18.0.5", "198.51.100.9, 10.0.0.2", 2, "198.51.100.9"),
    ("172.18.0.5", "198.51.100.9", 2, "172.18.0.5"),
    ("172.18.0.5", "not-an-address", 1, "172.18.0.5"),
    ("172.18.0.5", "198.51.100.9", 0, "172.18.0.5"),
    ("172.18.0.5", "2001:db8:1:2:3:4:5:6", 1, "2001:db8:1:2::/64"),
    ("172.18.0.5", "2001:db8:1:2:ffff::1", 1, "2001:db8:1:2::/64"),
    ("172.18.0.5", "::ffff:198.51.100.9", 1, "198.51.100.9"),
    ("testclient", None, 1, "unknown"),
    (None, None, 1, "unknown"),
    ("172.18.0.5", " , ", 1, "172.18.0.5"),
])
def test_client_source(peer, forwarded, hops, expected):
    """`client_source` reads the address the nearest trusted proxy wrote, normalises IPv6 to its /64, and falls back to the peer, then to `unknown`, rather than to a client's word."""
    assert source.client_source(peer, forwarded, hops) == expected


def test_the_keys_of_a_pair_a_source_and_a_user_do_not_collide():
    """A (source, name) pair key, a source key and the key of the name alone are three different strings, and the pair keys of one name end the same way."""
    keys = {source.pair_key("1.2.3.4", "admin"), source.source_key("1.2.3.4"), "admin", source.pair_key("1.2.3.4", "admin2")}
    assert len(keys) == 4
    assert source.pair_key("1.2.3.4", "admin").startswith(source.PAIR_PREFIX) and source.pair_key("1.2.3.4", "admin").endswith(source.pair_key_suffix("admin"))
    assert not source.pair_key("1.2.3.4", "admin2").endswith(source.pair_key_suffix("admin"))


def test_the_trusted_proxy_setting_is_read_and_checked(monkeypatch):
    """`GUI_TRUSTED_PROXY_HOPS` defaults to 1, reads a whole number, and a negative or non-numeric value stops the start with a message naming the variable."""
    monkeypatch.delenv("GUI_TRUSTED_PROXY_HOPS", raising=False)
    assert Settings().trusted_proxy_hops == 1
    monkeypatch.setenv("GUI_TRUSTED_PROXY_HOPS", "2")
    assert Settings().trusted_proxy_hops == 2
    monkeypatch.setenv("GUI_TRUSTED_PROXY_HOPS", "0")
    assert Settings().trusted_proxy_hops == 0
    for bad in ("-1", "two", "1.5"):
        monkeypatch.setenv("GUI_TRUSTED_PROXY_HOPS", bad)
        with pytest.raises(ValueError, match="GUI_TRUSTED_PROXY_HOPS"):
            Settings()
