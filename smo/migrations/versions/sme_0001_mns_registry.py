"""SME's MnS registry store (plan tasks S1.7, S1.12): the first revision of the `sme` branch.

Creates `sme.mns_registry`, the ProvMnS store of the TS 28.622 MnsRegistry, MnsInfo and MgmtDataInfo (`sme/app/mns_registry.py`), through the
router's own helper (`smo_shared.mns.store.create_moi_store`) under its own table name, since `migrations/table_owners.json` names each table once
and Echo's store is `moi_store`. The columns and indexes are `moi_store`'s: dn TEXT COLLATE "C" PRIMARY KEY, object_class, moi_id, parent_dn
(COLLATE "C"), depth, attributes JSONB DEFAULT '{}', moi_version DEFAULT 1, created_at, updated_at; indexes on parent_dn and object_class, and GIN on
attributes. No row is written here: SME writes the registry's container, the MnsRegistry and its own registration on the first request that finds
them missing. This replaces the contract bundle's custom `mns_producers` table, which no revision ever made (G12).

SME's role `smo_sme` already uses the schema `sme`; `migrations/db_roles.json` adds the shared tables its ProvMnS producer writes (the event feed,
the audit chain).

Downgrade: drops the table with its indexes (`drop_moi_store`); the registry is lost, and every module registers again at its next start.

Revision ID: sme_0001
Revises: 0068
"""
from alembic import op

from smo_shared.mns.store import create_moi_store, drop_moi_store

revision = "sme_0001"
down_revision = "0068"
branch_labels = ("sme",)
depends_on = None

SCHEMA = "sme"
TABLE = "mns_registry"


def upgrade() -> None:
    """The registry's store table in SME's schema."""
    create_moi_store(op, SCHEMA, TABLE)


def downgrade() -> None:
    """Drops it."""
    drop_moi_store(op, SCHEMA, TABLE)
