"""Drops the compatibility views `0023`, `0024` and `0025` left in `public` when the module tables moved into schemas of their own (PR-DB-2).

Each of those revisions moved a module's tables with `ALTER TABLE ... SET SCHEMA` and left an updatable view of the same name behind, "for the previous release's code,
until the revision after the next release" (docs/adr/0001-schema-migrations.md): the code of 0.3.0 names its tables without a schema and found them through `public`.
This is that revision. The previous release now is 0.4.0, whose modules connect as their own roles with the module's schema first on the search path
(`scripts/db_roles.py`) and never read the views (`scripts/check_previous_release_code.sh` has said so since 0.4.0), so nothing the rolling upgrade or the rollback
to 0.4.0 needs is removed. What does stop working: a connection that names a module table with no schema as the database owner (a script of the operator's, `psql` as
`smo`, the code of 0.3.0). Name the schema (`onboarding.application_package`) or connect as the module's role.

Which views go is read from the catalogue and not from a list: a view in `public` that depends on a base table of the same name in another schema, which is exactly
what the three revisions made and nothing else. The downgrade makes them again for every base table outside `public`, with the columns the tables had when the views were made (a column a later revision added is left out, as it was then).

The tables the previous release's code uses that are not moved (the A1 tables, `a1_ei_type` and the others, used by 0.4.0's `a1-related` module) are not touched:
they go in the revision after the next release.

Revision ID: 0029
Revises: 0028
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop every view in `public` that selects from a base table of the same name in another schema, which is what the compatibility views of 0023 to 0025 are; found in the catalogue, not from a list."""
    # A PL/pgSQL block rather than a fixed list: it removes exactly the views that depend on a same-named table in another schema, so a view of any other kind in `public` is left alone.
    op.execute("""
        DO $$
        DECLARE v record;
        BEGIN
            FOR v IN
                SELECT DISTINCT vc.relname
                FROM pg_class vc
                JOIN pg_namespace vn ON vn.oid = vc.relnamespace AND vn.nspname = 'public'
                JOIN pg_rewrite rw ON rw.ev_class = vc.oid
                JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = rw.oid AND d.refobjid <> vc.oid
                JOIN pg_class tc ON tc.oid = d.refobjid AND tc.relkind IN ('r', 'p') AND tc.relname = vc.relname
                JOIN pg_namespace tn ON tn.oid = tc.relnamespace AND tn.nspname <> 'public'
                WHERE vc.relkind = 'v'
            LOOP
                EXECUTE format('DROP VIEW public.%I', v.relname);
            END LOOP;
        END $$
    """)


# columns a revision after `0025` added to a table that has a view: the view, made by `0025`, does not have them, and `0027`'s downgrade (which runs after this one) drops the
# column, which it could not do under a view that selects it
ADDED_AFTER_THE_VIEWS = {("nfo", "lcm_operation"): {"created_at"}}               # 0027


def downgrade() -> None:
    """Create the views again for every base table outside `public`, with the columns the tables had when the views were first made (`ADDED_AFTER_THE_VIEWS` are left out).
    A table that belongs to an extension (TimescaleDB's catalog, on a server with it: S1.10) had no view and gets none."""
    bind = op.get_bind()
    columns: dict[tuple[str, str], list[str]] = {}
    rows = bind.exec_driver_sql("""
        SELECT c.table_schema, c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
        WHERE c.table_schema NOT IN ('public', 'information_schema') AND c.table_schema !~ '^pg_'
          AND NOT EXISTS (SELECT 1 FROM pg_class pc JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                          JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = pc.oid AND d.deptype = 'e'
                          WHERE pn.nspname = c.table_schema AND pc.relname = c.table_name)
        ORDER BY c.table_schema, c.table_name, c.ordinal_position
    """)
    for schema, table, column in rows:
        if column not in ADDED_AFTER_THE_VIEWS.get((schema, table), ()):
            columns.setdefault((schema, table), []).append(column)
    for (schema, table), names in columns.items():
        select = ", ".join(f'"{name}"' for name in names)
        op.execute(f'CREATE VIEW public."{table}" AS SELECT {select} FROM "{schema}"."{table}"')
