"""Analytics producers are who they say they are, and a report records who published it (call flow 08, DOC-CF-1.8).

`mdaf.mdaf_report` gains `published_by`: the rApp that published it (MDAF now refuses an rApp that is not a registered producer of the report's
analytics type), or the console user / SMO module. `ran_analytics.mdaf_producer` gains `sme_service_id`: the SME service API the registration
published (`mdaf.<analyticsType>.<producerId>`), which a deregistration withdraws.

Expand only: both columns are nullable with no default; a report or producer from before this revision keeps null. The previous release's code runs
on the new schema.

Revision ID: 0056
Revises: 0055
"""
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the two nullable columns (guarded by IF NOT EXISTS)."""
    op.execute("ALTER TABLE mdaf.mdaf_report ADD COLUMN IF NOT EXISTS published_by TEXT")
    op.execute("ALTER TABLE ran_analytics.mdaf_producer ADD COLUMN IF NOT EXISTS sme_service_id TEXT")


def downgrade() -> None:
    """Drops the two columns."""
    op.execute("ALTER TABLE ran_analytics.mdaf_producer DROP COLUMN IF EXISTS sme_service_id")
    op.execute("ALTER TABLE mdaf.mdaf_report DROP COLUMN IF EXISTS published_by")
