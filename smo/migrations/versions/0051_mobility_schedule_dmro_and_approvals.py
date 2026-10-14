"""The Mobility Optimization rApp's scheduled passes, its DMRO status and its writes held for approval (call flow 23, DOC-CF-1.8).

`mobility_instance` gains `dmro_status` (how applying the DMRO bounds went; a CIO change is held until they are verified), `evaluate_every_seconds`,
`last_evaluated_at` and `last_evaluation` (the worker's scheduled passes, as the EnergySaving rApp's in revision 0050). `mobility_relation` gains
`pending_approval_id`: the RAN NF OAM approval a revert or rollback waits for when an approval policy holds the rApp's writes for a person (PR-AI-11).

Expand only: five nullable columns with no default and no constraint, which the previous release's code never names. No existing row is rewritten: every
instance stays manual, and its DMRO status is unknown until its bounds are applied again ("Apply DMRO bounds" or a new deploy). The tables are in the
`mobility_optimization_rapp` schema (revision 0024) and need no grant of their own.

Revision ID: 0051
Revises: 0050
"""
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the five columns (NULL). Guarded by IF NOT EXISTS so a second run changes nothing."""
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance ADD COLUMN IF NOT EXISTS dmro_status JSONB")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance ADD COLUMN IF NOT EXISTS evaluate_every_seconds INTEGER")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance ADD COLUMN IF NOT EXISTS last_evaluation JSONB")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_relation ADD COLUMN IF NOT EXISTS pending_approval_id UUID")


def downgrade() -> None:
    """Drops the five columns: every instance is manual again, its DMRO status and a pending approval are forgotten. Guarded by IF EXISTS."""
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_relation DROP COLUMN IF EXISTS pending_approval_id")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance DROP COLUMN IF EXISTS last_evaluation")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance DROP COLUMN IF EXISTS last_evaluated_at")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance DROP COLUMN IF EXISTS evaluate_every_seconds")
    op.execute("ALTER TABLE mobility_optimization_rapp.mobility_instance DROP COLUMN IF EXISTS dmro_status")
