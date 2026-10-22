"""smo_shared/webhook.py — the shared SSRF guard every module's
caller-registered notification/callback destination now routes through
(HISTORY.md OI-6.3's PR #138 CodeQL py/full-ssrf finding). Run with:
cd smo/shared && PYTHONPATH=. python -m pytest tests -q
"""

import httpx
import pytest

from smo_shared.webhook import delete_webhook, get_webhook, is_safe_webhook_destination, post_webhook


# Table: destinations that must be allowed, including private-range addresses a real deployment's containers use.
@pytest.mark.parametrize("destination", [
    "http://consumer/callback",
    "http://rapp-1:8000/cb",
    "https://operator:8000/dispatch",
    "http://10.0.3.7/cb",       # private-range IP — a real deployment's own container
    "http://172.18.0.5/cb",     # Docker's default bridge range
])
def test_allows_ordinary_http_and_https_destinations(destination):
    assert is_safe_webhook_destination(destination) is True


# Table: destinations that must be refused: no destination, other schemes, loopback, link-local (cloud metadata), multicast and unspecified addresses,
# localhost and metadata host names in any case, and malformed URLs (which must be refused, not raise).
@pytest.mark.parametrize("destination", [
    None,
    "",
    "file:///etc/passwd",
    "gopher://internal/x",
    "data:text/plain,hi",
    "ftp://consumer/cb",
    "http://127.0.0.1/cb",
    "http://127.0.0.1:8000/cb",
    "http://[::1]/cb",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata endpoint
    "http://169.254.1.1/cb",                      # link-local, same range as metadata
    "http://localhost/cb",
    "http://LOCALHOST/cb",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://0.0.0.0/cb",
    "http://[",              # malformed (unclosed IPv6 bracket): refused, not an exception
    "http://[::1/cb",
    "file://[",
    "http://224.0.0.1/cb",   # multicast
    "not-a-url",
])
def test_rejects_dangerous_or_malformed_destinations(destination):
    assert is_safe_webhook_destination(destination) is False


def test_post_webhook_calls_httpx_post_for_an_allowed_destination(monkeypatch):
    """An allowed destination is called with httpx.post with the JSON body, and the response is returned."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post",
                         lambda url, json=None, timeout=None, **kw: calls.append((url, json)) or httpx.Response(200))
    resp = post_webhook("http://consumer/cb", json={"a": 1}, timeout=2.0)
    assert calls == [("http://consumer/cb", {"a": 1})]
    assert resp is not None and resp.status_code == 200


def test_post_webhook_no_ops_for_a_disallowed_destination(monkeypatch):
    """A destination the guard refuses is never called and None is returned."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append(url))
    resp = post_webhook("http://169.254.169.254/latest/meta-data/", json={"a": 1})
    assert calls == []
    assert resp is None


def test_post_webhook_swallows_an_unreachable_destination(monkeypatch):
    """A connection error is swallowed: post_webhook returns None."""
    def raise_error(url, json=None, timeout=None, **kw):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr("smo_shared.webhook.httpx.post", raise_error)
    assert post_webhook("http://consumer/cb", json={}) is None


def test_get_webhook_calls_httpx_get_for_an_allowed_destination(monkeypatch):
    """get_webhook calls an allowed destination with httpx.get."""
    monkeypatch.setattr("smo_shared.webhook.httpx.get", lambda url, timeout=None, **kw: httpx.Response(200))
    assert get_webhook("http://producer/health") is not None


def test_get_webhook_no_ops_for_a_disallowed_destination(monkeypatch):
    """get_webhook does not call a refused destination."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.get", lambda url, timeout=None, **kw: calls.append(url))
    assert get_webhook("http://127.0.0.1/health") is None
    assert calls == []


def test_delete_webhook_calls_httpx_delete_for_an_allowed_destination(monkeypatch):
    """delete_webhook calls an allowed destination with httpx.delete and returns the response."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.delete",
                         lambda url, timeout=None, **kw: calls.append(url) or httpx.Response(204))
    resp = delete_webhook("http://producer/jobs/123")
    assert calls == ["http://producer/jobs/123"]
    assert resp is not None and resp.status_code == 204


def test_delete_webhook_no_ops_for_a_disallowed_destination(monkeypatch):
    """delete_webhook does not call a refused destination."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.delete", lambda url, timeout=None, **kw: calls.append(url))
    assert delete_webhook("file:///etc/passwd") is None
    assert calls == []


# ------------------------------------------------------------------------------------------------------- SSRF guard bypass spellings

# Table: spellings of loopback, unspecified, metadata and link-local that a client library resolves to a blocked address but a plain text comparison or
# `ipaddress.ip_address` does not recognise. Each must be refused.
BYPASS_SPELLINGS = [
    "http://localhost./cb",                    # trailing dot
    "http://LOCALHOST../cb",
    "http://app.localhost/cb",                 # RFC 6761
    "http://metadata.google.internal./x",
    "http://127.1/cb",                         # short form
    "http://127.0.1/cb",
    "http://2130706433/cb",                    # decimal
    "http://0x7f000001/cb",                    # hexadecimal, whole address
    "http://0x7f.0.0.1/cb",                    # hexadecimal part
    "http://0X7F.0.0.1/cb",
    "http://0177.0.0.1/cb",                    # octal
    "http://0177.0.0.01/cb",
    "http://017700000001/cb",                  # octal, whole address
    "http://0/cb",                             # unspecified, which connects to this host on Linux
    "http://0.0.0.0/cb",
    "http://00/cb",
    "http://0x0/cb",
    "http://2852039166/cb",                    # 169.254.169.254 as a decimal
    "http://0xa9fea9fe/cb",                    # and as hexadecimal
    "http://169.254.169.254./cb",              # literal with a trailing dot
    "http://[::ffff:127.0.0.1]/cb",            # IPv4-mapped loopback
    "http://[::ffff:7f00:1]/cb",               # the same in hex groups
    "http://[::ffff:169.254.169.254]/cb",      # mapped metadata address
    "http://[::ffff:0.0.0.0]/cb",
    "http://[64:ff9b::7f00:1]/cb",             # NAT64 wrapping loopback
    "http://[2002:7f00:1::]/cb",               # 6to4 wrapping loopback
    "http://127。0。0。1/cb",       # ideographic full stops, which an IDNA-aware client folds to "."
    "http://１２７.0.0.1/cb",      # full-width digits
    "http://256.1/cb",                         # numeric text that is no address: nothing legitimate is written like this
    "http://1.2.3.4.5/cb",
    "http://08.0.0.1/cb",
]


@pytest.mark.parametrize("destination", BYPASS_SPELLINGS)
def test_rejects_alternative_spellings_of_blocked_addresses(destination):
    """Every spelling of a loopback, unspecified, link-local or metadata target is refused; before the fix `127.1`, `2130706433`, `0x7f.0.0.1`, `0` and
    `localhost.` were all let through to a client that connects to loopback."""
    assert is_safe_webhook_destination(destination) is False


# Table: ordinary destinations that look a little like the bypass forms and must keep working.
@pytest.mark.parametrize("destination", [
    "http://example.com./cb",
    "http://consumer./cb",
    "http://10.1/cb",                      # 10.0.0.1 in the short form: private ranges are allowed
    "http://0x0a000001/cb",                # 10.0.0.1
    "http://012.0.0.1/cb",                 # 10.0.0.1 in octal
    "http://[::ffff:10.0.0.1]/cb",         # mapped private address
    "http://[2001:db8::1]/cb",
    "http://localhost-proxy/cb",           # not localhost
    "http://notlocalhost/cb",
    "http://1.2.3.4.example.com/cb",       # a name, not an address
    "http://0x7f.example.com/cb",
])
def test_still_allows_ordinary_destinations_near_the_bypass_forms(destination):
    """Normalising the spellings must not refuse a private-range address or a host name that merely resembles one."""
    assert is_safe_webhook_destination(destination) is True


@pytest.mark.parametrize("destination", ["http://127.1/cb", "http://2130706433/cb", "http://0x7f.0.0.1/cb", "http://localhost./cb", "http://0/cb"])
def test_send_never_calls_a_bypass_spelling(monkeypatch, destination):
    """post_webhook/get_webhook/delete_webhook do not reach httpx for a bypass spelling (the whole path, not only the predicate)."""
    calls = []
    for verb in ("post", "get", "delete"):
        monkeypatch.setattr(f"smo_shared.webhook.httpx.{verb}", lambda *a, **k: calls.append(a))
    assert post_webhook(destination, {}) is None and get_webhook(destination) is None and delete_webhook(destination) is None
    assert calls == []


# ----------------------------------------------------------------------------------------------------------- resolved-address check

def _resolver(monkeypatch, mapping):
    """Helper: makes the module's name lookup answer from `mapping` {host: [addresses]}; a host not in it does not resolve."""
    import socket

    def fake(host, port, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0)) for a in mapping[host]]
    monkeypatch.setattr("smo_shared.webhook.socket.getaddrinfo", fake)


def test_a_name_that_resolves_to_loopback_is_not_called(monkeypatch):
    """A public-looking name whose record points at 127.0.0.1 (or the metadata address) is refused at send time."""
    _resolver(monkeypatch, {"evil.example": ["127.0.0.1"], "meta.example": ["169.254.169.254"], "v6.example": ["::1"]})
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda *a, **k: calls.append(a))
    for host in ("evil.example", "meta.example", "v6.example"):
        assert post_webhook(f"http://{host}/cb", {}) is None
    assert calls == []


def test_one_blocked_address_among_several_blocks_the_name(monkeypatch):
    """Every resolved address is checked, not the first: a name with a public and a loopback record is refused."""
    _resolver(monkeypatch, {"mixed.example": ["93.184.216.34", "127.0.0.1"]})
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda *a, **k: pytest.fail("must not be called"))
    assert post_webhook("http://mixed.example/cb", {}) is None


def test_a_name_that_resolves_to_a_public_or_private_address_is_called(monkeypatch):
    """Resolution must not break the ordinary case: public and private-range answers are called as before."""
    _resolver(monkeypatch, {"ok.example": ["93.184.216.34"], "inner.example": ["10.0.3.7"]})
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append(url) or httpx.Response(200))
    assert post_webhook("http://ok.example/cb", {}) is not None and post_webhook("http://inner.example/cb", {}) is not None
    assert calls == ["http://ok.example/cb", "http://inner.example/cb"]


def test_a_name_that_does_not_resolve_is_still_attempted(monkeypatch):
    """The fictional subscriber names of the unit tests (`http://consumer/cb`) do not resolve; they are passed to the client as before."""
    _resolver(monkeypatch, {})
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append(url) or httpx.Response(200))
    assert post_webhook("http://consumer/cb", {}) is not None and calls == ["http://consumer/cb"]


def test_forward_to_destination_refuses_a_name_that_resolves_to_loopback(monkeypatch):
    """The operator-API forwarder applies the same resolved-address check (it runs the lookup off the event loop)."""
    import asyncio

    from smo_shared.webhook import forward_to_destination
    _resolver(monkeypatch, {"evil.example": ["127.0.0.1"]})
    result = asyncio.run(forward_to_destination("GET", "http://evil.example/x", headers={}, params=None, content=None, timeout=1.0))
    assert result is None
