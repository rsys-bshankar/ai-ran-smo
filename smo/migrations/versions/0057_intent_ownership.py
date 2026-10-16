"""An rApp manages only its own intents and dispatches only for its own instance (call flow 09, DOC-CF-1.8).

`intent_service.intent` gains `owner_invoker_id`: the invoker that created the intent (an rApp's own id, the module's, or the dispatching rApp's for an
intent made from an autonomy dispatch). An rApp may change the admin state of, delete or answer negotiation for only the intents it owns.
`intent_service.autonomy_dispatch` gains `dispatched_by` (the rApp or module that asked) and `resolved_by` (the console user who scoped an ASSIST dispatch).

Expand only: the three columns are nullable with no default; a row from before this revision keeps null (an intent with no owner falls back to its
`rmio_id`). The previous release's code runs on the new schema.

Revision ID: 0057
Revises: 0056
"""
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Adds the three nullable columns (guarded by IF NOT EXISTS)."""
    op.execute("ALTER TABLE intent_service.intent ADD COLUMN IF NOT EXISTS owner_invoker_id TEXT")
    op.execute("ALTER TABLE intent_service.autonomy_dispatch ADD COLUMN IF NOT EXISTS dispatched_by TEXT")
    op.execute("ALTER TABLE intent_service.autonomy_dispatch ADD COLUMN IF NOT EXISTS resolved_by TEXT")


def downgrade() -> None:
    """Drops the three columns."""
    op.execute("ALTER TABLE intent_service.autonomy_dispatch DROP COLUMN IF EXISTS resolved_by")
    op.execute("ALTER TABLE intent_service.autonomy_dispatch DROP COLUMN IF EXISTS dispatched_by")
    op.execute("ALTER TABLE intent_service.intent DROP COLUMN IF EXISTS owner_invoker_id")
