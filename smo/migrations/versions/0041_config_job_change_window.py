"""A configuration job's change window and its approval (PR-MGT-4, MGT-4.1 to 4.3).

`write_config_job` gains `scheduled_at` and `window_end` (the change window it was asked for; either NULL is open-ended), and `decided_by`,
`decided_at` and `decision_reason` (who approved or rejected it, when and why). All five are nullable, so a job made before this revision, and a job
made by the previous release's code during a rolling upgrade, reads as one that never waited. A window that closes before it opens is refused by a
CHECK. The job status check also admits the three states of MGT-4.2/4.3: `PENDING_APPROVAL`, `SCHEDULED` and `REJECTED`. Expand only: the previous
release's code never writes the new states or columns.

Revision ID: 0041
Revises: 0040
"""
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

JOB = "ran_nf_oam.write_config_job"
STATES = "'PENDING', 'PROCESSING', 'HALTED', 'COMPLETED', 'PARTIAL_SUCCESS', 'FAILED'"


def upgrade() -> None:
    """Add the window and decision columns, the window CHECK, and widen the status check to the approval states."""
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ")
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS window_end TIMESTAMPTZ")
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS decided_by VARCHAR")
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ")
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS decision_reason VARCHAR")
    op.execute(f"ALTER TABLE {JOB} ADD CONSTRAINT write_config_job_window_check "
               "CHECK (scheduled_at IS NULL OR window_end IS NULL OR window_end > scheduled_at)")
    op.execute(f"ALTER TABLE {JOB} DROP CONSTRAINT write_config_job_status_check")
    op.execute(f"ALTER TABLE {JOB} ADD CONSTRAINT write_config_job_status_check "
               f"CHECK (status IN ({STATES}, 'PENDING_APPROVAL', 'SCHEDULED', 'REJECTED'))")


def downgrade() -> None:
    """End the jobs in the new states as FAILED (none of them sent anything), put the narrower check back and drop the columns."""
    op.execute(f"UPDATE {JOB} SET status = 'FAILED' WHERE status IN ('PENDING_APPROVAL', 'SCHEDULED', 'REJECTED')")
    op.execute(f"ALTER TABLE {JOB} DROP CONSTRAINT write_config_job_status_check")
    op.execute(f"ALTER TABLE {JOB} ADD CONSTRAINT write_config_job_status_check CHECK (status IN ({STATES}))")
    op.execute(f"ALTER TABLE {JOB} DROP CONSTRAINT IF EXISTS write_config_job_window_check")
    for column in ("decision_reason", "decided_at", "decided_by", "window_end", "scheduled_at"):
        op.execute(f"ALTER TABLE {JOB} DROP COLUMN IF EXISTS {column}")
