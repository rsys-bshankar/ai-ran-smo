"""The Traffic Steering rApp's scheduled passes and its writes held for approval (call flow 25, DOC-CF-1.8).

`traffic_instance` gains `evaluate_every_seconds`, `last_evaluated_at` and `last_evaluation` (the worker's scheduled passes, as the other three
sample rApps' in revisions 0050 to 0052). `traffic_cell` gains `pending_approval_id` and `pending_decision_id`: the RAN NF OAM approval a revert or
rollback waits for when an approval policy holds the rApp's writes for a person (PR-AI-11), and the audit row it settles.

Expand only: five nullable columns with no default and no constraint, which the previous release's code never names. No existing row is rewritten: every
instance stays manual. The tables are in the `traffic_steering_rapp` schema (revision 0024) and need no grant of their own.

Revision ID: 0053
Revises: 0052
"""
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the five columns (NULL). Guarded by IF NOT EXISTS so a second run changes nothing."""
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance ADD COLUMN IF NOT EXISTS evaluate_every_seconds INTEGER")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance ADD COLUMN IF NOT EXISTS last_evaluation JSONB")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_cell ADD COLUMN IF NOT EXISTS pending_approval_id UUID")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_cell ADD COLUMN IF NOT EXISTS pending_decision_id UUID")


def downgrade() -> None:
    """Drops the five columns: every instance is manual again and a pending approval is forgotten. Guarded by IF EXISTS."""
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_cell DROP COLUMN IF EXISTS pending_decision_id")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_cell DROP COLUMN IF EXISTS pending_approval_id")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance DROP COLUMN IF EXISTS last_evaluation")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance DROP COLUMN IF EXISTS last_evaluated_at")
    op.execute("ALTER TABLE traffic_steering_rapp.traffic_instance DROP COLUMN IF EXISTS evaluate_every_seconds")
