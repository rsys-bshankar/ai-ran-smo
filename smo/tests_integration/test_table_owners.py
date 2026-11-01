"""PR-DB-2.1/2.2: every table has exactly one owner, and the owner is the module whose models declare it.

`migrations/table_owners.json` is the map the per-module schemas and roles are built from (PR-DB-2.5 onward). This test loads every module's
models, one after another, and compares what each added to the shared metadata with the map, so a new table without an entry, a table
listed under two modules, or a module that stopped declaring one, fails here. The migrated Postgres is checked against the same file by
scripts/check_migration_matches_models.py (a table in the database that no model declares).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
OWNERS = json.loads((SMO_ROOT / "migrations" / "table_owners.json").read_text())
MODULES = [m for m in OWNERS if m != "shared" and not m.startswith("_")]      # `_retired` (a removed module's tables) and `_comment` are not modules
# The ProvMnS router's store (smo_shared/mns/store.py): one `moi_store` in the schema of each module that mounts the router, each listed under its
# module, so the one table name has several owners by design. A module's models put a copy of it on the shared metadata, which holds one table
# per name, so only the first module loaded adds it there; the checks below leave it out and check it apart (each module that lists it mounts it).
ROUTER_STORE = "moi_store"

# Run in a process of its own: the other tests load modules into the shared metadata too, and the question here is what each module adds to a clean one.
_PROBE = """
import json, sys
sys.path.insert(0, "tests_integration"); sys.path.insert(0, "sdk")
from loader import load_app_module
from smo_shared.db import Base
import smo_shared.audit, smo_shared.events, smo_shared.idempotency, smo_shared.module_identity, smo_shared.outbox, smo_shared.ratelimit, smo_shared.single_runner
result = {"shared": sorted(Base.metadata.tables)}
seen = set(result["shared"])
for module in json.loads(sys.argv[1]):
    load_app_module(module)
    now = set(Base.metadata.tables)
    result[module] = sorted(now - seen)
    seen = now
print(json.dumps(result))
"""


@pytest.fixture(scope="module")
def declared() -> dict[str, list[str]]:
    """The tables each module's models declare, found by loading every module's models in a subprocess and comparing what each added to the shared
    metadata; {module: sorted table names}.
    """
    env = {**os.environ, "SMO_DATABASE_URL": os.environ.get("SMO_DATABASE_URL", "sqlite://"), "SMO_ENROLLMENT_SECRET": "x"}
    done = subprocess.run([sys.executable, "-c", _PROBE, json.dumps(MODULES)], cwd=SMO_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_module_owns_exactly_the_tables_the_map_says(declared):
    """Each module's models declare exactly the tables the owner map lists for it."""
    for owner, tables in declared.items():
        mine, mapped = sorted(set(tables) - {ROUTER_STORE}), sorted(set(OWNERS[owner]) - {ROUTER_STORE})
        assert mine == mapped, f"{owner}: the models declare {mine}, the map says {mapped}"


def test_the_router_store_is_listed_by_exactly_the_modules_that_mount_the_router():
    """`moi_store` is in a module's list exactly when its models copy the router's store (`smo_shared.mns.store`) onto the shared metadata."""
    models = {m: SMO_ROOT / m / "app" / "models.py" for m in MODULES}
    mounting = sorted(m for m, path in models.items() if path.is_file() and "smo_shared.mns.store" in path.read_text(errors="ignore"))
    assert mounting == sorted(m for m in MODULES if ROUTER_STORE in OWNERS[m])


def test_no_table_has_two_owners():
    """No table is listed under more than one owner in the map."""
    listed = [t for owner, tables in OWNERS.items() if owner != "_comment" for t in tables if t != ROUTER_STORE]
    duplicates = sorted({t for t in listed if listed.count(t) > 1})
    assert not duplicates, f"listed under more than one owner: {duplicates}"


def test_the_map_covers_every_table_the_models_declare(declared):
    """Every table a module's models declare has an owner in the map."""
    mapped = {t for owner, tables in OWNERS.items() if owner != "_comment" for t in tables}
    orphans = sorted({t for tables in declared.values() for t in tables} - mapped)
    assert not orphans, f"tables with no owner in migrations/table_owners.json: {orphans}"
