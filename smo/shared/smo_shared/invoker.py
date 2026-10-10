"""The caller's identity as R1 Termination vouches for it, and who a request is really for.

R1 Termination introspects every bearer token (RFC 7662) and, on success, forwards the token's `client_id` (the API invoker's id) in
`X-R1-Invoker-Id` and its role in `X-R1-Role`. Any value of those headers a caller sent itself is dropped first, so a backend behind R1 can trust
them. A request that did not come through R1 (unit tests, an in-process call) has none: `invoker_id` returns None.

**On behalf of.** An SMO module often acts for an rApp: the rApp posts an action to DME, DME writes the change at RAN NF OAM. Seen by RAN NF OAM that
call is DME's, so the per-rApp safeguards (the kill switch, the rate limit, the blast-radius and magnitude limits) would never apply to an rApp that
writes through DME. So the module says who it acts for: `X-R1-On-Behalf-Of`, the originator of the request it is handling, which `R1Client` adds to
every onward call by itself (as it does the correlation id). R1 Termination forwards the header only from an `internal` caller (an rApp's own value
is dropped, so an rApp cannot pose as another), and a backend reads the effective invoker with `invoker_id`:

  - the originator of a request is the rApp's invoker id when an rApp called, or the `X-R1-On-Behalf-Of` an internal module passed on (so the chain
    holds across several hops), or nobody when a module acts on its own account;
  - `invoker_id(request)` is that originator when an internal module is acting for one, otherwise the caller's own `X-R1-Invoker-Id`.

  get_originator()           the originator of the request being handled (None outside a request, or when nobody is being acted for)
  on_own_account()           a block in which the module acts for nobody: `R1Client` adds no `X-R1-On-Behalf-Of` (nor claim). For what the platform does about an rApp
                             rather than for it, such as setting the rApp's own limits when the rApp reports that it is up (a module acting "for" the rApp there would be
                             refused by RAN NF OAM's rule that a caller cannot change its own limit)
  apply_invoker_context(app) installs the middleware that records it; `apply_correlation_id` calls it, so every service already has it

**The person behind an operator's call.** The operator's console (GUI BFF) calls every module with one SMO token, so the invoker id of all its calls is the console's. For the
few decisions that must be a named person's (the two-person approval of an rApp's action, SEC-15.8) the console says who is signed in: `X-R1-Acting-User`
(`smo-gui:<username>`). R1 Termination forwards it only from an `internal` caller and drops any value another caller sent, as it does `X-R1-On-Behalf-Of`, so a module may read it with
`acting_user` when the role is `internal`. It is a different header from On-Behalf-Of because that one names an rApp and the safeguards (kill switch, limits, ownership, scope) key on it.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator, Mapping

from fastapi import FastAPI, Request

from .roles import ROLE_HEADER, ROLE_INTERNAL, ROLE_RAPP

INVOKER_ID_HEADER = "X-R1-Invoker-Id"
ON_BEHALF_OF_HEADER = "X-R1-On-Behalf-Of"
ACTING_USER_HEADER = "X-R1-Acting-User"

_current_originator: ContextVar[str | None] = ContextVar("_current_originator", default=None)


def originator_of(headers: Mapping[str, str]) -> str | None:
    """Who the request is for, from the headers R1 Termination stamped: an rApp's own id, or what an internal module passed on, or nobody."""
    role = headers.get(ROLE_HEADER)
    if role == ROLE_RAPP:
        return headers.get(INVOKER_ID_HEADER) or None
    if role == ROLE_INTERNAL:
        return headers.get(ON_BEHALF_OF_HEADER) or None
    return None


def invoker_id(request: Request) -> str | None:
    """The invoker the safeguards apply to: the rApp an internal module is acting for, else the caller's own id."""
    if request.headers.get(ROLE_HEADER) == ROLE_INTERNAL:
        behalf = request.headers.get(ON_BEHALF_OF_HEADER)
        if behalf:
            return behalf
    return request.headers.get(INVOKER_ID_HEADER) or None


def acting_user(request: Request) -> str | None:
    """The person the operator's console says it is acting for (`X-R1-Acting-User`), only when the caller's role is `internal`; None otherwise.

    R1 Termination forwards the header from an `internal` caller alone, so behind the gateway a value here is the console's. The role is checked again here so that a request
    that carries the header without the `internal` role (a test, a direct call) is not believed.
    """
    if request.headers.get(ROLE_HEADER) != ROLE_INTERNAL:
        return None
    return request.headers.get(ACTING_USER_HEADER, "").strip() or None


def get_originator() -> str | None:
    """The rApp the request being handled is for, from the context variable `apply_invoker_context`'s middleware sets; None outside a request or when
    nobody is being acted for.
    """
    return _current_originator.get()


@contextmanager
def on_own_account() -> Iterator[None]:
    """Context manager: inside the block `get_originator()` is None, so `R1Client` adds no `X-R1-On-Behalf-Of` to onward calls. The previous value is
    restored on exit, also when the block raises.
    """
    token = _current_originator.set(None)
    try:
        yield
    finally:
        _current_originator.reset(token)


def apply_invoker_context(app: FastAPI) -> None:
    """Installs the HTTP middleware that records `originator_of(request.headers)` in a context variable for the duration of each request and resets it
    afterwards.

    Called by `correlation.apply_correlation_id`, so a module does not call it itself. The context variable is what `R1Client` reads to stamp
    `X-R1-On-Behalf-Of` on calls made while handling the request.
    """
    @app.middleware("http")
    async def _invoker_context_middleware(request: Request, call_next):
        token = _current_originator.set(originator_of(request.headers))
        try:
            return await call_next(request)
        finally:
            _current_originator.reset(token)
