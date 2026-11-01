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

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .dn import dn_from_path
from .errors import MnsError
from .scope import hierarchical, parse_filter, parse_list, parse_scope
from .service import DEFAULT_LIMIT, PROVMNS_VERSION, ProvMnS, etag

FLAT = "application/vnd.3gpp.object-tree-flat+json"
HIERARCHICAL = "application/vnd.3gpp.object-tree-hierarchical+json"
PATH = "/ProvMnS/{version}/{ldn:path}"


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


def provmns_router(mns: ProvMnS, get_session: Callable | None = None) -> APIRouter:
    """The four ProvMnS routes of `mns` (see the module description); `get_session` is the session dependency (default `smo_shared.db.get_session`)."""
    router = APIRouter(tags=["ProvMnS (TS 28.532)"])
    session = Depends(get_session or _default_session_dependency())

    @router.put(PATH, status_code=201)
    async def create_moi(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """createMOI (TS 28.532): creates the instance the URI names, or replaces its attributes."""
        try:
            dn = _dn(version, ldn)
            body = await _json_body(request)
            created, is_new, moi_version = mns.create_moi(db, dn, body, request.headers.get("if-match"))
        except MnsError as exc:
            return _error(exc, "default")
        return JSONResponse(status_code=201 if is_new else 200, content=created, headers={"ETag": etag(moi_version)})

    @router.get(PATH)
    def get_moi_attributes(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """getMOIAttributes (TS 28.532): the scoped, filtered instances, hierarchical or flat, one page at a time."""
        query = request.query_params
        try:
            dn = _dn(version, ldn)
            page = mns.get_moi_attributes(db, dn, parse_scope(query.get("scope")), parse_filter(query.get("filter")),
                                          parse_list(query.get("attributes"), "attributes"), parse_list(query.get("fields"), "fields"),
                                          _limit(query.get("limit")), query.get("cursor") or None)
        except MnsError as exc:
            return _error(exc, "get")
        headers = {}
        if page.next_cursor is not None:
            following = {**dict(query), "cursor": page.next_cursor}
            headers["Link"] = f'<{request.url.path}?{urlencode(following)}>; rel="next"'
        accept = request.headers.get("accept", "")
        if FLAT in accept:
            return JSONResponse(content=page.items, headers=headers, media_type=FLAT)
        media_type = HIERARCHICAL if HIERARCHICAL in accept else "application/json"
        return JSONResponse(content=hierarchical(dn, page.items), headers=headers, media_type=media_type)

    @router.patch(PATH)
    async def modify_moi_attributes(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """modifyMOIAttributes (TS 28.532): a merge patch or a JSON patch, by Content-Type."""
        try:
            dn = _dn(version, ldn)
            media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            body = await _json_body(request)
            changed, moi_version = mns.modify_moi_attributes(db, dn, media_type, body, request.headers.get("if-match"))
        except MnsError as exc:
            return _error(exc, "patch")
        return JSONResponse(content=changed, headers={"ETag": etag(moi_version)})

    @router.delete(PATH, status_code=204)
    def delete_moi(version: str, ldn: str, request: Request, db: Session = session) -> Response:
        """deleteMOI (TS 28.532): the instance and every instance it contains."""
        try:
            mns.delete_moi(db, _dn(version, ldn), request.headers.get("if-match"))
        except MnsError as exc:
            return _error(exc, "default")
        return Response(status_code=204)

    return router
