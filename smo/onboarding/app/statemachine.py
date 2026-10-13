"""The ApplicationPackage lifecycle: its states, its events, and the transition table `ONBOARDING_FSM`.

What it is: the table that decides which package events are legal from which state, and the two guards behind the cascade-delete and deprime rules. It
implements SMO Design v1.3 section 3.4 (AVAILABLE <-> DEPRECATED -> deleted), extended by Onboarding/rApp Mgmt LLD section 3 (the FAILED terminal state) and
section 4 (the cascade-delete guard as an actual query), and by the package priming stage of HISTORY.md §5.

Where it sits: `app/main.py` fires events on this table (`ONBOARDING_FSM.fire`, `_fire`) and stores the returned state; the generic engine is
`smo_shared/statemachine.py`. The table holds no per-package state.

What it owns: the allowed transitions and the guards. It does not write the package row, commit, or map a refusal to HTTP (`main._fire` does). A direct delete
of a FAILED package is not an edge here: the route handles it without the FSM.

Before editing: README section 2.3 lists the same table; change both together. A guard receives the keyword arguments `db` and `package` that `fire` is called with.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.statemachine import StateMachine

from .models import ApplicationPackage, PackageUsageRegistration


class PackageState(StrEnum):
    """The lifecycle states of an application package, stored as strings in `application_package.state`.

    PRIMING and DEPRIMING are transitional: the prime and deprime routes fire both of their events in one request, so neither is ever committed or observable. FAILED and
    DELETING are terminal (DELETING keeps the row).
    """
    ONBOARDING = "ONBOARDING"
    AVAILABLE = "AVAILABLE"
    PRIMING = "PRIMING"
    PRIMED = "PRIMED"
    DEPRIMING = "DEPRIMING"
    DEPRECATED = "DEPRECATED"
    DELETING = "DELETING"
    FAILED = "FAILED"


class PackageEvent(StrEnum):
    """The events that move a package between states; `ONBOARDING_FSM` says which are legal from which state.

    VALIDATE_OK and VALIDATE_FAILED come from the onboarding pipeline, the others from the lifecycle routes. PRIME_COMPLETE and DEPRIME_COMPLETE are fired by the routes
    right after PRIME and DEPRIME.
    """
    VALIDATE_OK = "VALIDATE_OK"
    VALIDATE_FAILED = "VALIDATE_FAILED"
    PRIME = "PRIME"
    PRIME_COMPLETE = "PRIME_COMPLETE"
    DEPRIME = "DEPRIME"
    DEPRIME_COMPLETE = "DEPRIME_COMPLETE"
    DEPRECATE = "DEPRECATE"
    CANCEL_DELETE = "CANCEL_DELETE"
    DELETE = "DELETE"


def _no_blocking_dependents(db: Session, package: ApplicationPackage) -> bool:
    """Onboarding/rApp Mgmt LLD section 4 — the actual cascade-delete query,
    not just the stated rule: blocked if any child package is
    AVAILABLE/DEPRECATED, or any usage registration has no stopped_at.
    """
    # Two queries, both `LIMIT 1`: a child package (parent_package_id = this package) in AVAILABLE or DEPRECATED, then an open usage registration. No route sets
    # parent_package_id today, so in practice the second query decides.
    blocking_child = db.scalar(
        select(ApplicationPackage).where(
            ApplicationPackage.parent_package_id == package.package_id,
            ApplicationPackage.state.in_([PackageState.AVAILABLE, PackageState.DEPRECATED]),
        ).limit(1)
    )
    if blocking_child is not None:
        return False
    active_usage = db.scalar(
        select(PackageUsageRegistration).where(
            PackageUsageRegistration.package_id == package.package_id,
            PackageUsageRegistration.stopped_at.is_(None),
        ).limit(1)
    )
    return active_usage is None


def _no_active_instances(db: Session, package: ApplicationPackage) -> bool:
    """Guard of DEPRIME: true when no usage registration of the package is open (stopped_at is null).

    The reference's deprimeRapp guard ('Unable to deprime as there are active rapp instances.'). It reuses the usage-registration signal of the cascade-delete guard:
    rApp Management calls usage/start on CreateInstance and usage/stop on TerminateInstance (HISTORY.md §2, cascade-delete-guard fix).
    """
    active_usage = db.scalar(
        select(PackageUsageRegistration).where(
            PackageUsageRegistration.package_id == package.package_id,
            PackageUsageRegistration.stopped_at.is_(None),
        ).limit(1)
    )
    return active_usage is None


def build_onboarding_fsm() -> StateMachine[PackageState, PackageEvent]:
    """Builds the package transition table (README section 2.3); `ONBOARDING_FSM` below is the one instance every route uses.

    Edges: ONBOARDING -> AVAILABLE or FAILED; AVAILABLE -> PRIMING -> PRIMED; PRIMED -> DEPRIMING (guarded) -> AVAILABLE; AVAILABLE or PRIMED -> DEPRECATED -> AVAILABLE (cancel);
    AVAILABLE or DEPRECATED -> DELETING (guarded). Every other (state, event) pair is illegal.
    """
    fsm: StateMachine[PackageState, PackageEvent] = StateMachine()
    fsm.add(PackageState.ONBOARDING, PackageEvent.VALIDATE_OK, PackageState.AVAILABLE)
    fsm.add(PackageState.ONBOARDING, PackageEvent.VALIDATE_FAILED, PackageState.FAILED)
    # HISTORY.md §5: the missing package-level priming stage
    # (COMMISSIONED->PRIMING->PRIMED->DEPRIMING in the reference; our
    # AVAILABLE plays the COMMISSIONED role). Real ACM/DME/SME resource
    # pre-provisioning behind PRIME stays out of scope — same elision as
    # the rest of this build's southbound calls — so PRIME/DEPRIME both
    # complete synchronously within one request rather than staying
    # observably PRIMING/DEPRIMING. CreateInstance's own AVAILABLE gate
    # (rapp-mgmt/app/main.py, D-SEC-RAPP-1) is an explicit, already-
    # confirmed design decision and is deliberately left unchanged here
    # — this only closes the lifecycle-and-blocking gap, not that one.
    fsm.add(PackageState.AVAILABLE, PackageEvent.PRIME, PackageState.PRIMING)
    fsm.add(PackageState.PRIMING, PackageEvent.PRIME_COMPLETE, PackageState.PRIMED)
    fsm.add(
        PackageState.PRIMED,
        PackageEvent.DEPRIME,
        PackageState.DEPRIMING,
        # `fire` passes its keyword context (db, package) to a guard; the guard lambdas of this table (this one and the two DELETE ones) keep db and package and ignore any other keyword.
        guard=lambda db, package, **_: _no_active_instances(db, package),
    )
    fsm.add(PackageState.DEPRIMING, PackageEvent.DEPRIME_COMPLETE, PackageState.AVAILABLE)
    fsm.add(PackageState.AVAILABLE, PackageEvent.DEPRECATE, PackageState.DEPRECATED)
    # DOC-CF-1.2 (call flow 06b): a PRIMED package can be deprecated too, without depriming, which is refused while instances run: deprecation only stops
    # new instances. No guard: running instances keep running. Priming provisions nothing in Phase 1, so DEPRECATED does not remember it: CANCEL_DELETE
    # returns AVAILABLE (prime again if wanted), and DELETE from DEPRECATED needs no deprime first.
    fsm.add(PackageState.PRIMED, PackageEvent.DEPRECATE, PackageState.DEPRECATED)
    fsm.add(PackageState.DEPRECATED, PackageEvent.CANCEL_DELETE, PackageState.AVAILABLE)
    fsm.add(
        PackageState.DEPRECATED,
        PackageEvent.DELETE,
        PackageState.DELETING,
        guard=lambda db, package, **_: _no_blocking_dependents(db, package),
    )
    fsm.add(
        PackageState.AVAILABLE,
        PackageEvent.DELETE,
        PackageState.DELETING,
        guard=lambda db, package, **_: _no_blocking_dependents(db, package),
    )
    # FAILED is terminal: nothing could depend on a package that never reached
    # AVAILABLE, so DeletePackage from FAILED skips the cascade check entirely
    # (Onboarding/rApp Mgmt LLD section 3) and is handled as a direct delete
    # by the caller, not through this FSM.
    return fsm


ONBOARDING_FSM = build_onboarding_fsm()
