"""A configuration write is saved before it is sent, and an O1 action whose outcome DME did not learn is UNCONFIRMED (call flow 03, DOC-CF-1.8).

`write_config_sub_change` may be `DISPATCHING`: RAN NF OAM now commits a job and its sub-changes before the first edit and marks each sub-change
DISPATCHING, committed, just before its edit goes out, so a crash during the dispatch leaves the change in flight visible (its outcome unknown) instead
of losing the whole job. The status check is replaced by one that also allows it.

`dme_action_record` gains `invoker_id` (who asked, as the gateway vouched for it): DME answers a replay of an action whose forward timed out
(UNCONFIRMED) by asking RAN NF OAM again under the same idempotency key, and only for the caller that made it. The column is nullable, and a record
made before this revision is never re-asked (a replay of it is IGNORED, as before).

Expand only: the check allows more, the column is nullable with no default. The previous release's code runs on the new schema.

Revision ID: 0054
Revises: 0053
"""
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

SUB = "ran_nf_oam.write_config_sub_change"
STATES = "'PENDING', 'APPLIED', 'REJECTED', 'REVERTED'"


def upgrade() -> None:
    """Allows DISPATCHING on a sub-change and adds `dme_action_record.invoker_id` (NULL). The column is guarded by IF NOT EXISTS."""
    op.execute(f"ALTER TABLE {SUB} DROP CONSTRAINT write_config_sub_change_status_check")
    op.execute(f"ALTER TABLE {SUB} ADD CONSTRAINT write_config_sub_change_status_check CHECK (status IN ({STATES}, 'DISPATCHING'))")
    op.execute("ALTER TABLE dme.dme_action_record ADD COLUMN IF NOT EXISTS invoker_id TEXT")


def downgrade() -> None:
    """Drops the column and puts the narrower check back; a sub-change left DISPATCHING (its outcome unknown) is marked REJECTED first, the nearest old state."""
    op.execute("ALTER TABLE dme.dme_action_record DROP COLUMN IF EXISTS invoker_id")
    op.execute(f"UPDATE {SUB} SET status = 'REJECTED', rejection_reason = COALESCE(rejection_reason, 'DISPATCH_INTERRUPTED') WHERE status = 'DISPATCHING'")
    op.execute(f"ALTER TABLE {SUB} DROP CONSTRAINT write_config_sub_change_status_check")
    op.execute(f"ALTER TABLE {SUB} ADD CONSTRAINT write_config_sub_change_status_check CHECK (status IN ({STATES}))")
