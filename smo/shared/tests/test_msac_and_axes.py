"""The TS 28.319 MSAC decision shared by RAN NF OAM and the ProvMnS router (smo_shared/msac.py), and the two scope axes ADR 0005 gained for D15
(`siteClusters`, `subNetworks`; smo_shared/scope.py), PR-CONV-S1.5.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_msac_and_axes.py -q
"""

import pytest
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from smo_shared import msac, scope
from smo_shared.msac import Rule
from smo_shared.scope import Scope


def rule(selector, operations, action="ALLOW"):
    """Helper: a Rule from a selector, an iterable of operations and an action."""
    return Rule(selector, frozenset(operations), action)


# ---- msac

def test_the_closed_lists_are_the_ts_28_319_ones():
    """The operations and actions are exactly TS 28.319's."""
    assert msac.OPERATIONS == ("create", "read", "update", "delete", "exec")
    assert msac.ACTIONS == ("ALLOW", "DENY")


# Table: selectors inside and outside the Jex subset this build evaluates.
@pytest.mark.parametrize(("selector", "supported"), [
    ("/", False), ("", False), ("SubNetwork=A", False), ("/SubNetwork=A", True), ("/SubNetwork=A/", True), ("/SubNetwork=A/*", True),
    ("/*", True), ("/SubNetwork=*/ManagedElement=1", True), ("/SubNetwork=A//ManagedElement=1", False), ("/SubNetwork=A[@x]", False),
    ("/SubNetwork=A/ManagedElement=1'", False), ("/1Class=A", False), ("/SubNetwork=", False), ("/SubNetwork=A@b", False), ('/S="x"', False), ("/SubNetwork=A/x[y]/", False),
])
def test_selector_supported(selector, supported):
    """Only `/Class=id` segments or `*`, an optional trailing `/`, are in the subset."""
    assert msac.selector_supported(selector) is supported


# Table: what a selector covers.
@pytest.mark.parametrize(("selector", "target", "selected"), [
    ("/", "/SubNetwork=A", True),
    ("/SubNetwork=A", "/SubNetwork=A", True),
    ("/SubNetwork=A/", "/SubNetwork=A", True),
    ("/SubNetwork=A", "/SubNetwork=A/ManagedElement=1", False),
    ("/SubNetwork=A/*", "/SubNetwork=A/ManagedElement=1", True),
    ("/SubNetwork=A/*", "/SubNetwork=AB/ManagedElement=1", False),
    ("/SubNetwork=*/ManagedElement=1", "/SubNetwork=Z/ManagedElement=1", True),
    ("/SubNetwork=*/ManagedElement=1", "/SubNetwork=Z/ManagedElement=2", False),
    ("/SubNetwork=A.B", "/SubNetwork=AxB", False),
    ("/SubNetwork=X", "/SubNetwork=X", True),
    ("/SubNetwork=X/", "/SubNetwork=X", True),
])
def test_selects(selector, target, selected):
    """A selector must match the whole DN path, `*` matching any run of characters and a regex character matching only itself."""
    assert msac.selects(selector, target) is selected


def test_no_matching_rule_refuses():
    """No rule, or none for the operation or target, refuses."""
    assert not msac.decide([], "/SubNetwork=A", "read")
    assert not msac.decide([rule("/SubNetwork=A", ["update"])], "/SubNetwork=A", "read")
    assert not msac.decide([rule("/SubNetwork=B", ["read"])], "/SubNetwork=A", "read")


def test_an_allow_permits_and_a_deny_wins():
    """A matching ALLOW permits; any matching DENY refuses whatever the order; a DENY for another operation does not."""
    allow = rule("/SubNetwork=A/*", ["read", "update"])
    deny = rule("/SubNetwork=A/ManagedElement=1", ["update"], "DENY")
    assert msac.decide([allow], "/SubNetwork=A/ManagedElement=1", "update")
    assert not msac.decide([allow, deny], "/SubNetwork=A/ManagedElement=1", "update")
    assert not msac.decide([deny, allow], "/SubNetwork=A/ManagedElement=1", "update")
    assert msac.decide([allow, deny], "/SubNetwork=A/ManagedElement=1", "read")
    assert msac.decide([allow, deny], "/SubNetwork=A/ManagedElement=2", "update")
    assert not msac.decide([rule("/", ["read"], "OTHER")], "/SubNetwork=A", "read")


# ---- scope: the D15 axes

def test_a_claim_may_name_the_new_axes_and_round_trips():
    """`siteClusters` and `subNetworks` parse like the other axes and survive encode/decode."""
    claimed = scope.from_claim({"siteClusters": ["sc-1"], "subNetworks": ["A", "B"]})
    assert claimed == Scope(siteClusters=frozenset({"sc-1"}), subNetworks=frozenset({"A", "B"}))
    assert scope.decode(scope.encode(claimed)) == claimed
    assert scope.to_claim(claimed) == {"siteClusters": ["sc-1"], "subNetworks": ["A", "B"]}


def test_an_unknown_axis_is_still_refused():
    """A claim naming any other axis is refused, and the message lists all four."""
    with pytest.raises(ValueError, match="regions, tenants, siteClusters and subNetworks"):
        scope.from_claim({"cells": ["x"]})


def test_permits_on_site_cluster_and_sub_networks():
    """A site cluster is matched like a region; subNetworks is satisfied by any SubNetwork on the DN and refuses a target under none."""
    clusters = Scope(siteClusters=frozenset({"sc-1"}))
    assert scope.permits(clusters, None, None, "sc-1")
    assert not scope.permits(clusters, None, None, "sc-2")
    assert not scope.permits(clusters, None, None, None)
    nets = Scope(subNetworks=frozenset({"B"}))
    assert scope.permits(nets, None, None, None, ("A", "B"))
    assert not scope.permits(nets, None, None, None, ("A",))
    assert not scope.permits(nets, None, None, None, ())
    assert scope.permits(None, None, None, None, ())


def test_narrowed_and_covers_handle_the_new_axes():
    """Narrowing intersects every axis; covers asks a subset on every axis the parent restricts."""
    first = Scope(siteClusters=frozenset({"a", "b"}), subNetworks=frozenset({"N"}))
    second = Scope(siteClusters=frozenset({"b", "c"}))
    assert scope.narrowed(first, second) == Scope(siteClusters=frozenset({"b"}), subNetworks=frozenset({"N"}))
    assert scope.covers(first, Scope(siteClusters=frozenset({"a"}), subNetworks=frozenset({"N"})))
    assert not scope.covers(first, Scope(siteClusters=frozenset({"a"})))
    assert not scope.covers(first, second)


class _Base(DeclarativeBase):
    """A declarative base of this file's own table."""


class Row(_Base):
    """A target with a region, a tenant and a site cluster."""

    __tablename__ = "axis_row"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    region: Mapped[str | None] = mapped_column(String)
    tenant: Mapped[str | None] = mapped_column(String)
    site_cluster: Mapped[str | None] = mapped_column(String)


@pytest.fixture
def db():
    """An in-memory SQLite with three rows: site clusters sc-1, sc-2 and none."""
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Row(name="one", site_cluster="sc-1"), Row(name="two", site_cluster="sc-2"), Row(name="none")])
        session.commit()
        yield session


def names(db, stmt):
    """Helper: the names a statement returns, sorted."""
    return sorted(db.scalars(stmt))


def test_filter_and_denied_with_a_site_cluster_column(db):
    """With a column, a site-cluster claim filters by it (a NULL never matches) and the denied condition is its complement."""
    claim = Scope(siteClusters=frozenset({"sc-1"}))
    base = select(Row.name)
    assert names(db, scope.filter_statement(base, claim, Row.region, Row.tenant, Row.site_cluster)) == ["one"]
    assert names(db, base.where(scope.denied_condition(claim, Row.region, Row.tenant, Row.site_cluster))) == ["none", "two"]


def test_an_axis_without_a_column_fails_closed(db):
    """A site-cluster claim without a column, and any subNetworks claim, lets no row through and denies every row."""
    base = select(Row.name)
    for claim in (Scope(siteClusters=frozenset({"sc-1"})), Scope(subNetworks=frozenset({"A"}))):
        assert names(db, scope.filter_statement(base, claim, Row.region, Row.tenant)) == []
        assert names(db, base.where(scope.denied_condition(claim, Row.region, Row.tenant))) == ["none", "one", "two"]
    assert names(db, scope.filter_statement(base, None, Row.region, Row.tenant)) == ["none", "one", "two"]
    assert names(db, base.where(scope.denied_condition(None, Row.region, Row.tenant))) == []
