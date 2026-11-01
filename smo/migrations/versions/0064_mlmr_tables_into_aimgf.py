"""MLMR's seven tables move into the aimgf schema: AIMgF absorbs MLMR (plan task S2.1, ADR 0010 section 3.1, decision D4).

The model registry is now the `registry` package of AIMgF (`aimgf/app/registry/`), so its tables are AIMgF's and only AIMgF's role writes them (D6,
`migrations/db_roles.json`). Each table moves with `ALTER TABLE mlmr.<table> SET SCHEMA aimgf`: the rows, the indexes, the constraints (the unique
`(model_type, version)`, the CHECK on a coordination group's size) and the foreign keys inside the set (`model_artifact`, `ml_model_profile` and
`model_change_subscription` on `aiml_model`, the repository references) travel with their table, so no row is copied and none can be lost. The empty
`mlmr` schema is then dropped. No compatibility view is left behind (rewrite mode, `scripts/rewrite_mode.json`: the previous release's code is not run
on this schema).

  aiml_model, ml_model_coordination_group, ml_model_profile, ml_model_repository, ml_models_storage, model_artifact, model_change_subscription:
      mlmr -> aimgf

Downgrade: recreates the `mlmr` schema and moves the seven tables back, rows included.

Revision ID: 0064
Revises: 0063
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

# Repositories first and dependants after (the order does not matter to SET SCHEMA, which moves the foreign keys with the table; it is kept readable).
TABLES = (
    "ml_model_repository",
    "aiml_model",
    "ml_model_coordination_group",
    "ml_models_storage",
    "ml_model_profile",
    "model_artifact",
    "model_change_subscription",
)


def upgrade() -> None:
    """Moves the seven tables, with their rows, from `mlmr` into `aimgf`, then drops the empty `mlmr` schema."""
    op.execute("CREATE SCHEMA IF NOT EXISTS aimgf")
    for table in TABLES:
        op.execute(f"ALTER TABLE mlmr.{table} SET SCHEMA aimgf")
    # RESTRICT (the default): fails, and so rolls the revision back, if anything other than the moved tables were left in the schema.
    op.execute("DROP SCHEMA mlmr")


def downgrade() -> None:
    """Recreates the `mlmr` schema and moves the seven tables, with their rows, back into it."""
    op.execute("CREATE SCHEMA IF NOT EXISTS mlmr")
    for table in TABLES:
        op.execute(f"ALTER TABLE aimgf.{table} SET SCHEMA mlmr")
