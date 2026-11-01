"""R1 Termination's routing of ProvMnS by MnS registry lookup (plan task S1.8): `/ProvMnS/{version}/{LDN}` goes to the producer of the LDN's class.

What it does: for a request whose first segment is `ProvMnS`, `resolve` takes the class of the URI's last RDN and asks the MnS registry (TS 28.622,
SME's; `smo_shared/mns/mns_registry.py`) which producer serves it: a PROV_MNS MnsInfo of that version whose MgmtDataInfo lists the class and whose
mnsScope holds the DN (`producer_for`: the closest scope wins). The request is forwarded to `<mnsAddress>/<LDN>`, the rest of the gateway's checks
having run as for any route (`main._proxy`). There is no static entry for a ProvMnS path in `main.ROUTES`, and none may be added (a test fails):
a module that mounts the ProvMnS router is reached by the classes it registers, not by a prefix.

The registry's own classes (MnsRegistry, MnsInfo, MgmtDataInfo) always go to SME (`SME_URL`), the registry's producer: that is how a module
registers before anything is known about it, and how this gateway reads the registry.

The registry read: a flat getMOIAttributes of the whole registry, every page, asked of SME directly (as the token introspection is, never through
this gateway) with the `internal` role. It is kept in memory for `R1_MNS_REGISTRY_CACHE_SECONDS` (read on every call; default 10, 0 reads it for
every request) and read again at once when a class is not found and the copy is more than a second old, so a module that just registered is
reached on its first request; and a change of the registry that goes through this gateway (a 2xx to PUT, PATCH or DELETE of one of the
registry's classes, as every module's registration does) drops the copy at once (`after_response`). When SME cannot be read, the last copy is used; with none, the answer is 503 `MNS_REGISTRY_UNAVAILABLE`. Requests
that find the copy out of date at the same moment may each read it (no lock: the gateway's event loop is the only one, and a read is one call).
Replicas keep a copy each.

Answers of its own (the gateway's RFC 7807 shape): 404 `NO_ROUTE` when the path is not a ProvMnS URI (no version, or an LDN that is not one) or
no producer serves the class there, 503 `MNS_REGISTRY_UNAVAILABLE`.
"""

import logging
import os
import time
from urllib.parse import urljoin

import httpx

from smo_shared import mtls, roles
from smo_shared.mns.dn import dn_from_path, parse_dn
from smo_shared.mns.errors import MnsError
from smo_shared.mns.mns_registry import READ_HEADERS, READ_PARAMS, REGISTRY_CLASSES, REGISTRY_PATH, MnsEntry, entries, next_page, producer_for
from smo_shared.timeouts import upstream_timeout

log = logging.getLogger(__name__)

PREFIX = "/ProvMnS"
CACHE_ENV = "R1_MNS_REGISTRY_CACHE_SECONDS"
DEFAULT_CACHE_SECONDS = 10.0
MISS_REFRESH_SECONDS = 1.0           # a class not found is looked up again only when the copy is older than this


class Unroutable(Exception):
    """Why a ProvMnS request is not forwarded: the gateway's status, title and detail (and `Retry-After` seconds, for a 503)."""

    def __init__(self, status: int, title: str, detail: str, retry_after: int | None = None):
        """Keeps what the gateway answers."""
        super().__init__(detail)
        self.status, self.title, self.detail, self.retry_after = status, title, detail, retry_after


def cache_seconds() -> float:
    """How long a registry read is used, from R1_MNS_REGISTRY_CACHE_SECONDS (default 10; a negative value is 0; not a number: the default, logged)."""
    raw = os.environ.get(CACHE_ENV, "")
    if not raw.strip():
        return DEFAULT_CACHE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("%s=%r is not a number: the MnS registry is kept %g s", CACHE_ENV, raw, DEFAULT_CACHE_SECONDS)
        return DEFAULT_CACHE_SECONDS


async def read_registry(sme_url: str) -> list[MnsEntry]:
    """Every producer in the registry at SME (`sme_url`), read page by page; httpx errors and a page that is not a 200 raise."""
    found: list[dict] = []
    url: str | None = sme_url + REGISTRY_PATH
    params: dict | None = READ_PARAMS
    async with httpx.AsyncClient(timeout=upstream_timeout(), **mtls.client_kwargs(sme_url)) as client:
        while url is not None:
            response = await client.get(url, params=params, headers={**READ_HEADERS, roles.ROLE_HEADER: roles.ROLE_INTERNAL})
            response.raise_for_status()
            found.extend(response.json())
            following = next_page(response.headers.get("link"))
            url, params = (urljoin(sme_url + "/", following.lstrip("/")) if following else None), None
    return entries(found)


class RegistryCache:
    """The last registry read and when it was made (see the module description)."""

    def __init__(self, reader=read_registry, clock=time.monotonic):
        """`reader(sme_url)` reads the registry; `clock` tells the time (both replaced by tests)."""
        self.reader, self.clock = reader, clock
        self.entries: list[MnsEntry] | None = None
        self.read_at = 0.0

    def clear(self) -> None:
        """Forgets the copy (the next request reads the registry)."""
        self.entries, self.read_at = None, 0.0

    def _age(self) -> float:
        return self.clock() - self.read_at

    async def get(self, sme_url: str, older_than: float) -> list[MnsEntry]:
        """The producers, read again when the copy is missing or older than `older_than` seconds; the old copy when the read fails (Unroutable 503
        when there is none)."""
        if self.entries is not None and self._age() < older_than:
            return self.entries
        try:
            self.entries, self.read_at = await self.reader(sme_url), self.clock()
        except (httpx.HTTPError, ValueError) as exc:
            if self.entries is None:
                raise Unroutable(503, "MNS_REGISTRY_UNAVAILABLE", "the MnS registry could not be read now; retry shortly", 5) from exc
            log.warning("MnS registry not read (%s): routing on the copy read %.0f s ago", exc.__class__.__name__, self._age())
        return self.entries


CACHE = RegistryCache()


def parse(rest_of_path: str) -> tuple[str, str, str, str]:
    """(version, LDN path, DN, class) of the path after `/ProvMnS/`; Unroutable 404 when it is not `<version>/<LDN>`."""
    version, _, ldn = rest_of_path.partition("/")
    if not version or not ldn:
        raise Unroutable(404, "NO_ROUTE", "a ProvMnS URI is /ProvMnS/{version}/{LDN}")
    try:
        dn = dn_from_path(ldn)
    except MnsError:
        raise Unroutable(404, "NO_ROUTE", "the path after the version does not name a managed object instance") from None
    return version, ldn, dn, parse_dn(dn)[-1].class_name


async def resolve(rest_of_path: str, sme_url: str, cache: RegistryCache = CACHE) -> str:
    """The URL to forward `/ProvMnS/<rest_of_path>` to: SME's for the registry's classes, else `<mnsAddress>/<LDN>` of the producer the registry
    names (read again once when none is found in an older copy). Unroutable when there is none or the registry cannot be read."""
    version, ldn, dn, class_name = parse(rest_of_path)
    if class_name in REGISTRY_CLASSES:
        return f"{sme_url}{PREFIX}/{version}/{ldn}"
    found = producer_for(await cache.get(sme_url, cache_seconds()), class_name, dn, version)
    if found is None:
        found = producer_for(await cache.get(sme_url, MISS_REFRESH_SECONDS), class_name, dn, version)
    if found is None:
        raise Unroutable(404, "NO_ROUTE", f"no MnS producer of {class_name} (ProvMnS {version}) is registered for {dn}")
    return f"{mtls.http_url(str(found.address).rstrip('/'))}/{ldn}"


def after_response(method: str, rest_of_path: str, status: int, cache: RegistryCache = CACHE) -> None:
    """Drops the registry copy when `method /ProvMnS/<rest_of_path>` changed the registry (a 2xx to a write of one of its classes), so the next
    request routes on what was just registered. Other replicas see it after their copy ages."""
    if method not in ("PUT", "PATCH", "DELETE") or not 200 <= status < 300:
        return
    try:
        class_name = parse(rest_of_path)[3]
    except Unroutable:
        return
    if class_name in REGISTRY_CLASSES:
        cache.clear()
