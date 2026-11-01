"""AIMgF's ProvMnS store, the first revision of the `aimgf` branch (plan tasks S2.7, S2.10).

Creates `aimgf.aimgf_moi_store` through the router's own helper (`smo_shared.mns.store.create_moi_store`), as Echo's revision 0066 did for `echo`, under
AIMgF's own table name, since `migrations/table_owners.json` names each table once and Echo's store is `moi_store` (SME's `mns_registry`, revision
sme_0001, is the same pattern):

  dn TEXT COLLATE "C" PRIMARY KEY, object_class, moi_id, parent_dn (COLLATE "C"), depth, attributes JSONB DEFAULT '{}', moi_version DEFAULT 1,
  created_at, updated_at; indexes on parent_dn and object_class, and GIN on attributes.

The store is the ProvMnS face of AIMgF's own tables: the twenty TS 28.105 classes and MLModelGovernance, written by AIMgF's projection in every
transaction that changes one of its rows (`aimgf/app/provmns/projection.py`). It starts empty; AIMgF projects every existing row into it on its first
start after this revision (`provmns/service.py`, `start`). The schema `aimgf` and its role `smo_aimgf` exist already (revision 0064,
`migrations/db_roles.json`), and the role's search path finds the table. No other table changes.

Downgrade: drops the table with its indexes (`drop_moi_store`); what it held is a projection of AIMgF's tables, which keep everything.

Revision ID: aimgf_0001
Revises: 0068
"""
from alembic import op

from smo_shared.mns.store import create_moi_store, drop_moi_store

revision = "aimgf_0001"
down_revision = "0068"
branch_labels = ("aimgf",)
depends_on = None

SCHEMA = "aimgf"
TABLE = "aimgf_moi_store"


def upgrade() -> None:
    """The router's table, under AIMgF's name, in AIMgF's schema."""
    create_moi_store(op, SCHEMA, TABLE)


def downgrade() -> None:
    """The table and its indexes."""
    drop_moi_store(op, SCHEMA, TABLE)
