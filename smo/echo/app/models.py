"""Echo's tables (plan task S1.6): only the ProvMnS store of `smo_shared.mns`, in the schema `echo` (revision 0066, `create_moi_store(op, "echo")`).

The router declares `moi_store` on its own declarative base (`smo_shared.mns.store.MnsBase`), so that a module that does not mount the router is not
expected to have the table. Echo mounts it, so this file puts a copy of that table on the shared `Base`: `scripts/check_migration_matches_models.py`
then checks it against the migrated schema like any module table, `migrations/table_owners.json` names Echo its owner (D6), and a unit test's
`Base.metadata.create_all` makes it.
"""

from smo_shared.db import Base
from smo_shared.mns.store import ManagedObject

MOI_STORE = ManagedObject.__table__.to_metadata(Base.metadata)
