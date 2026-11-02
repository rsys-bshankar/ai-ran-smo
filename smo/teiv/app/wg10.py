"""The O-RAN WG10 TE&IV R1 API served by TE&IV (O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00, `specs/oran/teiv/TEIV_API_R005_v04.yaml`, OpenAPI 1.2.1; the
bundle's `teiv.yaml` `x-smo-teiv-api`): TE&IV is the only door to smo-teiv (D19), and each of the 16 operations is passed to smo-teiv 0.3.0, which
implements the same operations (ADR 0013 section 11.7), at `/topology-inventory/v1/...` of TE&IV's R1 root (R1 Termination: `/teiv/...`).

What TE&IV does on the way:

  parameters       only the query parameters the standard declares for the operation are passed (offset, limit, targetFilter, scopeFilter, domain), and
                   only the Accept and Content-Type headers; the body of the three POSTs is passed as it is
  scoped callers   a caller with an ADR 0005 scope claim, or one MSAC manages, is refused (403 SCOPE_DENIED): smo-teiv's answers cannot be cut to a
                   claim or to access rules object by object, and the WG10 API names no DN to check before the answer; such a caller reads through
                   ProvMnS (provmns.py), where both are applied
  smo-teiv's SQL  an updateDecorator whose decorator names or values hold `'`, `"` or a backslash is refused (400 INVALID_DECORATOR_VALUE): smo-teiv 0.3.0
                   pastes the value into its SQL text unescaped, so a `'` would end the literal (an SQL injection; ADR 0013 section 12.5, d2)
  TE&IV's own data TE&IV's decorator schema (`smo-teiv-attrs`, S3.21) and its decorators hold TS 28.541 attributes that TE&IV alone writes (D6): a
                   deleteUserDefinedSchema of it, or an updateDecorator that names one of its decorators, is 403 TEIV_OWNED
  writes audited   createUserDefinedSchema, deleteUserDefinedSchema, updateClassifier and updateDecorator add a row to the audit trail (S1.5's step 4)
  smo-teiv's answer  passed through with its status and body (ProblemDetails for an error), including smo-teiv's extra paging links and the 409 it may
                   answer to updateClassifier / updateDecorator (the two differences `teiv.yaml` records); 503 TEIV_UNAVAILABLE when it does not answer
smo-teiv's own operations the standard does not have (groups, createSchema, getEntitiesByDomain, ...) have no route here, so no one reaches them.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from smo_shared import audit
from smo_shared.correlation import get_correlation_id
from smo_shared.db import get_session
from smo_shared.mns.checks import Caller, RouterChecks
from smo_shared.mns.errors import MnsError

from . import attrs
from .client import SmoTeivClient

BASE = "/topology-inventory/v1"
PAGING = ("offset", "limit")
FILTERS = ("targetFilter", "scopeFilter", "offset", "limit")
TAG = "WG10 TE&IV R1 (O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00)"


def problem(status: int, title: str, detail: str, headers: dict | None = None) -> JSONResponse:
    """An RFC 7807 problem, the error shape of the WG10 API."""
    return JSONResponse(status_code=status, content={"type": "about:blank", "title": title, "status": status, "detail": detail},
                        media_type="application/problem+json", headers=headers)


UNSAFE_IN_SQL_TEXT = set("'\"\\")


def _unsafe_decorators(body: bytes) -> bool:
    """Whether an updateDecorator body has a decorator (name or value, at any depth of a structured value) with a character smo-teiv 0.3.0 would
    paste into its SQL unescaped (d2): `'` ends its literal (an SQL injection), `"` and `\\` break the JSON text it builds."""
    try:
        decorators = json.loads(body or b"{}").get("decorators") or {}
    except (ValueError, AttributeError):
        return False                                    # not JSON, or not an object: smo-teiv refuses it itself (400)

    def bad(value) -> bool:
        if isinstance(value, str):
            return bool(UNSAFE_IN_SQL_TEXT & set(value))
        if isinstance(value, dict):
            return any(bad(k) or bad(v) for k, v in value.items())
        if isinstance(value, list):
            return any(bad(v) for v in value)
        return False
    return isinstance(decorators, dict) and bad(decorators)


def wg10_router(client: SmoTeivClient, checks: RouterChecks) -> APIRouter:
    """The 16 operations (module description), each a route of its own so the generated OpenAPI names them as the standard does."""
    router = APIRouter(prefix=BASE, tags=[TAG])

    def caller_allowed(request: Request) -> JSONResponse | None:
        caller = Caller.from_headers(request.headers)
        if caller.scope is not None or checks.msac.provider(caller) is not None:
            return problem(403, "SCOPE_DENIED", "a scoped or MSAC-managed caller reads TE&IV through ProvMnS, where the claim and the rules apply")
        return None

    def forward(request: Request, method: str, path: str, allowed: tuple[str, ...] = (), body: bytes | None = None) -> Response:
        refused = caller_allowed(request)
        if refused is not None:
            return refused
        params = {k: v for k, v in request.query_params.items() if k in allowed}
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "content-type")}
        try:
            answer = client.forward(method, path, params=params, content=body, headers=headers)
        except MnsError as exc:
            return problem(exc.status, exc.type, exc.reason, exc.headers)
        keep = {k: v for k, v in answer.headers.items() if k.lower() in ("content-type", "location")}
        return Response(content=answer.content, status_code=answer.status_code, headers=keep)

    def audited(request: Request, db: Session, action: str, target: str, response: Response, detail: dict) -> Response:
        """Records a write that smo-teiv carried out (a 2xx) in the audit trail; the answer is returned unchanged."""
        if 200 <= response.status_code < 300:
            caller = Caller.from_headers(request.headers)
            audit.record(db, actor=caller.actor, role=caller.role, action=action, target=f"{BASE}{target}", result=str(response.status_code),
                         correlation_id=get_correlation_id(), detail=detail)
            db.commit()
        return response

    @router.get("/domains", operation_id="getAllDomains", summary="Get all the available topology domains.")
    def get_all_domains(request: Request) -> Response:
        """getAllDomains, passed to smo-teiv."""
        return forward(request, "GET", "/domains", PAGING)

    @router.get("/domains/{domainName}/entity-types", operation_id="getTopologyEntityTypes", summary="Get all the entity types in a domain.")
    def get_entity_types(domainName: str, request: Request) -> Response:
        """getTopologyEntityTypes, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/entity-types", PAGING)

    @router.get("/domains/{domainName}/entity-types/{entityTypeName}/entities", operation_id="getTopologyByEntityTypeName",
                summary="Get all Topology Entity instances of a specific Topology Entity type.")
    def get_entities(domainName: str, entityTypeName: str, request: Request) -> Response:
        """getTopologyByEntityTypeName, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/entity-types/{entityTypeName}/entities", FILTERS)

    @router.get("/domains/{domainName}/entity-types/{entityTypeName}/entities/{entityId}", operation_id="getTopologyById",
                summary="Get a Topology Entity instance by id.")
    def get_entity(domainName: str, entityTypeName: str, entityId: str, request: Request) -> Response:
        """getTopologyById, passed to smo-teiv (the id path-encoded again)."""
        return forward(request, "GET", f"/domains/{domainName}/entity-types/{entityTypeName}/entities/{quote(entityId, safe='')}")

    @router.get("/domains/{domainName}/entity-types/{entityTypeName}/entities/{entityId}/relationships", operation_id="getAllRelationshipsForEntityId",
                summary="Get all relationships of a Topology Entity instance.")
    def get_entity_relationships(domainName: str, entityTypeName: str, entityId: str, request: Request) -> Response:
        """getAllRelationshipsForEntityId, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/entity-types/{entityTypeName}/entities/{quote(entityId, safe='')}/relationships", FILTERS)

    @router.get("/domains/{domainName}/relationship-types", operation_id="getTopologyRelationshipTypes",
                summary="Get all the relationship types in a domain.")
    def get_relationship_types(domainName: str, request: Request) -> Response:
        """getTopologyRelationshipTypes, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/relationship-types", PAGING)

    @router.get("/domains/{domainName}/relationship-types/{relationshipTypeName}/relationships", operation_id="getRelationshipsByType",
                summary="Get all Topology Relationship instances of a specific type.")
    def get_relationships(domainName: str, relationshipTypeName: str, request: Request) -> Response:
        """getRelationshipsByType, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/relationship-types/{relationshipTypeName}/relationships", FILTERS)

    @router.get("/domains/{domainName}/relationship-types/{relationshipTypeName}/relationships/{relationshipId}", operation_id="getRelationshipById",
                summary="Get a Topology Relationship instance by id.")
    def get_relationship(domainName: str, relationshipTypeName: str, relationshipId: str, request: Request) -> Response:
        """getRelationshipById, passed to smo-teiv."""
        return forward(request, "GET", f"/domains/{domainName}/relationship-types/{relationshipTypeName}/relationships/{quote(relationshipId, safe='')}")

    @router.get("/schemas", operation_id="getSchemas", summary="Get a list of all schemas.")
    def get_schemas(request: Request) -> Response:
        """getSchemas, passed to smo-teiv."""
        return forward(request, "GET", "/schemas", ("domain",) + PAGING)

    @router.get("/schemas/{schemaName}/content", operation_id="getSchemaByName", summary="Get the content of a schema.")
    def get_schema(schemaName: str, request: Request) -> Response:
        """getSchemaByName, passed to smo-teiv."""
        return forward(request, "GET", f"/schemas/{schemaName}/content")

    @router.post("/user-defined-schemas", operation_id="createUserDefinedSchema", status_code=201, summary="Create a new user defined schema.")
    async def create_user_defined_schema(request: Request, db: Session = Depends(get_session)) -> Response:
        """createUserDefinedSchema, passed to smo-teiv (the multipart body as it is); audited."""
        body = await request.body()
        return audited(request, db, "POST", "/user-defined-schemas", forward(request, "POST", "/user-defined-schemas", body=body),
                       {"operation": "createUserDefinedSchema"})

    @router.get("/user-defined-schemas", operation_id="getUserDefinedSchemas", summary="Get all user defined schemas.")
    def get_user_defined_schemas(request: Request) -> Response:
        """getUserDefinedSchemas, passed to smo-teiv."""
        return forward(request, "GET", "/user-defined-schemas", PAGING)

    @router.get("/user-defined-schemas/{schemaName}/content", operation_id="getUserDefinedSchemaByName", summary="Get a user defined schema by name.")
    def get_user_defined_schema(schemaName: str, request: Request) -> Response:
        """getUserDefinedSchemaByName, passed to smo-teiv."""
        return forward(request, "GET", f"/user-defined-schemas/{schemaName}/content")

    @router.delete("/user-defined-schemas/{schemaName}", operation_id="deleteUserDefinedSchema", status_code=204, summary="Delete a user defined schema.")
    def delete_user_defined_schema(schemaName: str, request: Request, db: Session = Depends(get_session)) -> Response:
        """deleteUserDefinedSchema, passed to smo-teiv, except for TE&IV's own schema (403 TEIV_OWNED); audited."""
        if schemaName == attrs.MODULE:
            return problem(403, "TEIV_OWNED", f"{attrs.MODULE} holds TE&IV's TS 28.541 attributes; only TE&IV changes it")
        return audited(request, db, "DELETE", f"/user-defined-schemas/{schemaName}", forward(request, "DELETE", f"/user-defined-schemas/{schemaName}"),
                       {"operation": "deleteUserDefinedSchema", "schema": schemaName})

    @router.post("/manage-classifiers", operation_id="updateClassifier", summary="Update or remove classifiers on entities and relationships.")
    async def update_classifier(request: Request, db: Session = Depends(get_session)) -> Response:
        """updateClassifier, passed to smo-teiv; audited."""
        body = await request.body()
        return audited(request, db, "POST", "/manage-classifiers", forward(request, "POST", "/manage-classifiers", body=body),
                       {"operation": "updateClassifier"})

    @router.post("/manage-decorators", operation_id="updateDecorator", summary="Update or remove decorators on entities and relationships.")
    async def update_decorator(request: Request, db: Session = Depends(get_session)) -> Response:
        """updateDecorator, passed to smo-teiv, except one that names a decorator of TE&IV's own schema (403 TEIV_OWNED); audited."""
        body = await request.body()
        try:
            names = list((json.loads(body or b"{}").get("decorators") or {}))
        except (ValueError, AttributeError):
            names = []
        if any(str(n).startswith(attrs.MODULE + ":") for n in names):
            return problem(403, "TEIV_OWNED", f"the decorators of {attrs.MODULE} are TE&IV's TS 28.541 attributes; they change through ProvMnS")
        if _unsafe_decorators(body):
            return problem(400, "INVALID_DECORATOR_VALUE", "a decorator value may not hold ', \" or \\: smo-teiv 0.3.0 writes the value into its SQL "
                           "text without escaping it (ADR 0013 section 12.5, d2)")
        return audited(request, db, "POST", "/manage-decorators", forward(request, "POST", "/manage-decorators", body=body),
                       {"operation": "updateDecorator", "decorators": names})

    return router
