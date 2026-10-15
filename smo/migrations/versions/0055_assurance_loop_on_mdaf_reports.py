"""SA SMOS closes the assurance loop on MDAF's reports, and a remedial action says when, who, why and what came of it (call flow 04, DOC-CF-1.8).

`assurance_monitor` gains `analytics_type` (the MDAF analytics type a monitor watches; SA SMOS subscribes to it and evaluates each report pushed),
`auto_action` (what a breach runs by itself, set by an admin), `remedy_changes` (the change list CONFIG_CHANGE writes), `last_evaluation` (the
outcome of the latest pushed report) and `created_at`.

`remedial_action` gains `created_at`, `requested_by`, `detail`, `result` and `report_id`, and its action type may be `RETRAIN` (a coordination-group
monitor's retrain, before recorded under whatever type was asked) or `ESCALATE` (an escalation with nothing dispatched, before recorded as
CONFIG_CHANGE). The type check is replaced by one that also allows them.

Expand only: every column is nullable with no server default, the check allows more. The previous release's code runs on the new schema.

Revision ID: 0055
Revises: 0054
"""
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

MONITOR = "sa_smos.assurance_monitor"
ACTION = "sa_smos.remedial_action"
TYPES = "'CONFIG_CHANGE', 'SCALE', 'RECONNECT', 'ROLLBACK'"
MONITOR_COLUMNS = (("analytics_type", "TEXT"), ("auto_action", "TEXT"), ("remedy_changes", "JSONB"), ("last_evaluation", "JSONB"),
                   ("created_at", "TIMESTAMPTZ"))
ACTION_COLUMNS = (("created_at", "TIMESTAMPTZ"), ("requested_by", "TEXT"), ("detail", "TEXT"), ("result", "JSONB"), ("report_id", "UUID"))


def upgrade() -> None:
    """Adds the columns (guarded by IF NOT EXISTS) and allows RETRAIN and ESCALATE as action types."""
    for name, kind in MONITOR_COLUMNS:
        op.execute(f"ALTER TABLE {MONITOR} ADD COLUMN IF NOT EXISTS {name} {kind}")
    for name, kind in ACTION_COLUMNS:
        op.execute(f"ALTER TABLE {ACTION} ADD COLUMN IF NOT EXISTS {name} {kind}")
    op.execute(f"ALTER TABLE {ACTION} DROP CONSTRAINT remedial_action_action_type_check")
    op.execute(f"ALTER TABLE {ACTION} ADD CONSTRAINT remedial_action_action_type_check CHECK (action_type IN ({TYPES}, 'RETRAIN', 'ESCALATE'))")


def downgrade() -> None:
    """Puts the narrower check back, recording a RETRAIN or ESCALATE row under the type the previous release wrote for it, and drops the columns."""
    op.execute(f"UPDATE {ACTION} SET action_type = 'CONFIG_CHANGE' WHERE action_type IN ('RETRAIN', 'ESCALATE')")
    op.execute(f"ALTER TABLE {ACTION} DROP CONSTRAINT remedial_action_action_type_check")
    op.execute(f"ALTER TABLE {ACTION} ADD CONSTRAINT remedial_action_action_type_check CHECK (action_type IN ({TYPES}))")
    for name, _ in ACTION_COLUMNS:
        op.execute(f"ALTER TABLE {ACTION} DROP COLUMN IF EXISTS {name}")
    for name, _ in MONITOR_COLUMNS:
        op.execute(f"ALTER TABLE {MONITOR} DROP COLUMN IF EXISTS {name}")
