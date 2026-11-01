"""The Kafka relay's position (ADR 0006 slice 2, plan task S1.10, D9): `smo_event_relay`, one row per sink that follows `smo_event` (smo_shared/kafka_relay.py).

`name` is the sink (`kafka:<topic>`), `last_seq` the highest `seq` of `smo_event` the sink has acknowledged without a gap, `updated_at` when it moved. The relay's
own position in Postgres, not a Kafka consumer offset, so a database restore and the topic cannot disagree in a way that loses an event (a restore rewinds the
position: duplicates downstream, never a loss). `event-purge` reads it to keep unsent events while the relay is on (events.purge). A new table, so the previous
release's code is untouched (expand only); the module roles that publish or purge `smo_event` get it in `migrations/db_roles.json`.

Revision ID: 0064
Revises: 0063
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create `smo_event_relay` in the public schema, beside `smo_event` (named with its schema, as 0042 does, so a module schema on the search path
    does not get it), and say on `smo_event` that a relay follows it."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.smo_event_relay (
            name VARCHAR PRIMARY KEY,
            last_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL
        )""")
    op.execute("COMMENT ON TABLE public.smo_event IS 'The event bus feed (smo_shared/events.py); a relay to Kafka follows it from smo_event_relay (ADR 0006)'")


def downgrade() -> None:
    """Drop the table: the relay then starts again from the beginning of the feed's window (duplicates on the topic, which consumers drop by `id`)."""
    op.execute("COMMENT ON TABLE public.smo_event IS NULL")
    op.execute("DROP TABLE IF EXISTS public.smo_event_relay")
