"""A configuration job whose change window closed before it started ends EXPIRED, and the worker's start keeps what it needs (PR-MGT-4, MGT-4.4, MGT-4.5).

`ran_nf_oam.write_config_job`'s status CHECK admits one more state, `EXPIRED`: the worker's `expire-windows` task moves a job that is still
`PENDING_APPROVAL` or `SCHEDULED` there when its `window_end` has passed. Two nullable columns are added for the worker's start: `requester_scope` (the scope claim of
the caller that asked for the job, so the start can ask the same question of its elements as the request did) and `start_attempts` (the consecutive starts that failed
unexpectedly; the worker ends the job FAILED at the limit, and the count lives in the row so it survives a restart and is shared by replicas). Expand only: a CHECK that
admits more values and two nullable columns; the previous release's code never writes `EXPIRED` or the columns, and a job the new release has expired reads as an
unknown status string, not an error, in the previous release's views.
The downgrade ends the expired jobs as REJECTED (nothing was sent for them either), drops the two columns and puts the narrower CHECK back.

Revision ID: 0044
Revises: 0043
"""
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

JOB = "ran_nf_oam.write_config_job"
STATES = ("'PENDING', 'PROCESSING', 'HALTED', 'COMPLETED', 'PARTIAL_SUCCESS', 'FAILED', 'PENDING_APPROVAL', 'SCHEDULED', 'REJECTED'")


def upgrade() -> None:
    """Replace the job status CHECK with one that also admits EXPIRED, and add the two nullable columns of the worker's start."""
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS requester_scope JSON")
    op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS start_attempts INTEGER")
    op.execute(f"ALTER TABLE {JOB} DROP CONSTRAINT write_config_job_status_check")
    op.execute(f"ALTER TABLE {JOB} ADD CONSTRAINT write_config_job_status_check CHECK (status IN ({STATES}, 'EXPIRED'))")


def downgrade() -> None:
    """End the EXPIRED jobs as REJECTED (none of them sent anything), drop the two columns and put the CHECK of revision 0041 back."""
    op.execute(f"ALTER TABLE {JOB} DROP COLUMN IF EXISTS start_attempts")
    op.execute(f"ALTER TABLE {JOB} DROP COLUMN IF EXISTS requester_scope")
    op.execute(f"UPDATE {JOB} SET status = 'REJECTED' WHERE status = 'EXPIRED'")
    op.execute(f"ALTER TABLE {JOB} DROP CONSTRAINT write_config_job_status_check")
    op.execute(f"ALTER TABLE {JOB} ADD CONSTRAINT write_config_job_status_check CHECK (status IN ({STATES}))")
