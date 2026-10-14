"""Who requested an AIMgF inference job (call flow 02, DOC-CF-1.6).

AIMgF's `inference_job` table gains one nullable column, `requested_by`: the invoker id the gateway vouched for when an rApp requested the job. Only that rApp, or the
owner of the job's model, may resolve the job (`POST /aimgf/inference-jobs/{id}/resolve`); before this any rApp could, with outputs of its choosing.

Expand only: a nullable column with no default and no constraint, which the previous release's code never names (its INSERT leaves it NULL, its reads ignore it). No existing row
is rewritten: a job requested before this revision keeps NULL, and then only the model's owner (and the console, an SMO module) may resolve it. The table is in the `aimgf` schema
(revision 0025) and needs no grant of its own.

Revision ID: 0046
Revises: 0045
"""
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `aimgf.inference_job.requested_by` (VARCHAR, NULL). Guarded by IF NOT EXISTS so a second run changes nothing. Nothing is read or backfilled."""
    op.execute("ALTER TABLE aimgf.inference_job ADD COLUMN IF NOT EXISTS requested_by VARCHAR")


def downgrade() -> None:
    """Drops `aimgf.inference_job.requested_by`; who requested each job is lost, the jobs themselves stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE aimgf.inference_job DROP COLUMN IF EXISTS requested_by")
