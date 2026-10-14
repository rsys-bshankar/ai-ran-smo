"""PR-DB-2.5/2.6: a module's tables live in a schema of its own, and its role can use that and nothing else.

The manifest tests need no database. The rest need SMO_TEST_POSTGRES_URL (CI's `migration-postgres` job, or a local server): they migrate a
fresh database to head, run scripts/db_roles.py with a password file, and connect as the module's role.
"""

import json
import re
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SMO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE = SMO_ROOT / "scripts" / "migrate.py"
DB_ROLES = SMO_ROOT / "scripts" / "db_roles.py"
ADMIN_URL = os.environ.get("SMO_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="SMO_TEST_POSTGRES_URL not set")
OWNERS = json.loads((SMO_ROOT / "migrations" / "table_owners.json").read_text())
ROLES = {m: s for m, s in json.loads((SMO_ROOT / "migrations" / "db_roles.json").read_text()).items() if m != "_comment"}


def _short(module: str) -> str:
    return module.rsplit("/", 1)[-1]


def _role(module: str) -> str:
    return "smo_" + _short(module).replace("-", "_")


def test_every_module_in_the_roles_manifest_is_a_module_with_tables_and_names_real_shared_tables():
    """The roles manifest agrees with the table-owners file: a module with a schema owns tables, its schema is named after it, and everything it
    lists as shared or read is a real table of the right owner.
    """
    for module, spec in ROLES.items():
        if spec["schema"] is None:          # a module with no table of its own (R1 Termination): a role, no schema
            assert not OWNERS.get(module), f"{module} owns tables, so it needs a schema"
        else:
            assert OWNERS.get(module), f"{module} is in db_roles.json but owns no table in table_owners.json"
            assert spec["schema"] == _short(module).replace("-", "_"), f"{module}: the schema is the module's name with _ for -"
            assert spec["schema"] not in ("public", "platform_")
        for table in spec.get("shared", []):
            assert table in OWNERS["shared"], f"{module}: {table} is not a shared table"
        for qualified in spec.get("read", []):
            schema, table = qualified.split(".")
            owner = next(m for m, s in ROLES.items() if s["schema"] == schema)   # the schema of the module that owns it
            assert table in OWNERS[owner], f"{module} reads {qualified}, which {owner} does not own"


def test_the_role_of_every_module_that_runs_a_worker_may_use_what_every_worker_uses():
    """A worker (`python -m smo_shared.worker`) claims each of its tasks in `periodic_run`, sweeps `notification_outbox` and purges `smo_event`; the role of a module with a
    worker service in docker-compose.yml must list all three, or every task of that worker fails with "permission denied" (mdaf-worker did, on every tick, since it was added)."""
    import yaml
    services = yaml.safe_load((SMO_ROOT / "docker-compose.yml").read_text())["services"]
    modules = {name.removesuffix("-worker") for name, svc in services.items() if "smo_shared.worker" in " ".join(svc.get("command", []))}
    assert {"mdaf", "ran-nf-oam"} <= modules
    for module in modules:
        role = ROLES.get(module) or ROLES[f"samples/{module}"]          # a sample rApp's role is keyed by its directory
        assert {"periodic_run", "notification_outbox", "smo_event"} <= set(role["shared"]), module


def test_the_role_of_every_module_that_publishes_events_may_write_the_event_table():
    """A module whose code uses the event bus (`smo_shared.events`) writes `smo_event` in the transaction of its change; its role must list the table, or the
    change itself fails with "permission denied" (NFO's did, once its deployments' state changes were published: call flow 15e)."""
    # A state machine's `fire` given the entity (`fire(state, event, deployment=d)`) publishes too (MSG-3.5), without importing the bus: AIMgF's model lifecycle (call flow 26).
    fires_with_entity = re.compile(r"_FSM\.fire\([^)]*\b\w+=\w")
    publishers = {m for m in ROLES if any("smo_shared import events" in f.read_text() or "smo_shared.events" in f.read_text()
                                          or fires_with_entity.search(f.read_text())
                                          for f in (SMO_ROOT / m / "app").glob("*.py"))}
    assert {"nfo", "dme", "ran-nf-oam", "aimgf"} <= publishers
    for module in publishers:
        assert "smo_event" in ROLES[module]["shared"], module


def test_a_role_name_is_derived_from_the_module_and_statements_quote_what_they_are_given():
    """Role names derive from the module name, the password is a quoted literal that cannot break out of the statement, the search path and revokes
    are the module's own, and a module that reads another's table gets SELECT only.
    """
    sys.path.insert(0, str(SMO_ROOT / "scripts"))
    import db_roles
    assert db_roles.role_name("rapp-mgmt") == "smo_rapp_mgmt" and db_roles.role_name("samples/energy-saving-rapp") == "smo_energy_saving_rapp"
    rendered = [s.as_string() for s in db_roles.statements("onboarding", ROLES["onboarding"], "pa'ss\"word", ["public", "onboarding", "dme"], "smo")]
    text_ = "\n".join(rendered)
    assert "'pa''ss\"word'" in text_                                  # the password is a quoted literal, never spliced in
    assert 'ALTER ROLE "smo_onboarding" SET search_path = "onboarding", public' in text_
    assert 'REVOKE ALL ON SCHEMA "dme" FROM "smo_onboarding"' in text_ and 'REVOKE ALL ON SCHEMA "onboarding"' not in text_
    # a module that reads another's table (SELECT only) and one with no schema of its own
    reading = "\n".join(s.as_string() for s in db_roles.statements("x", {"schema": None, "shared": [], "read": ["ran_nf_oam.rapp_kill"]}, "p", ["public", "ran_nf_oam"], "smo"))
    assert 'search_path = public' in reading and 'GRANT SELECT ON "ran_nf_oam"."rapp_kill" TO "smo_x"' in reading and 'GRANT USAGE ON SCHEMA "ran_nf_oam"' in reading
    assert "INSERT" not in reading.split('GRANT SELECT ON "ran_nf_oam"')[1]


@pytest.fixture
def database(tmp_path):
    """A fresh database migrated to head by `scripts/migrate.py` with a directory for the role password files; yields (url, environment, password
    directory, name) and drops the database and the roles afterwards (a role that other databases still have rights on is left).
    """
    name = f"smo_roles_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "SMO_DATABASE_URL": url, "SMO_DB_ROLE_PASSWORD_DIR": str(tmp_path)}
    migrated = subprocess.run([sys.executable, str(MIGRATE)], env=env, capture_output=True, text=True, cwd=SMO_ROOT)
    assert migrated.returncode == 0, migrated.stderr
    yield url, env, tmp_path, name
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        for module in ROLES:
            try:       # a role is cluster-wide: it stays when another database still has rights granted to it (the one the admin URL names, say)
                connection.execute(text(f'DROP ROLE IF EXISTS "{_role(module)}"'))
            except Exception:
                pass
    admin.dispose()


def _roles(env, *args):
    return subprocess.run([sys.executable, str(DB_ROLES), *args], env=env, capture_output=True, text=True, cwd=SMO_ROOT)


def _as_role(url, module, password):
    return create_engine(make_url(url).set(username=_role(module), password=password), isolation_level="AUTOCOMMIT")


def _missing(engine, statement) -> bool:
    """True when the statement fails because the relation it names does not exist (no view stands in for a moved table any more)."""
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    except Exception as exc:
        return "does not exist" in str(exc)
    return False


def _denied(engine, statement) -> bool:
    """True when the statement fails with a permission error, and False when it succeeds or fails for any other reason."""
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    except Exception as exc:
        return "permission denied" in str(exc)
    return False


@needs_postgres
def test_the_tables_are_in_the_modules_schema_and_public_holds_no_compatibility_view(database):
    """After migration each module's tables are in its own schema, none is left (or shadowed by a view) in `public`, and the unqualified names no
    longer work for the owner. Needs Postgres.
    """
    url, env, _, _ = database
    owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with owner.connect() as connection:
        for module, spec in ROLES.items():
            if spec["schema"] is None:
                continue
            in_schema = {r[0] for r in connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": spec["schema"]})}
            assert in_schema == set(OWNERS[module]), module
            for table in OWNERS[module]:
                assert not connection.execute(text("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"), {"t": table}).first(), table
                assert not connection.execute(text("SELECT 1 FROM pg_views WHERE schemaname = 'public' AND viewname = :t"), {"t": table}).first(), f"a view {table} is left in public"
        # the old, unqualified names are gone for the owner too (revision 0029; 0.4.0's modules use their roles' search paths): naming the schema is the way
        assert connection.execute(text("SELECT count(*) FROM onboarding.application_package")).scalar() == 0
    assert _missing(owner, "SELECT count(*) FROM application_package")
    owner.dispose()


@needs_postgres
def test_every_role_works_in_its_own_schema_and_is_refused_everywhere_else(database):
    """After `db_roles.py`, each module's role can use its own tables and its granted shared tables and is refused every other module's. Needs
    Postgres.
    """
    url, env, password_dir, _ = database
    for module in ROLES:
        (password_dir / f"db_password_{_short(module)}").write_text(f"pw-{_short(module)}")
    done = _roles(env)
    assert done.returncode == 0, done.stdout + done.stderr
    for module in ROLES:
        assert _role(module) in done.stdout
    all_tables = {t for owner, tables in OWNERS.items() if owner != "_comment" for t in tables}
    def schema_of_owner(owner):             # a module's own schema; the shared tables are in public; the retired A1 tables stayed in the schema 0025 gave them
        return ROLES[owner]["schema"] or "public" if owner in ROLES else ("a1_related" if owner == "_retired" else "public")
    schema_of = {t: schema_of_owner(owner) for owner, tables in OWNERS.items() if owner != "_comment" for t in tables}
    for module, spec in ROLES.items():
        role = _as_role(url, module, f"pw-{_short(module)}")
        own = set(OWNERS[module])
        granted_shared = set(spec.get("shared", []))
        read = {q.split(".")[1] for q in spec.get("read", [])}
        with role.connect() as connection:
            assert connection.execute(text("SHOW search_path")).scalar() == (f"{spec['schema']}, public" if spec["schema"] else "public"), module
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar()          # the pod's schema wait
            for table in own:                                       # unqualified, as the service's code writes them
                assert connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar() == 0, (module, table)
            for table in granted_shared:        # readable (the audit chain's head holds a row from the migration, so the count is not asked to be 0)
                connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
        for table in sorted(all_tables - own - granted_shared - read):
            assert _denied(role, f'SELECT * FROM "{schema_of[table]}"."{table}" LIMIT 1'), f"{_role(module)} can read {table}"
            if schema_of[table] != "public":        # no compatibility view in public since 0029: the bare name of another module's table is not a thing the role can even name
                assert _missing(role, f'SELECT * FROM "{table}" LIMIT 1'), f"{_role(module)} resolves the bare name {table}"
        assert _denied(role, "CREATE TABLE public.not_allowed (a int)")
        if spec["schema"]:
            assert _denied(role, f"CREATE TABLE {spec['schema']}.not_allowed (a int)")
        for qualified in spec.get("read", []):      # a read grant is SELECT and nothing else
            with role.connect() as connection:
                assert connection.execute(text(f"SELECT count(*) FROM {qualified}")).scalar() == 0, (module, qualified)
            assert _denied(role, f"DELETE FROM {qualified}"), f"{_role(module)} can change {qualified}"
        role.dispose()
    # one write path, end to end, as Onboarding's role does it
    onboarding = _as_role(url, "onboarding", "pw-onboarding")
    with onboarding.connect() as connection:
        connection.execute(text("INSERT INTO application_package (application_type, name, version, manifest_ref) VALUES ('rApp', 'mine', '1', 'm')"))
        assert connection.execute(text("SELECT count(*) FROM application_package")).scalar() == 1
        connection.execute(text("DELETE FROM application_package"))
    assert _missing(onboarding, "SELECT * FROM public.application_package")     # no compatibility view in public any more (revision 0029)
    onboarding.dispose()


@needs_postgres
def test_a_table_added_later_to_the_schema_is_usable_at_once_and_a_second_run_changes_nothing(database):
    """A table added to a schema after the roles were made is usable by the role at once (default privileges), and running the script again with a
    new password file rotates the password without changing the rights. Needs Postgres.
    """
    url, env, password_dir, _ = database
    (password_dir / "db_password_onboarding").write_text("first")
    assert _roles(env).returncode == 0
    owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with owner.connect() as connection:
        connection.execute(text("CREATE TABLE onboarding.added_later (id int primary key)"))
    role = _as_role(url, "onboarding", "first")
    with role.connect() as connection:
        connection.execute(text("INSERT INTO added_later VALUES (1)"))      # default privileges, before any rerun
    (password_dir / "db_password_onboarding").write_text("second")          # rotation: write the new file and run it again
    assert _roles(env).returncode == 0
    role.dispose()
    rotated = _as_role(url, "onboarding", "second")
    with rotated.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM added_later")).scalar() == 1
    old = _as_role(url, "onboarding", "first")
    with pytest.raises(Exception, match="password authentication failed"):
        with old.connect():
            pass
    rotated.dispose(); old.dispose(); owner.dispose()


@needs_postgres
def test_without_a_password_file_nothing_is_made(database):
    """With no password files the script succeeds, says it made no roles and creates none, which is how a release without per-module roles
    installs. Needs Postgres.
    """
    url, env, _, _ = database
    done = _roles(env)
    assert done.returncode == 0 and "none (no password files)" in done.stdout


@needs_postgres
def test_the_owner_finds_every_module_table_by_its_bare_name_so_a_release_with_the_roles_off_still_runs(database):
    """After the roles script the owner role still finds every module's tables by their bare names, so code that does not set a search path keeps
    working with the roles off. Needs Postgres.
    """
    url, env, _, _ = database
    assert _roles(env).returncode == 0
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM invoker_registration")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM rapp_instance")).scalar() == 0
    engine.dispose()
