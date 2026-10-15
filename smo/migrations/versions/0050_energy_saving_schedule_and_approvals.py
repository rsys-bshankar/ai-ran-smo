"""The EnergySaving rApp's scheduled passes and its writes held for approval (call flow 22, DOC-CF-1.8).

`energy_saving_instance` gains `evaluate_every_seconds` (how often the rApp's worker runs a pass; null keeps the instance manual), `last_evaluated_at` and
`last_evaluation` (when the last pass began and how it went). `energy_saving_cell` gains `pending_approval_id`: the RAN NF OAM approval a wake, rollback or
override waits for when an approval policy holds the rApp's writes for a person (PR-AI-11).

Expand only: four nullable columns with no default and no constraint, which the previous release's code never names (its INSERTs leave them NULL, its reads
ignore them). No existing row is rewritten: every instance stays manual. The tables are in the `energy_saving_rapp` schema (revision 0024) and need no grant of
their own.

Revision ID: 0050
Revises: 0049
"""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the four columns (NULL). Guarded by IF NOT EXISTS so a second run changes nothing."""
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance ADD COLUMN IF NOT EXISTS evaluate_every_seconds INTEGER")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance ADD COLUMN IF NOT EXISTS last_evaluation JSONB")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_cell ADD COLUMN IF NOT EXISTS pending_approval_id UUID")


def downgrade() -> None:
    """Drops the four columns: every instance is manual again and a pending approval is forgotten (the cell stays as it is). Guarded by IF EXISTS."""
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_cell DROP COLUMN IF EXISTS pending_approval_id")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance DROP COLUMN IF EXISTS last_evaluation")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance DROP COLUMN IF EXISTS last_evaluated_at")
    op.execute("ALTER TABLE energy_saving_rapp.energy_saving_instance DROP COLUMN IF EXISTS evaluate_every_seconds")
