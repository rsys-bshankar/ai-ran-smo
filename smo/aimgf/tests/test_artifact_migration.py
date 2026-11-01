"""Revision aimgf_0002 on a real Postgres with rows (plan task S2.5): every artifact's bytes are copied to the object store and are readable there with a
matching SHA-256 after the upgrade, the downgrade reads them back into `content` byte for byte, a second upgrade works, and with rows but no store the
upgrade stops with nothing changed.

Needs Postgres 18: set SMO_TEST_POSTGRES_URL to a server where the user may create databases (each test makes its own and drops it); skipped without
it. The store is a directory (file://) unless SMO_TEST_OBJECTSTORE_URL names another, an S3 endpoint such as the compose profile's SeaweedFS
(http://localhost:8333, with SMO_OBJECTSTORE_ACCESS_KEY(_FILE) and SMO_OBJECTSTORE_SECRET_KEY(_FILE) set). `scripts/migrate.py` runs as a separate process, as the
`migrate` service does.
"""

import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from smo_shared import objectstore

ROOT = Path(__file__).resolve().parents[2]
MIGRATE = ROOT / "scripts" / "migrate.py"
ADMIN_URL = os.environ.get("SMO_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="needs Postgres: set SMO_TEST_POSTGRES_URL")

# name, size, file name: an empty file, a few bytes, exactly one chunk, two and a half chunks, an ONNX-named file
ROWS = [("empty", 0, "e.zip"), ("small", 3, "m.zip"), ("one-chunk", objectstore.CHUNK, "c.zip"), ("chunks", 5 * objectstore.CHUNK // 2, "big.zip"),
        ("onnx", 1000, "m.onnx")]


@pytest.fixture
def database():
    """A fresh database on the test server, dropped afterwards; its URL."""
    admin = make_url(ADMIN_URL)
    name = f"aimgf_s25_{uuid.uuid4().hex[:10]}"
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    yield admin.set(database=name).render_as_string(hide_password=False)
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    engine.dispose()


@pytest.fixture
def store_env(tmp_path):
    """The object-store settings the migration process gets: SMO_TEST_OBJECTSTORE_URL with the keys of this environment, else a directory."""
    url = os.environ.get("SMO_TEST_OBJECTSTORE_URL") or f"file://{tmp_path / 'objects'}"
    env = {"SMO_OBJECTSTORE_URL": url}
    for name in ("SMO_OBJECTSTORE_ACCESS_KEY", "SMO_OBJECTSTORE_ACCESS_KEY_FILE", "SMO_OBJECTSTORE_SECRET_KEY", "SMO_OBJECTSTORE_SECRET_KEY_FILE",
                 "SMO_OBJECTSTORE_BUCKET_MODELS"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    store = objectstore.open_store(env)
    store.ensure_buckets()
    return env, store


def _migrate(url: str, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    """`scripts/migrate.py` in its own process against `url`, with only the settings given (no SQLite or in-memory store fallback)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SMO_")}
    env.update({"SMO_DATABASE_URL": url, "PYTHONPATH": str(ROOT / "shared"), **(extra or {})})
    return subprocess.run([sys.executable, str(MIGRATE), *args], env=env, capture_output=True, text=True, cwd=ROOT)


def _seed(url: str) -> dict:
    """At aimgf_0001: a model and an artifact per ROWS entry, random bytes; returns {artifact id: (model id, bytes, file name)}."""
    engine = create_engine(url)
    seeded = {}
    with engine.begin() as connection:
        for name, size, filename in ROWS:
            model_id, artifact_id, data = uuid.uuid4(), uuid.uuid4(), os.urandom(size)
            connection.execute(text("INSERT INTO aimgf.aiml_model (model_id, registration_id, model_type, version, registered_at) "
                                    "VALUES (:m, :r, :t, '1', now())"), {"m": model_id, "r": f"reg-{name}", "t": f"type-{name}"})
            connection.execute(text("INSERT INTO aimgf.model_artifact (artifact_id, model_id, artifact_version, filename, content, size_bytes, uploaded_at) "
                                    "VALUES (:a, :m, 1, :f, :c, :s, now())"), {"a": artifact_id, "m": model_id, "f": filename, "c": data, "s": size})
            seeded[artifact_id] = (model_id, data, filename)
    engine.dispose()
    return seeded


def _columns(url: str) -> set[str]:
    """The columns `aimgf.model_artifact` has in the database at `url`."""
    engine = create_engine(url)
    with engine.connect() as connection:
        found = set(connection.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'aimgf' "
                                            "AND table_name = 'model_artifact'")).scalars())
    engine.dispose()
    return found


def test_every_artifact_is_in_the_store_with_its_digest_and_the_downgrade_reads_it_back(database, store_env):
    """Upgrade: the rows' bytes (5 rows, 3.5 MiB and a bit) are in the store at `<model>/<artifact><ext>`, readable with the SHA-256 the row records,
    `content` is gone and the format comes from the file name; the migration prints what it moved. Downgrade: `content` holds the same bytes again
    and the three columns are gone. Upgrading again works."""
    env, store = store_env
    assert _migrate(database, "--revision", "aimgf_0001").returncode == 0
    seeded = _seed(database)
    total = sum(len(data) for _, data, _ in seeded.values())
    up = _migrate(database, extra=env)
    assert up.returncode == 0, up.stderr
    assert f"aimgf_0002: {len(ROWS)} model artifacts, {total} bytes, copied to the object store and read back with matching SHA-256" in up.stdout
    assert "content" not in _columns(database) and {"object_key", "sha256", "artifact_format"} <= _columns(database)
    engine = create_engine(database)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT artifact_id, model_id, object_key, sha256, artifact_format, size_bytes FROM aimgf.model_artifact")).all()
    assert len(rows) == len(ROWS)
    for row in rows:
        model_id, data, filename = seeded[row.artifact_id]
        fmt = "onnx" if filename.endswith(".onnx") else "zip-legacy"
        assert (row.object_key, row.artifact_format, row.size_bytes) == (f"{model_id}/{row.artifact_id}{'.onnx' if fmt == 'onnx' else '.zip'}", fmt, len(data))
        assert row.sha256 == hashlib.sha256(data).hexdigest()
        with store.get_stream("models", row.object_key) as download:
            assert download.sha256 == row.sha256 and b"".join(download.chunks) == data
    with pytest.raises(Exception, match="artifact_format_known"), engine.begin() as connection:
        connection.execute(text("UPDATE aimgf.model_artifact SET artifact_format = 'pickle'"))
    down = _migrate(database, "--downgrade", "aimgf_0001", extra=env)
    assert down.returncode == 0, down.stderr
    assert f"aimgf_0002 downgrade: {len(ROWS)} model artifacts, {total} bytes, read back from the object store into the database" in down.stdout
    assert "content" in _columns(database) and not {"object_key", "sha256", "artifact_format"} & _columns(database)
    with engine.connect() as connection:
        back = dict(connection.execute(text("SELECT artifact_id, content FROM aimgf.model_artifact")).all())
    assert {a: bytes(c) for a, c in back.items()} == {a: data for a, (_, data, _) in seeded.items()}
    again = _migrate(database, extra=env)
    assert again.returncode == 0 and f"{len(ROWS)} model artifacts, {total} bytes" in again.stdout, again.stderr
    engine.dispose()


def test_with_rows_and_no_store_the_upgrade_stops_and_changes_nothing(database, store_env):
    """No SMO_OBJECTSTORE_URL: the upgrade fails naming it, the database stays at aimgf_0001 with `content`; an empty table needs no store."""
    assert _migrate(database, "--revision", "aimgf_0001").returncode == 0
    empty = _migrate(database, "--revision", "aimgf_0002")
    assert empty.returncode == 0 and "aimgf_0002: 0 model artifacts, 0 bytes" in empty.stdout, empty.stderr
    assert _migrate(database, "--downgrade", "aimgf_0001").returncode == 0
    _seed(database)
    failed = _migrate(database)
    assert failed.returncode != 0 and "SMO_OBJECTSTORE_URL" in failed.stderr
    assert "content" in _columns(database) and "object_key" not in _columns(database)
    assert "aimgf_0001" in _migrate(database, "--current").stdout


def test_an_object_that_no_longer_matches_stops_the_downgrade(database, store_env, tmp_path):
    """A stored object whose bytes changed after the upgrade (a directory store, edited on disk) makes the downgrade fail, and the database keeps its
    columns at aimgf_0002."""
    env, store = store_env
    if not env["SMO_OBJECTSTORE_URL"].startswith("file://"):
        pytest.skip("edits the store's files on disk")
    assert _migrate(database, "--revision", "aimgf_0001").returncode == 0
    _seed(database)
    assert _migrate(database, extra=env).returncode == 0
    victim = next(p for p in (tmp_path / "objects" / "smo-models").rglob("*") if p.is_file() and p.stat().st_size == 3)
    victim.write_bytes(b"xyz")
    down = _migrate(database, "--downgrade", "aimgf_0001", extra=env)
    assert down.returncode != 0 and "IntegrityError" in down.stderr
    assert "object_key" in _columns(database) and "content" not in _columns(database)
