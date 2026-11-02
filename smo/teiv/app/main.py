"""TE&IV: topology and inventory through the standards, on O-RAN-SC smo-teiv (plan tasks S3.19, S3.20, S3.21; ADR 0013; D19).

TE&IV is the SMO's only door to smo-teiv, which stores the topology (`teiv/README.md`). It serves:

  /ProvMnS/{version}/{LDN}        TS 28.532 ProvMnS getMOIAttributes of the TS 28.541 / TS 28.622 classes it holds (model.CLASSES), through the shared
                                  router of `smo_shared.mns` (provmns.py, reader.py): scope, filter, attribute selection, paging, the S1.5 checks. The
                                  writes answer their checks (a stale If-Match is 412) and then 501 until S3.4.
  /topology-inventory/v1/...      the O-RAN WG10 TE&IV R1 API's 16 operations, passed to smo-teiv (wg10.py)
  /health /live /ready /version   the probes; ready needs smo-teiv to answer and TE&IV's decorator schema to be loaded in it

TE&IV's decorator schema (S3.21) is loaded into smo-teiv by the readiness probe when it is not there yet (`smo_teiv_check`). At start-up, in the
background and repeated until it succeeds, TE&IV registers in SME's MnS registry as the ProvMnS producer of its classes (`smo_shared.mns.mns_registry`, S1.12; `SMO_MNS_REGISTRATION=off` skips it), so
R1 Termination routes `/ProvMnS/...` of those classes to it by class name (S1.8). Its MnS scope is TEIV_MNS_SCOPE: the DNs under which its objects lie
(a comma is part of a DN, so the DNs are separated by `;`), `SubNetwork=SMO-RAN` by default; a producer with a scope is chosen before one without
(Echo serves SubNetwork and ManagedElement with none), so a DN under TE&IV's SubNetwork is TE&IV's.
"""

import json
import logging
import os

from urllib.parse import parse_qs

from fastapi import FastAPI

from smo_shared.correlation import apply_correlation_id
from smo_shared.health import database_check, install_health
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.mns import provmns_router
from smo_shared.mns.checks import RouterChecks
from smo_shared.mns.mns_registry import install_self_registration
from smo_shared.openapi_security import apply_r1_gateway_security

from . import attrs, model
from .client import SmoTeivClient
from .provmns import TeivProvMnS, etag
from .reader import TeivReader
from .wg10 import wg10_router

log = logging.getLogger("smo.teiv")
CLASSES = tuple(sorted(model.CLASSES))
SYSTEM_DN = "ManagementNode=teiv"


def mns_scope() -> list[str]:
    """TEIV_MNS_SCOPE: the DNs TE&IV's objects lie under, separated by `;` (default SubNetwork=SMO-RAN)."""
    raw = os.environ.get("TEIV_MNS_SCOPE", "SubNetwork=SMO-RAN")
    return [dn.strip() for dn in raw.split(";") if dn.strip()]


client = SmoTeivClient()
reader = TeivReader(client)
CHECKS = RouterChecks(module="teiv")
mns = TeivProvMnS(reader=reader, source="/smo/teiv", system_dn=SYSTEM_DN, classes=CLASSES, checks=CHECKS)


def smo_teiv_check() -> None:
    """Readiness: smo-teiv answers and TE&IV's decorator schema is loaded in it. The schema is loaded here, by the probe, when it is not there yet
    (`attrs.ensure_schema`: one read when it is present, the upload once when not), so no thread is needed: the orchestrator's probes repeat it
    until smo-teiv is up, and a replica takes no traffic before. Every replica does the same; loading it twice changes nothing."""
    if client.status("/domains") != 200:
        raise RuntimeError("smo-teiv's exposure API does not answer /domains")
    outcome = attrs.ensure_schema(client)
    if outcome == "created":
        log.info("TE&IV decorator schema %s loaded into smo-teiv", attrs.MODULE)


app = FastAPI(title="TE&IV")
install_logging(app)
install_metrics(app)
apply_r1_gateway_security(app)
apply_correlation_id(app)
install_health(app, checks=[database_check, smo_teiv_check])


class EtagMiddleware:
    """A getMOIAttributes of one instance (BASE_ONLY, the default scope, no attribute selection) answers its version as `ETag` (S3.21), for a
    read-modify-write. Plain ASGI: every other request passes straight through."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET" or not scope["path"].startswith("/ProvMnS/") or not _single(scope):
            await self.inner(scope, receive, send)
            return
        start: dict = {}
        chunks: list[bytes] = []

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"etag"]
            tag = _etag_of(body) if start.get("status") == 200 else None
            if tag:
                headers.append((b"etag", tag.encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})
        await self.inner(scope, receive, capture)


def _single(scope) -> bool:
    """Whether the request asks for one instance whole: BASE_ONLY (or no scope) and neither `attributes` nor `fields`."""
    query = parse_qs(scope.get("query_string", b"").decode())
    if "attributes" in query or "fields" in query:
        return False
    return not query.get("scope") or '"BASE_ONLY"' in query["scope"][0]


def _etag_of(body: bytes) -> str | None:
    """The ETag of the one resource an answer holds (None when it holds another shape)."""
    try:
        tree = json.loads(body)
    except ValueError:
        return None
    resource = tree[0] if isinstance(tree, list) and len(tree) == 1 else tree if isinstance(tree, dict) else None
    if isinstance(resource, dict) and "objectInstance" in resource and "attributes" in resource:
        return etag(resource)
    return None


app.add_middleware(EtagMiddleware)
app.include_router(provmns_router(mns))
app.include_router(wg10_router(client, CHECKS))
REGISTRATION = install_self_registration(app, "teiv", CLASSES, scope=mns_scope())

