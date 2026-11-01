#!/usr/bin/env python3
"""Compares the real Postgres schema, migrated to the head revision,
against every module's own SQLAlchemy ORM model definitions, table by
table and column by column.

HISTORY.md §2: two real Postgres-only bugs (`rapp_instance`'s
`pending_upgrade_instance_id` entirely missing from the migration;
`oauth_client_id` wrongly `NOT NULL` there even though the ORM/app code
sets it back to NULL) were only ever caught by luck — the
migration-postgres CI job checked table *count* (>= 30), never columns,
and SQLite's own unit tests build their schema from the ORM models
directly, never from this file, so neither test path could ever have
caught either bug. This is that column-level check, made automatic —
the same "catch real structural drift automatically instead of relying
on human diligence" philosophy already used for the OpenAPI spec
drift-check and the docker-compose-config CI job.

Scope is deliberately narrow: column *presence* and *nullability* only
— exactly the two bug classes already found, not a full type-equality
check (SQLAlchemy's generic String()/Text()/ARRAY-with-SQLite-variant
types don't map to a single canonical Postgres type name, so a strict
type comparison would produce noise unrelated to any real bug).

Beyond those two, `main` also holds the migrated schema to the table-ownership
rules: every table has an owner in `migrations/table_owners.json`, a module with
a schema of its own (`migrations/db_roles.json`) keeps all its tables there, and
no foreign key crosses a module boundary (PR-DB-2).

Requires a live Postgres migrated to the head revision with
`python scripts/migrate.py` (SMO_DATABASE_URL), which this script also
verifies: a model change needs a revision (migrations/versions/, PR-OPS-1,
docs/adr/0001-schema-migrations.md), and the check fails until the database,
migrated to head, has the column. Run from the migration-postgres CI job.
"""

import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

if not os.environ.get("SMO_DATABASE_URL"):  # no default credentials (PR-DB-1); checked before the app modules import smo_shared.db
    sys.exit("SMO_DATABASE_URL is not set: point it at a Postgres migrated with `python scripts/migrate.py` "
             "(see smo/CLAUDE.md, step 4).")

SMO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO_ROOT / "tests_integration"))
sys.path.insert(0, str(SMO_ROOT / "sdk"))  # the sample rApp imports the AI Runtime SDK

from loader import load_app_module  # noqa: E402

from smo_shared.db import Base  # noqa: E402
import smo_shared.idempotency  # noqa: E402,F401  (registers idempotency_key on the shared Base)
import smo_shared.module_identity  # noqa: E402,F401  (registers module_identity on the shared Base)
import smo_shared.single_runner  # noqa: E402,F401  (registers periodic_run on the shared Base)
import smo_shared.outbox  # noqa: E402,F401  (registers notification_outbox on the shared Base)
import smo_shared.audit  # noqa: E402,F401  (registers audit_log and audit_head)
import smo_shared.ratelimit  # noqa: E402,F401  (registers rate_bucket)

# Every module that persists to Postgres via smo_shared.db.Base. mock-o1-adaptor has no
# models and no tables in the migrations, so it is deliberately left out.
ALL_MODULES = [
    "r1-termination", "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam",
    "nfo", "focom", "aimgf", "ran-analytics", "mdaf",
    "intent-service", "so-smos", "sa-smos",
    "samples/energy-saving-rapp",  # Wave 10.1: the reference rApp's own tables
    "samples/mobility-optimization-rapp",  # Wave 10.2
    "samples/coverage-optimization-rapp",  # Wave 10.3
    "samples/traffic-steering-rapp",  # Wave 10.4
]


def check_at_head(engine) -> None:
    """Exits the process with a message unless the database is at the migration head: a model checked against an old schema would report drift that a migration has already fixed."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config(str(SMO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SMO_ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != head:
        sys.exit(f"the database is at revision {current!r}, the migration head is {head!r}: "
                 "run `python scripts/migrate.py` first")


def main() -> None:
    """Loads every module's ORM models onto the shared `Base`, compares them with the migrated schema and exits 1 with a list of every mismatch, or prints a confirmation.

        The comparisons, in order: each declared table exists; each declared column exists and has the same nullability; every table in the database has an owner in
        `migrations/table_owners.json` and the other way round; a module with a schema of its own (`migrations/db_roles.json`) has all its tables there and nothing else
        does; no foreign key points into another module's table (PR-DB-2.4). `SMO_DATABASE_URL` must be set (checked at import) and point at a migrated Postgres.
    """
    for module_dir in ALL_MODULES:
        load_app_module(module_dir)  # side effect: registers that module's tables on the shared Base

    database_url = os.environ["SMO_DATABASE_URL"]
    engine = create_engine(database_url)
    inspector = inspect(engine)
    check_at_head(engine)
    # a module that has a schema of its own (PR-DB-2.5) keeps its tables there; the ORM names them without one, and so does this check
    schema_of: dict[str, str | None] = {}
    for schema in inspector.get_schema_names():
        if schema.startswith("pg_") or schema == "information_schema":
            continue
        for name in inspector.get_table_names(schema=schema):
            schema_of[name] = None if schema == "public" else schema
    real_tables = set(schema_of)

    errors = []
    for table in Base.metadata.sorted_tables:
        if table.name not in real_tables:
            errors.append(f"table {table.name!r}: declared by the ORM but missing from the migrated schema")
            continue
        real_columns = {c["name"]: c for c in inspector.get_columns(table.name, schema=schema_of[table.name])}
        for column in table.columns:
            if column.name not in real_columns:
                errors.append(f"{table.name}.{column.name}: declared by the ORM but missing from the migrated schema")
                continue
            real_nullable = bool(real_columns[column.name]["nullable"])
            if real_nullable != bool(column.nullable):
                errors.append(
                    f"{table.name}.{column.name}: ORM says nullable={column.nullable}, "
                    f"schema says nullable={real_nullable}"
                )

    # PR-DB-2: every table has an owner in migrations/table_owners.json, and no foreign key reaches into another module's table
    owners = {table: owner for owner, tables in json.loads((SMO_ROOT / "migrations" / "table_owners.json").read_text()).items()
              if owner != "_comment" for table in tables}
    for table in sorted(real_tables - set(owners) - {"alembic_version"}):
        errors.append(f"table {table!r}: in the migrated schema but not in migrations/table_owners.json")
    for table in sorted(set(owners) - real_tables):
        errors.append(f"table {table!r}: in migrations/table_owners.json but not in the migrated schema")
    # a module with a schema of its own (migrations/db_roles.json) has all its tables in it, and nothing else is in it
    for module, spec in json.loads((SMO_ROOT / "migrations" / "db_roles.json").read_text()).items():
        if module == "_comment" or not spec["schema"]:      # a module with no tables of its own has no schema to keep to
            continue
        for table in sorted(real_tables & set(owners)):
            in_schema = schema_of[table] == spec["schema"]
            if (owners[table] == module) != in_schema:
                errors.append(f"table {table!r} (owner {owners[table]}) is in schema {schema_of[table] or 'public'}: "
                              f"schema {spec['schema']} is for {module}'s tables and only those")
    for table in sorted(real_tables & set(owners)):
        for fk in inspector.get_foreign_keys(table, schema=schema_of[table]):
            target = fk["referred_table"]
            if owners.get(target) not in (None, owners[table]):
                errors.append(f"foreign key {fk['name']}: {table} ({owners[table]}) -> {target} ({owners[target]}) crosses a module boundary; "
                              "keep the column as a plain id (PR-DB-2.4)")

    if errors:
        print(f"Schema mismatches between the migrated schema and the ORM models ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print(f"OK: {len(Base.metadata.sorted_tables)} tables checked against real Postgres, "
          f"all ORM-declared columns present with matching nullability.")


if __name__ == "__main__":
    main()
