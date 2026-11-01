"""Where a module's managed object instances live: one table, `moi_store`, in the mounting module's own schema (plan task S1.1).

    moi_store
      dn            TEXT PRIMARY KEY (COLLATE "C" on Postgres)   the full DN, `SubNetwork=A,ManagedElement=1,...`
      object_class  TEXT NOT NULL                                the class name (the last RDN's)
      moi_id        TEXT NOT NULL                                the id (the last RDN's)
      parent_dn     TEXT NULL                                    the DN one level up; NULL for a top-level instance
      depth         INTEGER NOT NULL                             the number of RDNs (1 at the top)
      attributes    JSONB NOT NULL DEFAULT '{}'                  the attributes, as the class's schema allows them
      moi_version   INTEGER NOT NULL DEFAULT 1                   raised by every UPDATE; the ETag (smo_shared.versioning)
      created_at    TIMESTAMPTZ NOT NULL
      updated_at    TIMESTAMPTZ NOT NULL
      indexes       parent_dn; object_class; GIN on attributes (Postgres)

The DN is the key and the order: keyset paging walks `dn`, and a subtree is the range [`dn`, `dn-`) (`smo_shared/mns/dn.py` says why that is exact),
which needs binary ordering, hence `COLLATE "C"` (a Postgres locale collation ignores punctuation). `moi_version` is `Versioned`'s column under this
name, so every ORM write is `UPDATE ... WHERE dn = :dn AND moi_version = :loaded` and a concurrent write is a `StaleDataError`.

The model is declared on its own `MnsBase`, not on `smo_shared.db.Base`: the table exists only in the schema of a module that mounts the router, and
`scripts/check_migration_matches_models.py` checks `Base`'s tables against every database. A module that mounts the router creates the table with a
revision that calls `create_moi_store(op, "<its schema>")` in `upgrade()` and `drop_moi_store(op, "<its schema>")` in `downgrade()`, and adds
`moi_store` under its name in `migrations/table_owners.json`; its role's search path (`<schema>, public`) finds the table, as for every module table.
Unit tests on SQLite call `create_all(engine)`.

A module whose store needs another table name (SME's registry, `mns_registry`) maps it with `store_model("<table>")`, passes that class to
`ProvMnS(model=...)`, and names the table in `create_moi_store(op, schema, table)` / `drop_moi_store(op, schema, table)`.
"""

import datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy import JSON, DateTime, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..versioning import Versioned

TABLE = "moi_store"
_DN = Text().with_variant(Text(collation="C"), "postgresql")
_ATTRIBUTES = JSON().with_variant(JSONB(), "postgresql")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class MnsBase(DeclarativeBase):
    """The declarative base of the ProvMnS store only (see the module description for why it is not `smo_shared.db.Base`)."""


class _StoreColumns:
    """The columns of a store table (the module description lists them), declared once for `ManagedObject` and for every `store_model` table."""

    dn: Mapped[str] = mapped_column(_DN, primary_key=True)
    object_class: Mapped[str] = mapped_column(Text, nullable=False)
    moi_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_dn: Mapped[str | None] = mapped_column(_DN, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    attributes: Mapped[dict] = mapped_column(_ATTRIBUTES, nullable=False, default=dict, server_default=sa.text("'{}'"))
    row_version: Mapped[int] = mapped_column("moi_version", Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


def _indexes(table: str) -> tuple:
    return Index(f"ix_{table}_parent_dn", "parent_dn"), Index(f"ix_{table}_object_class", "object_class")


class ManagedObject(_StoreColumns, Versioned, MnsBase):
    """One managed object instance (table `moi_store`); `row_version` is the `moi_version` column, `Versioned`'s optimistic-concurrency counter."""

    __tablename__ = TABLE
    __table_args__ = _indexes(TABLE)


_MODELS: dict[str, type[ManagedObject]] = {TABLE: ManagedObject}


def store_model(table: str) -> type[ManagedObject]:
    """The mapped class of the store table `table`: `ManagedObject` for `moi_store`, else a class of the same columns on a table of that name (made
    once per name). For a module whose ProvMnS store cannot be `moi_store` because the per-table tooling (`migrations/table_owners.json`) names each
    table once in the database, as SME's registry store `mns_registry` beside Echo's `moi_store` (plan task S1.12)."""
    if table not in _MODELS:
        made = type(f"ManagedObject_{table}", (_StoreColumns, Versioned, MnsBase), {"__tablename__": table, "__table_args__": _indexes(table)})
        _MODELS[table] = cast(type[ManagedObject], made)        # not a subclass of ManagedObject (another table), but the same columns
    return _MODELS[table]


def create_all(engine) -> None:
    """Creates `moi_store`, and every `store_model` table made so far, on `engine` (the SQLite unit tests; a real database gets them from a module's
    revision)."""
    MnsBase.metadata.create_all(engine)


def create_moi_store(op, schema: str, table: str = TABLE) -> None:
    """The Alembic `upgrade()` step: `<schema>.<table>` (`moi_store` unless named) with its indexes, as the module description lists them (the GIN
    index on Postgres only)."""
    op.create_table(
        table,
        sa.Column("dn", Text(collation="C"), primary_key=True),
        sa.Column("object_class", Text, nullable=False),
        sa.Column("moi_id", Text, nullable=False),
        sa.Column("parent_dn", Text(collation="C"), nullable=True),
        sa.Column("depth", Integer, nullable=False),
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("moi_version", Integer, nullable=False, server_default="1"),
        sa.Column("created_at", DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(f"ix_{table}_parent_dn", table, ["parent_dn"], schema=schema)
    op.create_index(f"ix_{table}_object_class", table, ["object_class"], schema=schema)
    op.create_index(f"ix_{table}_attributes", table, ["attributes"], schema=schema, postgresql_using="gin")


def drop_moi_store(op, schema: str, table: str = TABLE) -> None:
    """The Alembic `downgrade()` step: drops `<schema>.<table>` and its indexes (what it held is lost)."""
    op.drop_index(f"ix_{table}_attributes", table_name=table, schema=schema)
    op.drop_index(f"ix_{table}_object_class", table_name=table, schema=schema)
    op.drop_index(f"ix_{table}_parent_dn", table_name=table, schema=schema)
    op.drop_table(table, schema=schema)
