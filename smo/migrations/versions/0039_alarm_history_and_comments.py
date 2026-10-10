"""The history of every alarm and the comments on it (PR-MGT-8, steps MGT-8.2 and MGT-8.3; the alarm console's tabs GUI-2.3 and GUI-2.4).

RAN NF OAM gets two tables (its schema `ran_nf_oam`, `ran-nf-oam/app/models.py` is the model):

  alarm_history    one row per change of an alarm: RAISED, ACKNOWLEDGED, UNACKNOWLEDGED, CLEARED or SEVERITY_CHANGED (a CHECK), the value before
                   and after, who, when; written by `ran-nf-oam/app/alarm_history.py` in the transaction that changed the alarm
  alarm_comment    a note an operator left on an alarm: who, when, the text

Both reference `alarm` with ON DELETE CASCADE (an alarm removed by retention takes its history and comments with it) and are indexed on
`alarm_id`. Expand only: two new tables the previous release never reads or writes. An alarm the previous release changes during a rolling
upgrade gets no history row for that change (its code has no listener); nothing breaks. No existing row is read or backfilled: an alarm raised
before this revision has history only from now on. The table of a module with a database role needs no grant of its own (`scripts/db_roles.py`).

Revision ID: 0039
Revises: 0038
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create `ran_nf_oam.alarm_history` and `ran_nf_oam.alarm_comment` with their `alarm_id` indexes; every statement guarded, so a rerun changes nothing."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS ran_nf_oam.alarm_history (
            history_id UUID PRIMARY KEY,
            alarm_id   UUID NOT NULL REFERENCES ran_nf_oam.alarm (alarm_id) ON DELETE CASCADE,
            at         TIMESTAMP WITH TIME ZONE NOT NULL,
            event      VARCHAR NOT NULL
                       CONSTRAINT alarm_history_event_check CHECK (event IN ('RAISED', 'ACKNOWLEDGED', 'UNACKNOWLEDGED', 'CLEARED', 'SEVERITY_CHANGED')),
            from_value VARCHAR,
            to_value   VARCHAR,
            by         VARCHAR
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_alarm_history_alarm_id ON ran_nf_oam.alarm_history (alarm_id)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS ran_nf_oam.alarm_comment (
            comment_id UUID PRIMARY KEY,
            alarm_id   UUID NOT NULL REFERENCES ran_nf_oam.alarm (alarm_id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            author     VARCHAR NOT NULL,
            text       VARCHAR NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_alarm_comment_alarm_id ON ran_nf_oam.alarm_comment (alarm_id)")


def downgrade() -> None:
    """Drop both tables (the history and the comments are lost; the alarms stay). Guarded, so it can be run again."""
    op.execute("DROP TABLE IF EXISTS ran_nf_oam.alarm_comment")
    op.execute("DROP TABLE IF EXISTS ran_nf_oam.alarm_history")
