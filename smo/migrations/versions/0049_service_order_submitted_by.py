"""Who submitted a service order (call flow 10, DOC-CF-1.8).

SO SMOS's `service_order` gains `submitted_by`: the order's `requestedBy`, which the GUI BFF sets to `smo-gui:<user>` and SO SMOS sends to RAN NF OAM as the
`requestedBy` of every CONFIG step (a step may no longer name its own).

Expand only: one nullable VARCHAR(200) with no default and no constraint, which the previous release's code never names (its INSERTs leave it NULL, its reads ignore
it). No existing row is rewritten: an earlier order keeps no submitter. The table is in the `so_smos` schema (revision 0024) and needs no grant of its own.

Revision ID: 0049
Revises: 0048
"""
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `so_smos.service_order.submitted_by` (VARCHAR(200), NULL). Guarded by IF NOT EXISTS so a second run changes nothing."""
    op.execute("ALTER TABLE so_smos.service_order ADD COLUMN IF NOT EXISTS submitted_by VARCHAR(200)")


def downgrade() -> None:
    """Drops the column; the record of who submitted each order is lost, the orders stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE so_smos.service_order DROP COLUMN IF EXISTS submitted_by")
