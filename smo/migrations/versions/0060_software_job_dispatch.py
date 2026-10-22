"""A software management job is sent to its element, names what it installs and records why it failed (call flow 19, DOC-CF-1.8).

`ran_nf_oam.software_management_job` gains `remote_file_path` (the package the element downloads), `dispatched_phase` (the last phase whose RPC was sent,
so a phase is sent once whichever process sends it) and `failure_reason`; `ran_nf_oam.software_campaign` gains `remote_file_path`, which its jobs carry.

Expand only: every column is nullable with no default. A job from before this revision has no `dispatched_phase`; one still running is sent its current
phase's RPC by the next dispatch, which an element that already ran it answers as it would a repeat. The previous release's code runs on the new schema.

Revision ID: 0060
Revises: 0059
"""
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

JOB = "ran_nf_oam.software_management_job"
CAMPAIGN = "ran_nf_oam.software_campaign"


def upgrade() -> None:
    """Adds the nullable columns (guarded by IF NOT EXISTS)."""
    for column in ("remote_file_path", "dispatched_phase", "failure_reason"):
        op.execute(f"ALTER TABLE {JOB} ADD COLUMN IF NOT EXISTS {column} TEXT")
    op.execute(f"ALTER TABLE {CAMPAIGN} ADD COLUMN IF NOT EXISTS remote_file_path TEXT")


def downgrade() -> None:
    """Drops the columns."""
    op.execute(f"ALTER TABLE {CAMPAIGN} DROP COLUMN IF EXISTS remote_file_path")
    for column in ("failure_reason", "dispatched_phase", "remote_file_path"):
        op.execute(f"ALTER TABLE {JOB} DROP COLUMN IF EXISTS {column}")
