"""MLMF ceilings and the guarded metrics a report did not carry (call flow 13, DOC-CF-1.8).

AIMgF's `mlmf_subscription` gains `guard_kpi_ceiling` (metric name to maximum, for a metric where lower is better: a report above it breaches), and
`performance_report` gains `missing_metrics` (the guarded metrics the report did not carry; a missing metric no longer counts as 0 and so no longer breaches).

Expand only: two nullable JSONB columns with no default and no constraint, which the previous release's code never names (its INSERTs leave them NULL, its reads
ignore them). No existing row is rewritten: a subscription keeps no ceiling and an earlier report no list of missing metrics. Both tables are in the `aimgf` schema
(revision 0025) and need no grant of their own.

Revision ID: 0047
Revises: 0046
"""
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `aimgf.mlmf_subscription.guard_kpi_ceiling` and `aimgf.performance_report.missing_metrics` (JSONB, NULL). Guarded by IF NOT EXISTS so a second run changes nothing."""
    op.execute("ALTER TABLE aimgf.mlmf_subscription ADD COLUMN IF NOT EXISTS guard_kpi_ceiling JSONB")
    op.execute("ALTER TABLE aimgf.performance_report ADD COLUMN IF NOT EXISTS missing_metrics JSONB")


def downgrade() -> None:
    """Drops both columns; the ceilings and the record of missing metrics are lost, the subscriptions and reports stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE aimgf.performance_report DROP COLUMN IF EXISTS missing_metrics")
    op.execute("ALTER TABLE aimgf.mlmf_subscription DROP COLUMN IF EXISTS guard_kpi_ceiling")
