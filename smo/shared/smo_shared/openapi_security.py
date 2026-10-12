"""Declares, in each service's own generated OpenAPI spec, the R1 gateway
security this build already really enforces at runtime — without adding any
enforcing dependency to that service itself.

r1-termination/app/main.py's own `_authorized()` is the ONE real enforcement
point: RFC 7662 introspection against SME's token issuer, on every proxied
request, fail-closed. No individual backend service (aimgf, dme, mlmr, ...)
has ever had its own auth check — they all trust the gateway that's the only
way a real R1 consumer ever reaches them. That's still true after this
module exists: it only makes the OpenAPI contract honest about an interface
that was already real, not a new runtime behavior. See HISTORY.md §2 and docs/ARCHITECTURE.md's Wave 3
cross-cutting standardization item.

R1_CONTRACT_VERSION is this build's own semantic version for "the current,
Wave-3-complete R1 contract shape" — not a claim to match any one external
3GPP/O-RAN spec's own info.version, since most of these services compose
several specs (or none) rather than implement exactly one 1:1.
"""

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .errors import install_integrity_handlers, install_out_of_range_handler, install_validation_error_handler, install_validation_error_handler

BEARER_SCHEME_NAME = "r1BearerAuth"
ERROR_ENVELOPE = "ErrorEnvelope"
# Statuses any operation can answer with, whatever its own code raises: the framework (400 unparsable body, 413 size cap,
# 429 rate limit), the gateway's token check (401, 403) and the handlers' ProblemDetails (404, 409, 422, 503 ...).
STANDARD_ERROR_STATUSES = ("400", "401", "403", "404", "409", "413", "422", "429", "502", "503")
R1_CONTRACT_VERSION = "1.0.0"


def apply_r1_gateway_security(app: FastAPI, *, public_paths: frozenset[str] = frozenset()) -> None:
    """Every route on `app` requires the R1 gateway's bearer token, except
    `public_paths` (SME's own token-issuance/introspection endpoints, and
    r1-termination's own unauthenticated /health + /bootstrap — the two
    routes its own docstrings already say never call `_authorized`).
    """
    app.version = R1_CONTRACT_VERSION
    install_out_of_range_handler(app)  # a number too large for its column is a 422, not a 500 (errors.py)
    install_validation_error_handler(app)  # a 422 for a refused parameter no longer echoes the caller's input (errors.py, OPEN_ITEMS QA-9.1)
    install_validation_error_handler(app)  # a 422 for a refused parameter no longer echoes the caller's input (errors.py, OPEN_ITEMS QA-9.1)
    install_integrity_handlers(app)    # a reference to nothing, a duplicate, a value a CHECK refuses: 4xx; anything else unhandled: a problem document (errors.py)

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[BEARER_SCHEME_NAME] = {
            "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
        }
        schema["security"] = [{BEARER_SCHEME_NAME: []}]
        _declare_error_responses(schema)
        for path, operations in schema.get("paths", {}).items():
            if path in public_paths:
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation["security"] = []
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]  # FastAPI's documented way to customise the schema


def _declare_error_responses(schema: dict) -> None:
    """Declares the error body every operation can answer with (RFC 7807 ProblemDetails inside FastAPI's `detail`, a plain string from
    the framework, or the list FastAPI's own validation produces) so the spec describes what the service really sends; found by the
    contract test (tests_integration/test_contract_schemathesis.py). A status an operation already documents is left as it is, except 422,
    which handlers also use for ProblemDetails: its body is widened to the envelope."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components[ERROR_ENVELOPE] = {
        "title": ERROR_ENVELOPE, "type": "object", "required": ["detail"],
        "properties": {"detail": {"anyOf": [
            {"$ref": "#/components/schemas/ProblemDetails"} if "ProblemDetails" in components else {"type": "object"},
            {"type": "string"},
            {"type": "array", "items": {"type": "object"}},
        ]}},
    }
    components.setdefault("ProblemDetails", {
        "title": "ProblemDetails", "type": "object", "required": ["title", "status"],
        "properties": {"type": {"type": "string", "default": "about:blank"}, "title": {"type": "string"}, "status": {"type": "integer"},
                       "detail": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "instance": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    })
    components[ERROR_ENVELOPE]["properties"]["detail"]["anyOf"][0] = {"$ref": "#/components/schemas/ProblemDetails"}
    body = {"application/json": {"schema": {"$ref": f"#/components/schemas/{ERROR_ENVELOPE}"}}}
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            responses = operation["responses"]
            for status in STANDARD_ERROR_STATUSES:
                if status not in responses:
                    responses[status] = {"description": "Error", "content": body}
                elif status == "422":
                    responses[status] = {"description": responses[status].get("description", "Error"), "content": body}
