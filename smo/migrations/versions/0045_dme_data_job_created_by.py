"""Who created a DME data job (OI-7-rapp-data-jobs-teardown).

DME's `data_job` table gains one nullable column, `created_by`: the invoker id the gateway vouched for when the job was created. An rApp acts as the instance it
serves (call flow 01c), so for its jobs this is the instance's own invoker id, and rApp Management closes them by it when the instance is torn down
(`DELETE /dme/data-jobs?created_by=`). Before this a terminated instance's jobs stayed open and kept receiving every record of their type.

Expand only: a nullable column with no default and no constraint, which the previous release's code never names (its INSERT leaves it NULL, its reads ignore it). No existing row
is rewritten: a job created before this revision keeps NULL and is not closed by an instance's teardown. The table is in the `dme` schema (revision 0025) and needs no grant of its
own: the module role's default privileges on its schema cover a column.

Revision ID: 0045
Revises: 0044
"""
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `dme.data_job.created_by` (VARCHAR, NULL). Guarded by IF NOT EXISTS so a second run changes nothing. Nothing is read or backfilled."""
    op.execute("ALTER TABLE dme.data_job ADD COLUMN IF NOT EXISTS created_by VARCHAR")


def downgrade() -> None:
    """Drops `dme.data_job.created_by`; who created each job is lost, the jobs themselves stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE dme.data_job DROP COLUMN IF EXISTS created_by")
