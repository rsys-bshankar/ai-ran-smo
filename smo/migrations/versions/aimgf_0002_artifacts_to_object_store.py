"""Model artifacts out of the database into the object store (plan task S2.5), on the `aimgf` branch.

Upgrade, for `aimgf.model_artifact`:
  1. adds `object_key`, `sha256` (64 hex characters) and `artifact_format`;
  2. copies every row's `content` to the object store's `models` bucket at `<model id>/<artifact id><extension>` (the key AIMgF's upload route uses,
     `aimgf/app/registry/artifacts.py`), with its SHA-256 as the object's metadata; the digest is computed by Postgres (`sha256(content)`), the bytes are
     read 1 MiB at a time (`substring`) and streamed to the store, then read back from the store, hashed and compared, before the row records the key,
     the digest and the format (from the file name: `.onnx` onnx, `.safetensors` safetensors, anything else zip-legacy, which every upload before S2.5
     was);
  3. makes the three columns NOT NULL, adds the check that the format is one of the three, and drops `content`.
One artifact chunk (1 MiB) is in memory at a time. Prints the number of rows and bytes it copied.

The store is the one `smo_shared.objectstore.open_store()` names (SMO_OBJECTSTORE_URL and the keys, as AIMgF reads them): the `migrate` service needs the
same object-store settings as AIMgF. A table without rows needs no store. With rows and no store configured, the upgrade stops before it changes anything,
naming the setting.

Downgrade: adds `content` back, reads each row's object from the store 1 MiB at a time into it (appended in place, `content || chunk`), checks in Postgres
that the bytes hash to the row's `sha256` and have its `size_bytes`, then makes `content` NOT NULL and drops the three columns and the check. The objects
stay in the store (upgrading again writes the same keys). An object missing or not matching its digest stops the downgrade, which then changes nothing.

Revision ID: aimgf_0002
Revises: aimgf_0001
"""
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op

from smo_shared import objectstore

revision = "aimgf_0002"
down_revision = "aimgf_0001"
branch_labels = None
depends_on = None

SCHEMA = "aimgf"
TABLE = "model_artifact"
USE = "models"
CHUNK = objectstore.CHUNK
EXTENSIONS = {".onnx": "onnx", ".safetensors": "safetensors"}
SUFFIX = {"onnx": ".onnx", "safetensors": ".safetensors", "zip-legacy": ".zip"}
MEDIA_TYPES = {"onnx": "application/octet-stream", "safetensors": "application/octet-stream", "zip-legacy": "application/zip"}
CHECK = "artifact_format_known"


def format_of(filename: str) -> str:
    """The format a row uploaded before S2.5 is recorded with, from its file name."""
    lowered = (filename or "").lower()
    return next((fmt for ext, fmt in EXTENSIONS.items() if lowered.endswith(ext)), "zip-legacy")


def object_key(model_id: object, artifact_id: object, fmt: str) -> str:
    """The object key of an artifact, as AIMgF writes it."""
    return f"{model_id}/{artifact_id}{SUFFIX[fmt]}"


def _chunks(connection: sa.engine.Connection, artifact_id: object, size: int) -> Iterator[bytes]:
    """The row's `content`, CHUNK bytes per query."""
    query = sa.text(f"SELECT substring(content FROM :start FOR :n) FROM {SCHEMA}.{TABLE} WHERE artifact_id = :a")
    for start in range(0, size, CHUNK):
        yield bytes(connection.execute(query, {"start": start + 1, "n": CHUNK, "a": artifact_id}).scalar_one())


def _store():
    """The configured object store; RuntimeError naming the setting when there is none."""
    try:
        return objectstore.open_store()
    except ValueError as exc:
        raise RuntimeError(f"aimgf_0002 copies model artifacts to the object store and none is configured: {exc}") from None


def copy_out(connection: sa.engine.Connection, store) -> tuple[int, int]:
    """Copies every row's bytes to `store`, reads each back and checks its digest, then records key, digest and format; returns (rows, bytes)."""
    rows = connection.execute(sa.text(f"SELECT artifact_id, model_id, filename, octet_length(content) AS size, encode(sha256(content), 'hex') AS digest "
                                      f"FROM {SCHEMA}.{TABLE} ORDER BY model_id, artifact_version, artifact_id")).all()
    moved = 0
    for row in rows:
        fmt = format_of(row.filename)
        key = object_key(row.model_id, row.artifact_id, fmt)
        store.put_stream(USE, key, _chunks(connection, row.artifact_id, row.size), row.size, row.digest, MEDIA_TYPES[fmt])
        with store.get_stream(USE, key) as download:
            if download.sha256 != row.digest:
                raise objectstore.IntegrityError(f"aimgf_0002: the stored object of artifact {row.artifact_id} has another digest")
            for _ in download.chunks:            # hashed and counted as read: IntegrityError when they are not the row's bytes
                pass
        connection.execute(sa.text(f"UPDATE {SCHEMA}.{TABLE} SET object_key = :k, sha256 = :d, artifact_format = :f WHERE artifact_id = :a"),
                           {"k": key, "d": row.digest, "f": fmt, "a": row.artifact_id})
        moved += row.size
    return len(rows), moved


def copy_back(connection: sa.engine.Connection, store) -> tuple[int, int]:
    """Reads every row's object from `store` back into `content`, chunk by chunk, and checks the digest and size in Postgres; returns (rows, bytes)."""
    rows = connection.execute(sa.text(f"SELECT artifact_id, object_key, sha256, size_bytes FROM {SCHEMA}.{TABLE} ORDER BY model_id, artifact_version, artifact_id")).all()
    moved = 0
    for row in rows:
        connection.execute(sa.text(f"UPDATE {SCHEMA}.{TABLE} SET content = ''::bytea WHERE artifact_id = :a"), {"a": row.artifact_id})
        with store.get_stream(USE, row.object_key) as download:
            if download.sha256 is not None and download.sha256 != row.sha256:
                raise objectstore.IntegrityError(f"aimgf_0002 downgrade: the stored object of artifact {row.artifact_id} has another digest")
            for chunk in download.chunks:
                connection.execute(sa.text(f"UPDATE {SCHEMA}.{TABLE} SET content = content || :c WHERE artifact_id = :a"), {"c": chunk, "a": row.artifact_id})
        back = connection.execute(sa.text(f"SELECT octet_length(content), encode(sha256(content), 'hex') FROM {SCHEMA}.{TABLE} WHERE artifact_id = :a"),
                                  {"a": row.artifact_id}).one()
        if tuple(back) != (row.size_bytes, row.sha256):
            raise objectstore.IntegrityError(f"aimgf_0002 downgrade: artifact {row.artifact_id} read back is not the bytes of its digest and size")
        moved += row.size_bytes
    return len(rows), moved


def upgrade() -> None:
    """Three columns, the bytes copied out and checked, the columns required, `content` dropped."""
    connection = op.get_bind()
    for name, column_type in (("object_key", sa.String()), ("sha256", sa.String(64)), ("artifact_format", sa.String())):
        op.add_column(TABLE, sa.Column(name, column_type, nullable=True), schema=SCHEMA)
    count = connection.execute(sa.text(f"SELECT count(*) FROM {SCHEMA}.{TABLE}")).scalar_one()
    rows, moved = copy_out(connection, _store()) if count else (0, 0)
    print(f"aimgf_0002: {rows} model artifacts, {moved} bytes, copied to the object store and read back with matching SHA-256")
    for name in ("object_key", "sha256", "artifact_format"):
        op.alter_column(TABLE, name, nullable=False, schema=SCHEMA)
    op.create_check_constraint(CHECK, TABLE, "artifact_format IN ('onnx', 'safetensors', 'zip-legacy')", schema=SCHEMA)
    op.drop_column(TABLE, "content", schema=SCHEMA)


def downgrade() -> None:
    """`content` back from the store, checked; the three columns and the check dropped."""
    connection = op.get_bind()
    op.add_column(TABLE, sa.Column("content", sa.LargeBinary(), nullable=True), schema=SCHEMA)
    count = connection.execute(sa.text(f"SELECT count(*) FROM {SCHEMA}.{TABLE}")).scalar_one()
    rows, moved = copy_back(connection, _store()) if count else (0, 0)
    print(f"aimgf_0002 downgrade: {rows} model artifacts, {moved} bytes, read back from the object store into the database")
    op.alter_column(TABLE, "content", nullable=False, schema=SCHEMA)
    op.drop_constraint(CHECK, TABLE, type_="check", schema=SCHEMA)
    for name in ("artifact_format", "sha256", "object_key"):
        op.drop_column(TABLE, name, schema=SCHEMA)
