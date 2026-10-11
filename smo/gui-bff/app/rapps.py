"""The rApp directory, the declared page of one rApp, the proxy for its declared routes and the user's pins (PR-GUI-8, GUI-8.3 and GUI-8.5).

  GET    /api/rapps                              the directory: every rApp instance, with its package's name, version and vendor, searchable and filterable
  GET    /api/rapps/{instance}                   one instance and the `operatorUi` declaration of its package (read from Onboarding)
  *      /api/rapps/{instance}/operator/{route}  a call to the rApp's operator API, only for the routes the declaration lists (operator_ui.py), through the
                                                 gateway's `/rapps/{instanceId}/operator/...` prefix; every change is audited before it is sent and after
  GET    /api/me/pins                            the instances the signed-in user pinned to the sidebar (at most 5)
  PUT    /api/me/pins/{instance}, DELETE ...     pin, unpin

The browser never learns the rApp's address (only whether one is registered) and never calls it: the BFF asks the gateway with its own token, and the
gateway resolves the instance's registered `operatorApiBase`. The permission of a call comes from the declaration (`operator_ui.decide`): a read needs viewer, a
change operator, an undeclared route is refused whatever the role, a `readOnly` rApp allows no change. Declarations and the package index are kept for a
short time in this process (they are immutable per package id, and an upgrade is a new package id), so a page does not cost three upstream calls per refresh.
The same directory, filtered by text, is what the typeahead (app/search.py) searches: `install` keeps it on `app.state.rapp_search`.
"""

import json
import logging
import time
import uuid
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import operator_ui, scoping
from .db import MAX_PINS
from .rbac import RANK, Role
from .smo_client import SmoAuthError

log = logging.getLogger("smo-gui-bff")

MAX_PAGES = 20                  # of 500, when reading all instances or packages: 10 000 of either is far past what a directory is for
PACKAGE_INDEX_SECONDS = 30.0
PACKAGE_INDEX_REFRESH_SECONDS = 2.0
DECLARATION_SECONDS = 60.0
MAX_CACHED_DECLARATIONS = 500
_NEVER_FORWARD_RESPONSE = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers", "transfer-encoding",
                           "upgrade", "content-length", "content-encoding", "set-cookie", "server", "date"}


class UpstreamFailure(Exception):
    """The SMO could not be asked (no token, not reachable): carries the answer for the browser. Never holds exception text."""

    def __init__(self, response: JSONResponse):
        super().__init__("upstream failure")
        self.response = response


def declaration_of(status_body: Any) -> tuple[str, dict | None]:
    """(`declared` | `none` | `unreadable`, the declaration) from an Onboarding `onboarding-status` body. A declaration is used only when it has the shape the
    renderer and `operator_ui` read; anything else is `unreadable` and gives the page no routes."""
    capabilities = status_body.get("aiCapabilities") if isinstance(status_body, dict) else None
    if not isinstance(capabilities, dict) or "operatorUi" not in capabilities:
        return "none", None
    found = capabilities["operatorUi"]
    if not isinstance(found, dict) or not isinstance(found.get("panels"), list) or not all(isinstance(p, dict) for p in found["panels"]):
        return "unreadable", None
    return "declared", found


def install(app: FastAPI, *, current_session: Callable, audit: Callable, problem: Callable[..., JSONResponse]) -> None:
    """Add the routes to `app`. `current_session` is the BFF's session dependency, `audit` its audit writer, `problem` its error body."""
    package_index: dict[str, Any] = {"at": 0.0, "by_id": {}}
    declarations: dict[str, tuple[float, str, dict | None]] = {}

    # ------------------------------------------------------------ what the SMO says

    async def smo_get(path: str, params: dict | None = None) -> httpx.Response:
        try:
            return await app.state.gateway.request("GET", path, params=params)
        except SmoAuthError as exc:
            log.warning("rapps: SMO token unavailable: %s", exc)
            raise UpstreamFailure(problem(502, "SMO_AUTH_FAILED", "the BFF could not obtain an SMO access token from SME")) from exc
        except httpx.HTTPError as exc:
            log.warning("rapps: R1 Termination unreachable: %r", exc)
            raise UpstreamFailure(problem(502, "R1_UNREACHABLE", "R1 Termination did not answer")) from exc

    def failed(resp: httpx.Response, what: str) -> UpstreamFailure:
        log.warning("rapps: %s answered %s", what, resp.status_code)
        return UpstreamFailure(problem(502, "SMO_ERROR", f"{what} answered {resp.status_code}"))

    async def read_all(path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        for page in range(MAX_PAGES):
            resp = await smo_get(path, {**(params or {}), "limit": 500, "offset": page * 500, "total": "false"})
            if resp.status_code != 200:
                raise failed(resp, path.split("/")[1])
            body = resp.json()
            batch = body.get("items", []) if isinstance(body, dict) else []
            items += [i for i in batch if isinstance(i, dict)]
            if not (isinstance(body, dict) and body.get("hasMore")):
                break
        return items

    async def packages(force: bool = False, wanted: set[str] | None = None) -> dict[str, dict]:
        """packageId -> what the directory shows (never the declaration itself: it can be 64 KiB). The index is kept for PACKAGE_INDEX_SECONDS, except that a
        package someone asks for (`wanted`) that it does not hold has just been onboarded: it is read again (at most every PACKAGE_INDEX_REFRESH_SECONDS), so a rApp
        onboarded at run time shows with its name at once."""
        age = time.monotonic() - package_index["at"]
        missing = bool(wanted) and not wanted <= package_index["by_id"].keys() and age >= PACKAGE_INDEX_REFRESH_SECONDS
        if not force and not missing and age < PACKAGE_INDEX_SECONDS:
            return package_index["by_id"]
        by_id = {}
        for p in await read_all("/onboarding/packages"):
            state, _ = declaration_of(p)
            by_id[str(p.get("packageId"))] = {"name": p.get("name"), "version": p.get("version"), "vendor": p.get("vendor"),
                                              "applicationType": p.get("applicationType"), "packageState": p.get("state"), "hasPage": state == "declared"}
        package_index.update(at=time.monotonic(), by_id=by_id)
        return by_id

    async def declaration(package_id: str) -> tuple[str, dict | None]:
        cached = declarations.get(package_id)
        if cached is not None and time.monotonic() - cached[0] < DECLARATION_SECONDS:
            return cached[1], cached[2]
        resp = await smo_get(f"/onboarding/packages/{package_id}/onboarding-status")
        if resp.status_code == 404:
            return "none", None
        if resp.status_code != 200:
            raise failed(resp, "onboarding")
        state, found = declaration_of(resp.json())
        if len(declarations) >= MAX_CACHED_DECLARATIONS:
            declarations.clear()
        declarations[package_id] = (time.monotonic(), state, found)
        return state, found

    async def instance_of(instance_id: str) -> dict | None:
        resp = await smo_get(f"/rapp-mgmt/instances/{instance_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise failed(resp, "rapp-mgmt")
        body = resp.json()
        return body if isinstance(body, dict) else None

    def parse_instance(value: str) -> str | None:
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None

    def no_such_rapp() -> JSONResponse:
        return problem(404, "NO_SUCH_RAPP", "no such rApp instance")

    def view(inst: dict, meta: dict | None, pinned: set[str]) -> dict:
        meta = meta or {}
        return {"instanceId": inst.get("instanceId"), "packageId": inst.get("packageId"), "name": meta.get("name"), "version": meta.get("version"),
                "vendor": meta.get("vendor"), "state": inst.get("state"), "autonomyMode": inst.get("autonomyMode"),
                "hasPage": bool(meta.get("hasPage")), "operatorApiRegistered": bool(inst.get("operatorApiBase")),
                "pinned": inst.get("instanceId") in pinned}

    # ------------------------------------------------------------ the directory

    @app.get("/api/rapps")
    async def directory(search: str | None = Query(None, max_length=100, description="Case-insensitive text in the name, version, owner, instance id or package id."),
                        state: str | None = Query(None, max_length=20, description="Exactly this lifecycle state (RUNNING, FAULTED, ...)."),
                        owner: str | None = Query(None, max_length=100, description="Exactly this owner (the package's vendor), case-insensitive."),
                        has_page: bool | None = Query(None, alias="hasPage", description="Only the rApps that declare an operator page (true) or do not (false)."),
                        pinned: bool | None = Query(None, description="Only the pinned (true) or the not pinned (false)."),
                        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), session=Depends(current_session)):
        # Any signed-in role. Reads every rApp instance from rApp Management (up to `MAX_PAGES` pages of 500) and the package index, joins them, and filters, sorts and pages the
        # result in this process; the pins are the caller's own. The answer also carries every owner and state present, so the page can fill its filter lists from the unfiltered
        # set. 502 SMO_AUTH_FAILED, R1_UNREACHABLE or SMO_ERROR when the SMO cannot be asked. Writes nothing.
        try:
            instances = await read_all("/rapp-mgmt/instances")
            index = await packages(wanted={str(i.get("packageId")) for i in instances})
        except UpstreamFailure as failure:
            return failure.response
        mine = set(app.state.db.pins(session.user.username))
        rows = [view(i, index.get(str(i.get("packageId"))), mine) for i in instances]
        owners = sorted({r["vendor"] for r in rows if r["vendor"]}, key=str.lower)
        states = sorted({r["state"] for r in rows if r["state"]})
        needle = (search or "").strip().lower()
        if needle:
            rows = [r for r in rows if any(needle in str(r[k] or "").lower() for k in ("name", "version", "vendor", "instanceId", "packageId"))]
        if state:
            rows = [r for r in rows if r["state"] == state.upper()]
        if owner:
            rows = [r for r in rows if (r["vendor"] or "").lower() == owner.strip().lower()]
        if has_page is not None:
            rows = [r for r in rows if r["hasPage"] == has_page]
        if pinned is not None:
            rows = [r for r in rows if r["pinned"] == pinned]
        rows.sort(key=lambda r: (str(r["name"] or "~").lower(), str(r["version"] or ""), str(r["instanceId"])))
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset, "owners": owners, "states": states}

    async def search_directory(text: str) -> list[dict]:
        """The directory rows whose name, version, vendor, instance id or package id contain `text` (case-insensitive), in the directory's order.
        Used by the typeahead (app/search.py, `app.state.rapp_search`); pins are not marked. Raises `UpstreamFailure` when the SMO cannot be asked."""
        instances = await read_all("/rapp-mgmt/instances")
        index = await packages(wanted={str(i.get("packageId")) for i in instances})
        needle = text.strip().lower()
        rows = [view(i, index.get(str(i.get("packageId"))), set()) for i in instances]
        rows = [r for r in rows if any(needle in str(r[k] or "").lower() for k in ("name", "version", "vendor", "instanceId", "packageId"))]
        rows.sort(key=lambda r: (str(r["name"] or "~").lower(), str(r["version"] or ""), str(r["instanceId"])))
        return rows

    app.state.rapp_search = search_directory

    # ------------------------------------------------------------ one rApp

    @app.get("/api/rapps/{instance}")
    async def rapp(instance: str, session=Depends(current_session)):
        # One instance with the package's declared operator page. 404 NO_SUCH_RAPP for an id that is not a UUID or not an instance (the path value is parsed first, so it never
        # reaches the upstream URL as typed). `declarationState` is `declared`, `none` or `unreadable`; `canChange` is true only when there is a readable declaration that is not
        # read-only and the caller is operator or admin (the SPA uses it to show the actions; `operator_call` decides again on every call).
        instance_id = parse_instance(instance)
        if instance_id is None:
            return no_such_rapp()
        try:
            inst = await instance_of(instance_id)
            if inst is None:
                return no_such_rapp()
            package_id = str(inst.get("packageId"))
            index = await packages(wanted={package_id})
            kind, found = await declaration(package_id)
        except UpstreamFailure as failure:
            return failure.response
        read_only = bool(found and found.get("readOnly"))
        return {**view(inst, index.get(package_id), set(app.state.db.pins(session.user.username))),
                "declarationState": kind, "declaration": found, "readOnly": read_only,
                "canChange": found is not None and not read_only and RANK[session.user.role] >= RANK[Role.OPERATOR]}

    # ------------------------------------------------------------ the declared routes

    @app.api_route("/api/rapps/{instance}/operator/{route:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def operator_call(instance: str, route: str, request: Request, session=Depends(current_session)):
        # The proxy for a rApp's operator API; `operator_ui.decide` is the single gate. Order: instance id parsed (404), a body over `MAX_BODY_BYTES` refused (413) before it is
        # parsed, JSON parsed (400 INVALID_BODY), the instance and its package declaration read from the SMO, then the decision. A refusal is answered with its own status
        # (403 UNDECLARED_ROUTE / RAPP_READ_ONLY / FORBIDDEN, 422 for a bad query or input) and audited as DENIED. A change writes RAPP_ACTION twice, phase=requested before the
        # call is sent and phase=done with the outcome after it, so a call that never returns is still visible in the log; reads are not audited. The call goes to the gateway's
        # `/rapps/{instanceId}/operator/...` path with the BFF's own token; only `content-type` and `accept` are sent, and the upstream answer is returned minus hop-by-hop and
        # cookie headers. 502 SMO_AUTH_FAILED or R1_UNREACHABLE when the SMO cannot be reached.
        instance_id = parse_instance(instance)
        if instance_id is None:
            return no_such_rapp()
        method, path = request.method, "/" + route
        target = f"/rapps/{instance_id}/operator{path}"
        user = session.user
        raw = b""
        if method in ("POST", "PUT", "PATCH"):
            raw = await request.body()
            if len(raw) > operator_ui.MAX_BODY_BYTES:
                return problem(413, "PAYLOAD_TOO_LARGE", f"the body is over {operator_ui.MAX_BODY_BYTES} bytes")
        payload: Any = None
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                return problem(400, "INVALID_BODY", "expected a JSON object")
        try:
            inst = await instance_of(instance_id)
            if inst is None:
                return no_such_rapp()
            _, found = await declaration(str(inst.get("packageId")))
        except UpstreamFailure as failure:
            return failure.response
        decision = operator_ui.decide(found, instance_id, method, path, user.role, params=request.query_params.multi_items(), payload=payload,
                                      username=user.username, action_id=request.headers.get("x-action-id"))
        if isinstance(decision, operator_ui.Refusal):
            audit("DENIED", user, method=method, path=target, status_code=decision.status, detail=f"{decision.title}: {decision.detail}"[:500])
            return problem(decision.status, decision.title, decision.detail)
        change = method != "GET"
        label = f"rapp={instance_id} action={decision.action_id}"
        if change:
            audit("RAPP_ACTION", user, method=method, path=target, detail=f"{label} phase=requested")
        content = json.dumps(decision.body).encode() if decision.body is not None else None
        headers = {"accept": "application/json", **scoping.headers(user.username, user.scope)}   # SEC-15.8 / GUI-5: the person behind the BFF's token and their scope, as on the generic proxy
        if content is not None:
            headers["content-type"] = "application/json"
        try:
            upstream = await app.state.gateway.request(method, target, params=decision.query or None, content=content, headers=headers)
        except SmoAuthError as exc:
            log.warning("operator call %s %s: SMO token unavailable: %s", method, target, exc)
            if change:
                audit("RAPP_ACTION", user, method=method, path=target, status_code=502, detail=f"{label} phase=done outcome=SMO_AUTH_FAILED")
            return problem(502, "SMO_AUTH_FAILED", "the BFF could not obtain an SMO access token from SME")
        except httpx.HTTPError as exc:
            log.warning("operator call %s %s: R1 Termination unreachable: %r", method, target, exc)
            if change:
                audit("RAPP_ACTION", user, method=method, path=target, status_code=502, detail=f"{label} phase=done outcome=R1_UNREACHABLE")
            return problem(502, "R1_UNREACHABLE", "R1 Termination did not answer")
        if change:
            audit("RAPP_ACTION", user, method=method, path=target, status_code=upstream.status_code, detail=f"{label} phase=done")
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in _NEVER_FORWARD_RESPONSE}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=out)

    # ------------------------------------------------------------ pins

    @app.get("/api/me/pins")
    async def my_pins(session=Depends(current_session)):
        """The pinned instances with their names, oldest pin first. A pin of an instance that no longer exists is dropped here. When the SMO cannot be
        asked, the pins come back without names (the sidebar shows the short id) and nothing is dropped."""
        username = session.user.username
        ids = app.state.db.pins(username)
        names: dict[str, dict] = {}
        if ids:
            try:
                instances = await read_all("/rapp-mgmt/instances")
                index = await packages(wanted={str(i.get("packageId")) for i in instances})
            except UpstreamFailure:
                instances = None
            if instances is not None:
                known = {str(i.get("instanceId")): i for i in instances}
                for gone in [i for i in ids if i not in known]:
                    app.state.db.remove_pin(username, gone)
                ids = [i for i in ids if i in known]
                names = {i: view(known[i], index.get(str(known[i].get("packageId"))), set(ids)) for i in ids}
        return {"max": MAX_PINS, "items": [names.get(i) or {"instanceId": i, "name": None, "version": None, "state": None, "hasPage": False,
                                                           "operatorApiRegistered": False, "pinned": True} for i in ids]}

    @app.put("/api/me/pins/{instance}")
    async def pin(instance: str, session=Depends(current_session)):
        # Pins an instance for the caller. 404 NO_SUCH_RAPP when the instance does not exist (checked at rApp Management, so a pin cannot name a made-up id); 409 PIN_LIMIT at
        # `MAX_PINS`; pinning an already pinned instance is a success. The limit is enforced in the database, so two simultaneous pins cannot exceed it.
        instance_id = parse_instance(instance)
        if instance_id is None:
            return no_such_rapp()
        try:
            if await instance_of(instance_id) is None:
                return no_such_rapp()
        except UpstreamFailure as failure:
            return failure.response
        outcome = app.state.db.add_pin(session.user.username, instance_id)
        if outcome == "full":
            return problem(409, "PIN_LIMIT", f"at most {MAX_PINS} rApps can be pinned: unpin one first")
        return {"instanceId": instance_id, "pinned": True}

    @app.delete("/api/me/pins/{instance}", status_code=204)
    async def unpin(instance: str, session=Depends(current_session)):
        # Idempotent: 204 whether or not the instance was pinned, and also for a value that is not a UUID (nothing is stored under one). Writes only the caller's own pin.
        instance_id = parse_instance(instance)
        if instance_id is not None:
            app.state.db.remove_pin(session.user.username, instance_id)
        return Response(status_code=204)
