"""The O1-CM intent handler records that an enacted intent was deactivated or deleted (INTENT-LIFECYCLE-PUSH).

`sa_smos.o1_cm_enactment` gains two columns:

  deactivated_at    TIMESTAMPTZ NULL   when the handler learnt the intent was DEACTIVATED; null while it is ACTIVATED (cleared on activation)
  deleted_at        TIMESTAMPTZ NULL   when the handler learnt the intent was deleted

Intent Service now pushes the intent's RMIH on an admin-state change and on a delete, not only on create (`intent-service/app/main.py`,
`_push_to_rmih`); `sa-smos/app/o1cm.py` (`enact_intent`) records those on the enactment and changes nothing in the network.

Expand only: two nullable columns with no default, so every existing row reads "active, not deleted", which is what the handler knew of it. The previous
release's code runs on the new schema: it does not name the columns, and its insert leaves them null.

Downgrade: drops both columns (what they recorded is lost; nothing else reads it).

Revision ID: 0063
Revises: 0062
"""
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

COLUMNS = ("deactivated_at", "deleted_at")


def upgrade() -> None:
    """Adds the two nullable columns (guarded by IF NOT EXISTS)."""
    for column in COLUMNS:
        op.execute(f"ALTER TABLE sa_smos.o1_cm_enactment ADD COLUMN IF NOT EXISTS {column} TIMESTAMPTZ")


def downgrade() -> None:
    """Drops the two columns."""
    for column in COLUMNS:
        op.execute(f"ALTER TABLE sa_smos.o1_cm_enactment DROP COLUMN IF EXISTS {column}")
