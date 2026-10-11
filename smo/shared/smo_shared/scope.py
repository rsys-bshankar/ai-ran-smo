"""Tenant and region authorization (PR-SEC-10, docs/adr/0005-tenant-region-authorization.md): which targets a caller may touch.

A caller may carry a **scope claim**, set when its invoker is registered at SME (or edited by an operator): `{"regions": [...], "tenants": [...]}`. A target
(a managed element) carries a `region` and a `tenant` of its own. The claim is the gateway's to vouch for, the decision is the module's that owns the target:

  - SME returns the claim in the token introspection (`authz_scope`); R1 Termination forwards it as `X-R1-Scope` (JSON), dropping any value a caller sent;
  - an SMO module acting for an rApp passes the rApp's claim on in `X-R1-On-Behalf-Scope` (`R1Client` adds it by itself, like `X-R1-On-Behalf-Of`); R1 Termination
    forwards that header only from an `internal` caller;
  - the operator's console (the GUI backend, an `internal` caller) passes the signed-in person's claim in `X-R1-Acting-User-Scope` beside `X-R1-Acting-User` (GUI-5); R1
    Termination forwards it only from an `internal` caller and only with the acting user, and it can only narrow what the console's own claim allows;
  - the module that owns the data calls `scope_of(request.headers)` and then `permits(...)` / `filter_statement(...)`, so every module decides the same way.

The semantics (one rule, no exceptions):

  - **no claim** (`None`): unscoped. Everything is permitted, exactly as before this existed. An SMO module on its own account is unscoped, and so is a person of the
    operator's GUI whose account carries no claim.
  - **a claim**: every axis it names must match. `regions` names the regions the caller may touch, `tenants` the tenants; an axis the claim leaves out is not restricted.
    A target is permitted when, for each restricted axis, its value is set and is one of the listed. Matching is exact and case-sensitive (no wildcards, no hierarchy).
  - **a target with no region (or tenant)** is therefore not permitted to a caller that restricts that axis; only an unscoped caller sees it.
  - **a claim that cannot be read** (a malformed header) permits nothing (`DENY_ALL`): a restriction must never be lost because it was damaged.

An empty list is not a valid claim (it would be a caller that may touch nothing: stop the rApp with the kill switch instead), and `{}` is no claim.
"""

import json
import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import false, or_, true
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from .invoker import ACTING_USER_HEADER, ON_BEHALF_OF_HEADER, originator_of
from .roles import ROLE_HEADER, ROLE_INTERNAL

SCOPE_HEADER = "X-R1-Scope"
ON_BEHALF_SCOPE_HEADER = "X-R1-On-Behalf-Scope"
ACTING_USER_SCOPE_HEADER = "X-R1-Acting-User-Scope"       # GUI-5: the claim of the person the console acts for (`X-R1-Acting-User`)

AXES = ("regions", "tenants")
MAX_VALUES = 100                       # per axis
VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")


@dataclass(frozen=True)
class Scope:
    """A caller's claim: `None` on an axis does not restrict it; a set restricts the caller to those values."""
    regions: frozenset[str] | None = None
    tenants: frozenset[str] | None = None


DENY_ALL = Scope(regions=frozenset(), tenants=None)       # permits nothing: not even a target without a region


def valid_value(value: Any) -> bool:
    """Whether `value` may be a region or a tenant (a target's, or one in a claim)."""
    return isinstance(value, str) and VALUE_PATTERN.fullmatch(value) is not None


def from_claim(claim: Any) -> Scope | None:
    """A claim as received (a JSON object) to a `Scope`, or `None` for no claim (`None` or `{}`). `ValueError` (with a fixed message, never the input) for one
    that is not valid: an unknown key, an axis that is not a non-empty list of valid values without duplicates."""
    if claim is None:
        return None
    if not isinstance(claim, Mapping):
        raise ValueError("the scope claim must be an object with regions and/or tenants")
    unknown = set(claim) - set(AXES)
    if unknown:
        raise ValueError("the scope claim may only name regions and tenants")
    axes: dict[str, frozenset[str]] = {}
    for axis in AXES:
        values = claim.get(axis)
        if values is None:
            continue
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
            raise ValueError(f"{axis} must be a list of 1 to {MAX_VALUES} values")
        if not all(valid_value(v) for v in values):
            raise ValueError(f"every value of {axis} must be 1 to 100 characters of letters, digits and . _ : / @ + -, starting with a letter or digit")
        if len(set(values)) != len(values):
            raise ValueError(f"{axis} lists a value twice")
        axes[axis] = frozenset(values)
    return Scope(**axes) if axes else None


def from_introspection(claim: Any) -> Scope | None:
    """The claim SME answered with: read like `from_claim`, but one that is not valid (it cannot come from SME's own validation; a damaged store, a different
    release) permits nothing instead of raising, because a restriction is never dropped."""
    try:
        return from_claim(claim)
    except ValueError:
        return DENY_ALL


def to_claim(scope: Scope | None) -> dict[str, list[str]] | None:
    """The claim to store, return or forward: sorted lists, only the axes that restrict; `None` for no claim."""
    if scope is None:
        return None
    claim = {axis: sorted(values) for axis in AXES if (values := getattr(scope, axis)) is not None}
    return claim or None


def encode(scope: Scope | None) -> str | None:
    """The header value for `scope` (compact JSON), or `None` when there is nothing to say."""
    claim = to_claim(scope)
    return None if claim is None else json.dumps(claim, sort_keys=True, separators=(",", ":"))


def decode(value: str | None) -> Scope | None:
    """The scope a header carries. No header: no claim. A header that is not a valid claim: `DENY_ALL` (a restriction is never dropped because it was damaged)."""
    if value is None or value == "":
        return None
    try:
        return from_claim(json.loads(value))
    except (ValueError, RecursionError):
        return DENY_ALL


def narrowed(first: Scope | None, second: Scope | None) -> Scope | None:
    """What both claims permit: per axis, the values both list (an axis one of them leaves out is the other's). `None` only when neither restricts. Two claims that
    share no value on an axis give an empty set there, which permits nothing."""
    if first is None:
        return second
    if second is None:
        return first
    return Scope(regions=_both(first.regions, second.regions), tenants=_both(first.tenants, second.tenants))


def _both(first: frozenset[str] | None, second: frozenset[str] | None) -> frozenset[str] | None:
    """One axis of `narrowed`: the values of both, or the one that restricts, or `None` when neither does."""
    if first is None:
        return second
    if second is None:
        return first
    return first & second


def scope_of(headers: Mapping[str, str]) -> Scope | None:
    """The scope that applies to the request being handled, by the same rule as `invoker_id`: the rApp an SMO module acts for (the claim it passed on), else the
    caller's own claim. GUI-5: for an `internal` caller acting for a person (`X-R1-Acting-User`), the caller's own claim narrowed by that person's
    (`X-R1-Acting-User-Scope`; absent: the person is unscoped). `None`: unscoped."""
    internal = headers.get(ROLE_HEADER) == ROLE_INTERNAL
    if internal and headers.get(ON_BEHALF_OF_HEADER):
        return decode(headers.get(ON_BEHALF_SCOPE_HEADER))
    own = decode(headers.get(SCOPE_HEADER))
    if internal and headers.get(ACTING_USER_HEADER):
        return narrowed(own, decode(headers.get(ACTING_USER_SCOPE_HEADER)))
    return own


def request_scope(request: Request) -> Scope | None:
    return scope_of(request.headers)


def permits(scope: Scope | None, region: str | None, tenant: str | None) -> bool:
    """Whether a caller with `scope` may touch a target in `region` and `tenant` (either may be `None`: not set)."""
    if scope is None:
        return True
    if scope.regions is not None and (region is None or region not in scope.regions):
        return False
    return not (scope.tenants is not None and (tenant is None or tenant not in scope.tenants))


def covers(parent: Scope | None, child: Scope | None) -> bool:
    """Whether `parent` permits everything `child` permits (so a caller with `parent` may hand out `child`, never more than it has itself): an unscoped parent
    covers any child; a parent that restricts an axis is covered only by a child that restricts it to a subset."""
    if parent is None:
        return True
    for axis in AXES:
        allowed = getattr(parent, axis)
        if allowed is None:
            continue
        wanted = getattr(child, axis) if child is not None else None
        if wanted is None or not wanted <= allowed:
            return False
    return True


def filter_statement(stmt: Select, scope: Scope | None, region_column: ColumnElement, tenant_column: ColumnElement) -> Select:
    """`stmt` limited to the rows `permits` would allow: nothing is added for an unscoped caller. A NULL column never matches `IN`, so a target without a
    region or tenant drops out of the result for a caller that restricts that axis, as `permits` says."""
    if scope is None:
        return stmt
    if scope.regions is not None:
        stmt = stmt.where(region_column.in_(sorted(scope.regions)) if scope.regions else false())
    if scope.tenants is not None:
        stmt = stmt.where(tenant_column.in_(sorted(scope.tenants)) if scope.tenants else false())
    return stmt


def denied_condition(scope: Scope | None, region_column: ColumnElement, tenant_column: ColumnElement) -> ColumnElement[bool]:
    """The rows `permits` would refuse, as a condition that is true or false for every row (never NULL: a NULL region or tenant is a refusal, written out
    because `NOT (region IN (...))` is NULL for it and would let it through). For a subquery that asks "is there a target in this list that the caller may not touch"."""
    if scope is None:
        return false()
    parts: list[ColumnElement[bool]] = []
    if scope.regions is not None:
        parts.append(region_column.is_(None) | region_column.not_in(sorted(scope.regions)) if scope.regions else true())
    if scope.tenants is not None:
        parts.append(tenant_column.is_(None) | tenant_column.not_in(sorted(scope.tenants)) if scope.tenants else true())
    return or_(false(), *parts)


# ---- what an SMO module acting for an rApp passes on (R1Client reads it)

_current_originator_scope: ContextVar[Scope | None] = ContextVar("_current_originator_scope", default=None)


def get_originator_scope() -> Scope | None:
    """The claim of the originator of the request being handled (None outside a request, or when it is unscoped or nobody is being acted for)."""
    return _current_originator_scope.get()


def apply_scope_context(app: FastAPI) -> None:
    """Records, for the request being handled, the claim of whoever it is for, so `R1Client` can pass it on with `X-R1-On-Behalf-Of`. Installed by `apply_correlation_id`."""
    @app.middleware("http")
    async def _scope_context_middleware(request: Request, call_next):
        scope = scope_of(request.headers) if originator_of(request.headers) else None
        token = _current_originator_scope.set(scope)
        try:
            return await call_next(request)
        finally:
            _current_originator_scope.reset(token)
