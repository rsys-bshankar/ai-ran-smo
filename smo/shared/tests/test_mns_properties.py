"""Property tests of getMOIAttributes' scope and keyset paging (plan task S1.4), with hypothesis.

Random containment trees of one hand-registered class `Node` (ids drawn from a set chosen so that sibling DNs share prefixes: `1`, `10`, `1-b`, `1.c`,
`1_d`, ...) are stored, and the router's answers are compared with an oracle computed in Python from the DNs alone:

  scope     for every scope type and level, the selected DNs are exactly the oracle's, in DN order
  paging    for any page size and filter, the pages, put together, are the unpaged answer, with no instance twice
  stable    instances added or removed between two pages never make the walk skip or repeat an instance that was there all along

On in-memory SQLite always, and on Postgres when SMO_TEST_POSTGRES_URL is set (fewer examples: each one writes a tree).
"""

import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

from smo_shared.mns import ClassRegistry, ManagedObject, ProvMnS, Scope, create_moi_store, drop_moi_store, store
from smo_shared.mns.dn import depth_of, is_descendant
from smo_shared.mns.scope import parse_filter
from smo_shared.testing import make_test_engine

IDS = ["1", "10", "1-b", "1.c", "1_d", "2", "a", "a1", "A", "z~", "1:2", "1@x"]
POSTGRES_URL = os.environ.get("SMO_TEST_POSTGRES_URL")


def _registry() -> ClassRegistry:
    registry = ClassRegistry(class_files=(), roots=("Node",))
    registry.register("Node", {"type": "object", "properties": {"n": {"type": "integer"}}}, children=["Node"])
    return registry


MNS = ProvMnS(source="/smo/mns-property", system_dn="ManagementNode=mns-property", registry=_registry())


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def sessions(request):
    """A session factory over a database holding only `moi_store` (SQLite; or a schema of its own on Postgres, made by the Alembic helper); the
    event bus is off for these tests only (they read, and store rows without the router)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SMO_EVENT_BUS", "off")
        yield from _database(request.param)


def _database(kind: str):
    """Yields a session factory over `moi_store` on `kind` (`sqlite`, or `postgres` in a schema of its own, removed afterwards)."""
    if kind == "sqlite":
        engine = make_test_engine()
        store.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
        return
    if not POSTGRES_URL:
        pytest.skip("SMO_TEST_POSTGRES_URL not set")
    schema = f"mns_prop_{uuid.uuid4().hex[:10]}"
    admin = create_engine(POSTGRES_URL, future=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        create_moi_store(Operations(MigrationContext.configure(connection)), schema)
    engine = create_engine(POSTGRES_URL, future=True, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            drop_moi_store(Operations(MigrationContext.configure(connection)), schema)
            connection.execute(text(f'DROP SCHEMA "{schema}"'))
        admin.dispose()


@st.composite
def trees(draw) -> dict[str, int]:
    """A containment tree as {DN: n}: a first root, then each node under a random earlier node (or at the top), sibling ids distinct."""
    size = draw(st.integers(min_value=1, max_value=30))
    nodes: dict[str, int] = {}
    for index in range(size):
        parents = [None, *nodes] if nodes else [None]
        parent = draw(st.sampled_from(parents)) if index else None
        moi_id = draw(st.sampled_from(IDS))
        dn = f"Node={moi_id}" if parent is None else f"{parent},Node={moi_id}"
        if dn not in nodes:
            nodes[dn] = draw(st.integers(min_value=0, max_value=9))
    return nodes


def _store(sessions, tree: dict[str, int]) -> None:
    """Replaces the table's content with `tree` (bulk rows: the router's create path is tested elsewhere)."""
    with sessions() as db:
        db.execute(delete(ManagedObject))
        for dn, n in tree.items():
            parent = dn.rpartition(",")[0] or None
            db.add(ManagedObject(dn=dn, object_class="Node", moi_id=dn.rpartition("=")[2], parent_dn=parent, depth=depth_of(dn), attributes={"n": n}))
        db.commit()


def _oracle(tree, base: str, scope: Scope) -> list[str]:
    """The DNs `scope` selects from `base`, from the DNs alone, in binary (DN) order."""
    lowest, highest = scope.depth_range(depth_of(base))
    chosen = [dn for dn in tree if (dn == base or is_descendant(dn, base)) and lowest <= depth_of(dn) and (highest is None or depth_of(dn) <= highest)]
    return sorted(chosen)


def _walk(sessions, base, scope, limit, filter_tree=None, between=None) -> list[str]:
    """Every page of a getMOIAttributes, put together; `between(page_number)` runs after each page but the last."""
    out, cursor, number = [], None, 0
    while True:
        with sessions() as db:
            page = MNS.get_moi_attributes(db, base, scope, filter_tree, limit=limit, cursor=cursor)
        out.extend(item["objectInstance"] for item in page.items)
        if page.next_cursor is None:
            return out
        number += 1
        if between is not None:
            between(number)
        cursor = page.next_cursor


scopes = st.one_of(st.just(Scope("BASE_ONLY", 0)), st.just(Scope("BASE_ALL", 0)),
                   st.builds(Scope, st.just("BASE_NTH_LEVEL"), st.integers(0, 4)), st.builds(Scope, st.just("BASE_SUBTREE"), st.integers(0, 4)))

SETTINGS = settings(max_examples=int(os.environ.get("SMO_MNS_PROPERTY_EXAMPLES", "60")), deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


@SETTINGS
@given(tree=trees(), data=st.data(), scope=scopes)
def test_scope_selects_exactly_the_oracles_instances(sessions, tree, data, scope):
    """Every scope type and level selects exactly the instances the DNs say, in DN order (one page large enough for all)."""
    _store(sessions, tree)
    base = data.draw(st.sampled_from(sorted(tree)))
    assert _walk(sessions, base, scope, limit=500) == _oracle(tree, base, scope)


@SETTINGS
@given(tree=trees(), data=st.data(), scope=scopes, limit=st.integers(1, 7), threshold=st.none() | st.integers(0, 9))
def test_pages_put_together_are_the_unpaged_answer(sessions, tree, data, scope, limit, threshold):
    """Whatever the page size and filter, the pages are the unpaged answer, in order, with no instance twice."""
    _store(sessions, tree)
    base = data.draw(st.sampled_from(sorted(tree)))
    filter_tree = None if threshold is None else parse_filter(f"@.attributes.n >= {threshold}")
    expected = [dn for dn in _oracle(tree, base, scope) if threshold is None or tree[dn] >= threshold]
    walked = _walk(sessions, base, scope, limit, filter_tree)
    assert walked == expected
    assert len(walked) == len(set(walked))


@SETTINGS
@given(tree=trees(), data=st.data(), limit=st.integers(1, 5), extra=st.lists(st.sampled_from(IDS), max_size=5))
def test_paging_is_stable_under_concurrent_changes(sessions, tree, data, limit, extra):
    """Instances added and removed while a client pages (after its first page) never make the walk repeat an instance or miss one that was in the
    tree from start to end; the walk stays in DN order."""
    _store(sessions, tree)
    base = data.draw(st.sampled_from(sorted(tree)))
    scope = Scope("BASE_ALL", 0)
    removable = [dn for dn in sorted(tree) if dn != base and is_descendant(dn, base)]
    removed = set(data.draw(st.lists(st.sampled_from(removable), max_size=3, unique=True))) if removable else set()
    added = {f"{base},Node={moi_id}" for moi_id in extra} - set(tree)

    def change(number):
        if number != 1:
            return
        with sessions() as db:
            for dn in removed:
                db.execute(delete(ManagedObject).where(ManagedObject.dn == dn))
            for dn in added:
                db.add(ManagedObject(dn=dn, object_class="Node", moi_id=dn.rpartition("=")[2], parent_dn=base, depth=depth_of(dn), attributes={"n": 1}))
            db.commit()

    walked = _walk(sessions, base, scope, limit, between=change)
    assert walked == sorted(walked)
    assert len(walked) == len(set(walked))
    always_there = set(_oracle(tree, base, scope)) - {dn for dn in tree if any(dn == r or is_descendant(dn, r) for r in removed)}
    assert always_there <= set(walked)
    assert set(walked) <= set(tree) | added
