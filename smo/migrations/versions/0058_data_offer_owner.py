"""A data offer records who made it (call flow 12, DOC-CF-1.8).

`dme.data_offer` gains `created_by`: the invoker that created the offer, as the gateway vouched for it. An rApp may terminate only an offer it
created; the console and SMO modules may terminate any.

Expand only: the column is nullable with no default; an offer from before this revision keeps null, and only the console or a module may terminate it.
The previous release's code runs on the new schema.

Revision ID: 0058
Revises: 0057
"""
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the nullable column (guarded by IF NOT EXISTS)."""
    op.execute("ALTER TABLE dme.data_offer ADD COLUMN IF NOT EXISTS created_by TEXT")


def downgrade() -> None:
    """Drops the column."""
    op.execute("ALTER TABLE dme.data_offer DROP COLUMN IF EXISTS created_by")
