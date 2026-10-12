"""`rapp_limit` takes blast-radius and magnitude limits (PR-AI-10.3).

Two nullable columns, and `max_config_jobs_per_hour` stops being required (a manifest may declare only a blast radius). The table is new in this
release, so the previous release neither reads nor writes it. `ran-nf-oam/app/models.py` is the model.

The downgrade undoes only what the upgrade added: it drops the two columns (they are the upgrade's own) and puts `NOT NULL` back on `max_config_jobs_per_hour`. A row that has
no hourly cap (one written after the upgrade, with a blast radius only) cannot satisfy that constraint; the row is kept, not deleted, and gets 2147483647 jobs per hour, which
is "no hourly cap" in the only way the old schema can say it. The blast radius it had is lost with its column.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Make `max_config_jobs_per_hour` nullable and add `max_elements_per_job` and `max_change_percent` to `rapp_limit`."""
    # A manifest may declare only a blast radius (`max_elements_per_job`), so the hourly cap can be absent.
    op.execute("ALTER TABLE rapp_limit ALTER COLUMN max_config_jobs_per_hour DROP NOT NULL")
    op.execute("ALTER TABLE rapp_limit ADD COLUMN max_elements_per_job INTEGER")
    op.execute("ALTER TABLE rapp_limit ADD COLUMN max_change_percent DOUBLE PRECISION")


def downgrade() -> None:
    """Drop the two new columns and make `max_config_jobs_per_hour` NOT NULL again, keeping every row (a row without an hourly cap gets the largest INTEGER, "no cap")."""
    op.execute("ALTER TABLE rapp_limit DROP COLUMN max_change_percent")
    op.execute("ALTER TABLE rapp_limit DROP COLUMN max_elements_per_job")
    # A row with no hourly cap cannot satisfy the NOT NULL put back below. Deleting it would destroy a limit row this revision did not create (the table is 0014's), so it is
    # given the largest INTEGER instead: the old code compares a count with the cap, and no count reaches that, so the row still means "no hourly cap".
    op.execute("UPDATE rapp_limit SET max_config_jobs_per_hour = 2147483647 WHERE max_config_jobs_per_hour IS NULL")
    op.execute("ALTER TABLE rapp_limit ALTER COLUMN max_config_jobs_per_hour SET NOT NULL")
