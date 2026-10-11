"""A feature group names its data-lake token instead of carrying it (SEC-15.2).

AIMgF's `aimgf.feature_group` table gains one nullable column, `token_ref`: the name of a secret that the service resolves from its own environment or mounted secrets
(`AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE`) when it connects to the data lake. The existing `token` column, which holds a clear-text credential, becomes nullable: a group
registered with a `tokenRef` has no stored token. The routes never return either value any more (they show `tokenSet` and `tokenRef`).

Expand only: a nullable column nobody on the previous release names, and a NOT NULL constraint that is relaxed, never added. The previous release's code keeps working on the
upgraded database: its INSERT still supplies `token`, and its read of a group with no token returns null for it. No row is rewritten; groups registered before this revision keep their
token and have no reference. Removing the `token` column is the contract step of a later release, after the clear-text field has been dropped from the API.

Revision ID: 0043
Revises: 0042
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds `aimgf.feature_group.token_ref` (VARCHAR, NULL) and lets `token` be NULL. Both statements are safe to run twice. Nothing is backfilled."""
    op.execute("ALTER TABLE aimgf.feature_group ADD COLUMN IF NOT EXISTS token_ref VARCHAR")
    op.execute("ALTER TABLE aimgf.feature_group ALTER COLUMN token DROP NOT NULL")


def downgrade() -> None:
    """Puts NOT NULL back on `token` (a group that has only a reference gets an empty string first, so the constraint can be applied) and drops `token_ref`;
    the references are lost, and the groups that used one lose their credential too, which the operator must register again."""
    op.execute("UPDATE aimgf.feature_group SET token = '' WHERE token IS NULL")
    op.execute("ALTER TABLE aimgf.feature_group ALTER COLUMN token SET NOT NULL")
    op.execute("ALTER TABLE aimgf.feature_group DROP COLUMN IF EXISTS token_ref")
