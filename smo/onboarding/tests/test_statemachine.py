"""The package state machine (`app/statemachine.py`) on its own: legal and illegal transitions and the two guards, without the web layer.

Covers Onboarding/rApp Mgmt LLD sections 3 and 4 and the priming stage (HISTORY.md §5). Fixtures: `db` (an in-memory SQLite with the package and usage tables and a stub NFO
table) and the helper `make_package`. Run: `cd smo/onboarding && PYTHONPATH=.:../shared python -m pytest tests/test_statemachine.py -q`.
"""

import uuid

import pytest
from sqlalchemy import Column, Table, Uuid, create_engine
from sqlalchemy.orm import Session

from smo_shared.db import Base
from smo_shared.statemachine import IllegalTransition

from app.models import ApplicationPackage, PackageUsageRegistration
from app.statemachine import ONBOARDING_FSM, PackageEvent, PackageState


@pytest.fixture
def db():
    """A SQLAlchemy session over an in-memory SQLite holding the package and usage tables, with a stub for NFO's descriptor table so the foreign key resolves.
    """
    engine = create_engine("sqlite://")
    # nf_deployment_descriptor lives in the nfo module, out of scope for this
    # test package — stand in a minimal table so ApplicationPackage's FK
    # resolves. Production runs against the full consolidated migration
    # (001_init.sql), same pattern as nfo/tests' application_package stub.
    if "nf_deployment_descriptor" not in Base.metadata.tables:
        Table("nf_deployment_descriptor", Base.metadata, Column("nf_deployment_descriptor_id", Uuid, primary_key=True))
    Base.metadata.create_all(engine, tables=[ApplicationPackage.__table__, PackageUsageRegistration.__table__])
    with Session(engine) as session:
        yield session


def make_package(db, state=PackageState.ONBOARDING, parent_id=None) -> ApplicationPackage:
    """Adds and flushes a package in `state` (optionally a child of `parent_id`) and returns it; nothing is committed."""
    pkg = ApplicationPackage(
        application_type="rApp", name="energy-saving", version="1.0",
        state=state, manifest_ref="s3://pkg.csar", parent_package_id=parent_id,
    )
    db.add(pkg)
    db.flush()
    return pkg


def test_onboard_success_reaches_available(db):
    """VALIDATE_OK takes ONBOARDING to AVAILABLE."""
    pkg = make_package(db)
    new_state = ONBOARDING_FSM.fire(PackageState(pkg.state), PackageEvent.VALIDATE_OK, db=db, package=pkg)
    assert new_state == PackageState.AVAILABLE


def test_onboard_failure_reaches_failed_not_available(db):
    """VALIDATE_FAILED takes ONBOARDING to FAILED."""
    pkg = make_package(db)
    new_state = ONBOARDING_FSM.fire(PackageState(pkg.state), PackageEvent.VALIDATE_FAILED, db=db, package=pkg)
    assert new_state == PackageState.FAILED


def test_deprecate_then_cancel_delete_returns_to_available(db):
    """DEPRECATE and then CANCEL_DELETE bring an AVAILABLE package back to AVAILABLE."""
    pkg = make_package(db, state=PackageState.AVAILABLE)
    s = ONBOARDING_FSM.fire(PackageState.AVAILABLE, PackageEvent.DEPRECATE, db=db, package=pkg)
    assert s == PackageState.DEPRECATED
    s = ONBOARDING_FSM.fire(s, PackageEvent.CANCEL_DELETE, db=db, package=pkg)
    assert s == PackageState.AVAILABLE


def test_delete_blocked_by_available_child(db):
    """The cascade-delete guard (LLD section 4): a package with an AVAILABLE child cannot be deleted."""
    parent = make_package(db, state=PackageState.DEPRECATED)
    make_package(db, state=PackageState.AVAILABLE, parent_id=parent.package_id)

    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.DEPRECATED, PackageEvent.DELETE, db=db, package=parent)


def test_delete_blocked_by_active_usage_registration(db):
    """A usage registration with no stopped_at blocks DELETE."""
    pkg = make_package(db, state=PackageState.DEPRECATED)
    db.add(PackageUsageRegistration(package_id=pkg.package_id, consumer_id="some-rapp", stopped_at=None))
    db.flush()

    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.DEPRECATED, PackageEvent.DELETE, db=db, package=pkg)


def test_delete_allowed_once_usage_stopped(db):
    """A stopped registration no longer blocks DELETE, which then reaches DELETING."""
    import datetime

    pkg = make_package(db, state=PackageState.DEPRECATED)
    db.add(PackageUsageRegistration(
        package_id=pkg.package_id, consumer_id="some-rapp",
        stopped_at=datetime.datetime.now(datetime.UTC),
    ))
    db.flush()

    new_state = ONBOARDING_FSM.fire(PackageState.DEPRECATED, PackageEvent.DELETE, db=db, package=pkg)
    assert new_state == PackageState.DELETING


def test_no_transition_from_failed(db):
    """FAILED is terminal in the table: no event leaves it (the route deletes a FAILED package directly)."""
    pkg = make_package(db, state=PackageState.FAILED)
    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.FAILED, PackageEvent.VALIDATE_OK, db=db, package=pkg)


def test_prime_then_deprime_round_trip(db):
    """AVAILABLE -> PRIMING -> PRIMED -> DEPRIMING -> AVAILABLE through PRIME, PRIME_COMPLETE, DEPRIME and DEPRIME_COMPLETE (HISTORY.md §5)."""
    pkg = make_package(db, state=PackageState.AVAILABLE)
    s = ONBOARDING_FSM.fire(PackageState.AVAILABLE, PackageEvent.PRIME, db=db, package=pkg)
    assert s == PackageState.PRIMING
    s = ONBOARDING_FSM.fire(s, PackageEvent.PRIME_COMPLETE, db=db, package=pkg)
    assert s == PackageState.PRIMED

    s = ONBOARDING_FSM.fire(s, PackageEvent.DEPRIME, db=db, package=pkg)
    assert s == PackageState.DEPRIMING
    s = ONBOARDING_FSM.fire(s, PackageEvent.DEPRIME_COMPLETE, db=db, package=pkg)
    assert s == PackageState.AVAILABLE


def test_prime_from_onboarding_is_illegal(db):
    """PRIME has an edge only from AVAILABLE, so a package that never reached AVAILABLE cannot be primed."""
    pkg = make_package(db, state=PackageState.ONBOARDING)
    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.ONBOARDING, PackageEvent.PRIME, db=db, package=pkg)


def test_deprime_blocked_by_active_usage_registration(db):
    """The reference's deprimeRapp guard: DEPRIME is refused while a usage registration is open."""
    pkg = make_package(db, state=PackageState.PRIMED)
    db.add(PackageUsageRegistration(package_id=pkg.package_id, consumer_id="some-rapp", stopped_at=None))
    db.flush()

    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.PRIMED, PackageEvent.DEPRIME, db=db, package=pkg)


def test_delete_has_no_edge_from_primed(db):
    """DELETE has no edge from PRIMED: a primed package must be deprimed first, as the reference requires."""
    pkg = make_package(db, state=PackageState.PRIMED)
    with pytest.raises(IllegalTransition):
        ONBOARDING_FSM.fire(PackageState.PRIMED, PackageEvent.DELETE, db=db, package=pkg)


def test_primed_package_can_be_deprecated_without_a_guard(db):
    """DOC-CF-1.2: DEPRECATE has an edge from PRIMED with no guard, so an open usage registration does not block it; CANCEL_DELETE then returns AVAILABLE."""
    pkg = make_package(db, state=PackageState.PRIMED)
    db.add(PackageUsageRegistration(package_id=pkg.package_id, consumer_id="instance-1"))
    db.flush()

    state = ONBOARDING_FSM.fire(PackageState.PRIMED, PackageEvent.DEPRECATE, db=db, package=pkg)

    assert state == PackageState.DEPRECATED
    assert ONBOARDING_FSM.fire(state, PackageEvent.CANCEL_DELETE, db=db, package=pkg) == PackageState.AVAILABLE
