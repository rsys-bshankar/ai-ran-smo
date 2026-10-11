#!/usr/bin/env python3
"""PR-DB-2.6: per-module database roles.

    python scripts/db_roles.py            # make or reconcile the roles
    python scripts/db_roles.py --list     # what it would do, without touching the database

Run after `scripts/migrate.py`, as the owner (the same `SMO_DATABASE_URL` the migration used): compose's `migrate` service and the chart's migrate Job do.
`migrations/db_roles.json` lists the modules that have a role. For each one whose password file `db_password_<name>` (the module's directory name, `samples/` left off) exists in
`SMO_DB_ROLE_PASSWORD_DIR` (default /run/secrets) the script makes sure that

  * the login role `smo_<name>` exists with that password (the file is the only place the password is kept; running the script again after
    changing the file is how a password is rotated);
  * its default search path is `<schema>, public` (just `public` for a module with no tables of its own), so the module's unqualified table names resolve to its own tables, and it needs no code to say so;
  * it can use its schema (USAGE; SELECT, INSERT, UPDATE, DELETE on every table, USAGE on every sequence, and the same for tables made later),
    the shared tables named under `shared` (read and write), the tables named under `read` as `schema.table` (SELECT only: the one place a module
    reads another's table, listed so it is visible), and `alembic_version` (the pod's schema wait reads it);
  * it has no right on any other schema's tables, and cannot create anything in `public`.

A module without a password file is skipped, so a deployment that has not adopted the roles keeps connecting as the owner and nothing changes.
The script is idempotent: privileges are revoked and granted again each time, so a table added by a later revision is covered at once and a shared
table removed from the manifest is taken away. It needs a database role that may create roles (CREATEROLE) and own the tables; the `smo` role of
the bundled Postgres is the superuser, a managed database's admin user usually is not, and then the roles must be made by whoever has that right.
"""
import json
import os
import sys
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = SMO_ROOT / "migrations" / "db_roles.json"
PASSWORD_DIR_VARIABLE = "SMO_DB_ROLE_PASSWORD_DIR"  # noqa: S105 — the name of an environment variable, not a credential


# The sequences of the shared tables that have one (an identity column): a role given the table is given its sequence too.
SHARED_SEQUENCES = {"smo_event": "smo_event_seq_seq"}

def short_name(module: str) -> str:
    """The module's name without its directory (`samples/energy-saving-rapp` -> `energy-saving-rapp`): what the role, the password file and the chart's `databaseRole` use."""
    return module.rsplit("/", 1)[-1]


def role_name(module: str) -> str:
    """The database login role of a module: `smo_` plus its short name with `-` turned into `_` (`smo_energy_saving_rapp`)."""
    return "smo_" + short_name(module).replace("-", "_")


def load_manifest(path: Path = MANIFEST) -> dict[str, dict]:
    return {m: spec for m, spec in json.loads(path.read_text()).items() if m != "_comment"}


def password_file(module: str, environ=os.environ) -> Path:
    """The path of the module's password file `db_password_<short name>` in `SMO_DB_ROLE_PASSWORD_DIR` (default `/run/secrets`)."""
    return Path(environ.get(PASSWORD_DIR_VARIABLE, "/run/secrets")) / f"db_password_{short_name(module)}"


def statements(module: str, spec: dict, password: str | None, schemas: list[str], database: str) -> list:
    """The SQL that makes `role_name(module)` what the module needs, as psycopg `sql` objects (identifiers and the password are quoted by the driver).

        Order matters: login and search path first, then every privilege on every schema is revoked, then only the module's own schema, the shared tables, the read-only
        tables and `alembic_version` are granted back. `password` None makes the role `NOLOGIN`. `schemas` is every non-system schema of the database; `database` is
        the name `CONNECT` is granted on.
    """
    from psycopg import sql
    role = sql.Identifier(role_name(module))
    schema = spec.get("schema")                   # None: the module has no table of its own
    own = sql.Identifier(schema) if schema else None
    shared = [sql.Identifier(t) for t in spec.get("shared", [])]
    out: list = []
    out.append(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(password)) if password is not None
               else sql.SQL("ALTER ROLE {} NOLOGIN").format(role))
    out.append(sql.SQL("ALTER ROLE {} SET search_path = {}public").format(role, sql.SQL("{}, ").format(own) if own else sql.SQL("")))
    out.append(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role))
    out.append(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    # start from nothing on every schema, then give back what the module has a right to
    for name in schemas:
        s = sql.Identifier(name)
        out.append(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(s, role))
        out.append(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(s, role))
        if name != schema:
            out.append(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(s, role))
    out.append(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    if own:
        out.append(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(own, role))
        out.append(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(own, role))
        out.append(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(own, role))
        out.append(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(own, role))
        out.append(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}").format(own, role))
    out.append(sql.SQL("GRANT SELECT ON public.alembic_version TO {}").format(role))
    for table in shared:
        out.append(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON public.{} TO {}").format(table, role))
    for name in spec.get("shared", []):
        if name in SHARED_SEQUENCES:                 # MSG-3.3: inserting a row of a shared table with an identity column needs its sequence
            out.append(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE public.{} TO {}").format(sql.Identifier(SHARED_SEQUENCES[name]), role))
    for qualified in spec.get("read", []):
        read_schema, read_table = qualified.split(".", 1)
        out.append(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(read_schema), role))
        out.append(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(sql.Identifier(read_schema), sql.Identifier(read_table), role))
    return out


def main(argv: list[str]) -> int:
    """Makes or reconciles the roles of every module in the manifest that has a password file; prints one line per module and returns 0.

        `--list` only prints the plan and does not connect. On a database that is not Postgres it does nothing. The whole reconciliation runs in one transaction on one
        connection and is rolled back if any statement fails, so the roles are never left half granted. A module is skipped (and named in the output) when its password file is
        missing or empty, or when a schema it needs does not exist yet (migrations not run). Also sets the owner's own search path to the module schemas, which is what
        keeps the modules working when the roles are off and they all connect as the owner.
    """
    manifest = load_manifest()
    if "--list" in argv:
        for module, spec in manifest.items():
            present = password_file(module).exists()
            print(f"{role_name(module)}: schema {spec.get('schema')}, shared {spec.get('shared', [])}, read {spec.get('read', [])}, "
                  f"password file {'found' if present else 'missing (skipped)'}")
        return 0

    from sqlalchemy import create_engine, text
    from smo_shared.db import resolve_database_url
    engine = create_engine(resolve_database_url())
    if engine.dialect.name != "postgresql":
        print("per-module roles are for Postgres; nothing to do")
        return 0
    made = []
    raw = engine.raw_connection()
    try:
        raw.autocommit = False
        cur = raw.cursor()
        cur.execute("SELECT current_database()")
        database = cur.fetchone()[0]
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'")
        schemas = sorted(r[0] for r in cur.fetchall())
        # with the roles off, every module connects as the owner, and the compatibility views that let it find a table by its bare name are gone (0029):
        # the owner's search path lists the module schemas, so it finds them as a module's own role does
        own = sorted({spec["schema"] for spec in manifest.values() if spec.get("schema") in schemas})
        if own:
            from psycopg import sql
            cur.execute("SELECT current_user")
            owner = cur.fetchone()[0]
            cur.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = {}, public").format(
                sql.Identifier(owner), sql.Identifier(database), sql.SQL(", ").join(sql.Identifier(x) for x in own)))
        for module, spec in manifest.items():
            path = password_file(module)
            if not path.exists():
                print(f"{role_name(module)}: no {path}, skipped")
                continue
            password = path.read_text().strip()
            if not password:
                print(f"{role_name(module)}: {path} is empty, skipped")
                continue
            missing = [x for x in [spec.get("schema"), *(q.split(".")[0] for q in spec.get("read", []))] if x and x not in schemas]
            if missing:
                print(f"{role_name(module)}: schema {missing[0]} does not exist (run scripts/migrate.py first), skipped")
                continue
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name(module),))
            if cur.fetchone() is None:
                from psycopg import sql
                cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name(module))))
            for statement in statements(module, spec, password, schemas, database):
                cur.execute(statement)
            made.append(role_name(module))
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    print("roles ready: " + (", ".join(made) if made else "none (no password files)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
