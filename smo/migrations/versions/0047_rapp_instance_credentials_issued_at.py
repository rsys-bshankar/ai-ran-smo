"""`rapp_instance.credentials_issued_at` (SEC-15.19): when the instance's current secret was issued.

A nullable column on an existing table. rApp Management stamps it whenever the instance's secret is issued (at create, by `POST /instances/{id}/credentials`, by a
renewal by hand and by the automatic rotation), and the rotation worker reads it to find the instances whose secret is older than `RAPP_MGMT_SECRET_ROTATION_DAYS`.
`rapp-mgmt/app/models.py` is the model.

Expand only: the previous release's code neither reads nor writes the column. Every row already there gets NULL, which the worker reads as "issued when the instance was
created" (`created_at`), so nothing is rotated earlier than its age says. The table is in the `rapp_mgmt` schema (revision 0024/0025) and needs no grant of its own.

Revision ID: 0047
Revises: 0046
"""
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `rapp_mgmt.rapp_instance.credentials_issued_at` (TIMESTAMPTZ, NULL). Guarded by IF NOT EXISTS so a second run changes nothing. Nothing is backfilled."""
    op.execute("ALTER TABLE rapp_mgmt.rapp_instance ADD COLUMN IF NOT EXISTS credentials_issued_at TIMESTAMPTZ")


def downgrade() -> None:
    """Drops `rapp_mgmt.rapp_instance.credentials_issued_at`; when each secret was issued is forgotten, the instances stay. Guarded by IF EXISTS so it can be run again."""
    op.execute("ALTER TABLE rapp_mgmt.rapp_instance DROP COLUMN IF EXISTS credentials_issued_at")
