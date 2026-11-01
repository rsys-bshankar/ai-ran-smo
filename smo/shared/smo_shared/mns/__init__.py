"""`smo_shared.mns`: the generic TS 28.532 ProvMnS producer every module mounts (plan tasks S1.1, S1.2, S1.4; `smo/shared/README.md` section 3).

  service.ProvMnS          the four operations (createMOI, getMOIAttributes, modifyMOIAttributes, deleteMOI) on the module's `moi_store`
  router.provmns_router    their HTTP routes in D5's one form, /ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}
  registry.ClassRegistry   the classes, attribute schemas and containment, read from the vendored TS 28.541 / TS 28.623 YAML
  lifecycle                IMoiLifecycleHandler, the hooks a module puts its behaviour in
  scope                    scope, filter, attribute selection and the hierarchical answer
  patch                    the merge-patch and JSON-patch formats
  store                    the `moi_store` model (`store_model` for another table name) and the Alembic helpers a module's revision calls
  mns_registry             the TS 28.622 MnS registry: what a producer registers, how a reader finds one, the start-up self-registration
  dn, errors               DN handling; the TS 28.532 error bodies
"""

from .errors import MnsError
from .lifecycle import IMoiLifecycleHandler, Moi, MoiContext
from .registry import ClassRegistry, default_registry
from .router import provmns_router
from .scope import Scope
from .service import PROVMNS_VERSION, Page, ProvMnS
from .store import ManagedObject, MnsBase, create_moi_store, drop_moi_store

__all__ = ["PROVMNS_VERSION", "ClassRegistry", "IMoiLifecycleHandler", "ManagedObject", "MnsBase", "MnsError", "Moi", "MoiContext", "Page",
           "ProvMnS", "Scope", "create_moi_store", "default_registry", "drop_moi_store", "provmns_router"]
