"""The console's cross-object typeahead (GUI-9.2): `GET /api/search?q=<text>&limit=5`, behind the ⌘K box.

One question fans out in parallel to the modules that hold each kind of object, each bounded by `PER_SOURCE_TIMEOUT_SECONDS`, and the answer is
grouped by kind, each item carrying the GUI route that opens it:

  element   RAN NF OAM `GET /managed-entities?search=<q>&limit=<n>`       -> `/elements/<ref>`
  rapp      the BFF's own rApp directory (app/rapps.py), name/version/id  -> `/rapps/<instanceId>`
  alarm     RAN NF OAM `GET /alarms?managed_element_ref=<q>`, only when q could be an element ref (one token, no spaces) -> `/alarms?me=<ref>`
  model     MLMR `GET /models?limit=100`, filtered here by id / type / inference name (bounded: the first 100 models only)  -> `/aiml?model=<id>#models`
  decision  RAN NF OAM `GET /decision-records/{q}`, only when q is a UUID  -> `/decisions/<id>`

Installed by app/main.py. Reads only. A kind whose source failed or timed out is named in `partial` (the kind, not the module) and the others still
come back; a kind with no match is left out of `groups`. Every upstream read is checked against the permission table for the caller's role (all are
viewer reads today). Answers are cached `CACHE_SECONDS` per (q, limit, role) and shared by every user of the role, as the summary counts are: the sources are module-wide
reads every role may make. The element results are filtered here too (the ref or name must contain q), so a RAN NF OAM build without the `search`
parameter answers nothing wrong, just less.
"""

import asyncio
import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from . import scoping
from .rapps import UpstreamFailure
from .rbac import decide
from .smo_client import SmoAuthError

log = logging.getLogger("smo-gui-bff")

MIN_QUERY = 2
CACHE_SECONDS = 10.0
MAX_CACHED = 256
PER_SOURCE_TIMEOUT_SECONDS = 2.0
MODEL_SCAN = 100
# One token that could be a managed element ref (`gnb-001`, `ManagedElement=7`): what makes asking the alarm list by element worth a call.
ELEMENT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:=,/-]{1,127}$")


class _SourceFailed(Exception):
    """A source could not be asked or did not answer 200: its kind goes into `partial`."""


def _item(id_: str, label: str, hint: str | None, to: str) -> dict:
    return {"id": id_, "label": label, "hint": hint, "to": to}


def install(app: FastAPI, *, current_session: Callable, problem: Callable[..., JSONResponse]) -> None:
    """Add `GET /api/search` to `app`. Needs app/rapps.py installed (it reads `app.state.rapp_search`)."""
    cache: dict[tuple[str, int, str, str | None], tuple[float, dict]] = {}
    # GUI-5: the asking user's stored scope claim, set by the route for the sources it starts (a context variable reaches the tasks `gather` makes)
    asking_claim: ContextVar[str | None] = ContextVar("asking_claim", default=None)

    async def get_json(path: str, params: dict | None = None, *, missing_ok: bool = False) -> Any:
        """The JSON body of a 200 from `path` through the gateway; None for a 404 when `missing_ok`; `_SourceFailed` otherwise."""
        try:
            resp = await app.state.gateway.request("GET", path, params=params, timeout=PER_SOURCE_TIMEOUT_SECONDS,
                                                   headers=scoping.shared_headers(asking_claim.get()))
        except (SmoAuthError, httpx.HTTPError) as exc:
            raise _SourceFailed(path) from exc
        if missing_ok and resp.status_code in (404, 422):
            return None
        if resp.status_code != 200:
            raise _SourceFailed(f"{path}: {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise _SourceFailed(path) from exc

    def items_of(body: Any) -> list[dict]:
        rows = body.get("items") if isinstance(body, dict) else body
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def elements(q: str, limit: int) -> list[dict]:
        body = await get_json("/ran-nf-oam/managed-entities", {"search": q, "limit": limit})
        needle = q.lower()
        out = []
        for e in items_of(body):
            ref = str(e.get("managedElementRef") or "")
            if not ref or not (needle in ref.lower() or needle in str(e.get("name") or "").lower()):
                continue
            hint = " · ".join(str(v) for v in (e.get("vendorName"), e.get("region"), e.get("siteCluster")) if v) or None
            out.append(_item(ref, ref, hint, f"/elements/{quote(ref, safe='')}"))
        return out[:limit]

    async def rapps(q: str, limit: int) -> list[dict]:
        rows = await app.state.rapp_search(q)
        return [_item(str(r["instanceId"]), str(r.get("name") or r["instanceId"]),
                      " · ".join(str(v) for v in (r.get("version"), r.get("state")) if v) or None, f"/rapps/{r['instanceId']}")
                for r in rows[:limit]]

    async def alarms(q: str, limit: int) -> list[dict]:
        body = await get_json("/ran-nf-oam/alarms", {"managed_element_ref": q, "limit": limit})
        return [_item(str(a.get("alarmId")), f"{a.get('managedElementRef')}: {a.get('probableCause') or a.get('specificProblem') or 'alarm'}",
                      " · ".join(str(v) for v in (a.get("severity"), a.get("ackState"), a.get("raisedAt")) if v) or None,
                      f"/alarms?me={quote(str(a.get('managedElementRef') or q), safe='')}")
                for a in items_of(body)[:limit]]

    async def models(q: str, limit: int) -> list[dict]:
        body = await get_json("/mlmr/models", {"limit": MODEL_SCAN})
        needle = q.lower()
        out = []
        for m in items_of(body):
            model_id = str(m.get("modelId") or "")
            name = m.get("aIMLInferenceName") or m.get("modelType") or model_id
            if model_id and any(needle in str(v or "").lower() for v in (model_id, m.get("modelType"), m.get("aIMLInferenceName"))):
                out.append(_item(model_id, str(name), " · ".join(str(v) for v in (m.get("modelType"), m.get("version")) if v) or None,
                                 f"/aiml?model={quote(model_id, safe='')}#models"))
        return out[:limit]

    async def decision(q: str, limit: int) -> list[dict]:
        body = await get_json(f"/ran-nf-oam/decision-records/{q}", missing_ok=True)
        if not isinstance(body, dict) or not body.get("decisionId"):
            return []
        return [_item(str(body["decisionId"]), f"Decision {str(body['decisionId'])[:8]}",
                      " · ".join(str(v) for v in (body.get("disposition"), body.get("invokerId"), body.get("occurredAt")) if v) or None,
                      f"/decisions/{body['decisionId']}")]

    async def guarded(kind: str, source: Callable[[str, int], Awaitable[list[dict]]], q: str, limit: int) -> tuple[str, list[dict] | None]:
        """(kind, items) or (kind, None) when the source failed or took longer than PER_SOURCE_TIMEOUT_SECONDS. Never raises."""
        try:
            return kind, await asyncio.wait_for(source(q, limit), PER_SOURCE_TIMEOUT_SECONDS)
        except (_SourceFailed, UpstreamFailure, TimeoutError) as exc:
            log.warning("search %s failed: %r", kind, exc)
            return kind, None
        except Exception:        # deliberate catch-all: a malformed answer of one source must not fail the whole typeahead
            log.exception("search %s failed", kind)
            return kind, None

    @app.get("/api/search")
    async def search(q: str = Query(..., max_length=100, description="The text typed: at least 2 characters."),
                     limit: int = Query(5, ge=1, le=20, description="At most this many items per kind."),
                     session=Depends(current_session)):
        """Typeahead over elements, rApps, alarms (by element ref), models and decisions (by id): `{q, groups: [{type, items: [{id, label, hint,
        to}]}], partial: [type]}`. 400 `QUERY_TOO_SHORT` under two characters. Cached 10 s per question."""
        q = q.strip()
        if len(q) < MIN_QUERY:
            return problem(400, "QUERY_TOO_SHORT", f"type at least {MIN_QUERY} characters")
        # per role too, so a read a future rule narrows never leaks through the cache; per scope claim (GUI-5), so one user's elements never reach another's
        key = (q.lower(), limit, str(session.user.role), session.user.scope)
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        is_uuid = True
        try:
            uuid.UUID(q)
        except ValueError:
            is_uuid = False
        # (kind, source, the upstream path its permission is checked on); the order is the order of the groups in the answer
        plan: list[tuple[str, Callable[[str, int], Awaitable[list[dict]]], str]] = [
            ("element", elements, "/ran-nf-oam/managed-entities"), ("rapp", rapps, "/rapp-mgmt/instances")]
        if ELEMENT_REF_RE.match(q):
            plan.append(("alarm", alarms, "/ran-nf-oam/alarms"))
        plan.append(("model", models, "/mlmr/models"))
        if is_uuid:
            plan.append(("decision", decision, "/ran-nf-oam/decision-records/x"))
        plan = [p for p in plan if decide("GET", p[2], {}, session.user.role).allowed]
        asking_claim.set(session.user.scope)
        results = await asyncio.gather(*(guarded(kind, source, q, limit) for kind, source, _ in plan))
        body = {"q": q, "groups": [{"type": kind, "items": items} for kind, items in results if items],
                "partial": [kind for kind, items in results if items is None]}
        if len(cache) >= MAX_CACHED:
            cache.clear()
        cache[key] = (time.monotonic(), body)
        return body
