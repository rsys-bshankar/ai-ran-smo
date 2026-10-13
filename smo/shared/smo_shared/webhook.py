"""Shared outbound-webhook dispatch for every caller-registered
notification/callback destination in this build (DME's producer/type
callbacks, SME's event subscriptions, AIMgF's job-completion and guard
notifications, FOCOM/MDAF subscription callbacks, Intent
Service's RMIH and autonomy-dispatch notifications, ...).

CodeQL py/full-ssrf flags every one of these: each destination is
caller-supplied (set at subscribe/register time, stored, called back
later), and a best-effort POST/GET goes out to it. A private-IP
blocklist is the textbook SSRF mitigation, but a *hostname* allowlist
is not viable here — this build's legitimate targets are frequently
other rApp/producer containers whose hostnames are assigned at deploy
time (NFO) and are never known in advance (HISTORY.md OI-6.3's own
PR #138 discussion). So the barrier this module applies is the
part of SSRF mitigation that doesn't depend on knowing the target set
up front: block the scheme (http/https only — no file:///, gopher://,
data:, ...) and block the handful of literal addresses that are never
a legitimate webhook target anywhere (loopback, link-local — which
includes the 169.254.169.254 cloud metadata endpoint, multicast,
unspecified/reserved). Everything else — any other hostname, any other
private-range IP a real deployment's containers sit on — passes
through unchanged, same as before this module existed.

The literal-address check is done on the address as a client would read it, not as the text happens to look: a trailing dot (`localhost.`), the
short and numeric spellings `inet_aton` accepts (`127.1`, `2130706433`, `0x7f.0.0.1`, `0177.0.0.1`, `0`), full-width and ideographic-dot spellings that an
IDNA-aware client folds to ASCII, and an IPv4 address wrapped in IPv6 (`::ffff:127.0.0.1`, 6to4, NAT64) are all reduced to the IPv4 or IPv6 address they
mean before the blocked ranges are applied (`_literal_ip`).

`is_safe_webhook_destination` does not resolve DNS: unit tests across this build register fictional subscriber hostnames (`http://consumer/cb`,
`http://rapp-1/cb`, ...) that do not resolve outside a real Docker Compose network, and the check also runs at enqueue time, where a lookup would be a
network call per registration. The send itself (`_send`, `forward_to_destination`) resolves the host once (`_vet_resolution`) and refuses the call when any
address it resolves to is blocked, so a name that points at loopback or the metadata address is not called.

The connection then goes to the addresses that were vetted, not to whatever the name resolves to a second time (SEC-15.16, DNS rebinding). The callers and their
tests call the module-level `httpx.post`/`get`/`delete` and `httpx.AsyncClient`, which take no transport, so the pin is applied one level down: while a send is
in progress, a context variable holds `(host, port) -> vetted addresses`, and `httpcore`'s `connect_tcp` (the sync and the anyio backend, wrapped once at import,
`_install_pinned_connect`) opens the socket to those addresses, in the order the resolver gave them, instead of resolving the name. Only the TCP connect changes:
the request URL, the `Host` header, the TLS server name (SNI) and the certificate check all keep the original host name, and a literal IP is connected to
as before. A name that did not resolve at the check is pinned to no address, so the connect fails and a record that appears in between is not followed. A
redirect is not followed (httpx's default), so no second host is ever connected to; a proxy configured in the environment is connected to by its own name
(the proxy resolves the destination and is a trusted part of the deployment's network).
"""

import asyncio
import contextlib
import contextvars
import ipaddress
import logging
import re
import socket
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpcore
import httpx

from . import metrics, mtls

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata.google.internal", "metadata"})
_BLOCKED_SUFFIXES = (".localhost",)                  # RFC 6761: every name under localhost is loopback
_DOT_VARIANTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})   # dots an IDNA-aware client folds to "."
_NUMERIC_LABEL = re.compile(r"(0[xX][0-9a-fA-F]*|[0-9]+)")
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _canonical_host(host: str) -> str:
    """The host as a resolver sees it: lower case, compatibility-folded (full-width digits, ideographic dots) and without trailing dots, so `LOCALHOST.`
    and `localhost` are the same name."""
    return unicodedata.normalize("NFKC", host).translate(_DOT_VARIANTS).rstrip(".").lower()


def _parse_inet_aton(host: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address `inet_aton` reads from `host` (one to four dot-separated numbers, each decimal, `0x` hexadecimal or `0`-prefixed octal; the last
    number fills the remaining bytes), or None when `host` is not written that way.

    Raises ValueError for a host made only of numeric labels that is not a valid address (`256.1`, `08`, `1.2.3.4.5`): no real host name looks like that,
    and a client library may still read it as an address, so the caller treats it as blocked.
    """
    labels = host.split(".")
    if not 1 <= len(labels) <= 4 or not all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        if labels and all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
            raise ValueError("too many numeric labels")
        return None
    numbers = []
    for label in labels:
        try:
            if label[:2] in ("0x", "0X"):
                numbers.append(int(label[2:] or "0", 16))
            elif label.startswith("0"):                  # "0" itself reads the same in base 8
                numbers.append(int(label, 8))
            else:
                numbers.append(int(label))
        except ValueError as exc:                     # a digit 8 or 9 in an octal-looking label
            raise ValueError("bad numeric label") from exc
    *leading, last = numbers
    if any(n > 255 for n in leading) or last >= 256 ** (5 - len(numbers)):
        raise ValueError("numeric label out of range")
    value = last
    for position, n in enumerate(leading):
        value |= n << (8 * (3 - position))
    return ipaddress.IPv4Address(value)


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address `host` denotes when it is written as one, in any spelling a client accepts; None for an ordinary host name.

    Raises ValueError for numeric-looking text that is not a valid address (see `_parse_inet_aton`). An IPv6 literal may carry a zone (`fe80::1%eth0`),
    which is dropped.
    """
    host = _canonical_host(host)
    if ":" in host:
        try:
            return ipaddress.ip_address(host.partition("%")[0])
        except ValueError as exc:
            raise ValueError("not an IPv6 address") from exc
    return _parse_inet_aton(host)


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for an address that is never a legitimate callback target: loopback, link-local (including 169.254.169.254), multicast, unspecified or
    reserved. An IPv6 address that carries an IPv4 one (IPv4-mapped `::ffff:a.b.c.d`, 6to4 `2002::/16`) is judged by the IPv4 address inside it, and the NAT64
    prefix `64:ff9b::/96` is blocked whole (a translator in front of a NAT64 gateway can reach loopback and the metadata address through it). Private ranges are not blocked: real deployments' containers sit in them."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is not None and _is_blocked_address(embedded):
            return True
        if ip in _NAT64_PREFIX:                       # the whole translation prefix (inside the reserved ::/8 range on today's Python; stated here so it does not depend on that)
            return True
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved


def _is_blocked_literal_ip(host: str) -> bool:
    """True when `host` is an IP address, in any spelling, that is never a legitimate callback target (see `_is_blocked_address`), or numeric text that
    is not a valid address at all.

    False for a hostname (not resolved here) and for private-range addresses, which real deployments use.
    """
    try:
        ip = _literal_ip(host)
    except ValueError:
        return True                                   # numeric-looking but not an address: nothing legitimate is written like this
    return ip is not None and _is_blocked_address(ip)


def _is_blocked_hostname(host: str) -> bool:
    """True for a name that is loopback or metadata by definition (`localhost`, `localhost.`, `app.localhost`, `metadata.google.internal`)."""
    name = _canonical_host(host)
    return name in _BLOCKED_HOSTNAMES or name.endswith(_BLOCKED_SUFFIXES)


def _resolved_addresses(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address the system resolver gives for `host`; an empty list when the name does not resolve (the call then fails by itself)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(str(info[4][0]).partition("%")[0]))
        except ValueError:
            continue
    return found


class _Vetting(NamedTuple):
    """The outcome of vetting a destination's host: `blocked` (do not call), and for a host name `addresses`, every distinct address the one lookup gave (in the
    resolver's order; empty when the name did not resolve). `addresses` is None for a literal IP and for a destination that is blocked for want of a host."""
    blocked: bool
    addresses: tuple[str, ...] | None


def _vet_resolution(destination: str | None) -> _Vetting:
    """Resolves the host of `destination` once and judges it. Blocked when the host resolves to at least one blocked address: every address counts, not the
    first, because a name with one public and one loopback record could be connected to on either. A literal IP needs no lookup (the literal check has judged
    it), and a name that does not resolve is not blocked here (its `addresses` are empty, and the send pins it to nothing)."""
    if not destination:
        return _Vetting(True, None)
    try:
        parts = urlsplit(destination)
        host, port = parts.hostname, parts.port
    except ValueError:
        return _Vetting(True, None)
    if not host:
        return _Vetting(True, None)
    try:
        if _literal_ip(host) is not None:
            return _Vetting(False, None)
    except ValueError:
        return _Vetting(True, None)
    found = _resolved_addresses(host, port)
    return _Vetting(any(_is_blocked_address(ip) for ip in found), tuple(dict.fromkeys(str(ip) for ip in found)))


def _resolves_to_blocked_address(destination: str | None) -> bool:
    """True when the host of `destination` resolves to at least one blocked address (see `_vet_resolution`)."""
    return _vet_resolution(destination).blocked


# SEC-15.16: the connection goes to the vetted addresses. `(host, port) -> addresses` for the send in progress in this thread or task; None outside a send.
_PINNED: contextvars.ContextVar[dict[tuple[str, int], tuple[str, ...]] | None] = contextvars.ContextVar("smo_webhook_pinned", default=None)
_MARK, _CONNECT = "_smo_pinned", "connect_tcp"        # the attribute that marks a wrapped connect_tcp, and the method wrapped
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _pin_key(destination: str) -> tuple[str, int] | None:
    """The `(host, port)` the HTTP client will ask `connect_tcp` for when it calls `destination`, spelled as httpx spells it; None when httpx cannot read the URL."""
    try:
        url = httpx.URL(destination)
    except httpx.InvalidURL:
        return None
    return url.raw_host.decode(), url.port or _DEFAULT_PORTS.get(url.scheme, 0)


@contextlib.contextmanager
def _pinned_to(destination: str | None, vetting: _Vetting) -> Iterator[None]:
    """For the length of the block, a connection to the host of `destination` is opened to the addresses in `vetting` and the name is not looked up again. No-op for
    a literal IP (nothing to resolve)."""
    key = _pin_key(destination) if destination and vetting.addresses is not None else None
    if key is None or vetting.addresses is None:
        yield
        return
    token = _PINNED.set({**(_PINNED.get() or {}), key: vetting.addresses})
    try:
        yield
    finally:
        _PINNED.reset(token)


def _pinned_addresses(host: str, port: int) -> tuple[str, ...] | None:
    """The vetted addresses for a connection to `host`:`port` made inside `_pinned_to`; None when it is not pinned (connect as the client would)."""
    pins = _PINNED.get()
    return None if pins is None else pins.get((host, port))


def _connect_pinned(connect: Callable[..., Any], host: str, port: int, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Calls `connect(address, port, ...)` for each vetted address of `host`:`port` in turn and returns the first connection; the last failure is raised when
    none connects (a `ConnectError` when the name has no vetted address). `connect` is the original `connect_tcp` with `self` bound."""
    addresses = _pinned_addresses(host, port)
    if addresses is None:
        return connect(host, port, *args, **kwargs)
    failure: Exception = httpcore.ConnectError("the destination had no vetted address")
    for address in addresses:
        try:
            return connect(address, port, *args, **kwargs)
        except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
            failure = exc
    raise failure


async def _connect_pinned_async(connect: Callable[..., Awaitable[Any]], host: str, port: int, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """`_connect_pinned` for the asynchronous backend."""
    addresses = _pinned_addresses(host, port)
    if addresses is None:
        return await connect(host, port, *args, **kwargs)
    failure: Exception = httpcore.ConnectError("the destination had no vetted address")
    for address in addresses:
        try:
            return await connect(address, port, *args, **kwargs)
        except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
            failure = exc
    raise failure


def _pin_sync(original: Callable[..., Any]) -> Callable[..., Any]:
    """`original` (a synchronous backend's `connect_tcp`, unbound) as a function that connects to the vetted addresses inside `_pinned_to`; marked so it is wrapped once."""
    def connect_tcp(self: Any, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        return _connect_pinned(lambda h, p, *a, **k: original(self, h, p, *a, **k), host, port, args, kwargs)
    setattr(connect_tcp, _MARK, True)
    return connect_tcp


def _pin_async(original: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """`_pin_sync` for an asynchronous backend's `connect_tcp`."""
    async def connect_tcp(self: Any, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        return await _connect_pinned_async(lambda h, p, *a, **k: original(self, h, p, *a, **k), host, port, args, kwargs)
    setattr(connect_tcp, _MARK, True)
    return connect_tcp


def _install_pinned_connect(sync_backend: type, async_backend: type) -> None:
    """Wraps `connect_tcp` of a synchronous and an asynchronous httpcore backend class, once each, so a connection made inside `_pinned_to` goes to the vetted
    address (SEC-15.16). TLS is started later by httpcore on the stream with the origin's host name as the server name, so SNI and the certificate check are unaffected."""
    if not hasattr(getattr(sync_backend, _CONNECT), _MARK):
        setattr(sync_backend, _CONNECT, _pin_sync(getattr(sync_backend, _CONNECT)))
    if not hasattr(getattr(async_backend, _CONNECT), _MARK):
        setattr(async_backend, _CONNECT, _pin_async(getattr(async_backend, _CONNECT)))


_install_pinned_connect(httpcore.SyncBackend, httpcore.AnyIOBackend)


def is_safe_webhook_destination(destination: str | None) -> bool:
    """True when `destination` may be called: a non-empty `http` or `https` URL with a host that is not one of `localhost`, `metadata.google.internal`,
    `metadata` (or a name under `.localhost`, with or without a trailing dot) and not a blocked literal IP in any spelling (`127.1`, `2130706433`,
    `0x7f.0.0.1`, `::ffff:127.0.0.1`, ...).

    False for None, an empty or malformed URL, any other scheme or a missing host. This is the SSRF guard every outbound callback passes (the outbox
    checks it at enqueue and again at send). It does not resolve names (the send does, see `_send`).
    """
    if not destination:
        return False
    try:
        parts = urlsplit(destination)
        hostname = parts.hostname
    except ValueError:  # malformed, e.g. an unclosed IPv6 bracket ("http://[")
        return False
    if parts.scheme not in _ALLOWED_SCHEMES or not hostname:
        return False
    if _is_blocked_hostname(hostname):
        return False
    return not _is_blocked_literal_ip(hostname)


def _send(method: str, destination: str | None, **kwargs) -> httpx.Response | None:
    """The one place a callback leaves the process (PR-OBS-2.6): counted by outcome under the constant target `callback`, never by host
    (the destination is caller-supplied, so a host label would be unbounded)."""
    vetting = _vet_resolution(destination) if is_safe_webhook_destination(destination) else None
    if vetting is None or vetting.blocked:
        if destination and method == "post":
            log.warning("webhook destination %r rejected by SSRF guard; notification dropped", destination)
        metrics.record_outbound("webhook", "callback", method, "blocked")
        return None
    started = time.perf_counter()
    try:
        with _pinned_to(destination, vetting):          # SEC-15.16: the connect goes to the addresses vetted above
            resp = getattr(httpx, method)(destination, **mtls.webhook_kwargs(destination), **kwargs)   # PR-SEC-2: the client certificate only to an https destination inside the deployment
    except httpx.TimeoutException:
        metrics.record_outbound("webhook", "callback", method, "timeout", time.perf_counter() - started)
        return None
    except httpx.HTTPError:
        metrics.record_outbound("webhook", "callback", method, "error", time.perf_counter() - started)
        return None
    status = getattr(resp, "status_code", None)             # a stub in a test may answer with something that is not a response
    metrics.record_outbound("webhook", "callback", method, "error" if status is None else metrics.outcome_of(status),
                            time.perf_counter() - started)
    return resp


def post_webhook(destination: str | None, json: dict, timeout: float = 5.0) -> httpx.Response | None:
    """Best-effort POST to a caller-registered notification destination.
    No-ops — same as every call site's own pre-existing `except
    httpx.HTTPError: pass` already did for an unreachable destination —
    when the destination is missing or disallowed.
    """
    return _send("post", destination, json=json, timeout=timeout)


def get_webhook(destination: str | None, timeout: float = 5.0) -> httpx.Response | None:
    """Best-effort GET against a caller-registered callback destination
    (e.g. a producer health check). Same no-op semantics as post_webhook.
    """
    return _send("get", destination, timeout=timeout)


def delete_webhook(destination: str | None, timeout: float = 5.0) -> httpx.Response | None:
    """Best-effort DELETE against a caller-registered callback destination
    (e.g. telling a producer to stop a job). Same no-op semantics as
    post_webhook.
    """
    return _send("delete", destination, timeout=timeout)


def normalise_base_url(value: str | None) -> str | None:
    """A base URL a workload or an operator registers for calls the platform makes later (a rApp instance's `operatorApiBase`, PR-GUI-8): the origin
    plus an optional path prefix, `http` or `https`, no credentials, query or fragment, at most 300 characters, passing `is_safe_webhook_destination`.
    Returns it without a trailing slash, or None when it is not acceptable. The same guard runs again before every call (`forward_to_destination`)."""
    if not value or len(value) > 300 or value != value.strip():
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or any(c in value for c in "?#\\%"):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or port == 0 or ".." in parts.path or "//" in parts.path:
        return None
    if not is_safe_webhook_destination(value):
        return None
    return value.rstrip("/")


async def forward_to_destination(method: str, destination: str | None, *, headers: dict, params, content: bytes | None,
                                 timeout: float) -> httpx.Response | None:
    """One forwarded call (any method, with headers, query and body) to a caller-registered base URL, for the gateway's operator-API prefix (PR-GUI-8).

    Not a notification, so not a row of docs/NOTIFICATIONS.md's inventory, but the same guard: a destination that fails `is_safe_webhook_destination` is
    not called (None: the caller answers 502 without saying why) and an https destination inside the deployment gets the client certificate
    (`mtls.webhook_kwargs`). A timeout raises `httpx.TimeoutException` and another transport failure `httpx.HTTPError`, which the caller maps to a fixed
    504 / 502 body: neither the exception text nor the destination is ever put in a response.
    """
    # the lookup blocks, so it runs in a thread: this coroutine serves the gateway's event loop
    vetting = await asyncio.to_thread(_vet_resolution, destination) if is_safe_webhook_destination(destination) else None
    if vetting is None or vetting.blocked:
        metrics.record_outbound("webhook", "operator-api", method.lower(), "blocked")
        return None
    started = time.perf_counter()
    try:
        with _pinned_to(destination, vetting):          # SEC-15.16
            async with httpx.AsyncClient(timeout=timeout, **mtls.webhook_kwargs(destination)) as client:
                resp = await client.request(method, str(destination), headers=headers, params=params, content=content)
    except httpx.TimeoutException:
        metrics.record_outbound("webhook", "operator-api", method.lower(), "timeout", time.perf_counter() - started)
        raise
    except httpx.HTTPError:
        metrics.record_outbound("webhook", "operator-api", method.lower(), "error", time.perf_counter() - started)
        raise
    metrics.record_outbound("webhook", "operator-api", method.lower(), metrics.outcome_of(resp.status_code), time.perf_counter() - started)
    return resp
