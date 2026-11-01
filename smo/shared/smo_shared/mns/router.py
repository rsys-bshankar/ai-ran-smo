"""The HTTP face of a module's ProvMnS producer: one FastAPI router in D5's single form (plan task S1.1).

    from smo_shared.mns import ProvMnS, provmns_router
    mns = ProvMnS(source="/smo/echo", system_dn="ManagementNode=echo", classes=[...])
    app.include_router(provmns_router(mns))           # every route below; `get_session` defaults to smo_shared.db.get_session

  PUT     /ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}   createMOI            201 created, 200 replaced; body: Resource
  GET     /ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}   getMOIAttributes     200; query: scope, filter, attributes, fields, limit, cursor
  PATCH   /ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}   modifyMOIAttributes  200; body: a merge patch or a JSON patch (Content-Type)
  DELETE  /ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}   deleteMOI            204

`{version}` must be PROVMNS_VERSION (D5: one version, set in one constant; any other is a 404 UNKNOWN_VERSION). The URI-LDN-first-part may be empty
(`/ProvMnS/19.2.0/SubNetwork=A` names a top-level instance). Bodies and error bodies are the vendored TS 28.532 OpenAPI's (`errors.py`). Every answer
that names one instance carries its moiVersion as the `ETag`; PUT, PATCH and DELETE honour `If-Match` (412 when the instance moved on).

getMOIAttributes answers in the hierarchical form (`application/json`, or `application/vnd.3gpp.object-tree-hierarchical+json` when asked for), or
in the flat form when `Accept` names `application/vnd.3gpp.object-tree-flat+json` (scope.py). Paging is an SMO extension the 3GPP OpenAPI does not
have: `limit` (1 to 500, default 100) and `cursor`; when more follow, a `Link: <...>; rel="next"` header gives the next page's URI (RFC 8288), whose
`cursor` is the last DN of this page, so pages stay stable while instances are added or removed elsewhere in the tree (keyset paging).

The routes take the request apart by hand (the path, the query string, the body by its media type) rather than through FastAPI's parameter models,
so every refusal is a TS 28.532 error body, never FastAPI's own 422.
"""

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import tracing
from ..idempotency import run_idempotent
from .checks import Caller
from .dn import dn_from_path
from .errors import MnsError
from .scope import hierarchical, parse_filter, parse_list, parse_scope
from .service import DEFAULT_LIMIT, PROVMNS_VERSION, ProvMnS, etag

FLAT = "application/vnd.3gpp.object-tree-flat+json"
HIERARCHICAL = "application/vnd.3gpp.object-tree-hierarchical+json"
PATH = "/ProvMnS/{version}/{ldn:path}"
LINK = "Link"                 # the response header that names the next page (RFC 8288)
ACCEPT = "accept"             # the request header that chooses the answer's form
ANY_FORM = ""                 # what a request without Accept asks for: the hierarchical answer as application/json

# The error answers the routes declare in the module's OpenAPI document: the TS 28.532 error body (errors.py) for every status the router itself
# answers with. `x-smo-error-body` tells `smo_shared.openapi_security` that these are the operation's own error bodies, not the SMO's
# ProblemDetails envelope (found when SME, under the schemathesis contract test, mounted the router: plan task S1.12).
ERROR_BODY = {"title": "TS 28.532 error", "type": "object", "required": ["status", "type"],
              "properties": {"status": {"type": "string"}, "type": {"type": "string"}, "reason": {"type": "string"}, "title": {"type": "string"},
                             "badAttributes": {"type": "array", "items": {"type": "string"}},
                             "badObjects": {"type": "array", "items": {"type": "string"}},
                             "badQueryParams": {"type": "array", "items": {"type": "string"}}, "badOp": {"type": "string"}}}
ERROR_STATUSES = (400, 403, 404, 409, 412, 415, 422)
RESPONSES: dict[int | str, dict[str, Any]] = {status: {"description": "TS 28.532 error", "content": {"application/json": {"schema": ERROR_BODY}}}
                                              for status in ERROR_STATUSES}
OPENAPI_EXTRA = {"x-smo-error-body": "TS 28.532"}


def _error(exc: MnsError, operation: str) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body(operation), headers=exc.headers)


def _dn(version: str, ldn: str) -> str:
    if version != PROVMNS_VERSION:
        raise MnsError(404, "UNKNOWN_VERSION", f"ProvMnS version {version} is not served; this producer serves {PROVMNS_VERSION}")
    return dn_from_path(ldn)


async def _json_body(request: Request) -> Any:
    """The request body parsed as JSON (None when empty); a body that is not JSON is a 400 INVALID_BODY."""
    raw = await request.body()
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise MnsError(400, "INVALID_BODY", "the body is not JSON") from None


def _limit(raw: str | None) -> int:
    """The `limit` query parameter as an integer (DEFAULT_LIMIT when absent); anything but digits is a 400 naming `limit`."""
    if raw is None:
        return DEFAULT_LIMIT
    if not raw.isdigit():
        raise MnsError(400, "INVALID_QUERY", "limit is a positive integer", bad_query_params=["limit"])
    return int(raw)


def _default_session_dependency() -> Callable:
    from ..db import get_session        # imported here: smo_shared.db builds its engine at import, which a caller passing its own must not need
    return get_session


def _refusal(exc: HTTPException) -> MnsError:
    """A refusal of `smo_shared.idempotency` (a ProblemDetails in an HTTPException) as an MnsError, so a ProvMnS caller reads the TS 28.532 shape."""
    detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
    return MnsError(exc.status_code, str(detail.get("title", "INVALID_REQUEST")), str(detail.get("detail") or ""))


def _class_label(mns: ProvMnS, ldn: str) -> str:
    """The `class` label of the metric: the URI's class when the producer serves it, else `other` (a caller cannot grow the series)."""
    class_name = ldn.rpartition("/")[2].partition("=")[0]
    return class_name if mns.served(class_name) else "other"


def _trace(request: Request):
    """Step 5 of checks.py: the request's W3C trace for the operation, when no middleware made it current (`tracing.apply_tracing` does in a module)."""
    if tracing.get_trace_context() is not None:
        return contextlib.nullcontext()
    return tracing.trace_scope(tracing.context_from_headers(request.headers))


def provmns_router(mns: ProvMnS, get_session: Callable | None = None) -> APIRouter:
    """The four ProvMnS routes of `mns` (see the module description); `get_session` is the session dependency (default `smo_shared.db.get_session`).
    Every route passes the checks of checks.py: the caller and its scope, MSAC and the audit row in the service, the `Idempotency-Key` replay of
    createMOI, the trace and the `smo_provmns_http_duration_seconds` metric here."""
    router = APIRouter(tags=["ProvMnS (TS 28.532)"])
    session = Depends(get_session or _default_session_dependency())

    @router.put(PATH, status_code=201, responses=RESPONSES, openapi_extra=OPENAPI_EXTRA)
    async def create_moi(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """createMOI (TS 28.532): creates the instance the URI names, or replaces its attributes; an `Idempotency-Key` replays the first answer."""
        started, caller = time.perf_counter(), Caller.from_headers(request.headers)
        with _trace(request):
            try:
                dn = _dn(version, ldn)
                body = await _json_body(request)

                def run() -> Response:
                    created, is_new, moi_version = mns.create_moi(db, dn, body, request.headers.get("if-match"), caller)
                    return JSONResponse(status_code=201 if is_new else 200, content=created, headers={"ETag": etag(moi_version)})
                response = run_idempotent(request, db, mns.checks.module, 201, body, run)
            except MnsError as exc:
                response = _error(exc, "default")
            except HTTPException as exc:
                response = _error(_refusal(exc), "default")
        mns.checks.observe(_class_label(mns, ldn), "createMOI", response.status_code, started)
        return response

    @router.get(PATH, responses=RESPONSES, openapi_extra=OPENAPI_EXTRA)
    def get_moi_attributes(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """getMOIAttributes (TS 28.532): the scoped, filtered instances, hierarchical or flat, one page at a time."""
        started, caller = time.perf_counter(), Caller.from_headers(request.headers)
        with _trace(request):
            response = _read(mns, version, ldn, request, db, caller)
        mns.checks.observe(_class_label(mns, ldn), "getMOIAttributes", response.status_code, started)
        return response

    @router.patch(PATH, responses=RESPONSES, openapi_extra=OPENAPI_EXTRA)
    async def modify_moi_attributes(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """modifyMOIAttributes (TS 28.532): a merge patch or a JSON patch, by Content-Type."""
        started, caller = time.perf_counter(), Caller.from_headers(request.headers)
        with _trace(request):
            try:
                dn = _dn(version, ldn)
                media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
                body = await _json_body(request)
                changed, moi_version = mns.modify_moi_attributes(db, dn, media_type, body, request.headers.get("if-match"), caller)
                response: Response = JSONResponse(content=changed, headers={"ETag": etag(moi_version)})
            except MnsError as exc:
                response = _error(exc, "patch")
        mns.checks.observe(_class_label(mns, ldn), "modifyMOIAttributes", response.status_code, started)
        return response

    @router.delete(PATH, status_code=204, responses=RESPONSES, openapi_extra=OPENAPI_EXTRA)
    def delete_moi(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """deleteMOI (TS 28.532): the instance and every instance it contains."""
        started, caller = time.perf_counter(), Caller.from_headers(request.headers)
        with _trace(request):
            try:
                mns.delete_moi(db, _dn(version, ldn), request.headers.get("if-match"), caller)
                response = Response(status_code=204)
            except MnsError as exc:
                response = _error(exc, "default")
        mns.checks.observe(_class_label(mns, ldn), "deleteMOI", response.status_code, started)
        return response

    return router


def _read(mns: ProvMnS, version: str, ldn: str, request: Request, db: Session, caller: Caller) -> Response:
    """getMOIAttributes' answer: the page in the form `Accept` asks for, with the `Link` to the next page, or the error body."""
    query = request.query_params
    try:
        dn = _dn(version, ldn)
        page = mns.get_moi_attributes(db, dn, parse_scope(query.get("scope")), parse_filter(query.get("filter")),
                                      parse_list(query.get("attributes"), "attributes"), parse_list(query.get("fields"), "fields"),
                                      _limit(query.get("limit")), query.get("cursor") or None, caller)
    except MnsError as exc:
        return _error(exc, "get")
    headers = {}
    if page.next_cursor is not None:
        following = {**dict(query), "cursor": page.next_cursor}
        headers[LINK] = f'<{request.url.path}?{urlencode(following)}>; rel="next"'
    accept = request.headers.get(ACCEPT, ANY_FORM)
    if FLAT in accept:
        return JSONResponse(content=page.items, headers=headers, media_type=FLAT)
    media_type = HIERARCHICAL if HIERARCHICAL in accept else "application/json"
    return JSONResponse(content=hierarchical(dn, page.items), headers=headers, media_type=media_type)
