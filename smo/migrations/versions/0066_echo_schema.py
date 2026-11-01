"""Echo's schema: the reference ProvMnS module of plan task S1.6, the first to mount `smo_shared.mns`.

Creates the schema `echo` and, in it, the router's store `echo.moi_store` through the router's own helper (`smo_shared.mns.store.create_moi_store`):

  dn TEXT COLLATE "C" PRIMARY KEY, object_class, moi_id, parent_dn (COLLATE "C"), depth, attributes JSONB DEFAULT '{}', moi_version DEFAULT 1,
  created_at, updated_at; indexes on parent_dn and object_class, and GIN on attributes.

Echo's role `smo_echo` (`migrations/db_roles.json`) can use this schema and the shared tables it writes through `smo_shared` (the event feed, the audit
chain, idempotency keys, its module identity); nothing else (D6: Echo is the one writer of `echo.moi_store`).

Downgrade: drops the table with its indexes (`drop_moi_store`) and the schema (what it held is lost; nothing else reads it).

Revision ID: 0066
Revises: 0065
"""
from alembic import op

from smo_shared.mns.store import create_moi_store, drop_moi_store

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

SCHEMA = "echo"


def upgrade() -> None:
    """The schema, then the router's table in it."""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    create_moi_store(op, SCHEMA)


def downgrade() -> None:
    """The table, then the schema."""
    drop_moi_store(op, SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
