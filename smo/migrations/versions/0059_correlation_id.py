"""The correlation id is kept where the work outlives the request (call flow 14, DOC-CF-1.8).

`notification_outbox` and `smo_event` (shared), `so_smos.service_order` and `ran_nf_oam.write_config_job` gain `correlation_id`: the X-Correlation-ID
of the request or worker task that wrote the row. A notification is sent with it, an event carries it as the CloudEvents `correlationid`
attribute, and a service order or a config job shows it, so an operator can find in the logs the request that made it.

Expand only: every column is nullable with no default; a row from before this revision keeps null. The previous release's code runs on the new schema.

Revision ID: 0059
Revises: 0058
"""
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

TABLES = ("notification_outbox", "smo_event", "so_smos.service_order", "ran_nf_oam.write_config_job")


def upgrade() -> None:
    """Adds the nullable column to each table (guarded by IF NOT EXISTS)."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS correlation_id TEXT")


def downgrade() -> None:
    """Drops the columns."""
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS correlation_id")
