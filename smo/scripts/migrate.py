#!/usr/bin/env python3
"""Brings the SMO database to the latest schema revision (PR-OPS-1).

    python scripts/migrate.py                 upgrade every branch to its head ("heads", the default; "head" means the same)
    python scripts/migrate.py --revision 0001 upgrade to a given revision (a branch's own: aimgf@head, aimgf_0002)
    python scripts/migrate.py --downgrade X   reverse to revision X (aimgf@-1: one step on branch aimgf; -1 only while the history has one head); take a backup first
    python scripts/migrate.py --current       print the database's revisions (one per branch, comma separated) and exit
    python scripts/migrate.py --wait SECONDS  wait until every branch of the database is at (or past) this image's head of it, up to SECONDS; exit 1 if
                                              not (what a Helm install's init containers run, so a pod never starts on an older schema, PR-OPS-3.2)

The history is a shared trunk (`0001` to `0068`) and then one branch per module (`<module>_NNNN`, branch label the module) and one `shared`
branch (docs/adr/0001-schema-migrations.md, `scripts/check_revision_branches.py`). The database's `alembic_version` then holds one row per head.

The URL comes from `SMO_DATABASE_URL` (or the `*_FILE` forms), as for every service.

On Postgres every lock a revision takes waits at most `SMO_MIGRATE_LOCK_TIMEOUT_MS` (default 5000; 0: no limit) (MIGRATE-LOCK-HOLD). The
pending revisions run in one transaction, so while one of them waited on a lock another session held, it kept the locks the earlier
ones had taken on other modules' tables and stalled their reads for as long as it waited. Now a revision that cannot get its lock in
time fails, the transaction rolls back (releasing every lock it held, and changing nothing), and the upgrade is tried again after a
pause that doubles from 1 s to 10 s, up to `SMO_MIGRATE_LOCK_ATTEMPTS` attempts (default 10); then the error ends the process.

Before the upgrade it creates the TimescaleDB extension (D8, plan task S1.10) as `SMO_TIMESCALEDB` says: `auto` (the default) creates it when the
server has it (`pg_available_extensions`) and goes on without it otherwise, `require` refuses to migrate a server without it (compose and the chart's
bundled Postgres set this: their image is timescale/timescaledb), `off` never touches it. The `common_dme_telemetry` hypertables are S1.9's revisions,
which can rely on the extension then. Creating it needs a role that may (the bootstrap superuser `smo` of compose and the bundled Postgres; on a managed
Postgres, the operator's choice: `docs/CONFIGURATION.md`).

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


def current_heads(connection) -> tuple[str, ...]:
    """The revisions the database is at, one per branch it has (sorted; empty for a database without the history)."""
    return tuple(sorted(MigrationContext.configure(connection).get_current_heads()))


def current_revision(connection) -> str | None:
    """The database's revisions as one string, comma separated (`0068`, or `0068,aimgf_0002` once branches exist), or None."""
    return ",".join(current_heads(connection)) or None


def is_legacy(connection) -> bool:
    """True for a database created from `001_init.sql` directly: it has the schema (the marker table `service_profile`) but no `alembic_version` table, so it must be stamped before it is upgraded."""
    tables = set(inspect(connection).get_table_names())
    return "alembic_version" not in tables and LEGACY_MARKER_TABLE in tables


def migrate(connection, revision: str = "heads") -> str | None:
    """Upgrades `connection`'s database to `revision` (default every branch's head); returns the revisions it is then at (`current_revision`)."""
    config = alembic_config(connection)
    if revision == "head":
        revision = "heads"                        # with branches "head" is ambiguous; it always meant "everything this image has"
    if is_legacy(connection):
        command.stamp(config, BASELINE)
        connection.commit()
    at = current_heads(connection)
    known = set(image_revisions(connection))
    if revision == "heads" and any(head not in known for head in at):
        # a revision this image does not know is a later release's: this image runs on that schema (expand and contract) and has nothing to upgrade.
        # Without this a rollback that runs this image's migrate Job again (to a revision made by `helm install`) ends in "Can't locate revision".
        return current_revision(connection)
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


TIMESCALEDB_MODES = ("auto", "require", "off")


def ensure_timescaledb(connection, mode: str | None = None) -> str:
    """Creates the TimescaleDB extension on `connection`'s database as `mode` (default `SMO_TIMESCALEDB`, else `auto`) says, and returns what it did:
    `created` (now present, whether it was before or not), `unavailable` (auto, and the server has no such extension), `skipped` (off, or not Postgres).

    `require` on a server without the extension raises SystemExit naming the variable, so the migration stops before any revision runs; any other mode is
    refused the same way. The extension's own version is the image's (no `VERSION` clause): `ALTER EXTENSION timescaledb UPDATE` after an image bump is an
    operator step, as TimescaleDB documents it.
    """
    mode = (mode if mode is not None else os.environ.get("SMO_TIMESCALEDB", "")).strip().lower() or "auto"
    if mode not in TIMESCALEDB_MODES:
        raise SystemExit(f"SMO_TIMESCALEDB must be one of {', '.join(TIMESCALEDB_MODES)}, not {mode!r}")
    if mode == "off" or connection.dialect.name != "postgresql":
        return "skipped"
    available = connection.exec_driver_sql("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'").first() is not None
    if not available:
        if mode == "require":
            raise SystemExit("SMO_TIMESCALEDB=require, but this Postgres has no timescaledb extension (use an image with it, e.g. timescale/timescaledb)")
        return "unavailable"
    connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS timescaledb")
    connection.commit()
    return "created"


def schema_is_current(db_heads, image_revisions: list[str], image_heads, applied=None) -> bool:
    """Whether a database at `db_heads` (what `alembic_version` holds) can serve code whose history is `image_revisions` and ends in `image_heads`:
    every head of this image has been applied (`applied`: the revisions under `db_heads`, through parents and `depends_on`; default `db_heads`
    themselves), or the database is past this image (a revision this image does not know is a later release's: the previous release's code runs on
    a newer schema, by the expand/contract rule). Alembic records a revision a later one depends on only through that later one, so `db_heads` can be
    fewer than `image_heads` on a database that is up to date."""
    db_heads, image_heads = set(db_heads or ()), set(image_heads or ())
    if not db_heads or not image_revisions:
        return False
    if any(head not in image_revisions for head in db_heads):
        return True
    return image_heads <= (set(applied) if applied is not None else db_heads)


def applied_revisions(scripts, db_heads) -> set[str]:
    """Every revision a database at `db_heads` has applied: the heads and everything under them, through `down_revision` and `depends_on`."""
    pending, seen = list(db_heads), set()
    while pending:
        revision = scripts.get_revision(pending.pop())
        if revision is None or revision.revision in seen:
            continue
        seen.add(revision.revision)
        pending.extend(revision._all_down_revisions)
    return seen


def _scripts(connection):
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(alembic_config(connection))


def image_revisions(connection) -> list[str]:
    """The revision ids of this checkout's migration history, newest first. A database revision that is not in this list was made by a later release."""
    return [r.revision for r in _scripts(connection).walk_revisions()]


def image_heads(connection) -> tuple[str, ...]:
    """The heads of this checkout's history (the trunk's end until a branch has a revision, then one per branch), sorted."""
    return tuple(sorted(_scripts(connection).get_heads()))


def wait_for_schema(seconds: float, interval: float = 3.0) -> bool:
    """Blocks until the database answers and every branch of it is at this image's head (or past it); False when `seconds` pass first."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            engine = create_engine(resolve_database_url())
            with engine.connect() as connection:
                at, known = current_heads(connection), image_revisions(connection)
                applied = applied_revisions(_scripts(connection), [h for h in at if h in known])
                if schema_is_current(at, known, image_heads(connection), applied):
                    return True
        except Exception as exc:                  # the database is not up yet, or the migration has not made its tables: keep waiting
            print(f"waiting for the database: {type(exc).__name__}", flush=True)
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def downgrade(connection, revision: str) -> str | None:
    """Reverses to `revision` (a revision id, `<branch>@-1` for one step on a branch, `-1` while the history has one head); returns the revisions the
    database is then at."""
    _with_lock_retries(connection, lambda: command.downgrade(alembic_config(connection), revision))
    connection.commit()
    return current_revision(connection)


def main() -> int:
    """Command-line entry: waits for the schema (`--wait`), prints the current revisions (`--current`), downgrades (`--downgrade`), or upgrades to `--revision` (default heads).

        Returns 0, or 1 only when `--wait` runs out. An upgrade of a database that is already past this image's head is a no-op that says so (the rolling-upgrade rule: a
        previous release's image must start against a newer schema). Errors from Alembic or the database propagate and end the process with a traceback.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--revision", default="heads")
    parser.add_argument("--downgrade", metavar="REVISION", help="reverse to REVISION (-1: one step) instead of upgrading")
    parser.add_argument("--current", action="store_true")
    parser.add_argument("--wait", type=float, metavar="SECONDS", help="wait for every branch of the database to be at this image's head instead of migrating it")
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
        timescaledb = ensure_timescaledb(connection)
        print(f"timescaledb: {timescaledb}", flush=True)
        legacy = is_legacy(connection)
        revision = migrate(connection, args.revision)
        known = set(image_revisions(connection))
        if args.revision in ("head", "heads") and any(head not in known for head in current_heads(connection)):
            print(f"the database is at {revision}, a revision this image does not know (a later release): nothing to upgrade")
            return 0
        print(f"{'stamped the existing schema at ' + BASELINE + ' and ' if legacy else ''}upgraded to {revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
