"""smo_shared/webhook.py: the connection goes to the addresses that were vetted, not to a second answer of the resolver (SEC-15.16, DNS rebinding).

The tests run real requests against local plain and TLS servers. The resolver is a stub that answers a host name with a sequence of answers (the first for the
check, later ones for anything that asks again), and the loopback ban is lifted for the length of a test so a server on 127.0.0.1 can be the "vetted" address.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_webhook_pinned.py -q
"""

import asyncio
import datetime
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from smo_shared import webhook
from smo_shared.webhook import delete_webhook, forward_to_destination, get_webhook, post_webhook

NAME = "wh.example"


# ---- a resolver whose answers change -------------------------------------------------------------------------------------------------------------------

class _Resolver:
    """Answers `getaddrinfo` for the names it was given with a sequence of answers (the last repeats); an address is answered as itself. `asked` lists the names."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers: dict[str, list[list[str]]] = answers
        self.asked: list[str] = []
        self.real = socket.getaddrinfo

    def __call__(self, host: Any, port: Any = None, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in self.answers:
            return self.real(host, port, *args, **kwargs)
        self.asked.append(name)
        sequence = self.answers[name]
        answer = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if not answer:
            raise socket.gaierror(socket.EAI_NONAME, "no such name")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0)) for a in answer]


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, list[list[str]]]], _Resolver]:
    """Installs a `_Resolver` for the given answers over the autouse no-DNS stub."""
    def install(answers: dict[str, list[list[str]]]) -> _Resolver:
        stub = _Resolver(answers)
        monkeypatch.setattr(socket, "getaddrinfo", stub)
        return stub
    return install


@pytest.fixture(autouse=True)
def _no_proxy_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy in the environment (httpx reads it) would take the connection to the proxy instead of the test server."""
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def _loopback_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard bans loopback; the servers of these tests are on it. Only loopback is lifted, every other blocked range stays."""
    original = webhook._is_blocked_address
    monkeypatch.setattr(webhook, "_is_blocked_address", lambda ip: False if ip.is_loopback else original(ip))


# ---- local servers --------------------------------------------------------------------------------------------------------------------------------

class _Server:
    """A local HTTP(S) server on one address that records each request's Host header and the TLS server name the client sent."""

    def __init__(self, address: str = "127.0.0.1", port: int = 0, context: ssl.SSLContext | None = None, status: int = 200, location: str | None = None) -> None:
        self.hosts: list[str | None] = []
        self.server_names: list[str | None] = []
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.hosts.append(self.headers.get("Host"))
                outer.paths.append(f"{self.command} {self.path}")
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET = do_DELETE = _answer

            def log_message(self, *args: Any) -> None:
                pass

        class Httpd(HTTPServer):
            address_family = socket.AF_INET6 if ":" in address else socket.AF_INET

        self.httpd = Httpd((address, port), Handler)
        if context is not None:
            def note_server_name(sock: Any, name: str | None, ctx: Any) -> None:
                self.server_names.append(name)
            context.sni_callback = note_server_name
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port: int = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers() -> Iterator[list[_Server]]:
    """The servers a test started, closed afterwards."""
    started: list[_Server] = []
    yield started
    for server in started:
        server.close()


def _start(servers: list[_Server], **kwargs: Any) -> _Server:
    server = _Server(**kwargs)
    servers.append(server)
    return server


def _certificates(tmp_path: Path, name: str) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A CA and a server certificate for `name` (as a DNS name only): returns the server's TLS context and a client context that trusts only that CA."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False).sign(ca_key, hashes.SHA256()))
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    client_context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    return server_context, client_context


# ---- the rebinding itself --------------------------------------------------------------------------------------------------------------------------

def test_a_public_answer_then_a_loopback_answer_is_never_connected_to(monkeypatch, resolver):
    """The check sees a public address; a second lookup (the client's own) would see loopback. Only the public one is connected to, however the call ends."""
    stub = resolver({NAME: [["93.184.216.34"], ["127.0.0.1"]]})
    attempts: list[Any] = []

    def refuse(address: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(address)
        raise OSError("refused for the test")
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert post_webhook(f"http://{NAME}:8123/cb", {"a": 1}) is None
    assert attempts == [("93.184.216.34", 8123)]
    assert stub.asked == [NAME]                                   # the name was looked up once, by the check


def test_the_connection_goes_to_the_vetted_address_and_keeps_the_host_header(resolver, servers):
    """A rebinding answer (an "evil" server on the same port at the second answer) gets no connection; the vetted server gets the request with the original Host header."""
    good = _start(servers)
    evil = _start(servers, address="127.0.0.2", port=good.port)
    stub = resolver({NAME: [["127.0.0.1"], ["127.0.0.2"]]})
    answer = post_webhook(f"http://{NAME}:{good.port}/cb?x=1", {"a": 1})
    assert answer is not None and answer.status_code == 200
    assert good.hosts == [f"{NAME}:{good.port}"] and good.paths == ["POST /cb?x=1"]
    assert evil.hosts == [] and stub.asked == [NAME]


def test_without_the_pin_a_second_lookup_reaches_the_other_address(resolver, servers):
    """The control of the test above: outside a send the client resolves the name itself, and the second answer wins (which is what the pin prevents)."""
    good = _start(servers)
    evil = _start(servers, address="127.0.0.2", port=good.port)
    resolver({NAME: [["127.0.0.1"], ["127.0.0.2"]]})
    assert webhook._vet_resolution(f"http://{NAME}:{good.port}/").addresses == ("127.0.0.1",)
    with httpx.Client() as client:
        assert client.get(f"http://{NAME}:{good.port}/").status_code == 200
    assert good.hosts == [] and evil.hosts == [f"{NAME}:{good.port}"]


@pytest.mark.parametrize("function,method", [(post_webhook, "POST"), (get_webhook, "GET"), (delete_webhook, "DELETE")])
def test_every_verb_is_pinned(resolver, servers, function, method):
    """post_webhook, get_webhook and delete_webhook all connect to the vetted address."""
    good = _start(servers)
    resolver({NAME: [["127.0.0.1"], ["10.255.255.1"]]})
    url = f"http://{NAME}:{good.port}/x"
    answer = function(url, {"a": 1}) if method == "POST" else function(url)
    assert answer is not None and answer.status_code == 200
    assert good.paths == [f"{method} /x"]


def test_with_several_records_the_next_vetted_address_is_tried_when_one_does_not_connect(resolver, servers):
    """Two A records: the first refuses connections, the second serves. The request reaches the second, in the resolver's order; an address outside the vetted ones is not tried."""
    good = _start(servers, address="127.0.0.3")
    resolver({NAME: [["127.0.0.9", "127.0.0.3"], ["127.0.0.1"]]})
    answer = post_webhook(f"http://{NAME}:{good.port}/cb", {})
    assert answer is not None and answer.status_code == 200 and good.hosts == [f"{NAME}:{good.port}"]


def test_the_addresses_are_tried_in_the_order_the_resolver_gave_them(monkeypatch, resolver):
    """The order of the records is kept and a repeated record is tried once."""
    resolver({NAME: [["127.0.0.5", "127.0.0.4", "127.0.0.5"]]})
    attempts: list[Any] = []

    def refuse(address: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(address[0])
        raise OSError("refused for the test")
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert post_webhook(f"http://{NAME}:8123/cb", {}) is None
    assert attempts == ["127.0.0.5", "127.0.0.4"]


def test_a_name_that_did_not_resolve_at_the_check_is_not_connected_to_when_it_resolves_later(resolver, servers):
    """NXDOMAIN for the check and a live record afterwards: the connect fails, nothing is looked up again."""
    good = _start(servers)
    stub = resolver({NAME: [[], ["127.0.0.1"]]})
    assert post_webhook(f"http://{NAME}:{good.port}/cb", {}) is None
    assert good.hosts == [] and stub.asked == [NAME]


def test_a_blocked_second_record_refuses_the_call_before_any_connection(monkeypatch, resolver, servers):
    """Failure vetting: a name with a public record and a metadata-address record is refused, so nothing is connected to (the guard's rule is unchanged)."""
    monkeypatch.undo()
    good = _start(servers)
    resolver({NAME: [["127.0.0.1", "169.254.169.254"]]})
    monkeypatch.setattr(webhook, "_is_blocked_address", lambda ip: ip.is_link_local)
    assert post_webhook(f"http://{NAME}:{good.port}/cb", {}) is None
    assert good.hosts == []


def test_a_literal_ip_is_connected_to_as_before_and_not_pinned(monkeypatch, resolver, servers):
    """A destination written as an address is not looked up and not pinned."""
    good = _start(servers)
    stub = resolver({})
    seen: list[Any] = []
    original = webhook._pinned_to

    def spy(destination: str | None, vetting: webhook._Vetting) -> Any:
        seen.append(vetting)
        return original(destination, vetting)
    monkeypatch.setattr(webhook, "_pinned_to", spy)
    answer = post_webhook(f"http://127.0.0.1:{good.port}/cb", {})
    assert answer is not None and answer.status_code == 200 and good.hosts == [f"127.0.0.1:{good.port}"]
    assert seen == [webhook._Vetting(False, None)] and stub.asked == []


def test_an_ipv6_record_is_pinned(resolver, servers):
    """A name with a AAAA record is connected to that address."""
    try:
        good = _start(servers, address="::1")
    except OSError:
        pytest.skip("no IPv6 loopback in this environment")
    resolver({NAME: [["::1"], ["127.0.0.1"]]})
    answer = post_webhook(f"http://{NAME}:{good.port}/cb", {})
    assert answer is not None and answer.status_code == 200 and good.hosts == [f"{NAME}:{good.port}"]


def test_an_ipv6_address_is_handed_to_the_connect_as_it_is(monkeypatch, resolver):
    """A AAAA record is connected to by its address (also where this environment has no IPv6 loopback to serve it), then the next record is tried."""
    resolver({NAME: [["2001:db8::1", "2001:db8::2"], ["127.0.0.1"]]})
    attempts: list[Any] = []

    def refuse(address: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(address)
        raise OSError("refused for the test")
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert post_webhook(f"http://{NAME}:8123/cb", {}) is None
    assert attempts == [("2001:db8::1", 8123), ("2001:db8::2", 8123)]


def test_a_redirect_is_not_followed(resolver, servers):
    """A 3xx is returned as it is: no second host is looked up or connected to."""
    target = _start(servers)
    redirecting = _start(servers, address="127.0.0.2", status=302, location=f"http://other.example:{target.port}/x")
    stub = resolver({NAME: [["127.0.0.2"]], "other.example": [["127.0.0.1"]]})
    answer = post_webhook(f"http://{NAME}:{redirecting.port}/cb", {})
    assert answer is not None and answer.status_code == 302
    assert target.hosts == [] and stub.asked == [NAME]


# ---- TLS: the name stays the name -----------------------------------------------------------------------------------------------------------------

def test_https_connects_to_the_vetted_address_with_the_name_as_sni_and_verifies_the_certificate_against_it(monkeypatch, resolver, servers, tmp_path):
    """The TLS server name is the host name and the certificate (valid for that name only) is verified against it, while the socket goes to the vetted address."""
    server_context, client_context = _certificates(tmp_path, NAME)
    good = _start(servers, context=server_context)
    resolver({NAME: [["127.0.0.1"], ["127.0.0.2"]]})
    monkeypatch.setattr(webhook.mtls, "webhook_kwargs", lambda destination: {"verify": client_context})
    answer = post_webhook(f"https://{NAME}:{good.port}/cb", {"a": 1})
    assert answer is not None and answer.status_code == 200
    assert good.server_names == [NAME] and good.hosts == [f"{NAME}:{good.port}"]


def test_https_still_fails_when_the_certificate_is_not_for_the_name(monkeypatch, resolver, servers, tmp_path):
    """Connecting by address does not weaken the check: a certificate for another name is refused."""
    server_context, client_context = _certificates(tmp_path, "other.example")
    good = _start(servers, context=server_context)
    resolver({NAME: [["127.0.0.1"]]})
    monkeypatch.setattr(webhook.mtls, "webhook_kwargs", lambda destination: {"verify": client_context})
    assert post_webhook(f"https://{NAME}:{good.port}/cb", {}) is None
    assert good.hosts == []


# ---- the gateway's forwarded call (asynchronous) ---------------------------------------------------------------------------------------------------

def _forward(url: str) -> httpx.Response | None:
    return asyncio.run(forward_to_destination("POST", url, headers={"a": "b"}, params=[("q", "1")], content=b"{}", timeout=3.0))


def test_a_forwarded_call_goes_to_the_vetted_address(resolver, servers):
    """forward_to_destination (anyio backend) connects to the address of the check, not to a later answer."""
    good = _start(servers)
    stub = resolver({NAME: [["127.0.0.1"], ["10.255.255.1"]]})
    answer = _forward(f"http://{NAME}:{good.port}/x")
    assert answer is not None and answer.status_code == 200
    assert good.hosts == [f"{NAME}:{good.port}"] and good.paths == ["POST /x?q=1"] and stub.asked == [NAME]


def test_a_forwarded_call_to_a_name_that_did_not_resolve_fails_to_connect(resolver, servers):
    """NXDOMAIN at the check, a record later: the forwarded call raises the transport error the gateway maps to 502."""
    good = _start(servers)
    resolver({NAME: [[], ["127.0.0.1"]]})
    with pytest.raises(httpx.ConnectError):
        _forward(f"http://{NAME}:{good.port}/x")
    assert good.hosts == []


# ---- the pieces -------------------------------------------------------------------------------------------------------------------------------------

def test_the_pin_is_set_for_the_send_only():
    """Outside `_pinned_to` nothing is pinned; inside, the host and port are; afterwards it is gone, also when the block raises."""
    vetting = webhook._Vetting(False, ("1.2.3.4",))
    assert webhook._pinned_addresses(NAME, 80) is None
    with webhook._pinned_to(f"http://{NAME}/x", vetting):
        assert webhook._pinned_addresses(NAME, 80) == ("1.2.3.4",)
        assert webhook._pinned_addresses(NAME, 81) is None and webhook._pinned_addresses("other.example", 80) is None
    assert webhook._pinned_addresses(NAME, 80) is None
    with pytest.raises(RuntimeError):
        with webhook._pinned_to(f"https://{NAME}/x", vetting):
            assert webhook._pinned_addresses(NAME, 443) == ("1.2.3.4",)
            raise RuntimeError("boom")
    assert webhook._pinned_addresses(NAME, 443) is None


def test_a_literal_ip_or_an_unreadable_url_pins_nothing():
    """No addresses (a literal IP) and a URL httpx cannot read leave the pin unset."""
    with webhook._pinned_to(f"http://{NAME}/x", webhook._Vetting(False, None)):
        assert webhook._PINNED.get() is None
    with webhook._pinned_to("http://x\x00/", webhook._Vetting(False, ("1.2.3.4",))):
        assert webhook._PINNED.get() is None
    with webhook._pinned_to(None, webhook._Vetting(False, ("1.2.3.4",))):
        assert webhook._PINNED.get() is None


def test_a_pin_inside_a_pin_keeps_the_outer_one():
    """Two pins nest: the inner adds its host, and the outer is back after it."""
    with webhook._pinned_to("http://a.example/", webhook._Vetting(False, ("1.1.1.1",))):
        with webhook._pinned_to("http://b.example/", webhook._Vetting(False, ("2.2.2.2",))):
            assert webhook._pinned_addresses("a.example", 80) == ("1.1.1.1",) and webhook._pinned_addresses("b.example", 80) == ("2.2.2.2",)
        assert webhook._pinned_addresses("b.example", 80) is None and webhook._pinned_addresses("a.example", 80) == ("1.1.1.1",)


@pytest.mark.parametrize("url,key", [
    ("http://Wh.Example/x", ("wh.example", 80)), ("https://wh.example/x", ("wh.example", 443)), ("http://wh.example:8080/x", ("wh.example", 8080)),
    ("https://[::1]:9000/", ("::1", 9000)), ("http://bücher.example/", ("xn--bcher-kva.example", 80)),
])
def test_the_pin_key_is_the_host_and_port_as_httpx_connects_to_them(url, key):
    """Default ports, case, brackets and IDNA follow httpx's own reading of the URL."""
    assert webhook._pin_key(url) == key


def test_the_pin_key_of_an_unreadable_url_is_none():
    """A URL httpx cannot read has no key, so nothing is pinned for it."""
    assert webhook._pin_key("http://x\x00/") is None


def test_an_unknown_scheme_falls_back_to_port_zero():
    """A scheme with no default port gets port 0 (the guard never lets one through; this only fixes the key)."""
    assert webhook._pin_key("gopher://wh.example/") == ("wh.example", 0)


def test_vetting_lists_each_address_once_in_the_resolvers_order(resolver):
    """One lookup; a repeated record is dropped; blocked when any record is blocked (loopback is lifted by this module's fixture, link-local is not)."""
    stub = resolver({NAME: [["8.8.8.8", "1.1.1.1", "8.8.8.8"]], "bad.example": [["8.8.8.8", "169.254.169.254"]], "none.example": [[]]})
    assert webhook._vet_resolution(f"http://{NAME}/") == webhook._Vetting(False, ("8.8.8.8", "1.1.1.1"))
    assert webhook._vet_resolution("http://bad.example/") == webhook._Vetting(True, ("8.8.8.8", "169.254.169.254"))
    assert webhook._vet_resolution("http://none.example/") == webhook._Vetting(False, ())
    assert stub.asked == [NAME, "bad.example", "none.example"]


@pytest.mark.parametrize("destination", [None, "", "http://", "http://[bad/", "http://256.1/", "http://rapp:99999/"])
def test_vetting_blocks_what_has_no_usable_host(destination):
    """A missing, empty, malformed or numeric-but-invalid host is blocked and carries no addresses."""
    assert webhook._vet_resolution(destination) == webhook._Vetting(True, None)


def test_vetting_a_literal_ip_is_not_blocked_and_carries_no_addresses():
    """A public literal IP passes the vetting without a lookup and with no addresses to pin."""
    assert webhook._vet_resolution("http://8.8.8.8/") == webhook._Vetting(False, None)


def test_the_pinned_connect_is_installed_on_httpcores_backends_once():
    """The real backends are wrapped (and marked) at import; installing again does not wrap the wrapper."""
    before = (httpcore.SyncBackend.connect_tcp, httpcore.AnyIOBackend.connect_tcp)
    webhook._install_pinned_connect(httpcore.SyncBackend, httpcore.AnyIOBackend)
    assert (httpcore.SyncBackend.connect_tcp, httpcore.AnyIOBackend.connect_tcp) == before
    assert hasattr(before[0], webhook._MARK) and hasattr(before[1], webhook._MARK)


def test_installing_wraps_each_backend_once_and_keeps_the_original_behind_it():
    """On two stand-in backends: each `connect_tcp` is replaced by a marked wrapper that still reaches the original with `self`; a second install changes nothing."""
    log: list[Any] = []

    class Sync:
        def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> str:
            log.append(("sync", self, host, port, args, kwargs))
            return "sync stream"

    class Async:
        async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> str:
            log.append(("async", self, host, port, args, kwargs))
            return "async stream"
    plain = (Sync.connect_tcp, Async.connect_tcp)
    webhook._install_pinned_connect(Sync, Async)
    wrapped = (Sync.connect_tcp, Async.connect_tcp)
    assert wrapped[0] is not plain[0] and wrapped[1] is not plain[1]
    assert hasattr(wrapped[0], webhook._MARK) and hasattr(wrapped[1], webhook._MARK) and not hasattr(plain[0], webhook._MARK) and not hasattr(plain[1], webhook._MARK)
    assert getattr(wrapped[0], webhook._MARK) is True and getattr(wrapped[1], webhook._MARK) is True
    assert asyncio.iscoroutinefunction(wrapped[1]) and not asyncio.iscoroutinefunction(wrapped[0])
    webhook._install_pinned_connect(Sync, Async)
    assert (Sync.connect_tcp, Async.connect_tcp) == wrapped
    sync_owner, async_owner = Sync(), Async()
    assert sync_owner.connect_tcp("h", 1, 2.0, local_address="x") == "sync stream"
    assert asyncio.run(async_owner.connect_tcp("h", 2, 3.0, local_address="y")) == "async stream"
    assert log == [("sync", sync_owner, "h", 1, (2.0,), {"local_address": "x"}), ("async", async_owner, "h", 2, (3.0,), {"local_address": "y"})]


def test_a_wrapped_connect_goes_to_the_pinned_address_and_passes_the_other_arguments_on():
    """Inside a pin the wrapper replaces the host by each vetted address (not the port or the rest); outside one it passes the host through."""
    log: list[Any] = []

    class Sync:
        def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> str:
            log.append((self, host, port, args, kwargs))
            return "stream"
    wrapped = webhook._pin_sync(Sync.connect_tcp)
    owner = Sync()
    with webhook._pinned_to(f"http://{NAME}:81/", webhook._Vetting(False, ("1.1.1.1",))):
        assert wrapped(owner, NAME, 81, 5.0, local_address=None) == "stream"
        assert wrapped(owner, "other.example", 81, 5.0) == "stream"
    assert log == [(owner, "1.1.1.1", 81, (5.0,), {"local_address": None}), (owner, "other.example", 81, (5.0,), {})]


def test_a_wrapped_asynchronous_connect_goes_to_the_pinned_address_too():
    """The same for the asynchronous backend's wrapper."""
    log: list[Any] = []

    class Async:
        async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> str:
            log.append((self, host, port, args, kwargs))
            return "stream"
    wrapped = webhook._pin_async(Async.connect_tcp)
    owner = Async()

    async def run() -> None:
        with webhook._pinned_to(f"http://{NAME}:81/", webhook._Vetting(False, ("1.1.1.1",))):
            assert await wrapped(owner, NAME, 81, 5.0, local_address=None) == "stream"
            assert await wrapped(owner, "other.example", 81, 5.0) == "stream"
    asyncio.run(run())
    assert log == [(owner, "1.1.1.1", 81, (5.0,), {"local_address": None}), (owner, "other.example", 81, (5.0,), {})]


def test_an_unpinned_connection_is_passed_through_unchanged():
    """With no pin set, the original connect gets the host, port and every other argument as given."""
    calls: list[Any] = []

    def connect(*a: Any, **k: Any) -> Any:
        calls.append((a, k))
        return "stream"
    result = webhook._connect_pinned(connect, NAME, 80, (1.5,), {"local_address": None})
    assert result == "stream" and calls == [((NAME, 80, 1.5), {"local_address": None})]


def test_a_pinned_connection_that_times_out_tries_the_next_address_and_raises_the_last_failure():
    """A connect timeout counts as a failure of that address; the port and the other arguments reach every attempt; when none connects the last failure is raised."""
    seen: list[Any] = []

    def connect(address: str, port: int, *args: Any, **kwargs: Any) -> Any:
        seen.append((address, port, args, kwargs))
        raise httpcore.ConnectTimeout(address)
    expected = [("1.1.1.1", 80, (1.5,), {"x": 1}), ("2.2.2.2", 80, (1.5,), {"x": 1})]
    with webhook._pinned_to(f"http://{NAME}/", webhook._Vetting(False, ("1.1.1.1", "2.2.2.2"))):
        with pytest.raises(httpcore.ConnectTimeout, match="^2.2.2.2$"):
            webhook._connect_pinned(connect, NAME, 80, (1.5,), {"x": 1})
        assert seen == expected
        seen.clear()
        with pytest.raises(httpcore.ConnectTimeout, match="^2.2.2.2$"):
            asyncio.run(webhook._connect_pinned_async(_as_async(connect), NAME, 80, (1.5,), {"x": 1}))
        assert seen == expected


def test_a_pin_with_no_address_is_a_connect_error():
    """A name that did not resolve is pinned to nothing: the connect fails, for both backends, and the original is never called."""
    seen: list[Any] = []

    def connect(address: str, port: int, *args: Any, **kwargs: Any) -> Any:
        seen.append(address)
    with webhook._pinned_to(f"http://{NAME}/", webhook._Vetting(False, ())):
        with pytest.raises(httpcore.ConnectError, match="^the destination had no vetted address$"):
            webhook._connect_pinned(connect, NAME, 80, (), {})
        with pytest.raises(httpcore.ConnectError, match="^the destination had no vetted address$"):
            asyncio.run(webhook._connect_pinned_async(_as_async(connect), NAME, 80, (), {}))
    assert seen == []


def test_an_unpinned_asynchronous_connection_is_passed_through_unchanged():
    """With no pin set, the asynchronous connect gets the host, port and every other argument as given."""
    calls: list[Any] = []

    async def connect(*a: Any, **k: Any) -> Any:
        calls.append((a, k))
        return "stream"
    assert asyncio.run(webhook._connect_pinned_async(connect, NAME, 80, (2.0,), {"x": 1})) == "stream" and calls == [((NAME, 80, 2.0), {"x": 1})]


def test_an_error_other_than_a_connect_failure_is_not_swallowed():
    """Only a failure to connect moves on to the next address."""
    def connect(address: str, port: int, *args: Any, **kwargs: Any) -> Any:
        raise ValueError("not a connect failure")
    with webhook._pinned_to(f"http://{NAME}/", webhook._Vetting(False, ("1.1.1.1", "2.2.2.2"))):
        with pytest.raises(ValueError):
            webhook._connect_pinned(connect, NAME, 80, (), {})


def _as_async(function: Callable[..., Any]) -> Callable[..., Any]:
    async def run(*args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)
    return run


def test_the_loopback_ban_is_back_after_the_fixture(monkeypatch):
    """The fixture of this module lifts the loopback ban for its own tests only (a guard against the lift leaking)."""
    monkeypatch.undo()
    assert webhook._is_blocked_address(ipaddress.ip_address("127.0.0.1")) is True
