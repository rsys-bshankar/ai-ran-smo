"""The error answers of the ProvMnS router, in the shapes of the vendored TS 28.532 OpenAPI (`specs/5G_APIs/TS28532_ProvMnS.yaml`).

Every operation answers an error with a body of `TS28623_ComDefs.yaml`: `ErrorResponseDefault` (createMOI, deleteMOI, and modifyMOIAttributes when no
patch operation is to blame), `ErrorResponseGet` (getMOIAttributes: `badQueryParams` instead of `badAttributes` / `badObjects`) or `ErrorResponsePatch`
(modifyMOIAttributes when one operation of a JSON patch failed: `badOp` names it). `status` is the HTTP status as a string, as the schemas declare it;
`type` is one of the codes below; `reason` says what was wrong; `title` is the code's short text.

These bodies are TS 28.532's, not the RFC 7807 `{"detail": ProblemDetails}` envelope of the SMO's own routes (`smo_shared.errors`): a ProvMnS consumer
reads the 3GPP shape.
"""

from typing import Any

# The error codes (`type`) and their titles. A code, once published, is not renamed: consumers match on it.
TITLES = {
    "INVALID_DN": "The URI does not name a managed object instance",
    "INVALID_BODY": "The request body is not a valid resource",
    "INVALID_QUERY": "A query parameter is not valid",
    "UNSUPPORTED_MEDIA_TYPE": "The patch media type is not one of the four TS 28.532 allows",
    "UNKNOWN_VERSION": "This producer does not serve that ProvMnS version",
    "CLASS_NOT_SUPPORTED": "No schema is registered for this class",
    "SCHEMA_VALIDATION_FAILED": "The attributes do not satisfy the class's schema",
    "CONTAINMENT_VIOLATION": "The class may not be contained in its parent's class",
    "PARENT_NOT_FOUND": "The parent managed object instance does not exist",
    "MOI_NOT_FOUND": "The managed object instance does not exist",
    "CLASS_MISMATCH": "The managed object instance is of another class",
    "PATCH_FAILED": "A patch operation could not be applied",
    "STALE_VERSION": "The managed object instance changed since the version the request names",
    "CONCURRENT_MODIFICATION": "The managed object instance was changed by another request while this one ran",
    "VETOED": "The producer's domain logic refused the change",
}


class MnsError(Exception):
    """An error answer of the ProvMnS router: the HTTP status, the code (`type`), the reason, and the lists the TS 28.623 error schemas carry.

    Raised by the router, the class registry and the lifecycle handlers (a handler vetoes a change by raising one, usually with `type_="VETOED"`). The
    router turns it into the JSON body of the operation's schema (`body`).
    """

    def __init__(self, status: int, type_: str, reason: str, *, bad_attributes: list[str] | None = None, bad_objects: list[str] | None = None,
                 bad_query_params: list[str] | None = None, bad_op: str | None = None, headers: dict[str, str] | None = None):
        """Keeps the HTTP status, the code, the reason and the lists for `body`; `headers` are added to the answer (a 412 carries the current ETag)."""
        super().__init__(reason)
        self.status = status
        self.type = type_
        self.reason = reason
        self.bad_attributes = bad_attributes
        self.bad_objects = bad_objects
        self.bad_query_params = bad_query_params
        self.bad_op = bad_op
        self.headers = headers

    def body(self, operation: str) -> dict[str, Any]:
        """The error body for `operation`: `get` (ErrorResponseGet), `patch` (ErrorResponsePatch when `bad_op` is set, else the default shape) or
        anything else (ErrorResponseDefault). Lists that are not set are left out, as the schemas allow."""
        out: dict[str, Any] = {"status": str(self.status), "type": self.type, "reason": self.reason, "title": TITLES.get(self.type, self.type)}
        if operation == "get":
            if self.bad_query_params is not None:
                out["badQueryParams"] = self.bad_query_params
            return out
        if operation == "patch" and self.bad_op is not None:
            out["badOp"] = self.bad_op
            return out
        if self.bad_attributes is not None:
            out["badAttributes"] = self.bad_attributes
        if self.bad_objects is not None:
            out["badObjects"] = self.bad_objects
        return out
