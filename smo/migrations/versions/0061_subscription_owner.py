"""A PM or FM subscription records who made it (call flow 20, DOC-CF-1.8).

`ran_nf_oam.pm_subscription` and `ran_nf_oam.fm_subscription` gain `created_by`: the console's signed-in person or the subscribing module's invoker id.
Only that caller, or an admin in the console, may delete the subscription.

Expand only: the column is nullable with no default. A subscription from before this revision has no subscriber and may be deleted by anyone who may see
it, as before. The previous release's code runs on the new schema.

Revision ID: 0061
Revises: 0060
"""
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

TABLES = ("ran_nf_oam.pm_subscription", "ran_nf_oam.fm_subscription")


def upgrade() -> None:
    """Adds the nullable column (guarded by IF NOT EXISTS)."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS created_by TEXT")


def downgrade() -> None:
    """Drops the column."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS created_by")
