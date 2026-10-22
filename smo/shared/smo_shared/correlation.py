"""Real request-correlation-ID propagation across the R1 mesh — Wave 3
cross-cutting standardization's last item (docs/architecture/
docs/ARCHITECTURE.md). Audited at the start of the Wave 3
standardization pass ("Correlation-ID: totally absent") but never picked
up by any of the four shipped slices (OAuth2/JWT+Versioning, Error
Schema, Pagination, Subscriptions) — this closes it.

No formal 3GPP/O-RAN spec defines a header for this exact purpose:
specs/5G_APIs/TS29500_CustomHeaders.abnf's own real
3gpp-Sbi-Correlation-Info header is a different concept entirely
(subscriber-identity correlation — imsi/msisdn/impu/etc — not request
tracing), confirmed by reading its own ABNF grammar directly, not
assumed from the name alone. The real O-RAN SC smo-teiv component
(already this build's own ground truth for FOCOM's TEIV export) does
track a genuine per-event `correlationid` (its own CloudEvent extension
attribute, echoed via MDC into structured JSON logs) — the same concept,
a different transport (CloudEvents, not this build's own plain-REST R1
mesh). `X-Correlation-ID` is this module's own HTTP-native name for the
same idea, matching the near-universal industry convention for exactly
this purpose.

Deliberately not declared in any service's OpenAPI schema (unlike
openapi_security.py's security scheme): a middleware-injected header on
every route isn't a per-operation contract element, and adding it as a
formal parameter to ~200 operations across 18 services would be a much
larger, largely cosmetic diff for no real behavior gain.
"""

import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import FastAPI, Request

HEADER_NAME = "X-Correlation-ID"
_current_correlation_id: ContextVar[str | None] = ContextVar("_current_correlation_id", default=None)
_VALID = re.compile(r"^[\x21-\x7e]{1,128}$")    # 1 to 128 printable ASCII characters, no space (call flow 14a)
log = logging.getLogger(__name__)


def accepted_correlation_id(value: str | None) -> str:
    """The id a request carries on: the caller's `X-Correlation-ID` when it is 1 to 128 printable ASCII characters, else a fresh UUID (call flow 14a).

    A header with other characters would make every downstream call fail (httpx refuses to encode it) and a long one would bloat every log line, so
    either is replaced and the replacement logged; the refused value itself is not logged, only its length.
    """
    if value and _VALID.match(value):
        return value
    fresh = str(uuid.uuid4())
    if value:
        log.warning("refused an X-Correlation-ID of %d characters (not 1-128 printable ASCII); using %s", len(value), fresh)
    return fresh


def get_correlation_id() -> str | None:
    """The current request's correlation ID, if this call is happening
    inside a request handled by a service with `apply_correlation_id`
    applied — None outside any request context (e.g. a standalone
    script, or a service that hasn't applied it).
    """
    return _current_correlation_id.get()


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[str]:
    """Makes `correlation_id` the current one inside the block, outside any request: a worker's task (call flow 22e: a scheduled pass of the
    EnergySaving rApp) gets the same id on every downstream call, as a request would. The previous value comes back after the block."""
    token = _current_correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _current_correlation_id.reset(token)


def apply_correlation_id(app: FastAPI) -> None:
    """Every request gets a real correlation ID: the caller's own
    X-Correlation-ID if it sent one, else a freshly generated one.
    Stored for the duration of the request — R1Client
    (smo_shared/r1_client.py) reads it via get_correlation_id() and
    attaches it to every downstream call this handler makes, so one
    inbound request's whole cross-service fan-out shares one ID — and
    echoed back on the response so a caller that didn't send one can
    still see what got assigned.

    Every service calls this, so it also installs the invoker context
    (smo_shared/invoker.py): who the request is really for, which R1Client
    passes on the same way, and the W3C trace context (smo_shared/tracing.py:
    a `traceparent` the caller sent is continued downstream; spans only when
    SMO_OTEL_ENDPOINT is set).
    """
    from .invoker import apply_invoker_context
    from .scope import apply_scope_context
    from .security_headers import apply_security_headers
    from .tracing import apply_tracing
    apply_invoker_context(app)
    apply_scope_context(app)    # the claim of that originator (PR-SEC-10), passed on beside it
    apply_security_headers(app)
    apply_tracing(app)       # inside the correlation middleware below, so a span can carry the correlation id (PR-OBS-3)

    @app.middleware("http")
    async def _correlation_id_middleware(request: Request, call_next):
        correlation_id = accepted_correlation_id(request.headers.get(HEADER_NAME))
        request.state.correlation_id = correlation_id     # for the unhandled-exception handler, which runs outside this middleware (call flow 14a)
        token = _current_correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            _current_correlation_id.reset(token)
        response.headers[HEADER_NAME] = correlation_id
        return response
