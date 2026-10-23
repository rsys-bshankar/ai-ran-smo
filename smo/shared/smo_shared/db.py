"""The database plumbing every module shares: the engine and session factory, the declarative `Base`, and the two ways to get a session.

Where it sits: every module's `models.py` subclasses `Base`; request handlers take `get_session` as a FastAPI dependency, background code uses
`session_scope()`. The URL comes from `SMO_DATABASE_URL` (or `SMO_DATABASE_URL_FILE`, with the password from `secretfile.read_secret`); see PR-DB-1
and PR-ST-6 for the decisions. There is no default URL: a process without one raises `MissingDatabaseUrl` at import, unless `SMO_ALLOW_SQLITE_FALLBACK` is set (the unit-test conftests set it).

What a maintainer must know: `engine` and `SessionLocal` are built when this module is first imported (the engine opens no connection until first
use), so the environment must be complete before any `smo_shared.db` import. `session_scope` commits and `get_session` does not: a route that writes
through `get_session` commits itself.
"""
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .secretfile import read_secret

# The modules share ONE Postgres database, not one per module. Each module with tables has its own schema and its own role
# (revisions 0023 to 0025, `migrations/db_roles.json`, `scripts/db_roles.py`): the role's search path is `<schema>, public`, so a
# model names its table without the schema, and the shared tables (outbox, audit, identities) stay in `public` (OPEN_ITEMS DB-2.7).
#
# There is no default URL (PR-DB-1): a default would put a known database password in every image and
# let a service that lost its configuration quietly talk to whatever answers on `postgres:5432`.
# `SMO_DATABASE_URL` must be set, or the process refuses to start with a message saying so.
class MissingDatabaseUrl(RuntimeError):
    """`SMO_DATABASE_URL` is not set."""


# The unit tests build the engine at import without a database (they override the session, or use
# `testing.make_test_engine()`), so when `SMO_ALLOW_SQLITE_FALLBACK` is switched on an unset URL is an in-memory SQLite, never a server.
# The fallback is an explicit opt-in (each test suite's conftest.py sets it through `testing.enable_sqlite_fallback()`), not a guess from
# "pytest" being in sys.modules: a production process that happens to import pytest (a plugin, a debugging shell) must still refuse to start
# without a database rather than silently keep its data in memory.
TEST_DATABASE_URL = "sqlite://"
SQLITE_FALLBACK_VARIABLE = "SMO_ALLOW_SQLITE_FALLBACK"
_TRUE = frozenset({"1", "true", "yes", "on"})


def resolve_database_url(environ=os.environ, allow_sqlite_fallback: bool | None = None) -> str:
    """The database URL, from `SMO_DATABASE_URL` or the file named by `SMO_DATABASE_URL_FILE`; the password may
    be kept out of the URL in `SMO_DATABASE_PASSWORD` or, better, the file named by `SMO_DATABASE_PASSWORD_FILE`
    (`secretfile.py`), and is then put into it. Compose uses that last form, so no container's environment
    carries the password.

    When no URL is configured it returns the in-memory SQLite URL only if `allow_sqlite_fallback` is true (None reads `SMO_ALLOW_SQLITE_FALLBACK`
    from `environ`: `1`, `true`, `yes` or `on`); otherwise it raises `MissingDatabaseUrl`.
    """
    url = (read_secret("SMO_DATABASE_URL", environ) or "").strip()
    if not url:
        if allow_sqlite_fallback is None:
            allow_sqlite_fallback = (environ.get(SQLITE_FALLBACK_VARIABLE) or "").strip().lower() in _TRUE
        if allow_sqlite_fallback:
            return TEST_DATABASE_URL
        raise MissingDatabaseUrl(
            "SMO_DATABASE_URL is not set. Set it to the Postgres URL for this deployment, for example "
            "postgresql+psycopg://<user>:<password>@<host>:5432/<database> (docker compose sets it, with the "
            "password in a secret file: run scripts/init_secrets.sh, see docs/SECRETS.md). There is deliberately no default."
        )
    password = read_secret("SMO_DATABASE_PASSWORD", environ)
    if password is not None:
        url = make_url(url).set(password=password).render_as_string(hide_password=False)
    return url


DATABASE_URL = resolve_database_url()



def engine_options(url: str, environ=os.environ) -> dict:
    """`create_engine` keyword arguments for `url`, tuned from the environment (PR-ST-6).

    Connection pool (one pool per process, so a module run as N replicas with W workers each holds up to
    N x W x (pool_size + max_overflow) connections; size it against Postgres's `max_connections`):

      SMO_DB_POOL_SIZE                5      connections kept open per process
      SMO_DB_MAX_OVERFLOW             10     extra connections allowed under load
      SMO_DB_POOL_TIMEOUT_SECONDS     30     how long a request waits for a free connection
      SMO_DB_POOL_RECYCLE_SECONDS     1800   replace a connection older than this (0: never)

    Postgres session limits, so a stuck query or a leaked transaction cannot hold a connection for ever
    (0 turns a limit off):

      SMO_DB_STATEMENT_TIMEOUT_MS                    30000    cancel a statement that runs longer
      SMO_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS          300000   end a session idle inside a transaction this long

    The idle-in-transaction default is deliberately longer than any request: a route may hold a transaction
    open across calls to other modules (each bounded by `timeouts.py`).

    Behind a connection pooler in transaction mode (PgBouncer, PR-DB-5), set SMO_DB_POOLER=transaction. The process then does not send the
    session limits above as a startup parameter (a pooler refuses `options`; the pooler sets the same limits itself, see pgbouncer/entrypoint.sh)
    and does not use server-side prepared statements, which a pooler that hands the next transaction a different server connection loses
    ("prepared statement ... does not exist"):

      SMO_DB_POOLER                   (unset)  transaction | session | (unset)
      SMO_DB_PREPARE_THRESHOLD        see text number of repeats after which psycopg prepares a statement (default 5; with SMO_DB_POOLER=transaction
                                      the default is off, and a value turns it back on for a pooler that tracks prepared statements,
                                      PgBouncer 1.21+ with max_prepared_statements > 0)

    SQLite (the unit tests) gets none of these: it has no server-side pool or session limits.
    """
    options: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        return options

    def number(name: str, default: int) -> int:
        return int(environ.get(name, default))

    options["pool_size"] = number("SMO_DB_POOL_SIZE", 5)
    options["max_overflow"] = number("SMO_DB_MAX_OVERFLOW", 10)
    options["pool_timeout"] = number("SMO_DB_POOL_TIMEOUT_SECONDS", 30)
    recycle = number("SMO_DB_POOL_RECYCLE_SECONDS", 1800)
    options["pool_recycle"] = recycle if recycle > 0 else -1
    if url.startswith("postgresql"):
        pooled = (environ.get("SMO_DB_POOLER") or "").strip().lower()
        if pooled not in ("", "session", "transaction"):
            raise ValueError(f"SMO_DB_POOLER must be 'transaction', 'session' or unset, not {pooled!r}")
        threshold = environ.get("SMO_DB_PREPARE_THRESHOLD")
        if threshold not in (None, ""):
            options.setdefault("connect_args", {})["prepare_threshold"] = None if threshold.lower() == "off" else int(threshold)
        elif pooled == "transaction":
            options.setdefault("connect_args", {})["prepare_threshold"] = None
        if pooled:
            return options                    # the pooler sets the session limits on its server connections
        session_limits = []
        statement = number("SMO_DB_STATEMENT_TIMEOUT_MS", 30000)
        idle = number("SMO_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 300000)
        if statement > 0:
            session_limits.append(f"-c statement_timeout={statement}")
        if idle > 0:
            session_limits.append(f"-c idle_in_transaction_session_timeout={idle}")
        if session_limits:
            options.setdefault("connect_args", {})["options"] = " ".join(session_limits)
    return options


def build_engine(url: str, environ=os.environ):
    """Creates the SQLAlchemy engine for `url` with the options `engine_options` derives from `environ`. Opens no connection."""
    return create_engine(url, **engine_options(url, environ))


engine = build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """The declarative base of every module's ORM models; all tables share this one metadata."""
    pass


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for one unit of work outside a request: commits when the block ends normally, rolls back and re-raises on any exception, always
    closes.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI dependency that yields one `SessionLocal` session per request and always closes it.

    It never commits: a handler that writes must call `commit()` itself, and an exception leaves the transaction to be rolled back when the session
    closes.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
