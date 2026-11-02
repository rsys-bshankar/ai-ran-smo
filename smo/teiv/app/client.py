"""TE&IV's client of smo-teiv's exposure API (plan task S3.19): the only code of the SMO that calls it (D19; `scripts/check_interfaces.py` fails any file
outside `teiv/` that names `/topology-inventory/`).

Configuration (read once, at start):

  TEIV_EXPOSURE_URL          smo-teiv's exposure process, `http://teiv-exposure:8080` by default (the compose profile `teiv`, the chart's Service)
  TEIV_CONNECT_TIMEOUT_SECONDS   time to connect (default 2)
  TEIV_READ_TIMEOUT_SECONDS      time for one answer (default 5): a ProvMnS read waits no longer for smo-teiv than this
  TEIV_MAX_CONNECTIONS       connections kept to smo-teiv (default 32; the fan-out reads of reader.py use them in parallel)

What can go wrong, and what the caller sees (an `MnsError`, so the ProvMnS router answers it in the TS 28.532 shape):

  smo-teiv cannot be reached, times out, or answers 5xx    503 TEIV_UNAVAILABLE with Retry-After: TE&IV is up, its store is not; the caller retries
  smo-teiv answers 404 for an entity                       `get_entity` returns None (the reader decides: MOI_NOT_FOUND for the base of a read)
  smo-teiv answers another 4xx                             502 TEIV_REFUSED with smo-teiv's own detail: TE&IV asked something smo-teiv does not take,
                                                           which is TE&IV's defect, never the caller's; logged with the request
Nothing is retried here: a read is cheap to repeat for the caller, and retrying inside a request would only stretch its time past the caller's own
timeout. The WG10 door (`wg10.py`) uses `forward`, which passes smo-teiv's answer through as it is.
"""

import logging
import os
from collections.abc import Iterator
from urllib.parse import quote

import httpx

from smo_shared.mns.errors import MnsError

log = logging.getLogger("smo.teiv.client")
API = "/topology-inventory/v1"
PAGE = 500                       # smo-teiv's largest page
RETRY_AFTER_SECONDS = "5"


def _float(name: str, default: float) -> float:
    """A positive number of seconds from the environment variable `name`, or `default` (also for a value that is not one, which is logged)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s is not a number of seconds; using %s", name, default)
        return default
    return value if value > 0 else default


def unavailable(reason: str) -> MnsError:
    """The 503 a ProvMnS read answers when smo-teiv cannot answer it."""
    return MnsError(503, "TEIV_UNAVAILABLE", f"TE&IV's store (smo-teiv) did not answer: {reason}", headers={"Retry-After": RETRY_AFTER_SECONDS})


class SmoTeivClient:
    """A client of smo-teiv's API with bounded timeouts (module description): a synchronous one for single calls (the probes, the WG10 door), and an
    asynchronous one per ProvMnS read (`read_client`), on which the reader sends that read's requests at once."""

    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        """`base_url` defaults to TEIV_EXPOSURE_URL; `transport` replaces the network (a test's fake smo-teiv)."""
        base = (base_url or os.environ.get("TEIV_EXPOSURE_URL") or "http://teiv-exposure:8080").rstrip("/")
        timeout = httpx.Timeout(_float("TEIV_READ_TIMEOUT_SECONDS", 5.0), connect=_float("TEIV_CONNECT_TIMEOUT_SECONDS", 2.0))
        connections = int(_float("TEIV_MAX_CONNECTIONS", 32))
        self.base_url, self.timeout, self.connections = base + API, timeout, connections
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                 limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections))
        self.async_transport: httpx.AsyncBaseTransport | None = transport if isinstance(transport, httpx.AsyncBaseTransport) else None
        self.tls = httpx.create_ssl_context()       # made once: building it is most of what opening a read's client costs

    def use_transport(self, transport: httpx.MockTransport) -> None:
        """Sends every request, the reads' too, to `transport` (a test's fake smo-teiv)."""
        self.http.close()
        self.http = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=transport)
        self.async_transport = transport

    def close(self) -> None:
        """Closes the connections."""
        self.http.close()

    # ---- the reads' asynchronous side: one AsyncClient per ProvMnS read, its requests sent at once (reader.py), closed with the read

    def read_client(self) -> httpx.AsyncClient:
        """The client of one read: at most TEIV_MAX_CONNECTIONS requests at once, the same timeouts; closed by the read (`async with`)."""
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.async_transport, verify=self.tls,
                                 limits=httpx.Limits(max_connections=self.connections, max_keepalive_connections=self.connections))

    async def _asend(self, http: httpx.AsyncClient, path: str, params: dict | None = None) -> httpx.Response:
        """One GET on a read's client; MnsError 503 for a transport failure or a 5xx, as `_send`."""
        try:
            response = await http.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        except httpx.TimeoutException:
            raise unavailable("timed out") from None
        except httpx.TransportError as exc:
            raise unavailable(type(exc).__name__) from None
        if response.status_code >= 500:
            log.warning("smo-teiv answered %s for GET %s", response.status_code, path)
            raise unavailable(f"HTTP {response.status_code}")
        return response

    async def aget_entity(self, http: httpx.AsyncClient, domain: str, entity_type: str, identifier: str) -> dict | None:
        """`get_entity` on a read's client."""
        path = f"/domains/{domain}/entity-types/{entity_type}/entities/{quote(identifier, safe='')}"
        response = await self._asend(http, path)
        if response.status_code == 404:
            return None
        return self._json(response, path)

    async def apages(self, http: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
        """`pages` on a read's client: every page of a list answer."""
        out, offset = [], 0
        while True:
            body = self._json(await self._asend(http, path, {**params, "offset": offset, "limit": PAGE}), path)
            out.append(body)
            seen = sum(len(v) for block in body.get("items", []) for v in block.values())
            offset += PAGE
            if not seen or offset >= body.get("totalCount", 0):
                return out

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request; MnsError 503 for a transport failure or a 5xx."""
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise unavailable("timed out") from None
        except httpx.TransportError as exc:
            raise unavailable(type(exc).__name__) from None
        if response.status_code >= 500:
            log.warning("smo-teiv answered %s for %s %s", response.status_code, method, path)
            raise unavailable(f"HTTP {response.status_code}")
        return response

    def _json(self, response: httpx.Response, path: str) -> dict:
        """The JSON of a 2xx answer; MnsError 502 TEIV_REFUSED, with smo-teiv's detail, for a 4xx."""
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except ValueError:
                pass
            log.error("smo-teiv refused %s: %s %s", path, response.status_code, detail)
            raise MnsError(502, "TEIV_REFUSED", f"TE&IV's store refused a request TE&IV made ({response.status_code}: {detail})")
        return response.json()

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET `path` (below /topology-inventory/v1) with `params`, as JSON; MnsError for anything but a 2xx."""
        return self._json(self._send("GET", path, params={k: v for k, v in (params or {}).items() if v is not None}), path)

    def status(self, path: str) -> int:
        """The HTTP status of GET `path` (a 5xx or a transport failure raises, as in `get`)."""
        return self._send("GET", path).status_code

    def get_entity(self, domain: str, entity_type: str, identifier: str, target_filter: str | None = None) -> dict | None:
        """The answer for one entity (None for a 404)."""
        path = f"/domains/{domain}/entity-types/{entity_type}/entities/{quote(identifier, safe='')}"
        response = self._send("GET", path, params={"targetFilter": target_filter} if target_filter else None)
        if response.status_code == 404:
            return None
        return self._json(response, path)

    def pages(self, path: str, params: dict) -> Iterator[dict]:
        """Every page of a list answer (`items`, `totalCount`), PAGE at a time."""
        offset = 0
        while True:
            body = self.get(path, {**params, "offset": offset, "limit": PAGE})
            yield body
            seen = sum(len(v) for block in body.get("items", []) for v in block.values())
            offset += PAGE
            if not seen or offset >= body.get("totalCount", 0):
                return

    def upload_schema(self, filename: str, content: bytes) -> dict:
        """createUserDefinedSchema with a YANG file (smo-teiv takes only the media type application/yang)."""
        response = self._send("POST", "/user-defined-schemas", files={"file": (filename, content, "application/yang")})
        return self._json(response, "/user-defined-schemas") if response.status_code >= 400 or response.content else {}

    def forward(self, method: str, path: str, *, params=None, content: bytes | None = None, headers: dict | None = None) -> httpx.Response:
        """One request passed through as it is (the WG10 door): any status comes back; MnsError 503 only when smo-teiv does not answer."""
        return self._send(method, path, params=params, content=content, headers=headers)
