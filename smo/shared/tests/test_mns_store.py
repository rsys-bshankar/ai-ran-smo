"""The ProvMnS store's Alembic helpers (smo_shared/mns/store.py): on real Postgres, `create_moi_store` makes the table the model declares (columns,
types, nullability, the C collation of the DN columns, the three indexes, GIN on attributes) in the schema it is given, and `drop_moi_store` removes it
all again, so a module's revision has a real downgrade. Skipped without SMO_TEST_POSTGRES_URL. On SQLite, `create_all` makes the model's table.
"""

import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from smo_shared.mns import ManagedObject, create_moi_store, drop_moi_store, store
from smo_shared.testing import make_test_engine

POSTGRES_URL = os.environ.get("SMO_TEST_POSTGRES_URL")


def test_create_all_on_sqlite():
    """The SQLite fallback of the unit tests gets the model's columns."""
    engine = make_test_engine()
    store.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("moi_store")}
    assert columns == {"dn", "object_class", "moi_id", "parent_dn", "depth", "attributes", "moi_version", "created_at", "updated_at"}


@pytest.mark.skipif(not POSTGRES_URL, reason="SMO_TEST_POSTGRES_URL not set")
def test_the_helpers_make_and_remove_the_models_table():
    """upgrade: the model's table in the given schema (nullability equal column by column), DN columns collated "C", attributes JSONB with a GIN index;
    downgrade: nothing of it left."""
    schema = f"mns_store_{uuid.uuid4().hex[:10]}"
    engine = create_engine(POSTGRES_URL, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            create_moi_store(Operations(MigrationContext.configure(connection)), schema)
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("moi_store", schema=schema)}
        model = {column.name: column for column in ManagedObject.__table__.columns}
        assert set(columns) == set(model)
        assert {name: column["nullable"] for name, column in columns.items()} == {name: column.nullable for name, column in model.items()}
        assert inspector.get_pk_constraint("moi_store", schema=schema)["constrained_columns"] == ["dn"]
        with engine.connect() as connection:
            collations = dict(connection.execute(text(
                "SELECT column_name, collation_name FROM information_schema.columns WHERE table_schema = :s AND table_name = 'moi_store'"), {"s": schema}).all())
            types = dict(connection.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = :s AND table_name = 'moi_store'"), {"s": schema}).all())
            indexes = dict(connection.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s"), {"s": schema}).all())
        assert collations["dn"] == "C" and collations["parent_dn"] == "C"
        assert types["attributes"] == "jsonb" and types["moi_version"] == "integer" and types["created_at"] == "timestamp with time zone"
        assert {"ix_moi_store_parent_dn", "ix_moi_store_object_class", "ix_moi_store_attributes"} <= set(indexes)
        assert "USING gin" in indexes["ix_moi_store_attributes"]
        with engine.begin() as connection:
            drop_moi_store(Operations(MigrationContext.configure(connection)), schema)
        assert not inspect(engine).has_table("moi_store", schema=schema)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
