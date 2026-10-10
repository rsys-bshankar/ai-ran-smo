"""Who registered a DME type first (SEC-15.10).

DME's `dme_type` table gains one nullable column, `registered_by`: the invoker id the gateway vouched for when the type was first registered (an rApp's own id, or an SMO
module's), or the request's `producerId` when the call did not come through the gateway. `POST /dme/production-capabilities` changes the definition of an existing type only for
that caller, an SMO module or the operator; before this a second caller could overwrite the schema of a type another producer registered.

Expand only: a nullable column with no default and no constraint, which the previous release's code never names (its INSERT leaves it NULL, its reads ignore it). No existing row
is rewritten: a type registered before this revision keeps NULL, and for those the code lets a producer linked to the type stand in for the first one. The table is in the `dme`
schema (revision 0025) and needs no grant of its own: the module role's default privileges on its schema cover a column.

Revision ID: 0039
Revises: 0038
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `dme.dme_type.registered_by` (VARCHAR, NULL). Guarded by IF NOT EXISTS so a second run changes nothing. Nothing is read or backfilled."""
    op.execute("ALTER TABLE dme.dme_type ADD COLUMN IF NOT EXISTS registered_by VARCHAR")


def downgrade() -> None:
    """Drops `dme.dme_type.registered_by`; who registered each type is lost, the types themselves stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE dme.dme_type DROP COLUMN IF EXISTS registered_by")
