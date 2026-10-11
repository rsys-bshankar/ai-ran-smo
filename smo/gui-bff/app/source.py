"""Where a sign-in attempt comes from, and the keys of its failure counters (SEC-15.11).

The lockout of `main.py` counts failed sign-ins in `LoginFailure` rows (`db.py`). A counter keyed by the user name alone lets anyone lock any user out from anywhere, and
does not slow one source that tries many names. This file gives the two keys that replace it: the pair (source, user name), which is what locks an account for the source that
failed, and the source alone, which throttles a source whatever names it tries. It owns the reading of the address (`client_source`); it does not count or lock anything.

The address is the TCP peer, or, when this backend sits behind `GUI_TRUSTED_PROXY_HOPS` reverse proxies (the console's nginx is one: it appends the peer it saw to
`X-Forwarded-For`), the entry that many places from the right of that header, which the nearest trusted proxy wrote. Entries further left are the client's own words and are never
used. A header with too few entries, or an entry that is not an address, falls back to the TCP peer. An IPv6 address is reduced to its /64, because one subscriber holds a whole /64
and could otherwise change address at will.
"""

import ipaddress

# Separates the parts of a counter key. A control character, so it cannot be typed into a user name by accident and the key of one (source, name) pair is never the key of another.
_SEP = "\x1f"
UNKNOWN_SOURCE = "unknown"


def client_source(peer: str | None, forwarded_for: str | None, hops: int) -> str:
    """Returns the normalised address a sign-in comes from: an IPv4 address, or the /64 of an IPv6 one (`2001:db8::/64`), or `unknown` when nothing usable is known.

    `peer` is the TCP peer (`request.client.host`), `forwarded_for` the raw `X-Forwarded-For` value, `hops` the number of trusted reverse proxies in front of this backend (0: use
    the peer and ignore the header, which is the only safe reading when clients reach the backend directly). With `hops` N the Nth entry from the right is used.
    """
    candidate = peer
    if hops > 0 and forwarded_for:
        entries = [e.strip() for e in forwarded_for.split(",") if e.strip()]
        if len(entries) >= hops:
            candidate = entries[-hops]
    return _normalise(candidate) or _normalise(peer) or UNKNOWN_SOURCE


def _normalise(text: str | None) -> str | None:
    """The address in `text` as a counter key (IPv4 as is, IPv6 as its /64, an IPv4-mapped IPv6 address as the IPv4), or None when it is not an IP address."""
    try:
        address = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


# The start of every pair key. With `pair_key_suffix` it selects the pair keys of one user name from every source.
PAIR_PREFIX = f"p{_SEP}"


def pair_key(source: str, username: str) -> str:
    """The key of the counter for one user name tried from one source: the lock that applies to that source only."""
    return f"{PAIR_PREFIX}{source}{_SEP}{username}"


def source_key(source: str) -> str:
    """The key of the counter for everything one source has failed, whatever names it tried: the per-source throttle."""
    return f"s{_SEP}{source}"


def pair_key_suffix(username: str) -> str:
    """The end shared by the pair keys of `username` from every source, for removing them all when the account is deleted."""
    return f"{_SEP}{username}"
