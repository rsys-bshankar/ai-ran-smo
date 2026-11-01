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
"""

import datetime

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


class ManagedObject(Versioned, MnsBase):
    """One managed object instance (table `moi_store`); `row_version` is the `moi_version` column, `Versioned`'s optimistic-concurrency counter."""

    __tablename__ = TABLE
    __table_args__ = (Index(f"ix_{TABLE}_parent_dn", "parent_dn"), Index(f"ix_{TABLE}_object_class", "object_class"))

    dn: Mapped[str] = mapped_column(_DN, primary_key=True)
    object_class: Mapped[str] = mapped_column(Text, nullable=False)
    moi_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_dn: Mapped[str | None] = mapped_column(_DN, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    attributes: Mapped[dict] = mapped_column(_ATTRIBUTES, nullable=False, default=dict, server_default=sa.text("'{}'"))
    row_version: Mapped[int] = mapped_column("moi_version", Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


def create_all(engine) -> None:
    """Creates `moi_store` on `engine` (the SQLite unit tests; a real database gets it from a module's revision)."""
    MnsBase.metadata.create_all(engine)


def create_moi_store(op, schema: str) -> None:
    """The Alembic `upgrade()` step: `<schema>.moi_store` with its indexes, as the module description lists them (the GIN index on Postgres only)."""
    op.create_table(
        TABLE,
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
    op.create_index(f"ix_{TABLE}_parent_dn", TABLE, ["parent_dn"], schema=schema)
    op.create_index(f"ix_{TABLE}_object_class", TABLE, ["object_class"], schema=schema)
    op.create_index(f"ix_{TABLE}_attributes", TABLE, ["attributes"], schema=schema, postgresql_using="gin")


def drop_moi_store(op, schema: str) -> None:
    """The Alembic `downgrade()` step: drops `<schema>.moi_store` and its indexes (what it held is lost)."""
    op.drop_index(f"ix_{TABLE}_attributes", table_name=TABLE, schema=schema)
    op.drop_index(f"ix_{TABLE}_object_class", table_name=TABLE, schema=schema)
    op.drop_index(f"ix_{TABLE}_parent_dn", table_name=TABLE, schema=schema)
    op.drop_table(TABLE, schema=schema)
