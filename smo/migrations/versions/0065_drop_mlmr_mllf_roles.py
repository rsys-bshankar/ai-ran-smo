"""The database roles of the removed MLMR and MLLF services are dropped (plan task S2.8, ADR 0010).

Since S2.1 and S2.2 the model registry and the deploy gate are packages of AIMgF, and S2.8 removes the two services. Their roles go with them:

  smo_mlmr   made by `scripts/db_roles.py` while MLMR had an entry in `migrations/db_roles.json`; it owns no table since revision 0064 (its
             tables moved into the aimgf schema), only privileges on the shared tables and its default search path
  smo_mllf   never made by a release (MLLF had no database), dropped if an operator made it by hand

Each is dropped only when it exists: `DROP OWNED BY` first (it revokes every privilege the role holds in this database, and the default
privileges granted to it), then `DROP ROLE`. Roles are cluster-wide, so the revision needs the right to drop them (the owner role that runs the
migrations has it in compose and the chart, as it has for `scripts/db_roles.py`). No table, column or row changes.

Downgrade: recreates `smo_mlmr` with no login and no privilege, which is what `scripts/db_roles.py` of the previous release expects to find and
complete (it sets the password, the search path and the grants); `smo_mllf` is not recreated, since no release made it.

Revision ID: 0065
Revises: 0064
"""
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

ROLES = ("smo_mlmr", "smo_mllf")


def upgrade() -> None:
    """Drops each role that exists, after revoking what it owns or was granted in this database."""
    for role in ROLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'DROP OWNED BY {role}';
                    EXECUTE 'DROP ROLE {role}';
                END IF;
            END
            $$""")


def downgrade() -> None:
    """Recreates `smo_mlmr` with no login and no privilege, for `scripts/db_roles.py` of the previous release to complete."""
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'smo_mlmr') THEN
                CREATE ROLE smo_mlmr NOLOGIN;
            END IF;
        END
        $$""")
