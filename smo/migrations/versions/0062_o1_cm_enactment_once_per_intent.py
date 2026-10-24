"""The O1-CM intent handler records one enactment per intent (SA-REPLAY).

  sa_smos.ux_o1_cm_enactment_intent_id    UNIQUE (intent_id): a second push of an intent (Intent Service's outbox re-sends a push that got no answer in
                                          time) is answered from the first enactment; two pushes at once are settled by this index, not by the lookup
                                          (`sa-smos/app/o1cm.py`, `enact_intent`)

Before the index is made, the duplicates the handler wrote before this revision are removed: for each intent the oldest enactment (by `created_at`, then
`enactment_id`) is kept, the one whose IntentReport Intent Service got first, and the later ones are deleted. The table is locked against writes (SHARE ROW
EXCLUSIVE) from the deletion to the index, so a replica of the previous release cannot add a duplicate in between and make the index fail.

Expand only: a new index, and no column changes. The previous release's code runs on the new schema: it reads and lists enactments as before, and it records a
first push as before. Only its repeat of a push for an intent already enacted now fails at the insert, after it has published a second report as it always
did: the shared IntegrityError handler answers 409 RESOURCE_ALREADY_EXISTS, which Intent Service's outbox counts as delivered, where it used to record a
duplicate. The current code answers that push from the record. The deleted rows are duplicates of a kept one.

Downgrade: drops the index. The deleted duplicates are not restored.

Revision ID: 0062
Revises: 0061
"""
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Locks the table against writes, deletes every enactment but the oldest of its intent, and creates the unique index; tolerates a rerun."""
    op.execute("LOCK TABLE sa_smos.o1_cm_enactment IN SHARE ROW EXCLUSIVE MODE")
    # row_number() per intent, oldest first; enactment_id breaks a tie of created_at so exactly one row per intent has n = 1
    op.execute("""
        DELETE FROM sa_smos.o1_cm_enactment e
        USING (SELECT enactment_id, row_number() OVER (PARTITION BY intent_id ORDER BY created_at, enactment_id) AS n
               FROM sa_smos.o1_cm_enactment) ranked
        WHERE e.enactment_id = ranked.enactment_id AND ranked.n > 1
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_o1_cm_enactment_intent_id ON sa_smos.o1_cm_enactment (intent_id)")


def downgrade() -> None:
    """Drops the index if it exists."""
    op.execute("DROP INDEX IF EXISTS sa_smos.ux_o1_cm_enactment_intent_id")
