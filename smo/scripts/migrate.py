#!/usr/bin/env python3
"""Brings the SMO database to the latest schema revision (PR-OPS-1).

    python scripts/migrate.py                 upgrade to head (the default)
    python scripts/migrate.py --revision 0001 upgrade to a given revision
    python scripts/migrate.py --downgrade -1  reverse one revision (or to a revision id); take a backup first
    python scripts/migrate.py --current       print the database's revision and exit
    python scripts/migrate.py --wait SECONDS  wait until the database is at (or past) this image's head revision, up to SECONDS; exit 1 if it is not
                                              (what a Helm install's init containers run, so a pod never starts on an older schema, PR-OPS-3.2)

The URL comes from `SMO_DATABASE_URL` (or the `*_FILE` forms), as for every service.

On Postgres every lock a revision takes waits at most `SMO_MIGRATE_LOCK_TIMEOUT_MS` (default 5000; 0: no limit) (MIGRATE-LOCK-HOLD). The
pending revisions run in one transaction, so while one of them waited on a lock another session held, it kept the locks the earlier
ones had taken on other modules' tables and stalled their reads for as long as it waited. Now a revision that cannot get its lock in
time fails, the transaction rolls back (releasing every lock it held, and changing nothing), and the upgrade is tried again after a
pause that doubles from 1 s to 10 s, up to `SMO_MIGRATE_LOCK_ATTEMPTS` attempts (default 10); then the error ends the process.

A database that has the schema but no `alembic_version` table was created from `migrations/001_init.sql` directly
(docker compose's initdb does this): it is stamped at the baseline revision first, so it is upgraded from there,
never re-created. An empty database runs the baseline.
"""

import argparse
import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import DBAPIError

SMO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO_ROOT / "shared"))

from smo_shared.db import resolve_database_url  # noqa: E402

BASELINE = "0001"
# a table every baseline database has: its presence without alembic_version means "legacy, stamp me"
LEGACY_MARKER_TABLE = "service_profile"


def alembic_config(connection) -> Config:
    """An Alembic `Config` for the migrations in `smo/migrations` that runs on the given SQLAlchemy `connection` (passed to `migrations/env.py` through `config.attributes`), so the caller owns the transaction."""
    config = Config(str(SMO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SMO_ROOT / "migrations"))
    config.attributes["connection"] = connection
    return config


def current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def is_legacy(connection) -> bool:
    """True for a database created from `001_init.sql` directly: it has the schema (the marker table `service_profile`) but no `alembic_version` table, so it must be stamped before it is upgraded."""
    tables = set(inspect(connection).get_table_names())
    return "alembic_version" not in tables and LEGACY_MARKER_TABLE in tables


def migrate(connection, revision: str = "head") -> str | None:
    """Upgrades `connection`'s database to `revision`; returns the revision it is then at."""
    config = alembic_config(connection)
    if is_legacy(connection):
        command.stamp(config, BASELINE)
        connection.commit()
    at = current_revision(connection)
    if revision == "head" and at is not None and at not in image_revisions(connection):
        # a revision this image does not know is a later release's: this image runs on that schema (expand and contract) and has nothing to upgrade.
        # Without this a rollback that runs this image's migrate Job again (to a revision made by `helm install`) ends in "Can't locate revision".
        return at
    _with_lock_retries(connection, lambda: command.upgrade(config, revision))
    connection.commit()
    return current_revision(connection)


LOCK_NOT_AVAILABLE = "55P03"     # Postgres: a lock was not granted within lock_timeout


def _env_int(name: str, default: int) -> int:
    """`name` from the environment as a whole number of at least 0, or `default` when it is unset or empty; anything else is refused with the variable named."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, not {raw!r}") from None
    if value < 0:
        raise SystemExit(f"{name} must be 0 or more, not {value}")
    return value


def _is_lock_timeout(error: Exception) -> bool:
    """Whether `error` is Postgres refusing a lock after `lock_timeout` (SQLSTATE 55P03), however the driver wraps it."""
    original = getattr(error, "orig", error)
    return getattr(original, "sqlstate", None) == LOCK_NOT_AVAILABLE or getattr(original, "pgcode", None) == LOCK_NOT_AVAILABLE


def _with_lock_retries(connection, run, sleep=time.sleep) -> None:
    """Runs `run` (one Alembic upgrade or downgrade, in its own transaction) on `connection` with a lock timeout on Postgres, again after a lock timeout (MIGRATE-LOCK-HOLD).

    Sets the session's `lock_timeout` to `SMO_MIGRATE_LOCK_TIMEOUT_MS` (default 5000; 0 leaves it unset) and commits it, so the transaction Alembic opens has it. A
    lock not granted in time rolls the whole transaction back, which releases the locks the earlier revisions took instead of holding them while it waits; the run is
    repeated after 1, 2, 4, 8 then 10 s, up to `SMO_MIGRATE_LOCK_ATTEMPTS` attempts (default 10), and the last error is raised. Any other error is raised at once.
    Another database (SQLite in the tests) runs `run` once, unchanged.
    """
    if connection.dialect.name != "postgresql":
        run()
        return
    timeout_ms = _env_int("SMO_MIGRATE_LOCK_TIMEOUT_MS", 5000)
    attempts = max(1, _env_int("SMO_MIGRATE_LOCK_ATTEMPTS", 10))
    if timeout_ms:
        connection.exec_driver_sql(f"SET lock_timeout = {timeout_ms}")
        connection.commit()
    pause = 1.0
    for attempt in range(1, attempts + 1):
        try:
            run()
            return
        except DBAPIError as error:
            if connection.in_transaction():
                connection.rollback()
            if not _is_lock_timeout(error) or attempt == attempts:
                raise
            print(f"a lock was not granted within {timeout_ms} ms (attempt {attempt} of {attempts}); nothing was changed, trying again in {pause:g} s", flush=True)
            sleep(pause)
            pause = min(pause * 2, 10.0)


def schema_is_current(db_revision: str | None, image_revisions: list[str]) -> bool:
    """Whether a database at `db_revision` can serve code whose revision history is `image_revisions` (head first): it is at that head, or past it
    (a revision this image does not know is a later one: the previous release's code runs on a newer schema, by the expand/contract rule)."""
    if db_revision is None or not image_revisions:
        return False
    return db_revision not in image_revisions or db_revision == image_revisions[0]


def image_revisions(connection) -> list[str]:
    """The revision ids of this checkout's migration history, newest first. A database revision that is not in this list was made by a later release."""
    from alembic.script import ScriptDirectory
    return [r.revision for r in ScriptDirectory.from_config(alembic_config(connection)).walk_revisions()]


def wait_for_schema(seconds: float, interval: float = 3.0) -> bool:
    """Blocks until the database answers and is at this image's head (or past it); False when `seconds` pass first."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            engine = create_engine(resolve_database_url())
            with engine.connect() as connection:
                if schema_is_current(current_revision(connection), image_revisions(connection)):
                    return True
        except Exception as exc:                  # the database is not up yet, or the migration has not made its tables: keep waiting
            print(f"waiting for the database: {type(exc).__name__}", flush=True)
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def downgrade(connection, revision: str) -> str | None:
    """Reverses to `revision` (a revision id, or `-1` for one step); returns the revision the database is then at."""
    _with_lock_retries(connection, lambda: command.downgrade(alembic_config(connection), revision))
    connection.commit()
    return current_revision(connection)


def main() -> int:
    """Command-line entry: waits for the schema (`--wait`), prints the current revision (`--current`), downgrades (`--downgrade`), or upgrades to `--revision` (default head).

        Returns 0, or 1 only when `--wait` runs out. An upgrade of a database that is already past this image's head is a no-op that says so (the rolling-upgrade rule: a
        previous release's image must start against a newer schema). Errors from Alembic or the database propagate and end the process with a traceback.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--revision", default="head")
    parser.add_argument("--downgrade", metavar="REVISION", help="reverse to REVISION (-1: one step) instead of upgrading")
    parser.add_argument("--current", action="store_true")
    parser.add_argument("--wait", type=float, metavar="SECONDS", help="wait for the database to be at this image's head instead of migrating it")
    args = parser.parse_args()
    if args.wait is not None:
        ready = wait_for_schema(args.wait)
        print("the schema is current" if ready else f"the schema was not current after {args.wait:g} s", flush=True)
        return 0 if ready else 1
    engine = create_engine(resolve_database_url())
    with engine.connect() as connection:
        if args.current:
            print(current_revision(connection) or "(none)")
            return 0
        if args.downgrade:
            print(f"downgraded to {downgrade(connection, args.downgrade) or '(none)'}")
            return 0
        legacy = is_legacy(connection)
        revision = migrate(connection, args.revision)
        if args.revision == "head" and revision not in image_revisions(connection):
            print(f"the database is at {revision}, a revision this image does not know (a later release): nothing to upgrade")
            return 0
        print(f"{'stamped the existing schema at ' + BASELINE + ' and ' if legacy else ''}upgraded to {revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
