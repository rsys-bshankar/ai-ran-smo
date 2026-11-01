"""The cross-cutting checks every ProvMnS operation passes inside the router (plan task S1.5), each a pluggable step, in this order:

  1. caller and scope   who is asking (the identity, role and scope claim R1 Termination vouches for in its headers; `Caller`) and whether the claim
                        reaches the target DN (`ScopeStep`: a `Placement` says where a DN is, an `Authorizer` decides). ADR 0005: a scoped caller may
                        touch a target only when every axis its claim restricts (`regions`, `tenants`, `siteClusters`, `subNetworks`, D15) matches.
                        Authentication itself stays at the gateway (ADR 0005 section 1); the module reads what it vouched for and never trusts a header
                        the gateway did not set (R1 Termination drops a caller's own).
  2. MSAC               TS 28.319 access rules for the caller (`MsacStep`): a provider returns the caller's rules, `smo_shared.msac.decide` applies
                        them; a caller the provider does not manage (None) passes.
  3. idempotency        createMOI with an `Idempotency-Key` replays the first answer (`smo_shared.idempotency.run_idempotent`; in router.py).
  4. audit              every committed write adds a row to the hash-chained audit trail in its own transaction (`smo_shared.audit.record`).
  5. traceparent        the notifications the write publishes keep the request's W3C trace (`smo_event.traceparent`, revision 0067; router.py sets the
                        trace from the request's header when no middleware did).
  6. metric             `smo_provmns_http_duration_seconds{module, class, operation, outcome}` around the whole operation (router.py).

Refusals follow ADR 0005 section 5. A read of a DN outside the caller's reach answers 404 MOI_NOT_FOUND, exactly as for a DN that does not exist (a 403
would tell the caller the DN exists); a write answers 403 SCOPE_DENIED (or MSAC_ACCESS_DENIED), the caller having named the target; the instances a
scoped read reaches below the base are filtered, never refused. Scope is checked before anything else of the request, so a refusal says nothing about
the class, the body or the version.

How S1.14 replaces the decision without touching the router: `Authorizer` is the one interface the scope step calls (`permits(caller, operation, target)`).
`ClaimAuthorizer`, the default, applies the ADR 0005 claim (`smo_shared.scope.permits`). An `AccessRuleAuthorizer` that reads TS 28.319 Identity, Role and
AccessRule (the ADR 0005 claim expressed as AccessRules) implements the same method and is passed as `RouterChecks(scope=ScopeStep(authorizer=...))` by the
module; the router, the service and the other steps stay as they are. `Placement` (where a DN is: region, tenant, site cluster, SubNetworks) is
separate, so the same placement serves either decision.
"""

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from prometheus_client import Histogram
from sqlalchemy.orm import Session

from .. import audit, msac
from .. import scope as claims
from ..correlation import get_correlation_id
from ..invoker import ACTING_USER_HEADER, INVOKER_ID_HEADER, ON_BEHALF_OF_HEADER
from ..roles import ROLE_HEADER, ROLE_INTERNAL
from .dn import parse_dn, path_of
from .errors import MnsError

OPERATIONS = {"createMOI": "create", "getMOIAttributes": "read", "modifyMOIAttributes": "update", "deleteMOI": "delete"}   # ProvMnS -> MSAC
METHODS = {"createMOI": "PUT", "getMOIAttributes": "GET", "modifyMOIAttributes": "PATCH", "deleteMOI": "DELETE"}
DURATION = Histogram("smo_provmns_http_duration_seconds", "ProvMnS operations, by module, class, operation and outcome (PR-CONV-S1.5).",
                     ["module", "class", "operation", "outcome"])


# ---------------------------------------------------------------------------- who is asking

@dataclass(frozen=True)
class Caller:
    """The caller of one operation, as R1 Termination vouched for it: `invoker` (the rApp an internal module acts for, else the caller's own id), `role`,
    `acting_user` (the person the console acts for, from an internal caller only), `scope` (the ADR 0005 claim; None: unscoped)."""

    invoker: str | None = None
    role: str | None = None
    acting_user: str | None = None
    scope: claims.Scope | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "Caller":
        """The caller of a request, read from the gateway's headers (`invoker.invoker_id`, `invoker.acting_user` and `scope.scope_of` say the same)."""
        role = headers.get(ROLE_HEADER) or None
        internal = role == ROLE_INTERNAL
        invoker = (headers.get(ON_BEHALF_OF_HEADER) if internal else None) or headers.get(INVOKER_ID_HEADER) or None
        acting = headers.get(ACTING_USER_HEADER, "").strip() if internal else ""
        return cls(invoker, role, acting or None, claims.scope_of(headers))

    @property
    def actor(self) -> str:
        """Who the audit row names: the person, else the invoker, else `unauthenticated` (a call that did not come through the gateway)."""
        return self.acting_user or self.invoker or "unauthenticated"


INTERNAL = Caller(invoker=None, role=ROLE_INTERNAL)        # a module calling its own producer in-process: unscoped, unmanaged


# ---------------------------------------------------------------------------- 1. scope

@dataclass(frozen=True)
class Target:
    """Where a DN is, for a scope decision: its region, tenant and site cluster (None: not placed) and the ids of the SubNetworks it lies under."""

    dn: str
    region: str | None = None
    tenant: str | None = None
    site_cluster: str | None = None
    sub_networks: tuple[str, ...] = ()


def sub_networks_of(dn: str) -> tuple[str, ...]:
    """The ids of every SubNetwork RDN of `dn`, outermost first (the D15 axis comes from the DN itself)."""
    return tuple(rdn.moi_id for rdn in parse_dn(dn) if rdn.class_name == "SubNetwork")


class Placement(Protocol):
    """Where a DN is (region, tenant, site cluster); the SubNetworks always come from the DN."""

    def __call__(self, dn: str) -> Target: ...


def no_placement(dn: str) -> Target:
    """The default placement: no region, tenant or site cluster (a caller that restricts one of them reaches nothing); SubNetworks from the DN."""
    return Target(dn, sub_networks=sub_networks_of(dn))


@dataclass(frozen=True)
class PrefixPlacement:
    """A placement by DN prefix: `{"SubNetwork=BLR": {"region": "south", "tenant": "acme", "siteCluster": "blr-1"}, ...}`. A DN takes the values of the
    longest prefix that is itself or one of its ancestors (a rule for `SubNetwork=BLR` covers `SubNetwork=BLR,ManagedElement=1`, not `SubNetwork=BLR2`);
    a DN no rule covers is not placed. A rule may set any of the three values."""

    rules: Mapping[str, Mapping[str, str]]

    def __call__(self, dn: str) -> Target:
        """The target of `dn` under these rules."""
        rdns = [str(rdn) for rdn in parse_dn(dn)]
        for length in range(len(rdns), 0, -1):
            rule = self.rules.get(",".join(rdns[:length]))
            if rule is not None:
                return Target(dn, rule.get("region"), rule.get("tenant"), rule.get("siteCluster"), sub_networks_of(dn))
        return no_placement(dn)


class Authorizer(Protocol):
    """The decision of the scope step: may `caller` do `operation` (`createMOI`, ...) on `target`? S1.14 implements it with TS 28.319 AccessRules."""

    def permits(self, caller: Caller, operation: str, target: Target) -> bool: ...


class ClaimAuthorizer:
    """The ADR 0005 decision: the caller's claim against the target's placement (`smo_shared.scope.permits`), whatever the operation."""

    def permits(self, caller: Caller, operation: str, target: Target) -> bool:
        """Whether the claim reaches the target."""
        return claims.permits(caller.scope, target.region, target.tenant, target.site_cluster, target.sub_networks)


@dataclass
class ScopeStep:
    """Step 1: the target must be within the caller's reach (see the module description for the refusals)."""

    placement: Callable[[str], Target] = no_placement
    authorizer: Authorizer = field(default_factory=ClaimAuthorizer)

    def allows(self, caller: Caller, operation: str, dn: str) -> bool:
        """Whether `caller` may do `operation` on `dn`."""
        return self.authorizer.permits(caller, operation, self.placement(dn))

    def check(self, caller: Caller, operation: str, dn: str) -> None:
        """Refuses a target outside the caller's reach: 404 for a read (as if it did not exist), 403 SCOPE_DENIED for a write."""
        if self.allows(caller, operation, dn):
            return
        if operation == "getMOIAttributes":
            raise MnsError(404, "MOI_NOT_FOUND", f"{dn} does not exist", bad_objects=[dn])
        raise MnsError(403, "SCOPE_DENIED", f"{dn} is outside the caller's scope", bad_objects=[dn])


# ---------------------------------------------------------------------------- 2. MSAC

class MsacProvider(Protocol):
    """The MSAC rules that apply to a caller, or None when the caller is not managed by MSAC (it then passes)."""

    def __call__(self, caller: Caller) -> Iterable[msac.Rule] | None: ...


def unmanaged(caller: Caller) -> None:
    """The default provider: nobody is managed (MSAC passes every caller)."""
    return None


@dataclass
class MsacStep:
    """Step 2: TS 28.319 MSAC on the target's DN path (`/Class=id/...`), with `smo_shared.msac.decide` (DENY beats ALLOW, no matching rule refuses)."""

    provider: Callable[[Caller], Iterable[msac.Rule] | None] = unmanaged

    def allows(self, caller: Caller, operation: str, dn: str) -> bool:
        """Whether MSAC lets `caller` do `operation` on `dn` (always, for a caller the provider does not manage)."""
        rules = self.provider(caller)
        return rules is None or msac.decide(rules, "/" + path_of(dn), OPERATIONS[operation])

    def check(self, caller: Caller, operation: str, dn: str) -> None:
        """Refuses what the rules do not allow: 404 for a read (as for scope), 403 MSAC_ACCESS_DENIED for a write."""
        if self.allows(caller, operation, dn):
            return
        if operation == "getMOIAttributes":
            raise MnsError(404, "MOI_NOT_FOUND", f"{dn} does not exist", bad_objects=[dn])
        raise MnsError(403, "MSAC_ACCESS_DENIED", f"no access rule allows {OPERATIONS[operation]} on {dn}", bad_objects=[dn])


# ---------------------------------------------------------------------------- the steps together

@dataclass
class RouterChecks:
    """The steps a producer applies (`ProvMnS(checks=...)`). `module` labels the metric; `audit` turns the audit step off (a module whose writes are
    audited elsewhere; the default is on). Idempotency, the trace and the metric are the router's (router.py)."""

    module: str = "unknown"
    scope: ScopeStep = field(default_factory=ScopeStep)
    msac: MsacStep = field(default_factory=MsacStep)
    audit: bool = True

    def authorize(self, caller: Caller, operation: str, dn: str) -> None:
        """Steps 1 and 2 for the target of an operation, in that order."""
        self.scope.check(caller, operation, dn)
        self.msac.check(caller, operation, dn)

    def visible(self, caller: Caller, dn: str) -> bool:
        """Whether a read may show the instance at `dn` (an instance below a read's base that steps 1 or 2 refuse is left out of the answer)."""
        return self.scope.allows(caller, "getMOIAttributes", dn) and self.msac.allows(caller, "getMOIAttributes", dn)

    def record(self, db: Session, caller: Caller, operation: str, dn: str, status: int, detail: dict) -> None:
        """Step 4: one audit row for a write, in `db`'s transaction (it commits, or rolls back, with the change)."""
        if not self.audit:
            return
        audit.record(db, actor=caller.actor, role=caller.role, action=METHODS[operation], target=f"/ProvMnS/{path_of(dn)}", result=str(status),
                     correlation_id=get_correlation_id(), detail={"operation": operation, "dn": dn, **detail})

    def observe(self, class_name: str, operation: str, status: int, started: float) -> None:
        """Step 6: the operation's duration under its class (`other` for one the producer does not serve), operation and outcome (`2xx` ... `5xx`)."""
        DURATION.labels(self.module, class_name, operation, f"{status // 100}xx").observe(time.perf_counter() - started)
