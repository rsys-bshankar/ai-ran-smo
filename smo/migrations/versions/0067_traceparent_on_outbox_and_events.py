"""The W3C trace position is kept on the rows that outlive the request (PR-CONV-S1.5).

`notification_outbox` and `smo_event` (shared) gain `traceparent`: the W3C `traceparent` of the request or worker task that wrote the row (`00-<trace id>-<span
id>-<flags>`), when it belonged to a trace. A notification is sent with it as the `traceparent` header, so the receiver joins the same trace however
much later the outbox sends it; an event carries it as the CloudEvents distributed-tracing extension attribute `traceparent`. The ProvMnS router of
`smo_shared.mns` writes its notifications through these rows, so a change made through ProvMnS and the notification it causes share one trace.

Expand only: both columns are nullable with no default; a row from before this revision keeps null, and the previous release's code runs on the new schema.

Downgrade: drops both columns (the trace positions they held are lost; nothing else reads them).

Revision ID: 0067
Revises: 0066
"""
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

TABLES = ("notification_outbox", "smo_event")


def upgrade() -> None:
    """Adds the nullable column to each table (guarded by IF NOT EXISTS)."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS traceparent TEXT")


def downgrade() -> None:
    """Drops the columns."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS traceparent")
