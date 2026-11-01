"""SME's MnS registry (plan tasks S1.7, S1.12): the TS 28.622 MnsRegistry, MnsInfo and MgmtDataInfo, served over ProvMnS by the shared router.

What it is: a ProvMnS producer of `smo_shared.mns` for the three classes of the vendored TS 28.623 MnS Registry NRM, on SME's store table
`sme.mns_registry` (`smo_shared.mns.store.store_model`; revision `sme_0001`). The layout, what a registration holds and how a reader finds a producer
are `smo_shared/mns/mns_registry.py`'s; this file adds what only the registry's owner does:

  - the registry itself: `SubNetwork=SMO` (its container, not served here) and `SubNetwork=SMO,MnsRegistry=1`, with SME's own MnsInfo and
    MgmtDataInfo (SME is the producer of the three classes), written by `ensure_registry` when a request finds them missing (a new database, a
    test's); a registry read therefore always lists SME;
  - MnsRegistry is read only (getMOIAttributes, as SME's contract says): a createMOI, modifyMOIAttributes or deleteMOI that would write it, a subtree
    delete that reaches it included, is 422 VETOED;
  - an MnsInfo's `mnsAddress` must be an http or https URL with a host (R1 Termination forwards to it), else 422 VETOED naming `mnsAddress`;
  - the events of the registry on the SMO event bus (docs/EVENTS.md), in the transaction of the change: `org.oran.smo.mnsregistry.ServiceReady`
    when an MnsInfo is created, `org.oran.smo.mnsregistry.NotifyServiceChange` when one changes (`changeType` MODIFIED) or is deleted (REMOVED). The
    data is the MnsInfo: `mnsInfo` (its DN) and its attributes after the change (before it, for a deletion). A registration repeated unchanged
    changes nothing and publishes nothing.

Where it sits: every ProvMnS module registers itself here at start-up through R1 Termination (`install_self_registration`); R1 Termination reads
the whole registry directly from SME to route `/ProvMnS/...` by class (S1.8). Writes come through R1 Termination, whose role policy lets no rApp
change a ProvMnS resource, or from inside the network as every module's routes do.
"""

from collections.abc import Iterator
from urllib.parse import urlsplit

from fastapi import Depends
from sqlalchemy.orm import Session

from smo_shared import events
from smo_shared.db import get_session
from smo_shared.auth import AccessRuleAuthorizer
from smo_shared.mns import ClassRegistry, IMoiLifecycleHandler, MnsError, Moi, MoiContext, ProvMnS, provmns_router
from smo_shared.mns.checks import RouterChecks, ScopeStep
from smo_shared.mns.subscriptions import CLASSES as SUBSCRIPTION_CLASSES
from smo_shared.mns.dn import depth_of, parent_of, parse_dn
from smo_shared.mns.mns_registry import REGISTRY_CLASSES, REGISTRY_DN, default_address, registration
from smo_shared.mns.store import ManagedObject, store_model

from . import msac_store

MODULE = "sme"
SOURCE = "/smo/sme"
TABLE = "mns_registry"
RegistryObject = store_model(TABLE)
SUBNETWORK_DN = parent_of(REGISTRY_DN)


class ReadOnlyRegistry(IMoiLifecycleHandler):
    """MnsRegistry's hooks: every write is refused (the registry exists once, made by `ensure_registry`)."""

    @staticmethod
    def _refuse(moi: Moi) -> None:
        raise MnsError(422, "VETOED", "the MnsRegistry is read only: register a producer with its own MnsInfo and MgmtDataInfo",
                       bad_objects=[moi.dn])

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Refused."""
        self._refuse(moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Refused."""
        self._refuse(old)

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Refused."""
        self._refuse(moi)


def _check_address(moi: Moi) -> None:
    """422 VETOED naming `mnsAddress` unless it is an http or https URL with a host."""
    address = moi.attributes.get("mnsAddress")
    parts = urlsplit(address) if isinstance(address, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise MnsError(422, "VETOED", "mnsAddress is the producer's ProvMnS root, an http or https URL", bad_attributes=["mnsAddress"],
                       bad_objects=[moi.dn])


def _publish(ctx: MoiContext, change: str, moi: Moi, change_type: str | None = None) -> None:
    data = {"mnsInfo": moi.dn, **moi.attributes}
    if change_type is not None:
        data["changeType"] = change_type
    events.publish(ctx.session, "mnsregistry", change, SOURCE, subject=moi.dn, data=data)


class MnsInfoEvents(IMoiLifecycleHandler):
    """MnsInfo's hooks: the address check, and ServiceReady / NotifyServiceChange on the event bus."""

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Checks the address."""
        _check_address(moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Checks the new address."""
        _check_address(new)

    def after_create(self, ctx: MoiContext, moi: Moi) -> None:
        """ServiceReady: a producer is registered."""
        _publish(ctx, "ServiceReady", moi)

    def after_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """NotifyServiceChange, MODIFIED."""
        _publish(ctx, "NotifyServiceChange", new, "MODIFIED")

    def after_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """NotifyServiceChange, REMOVED."""
        _publish(ctx, "NotifyServiceChange", moi, "REMOVED")


# SME's own class registry (the shared default stays as it is for the other modules of a process): the TS 28.319 MSAC NRM indexed, and its three
# classes allowed under a SubNetwork (msac_store.py).
CLASS_REGISTRY = ClassRegistry()
CLASS_REGISTRY.index_file("TS28319_MsacNrm.yaml")
CHECKS = RouterChecks(module=MODULE, scope=ScopeStep(authorizer=AccessRuleAuthorizer(msac_store.local_rules(RegistryObject))))
mns = ProvMnS(source=SOURCE, system_dn=REGISTRY_DN, registry=CLASS_REGISTRY, classes=REGISTRY_CLASSES, model=RegistryObject, checks=CHECKS)
mns.serve("MnsRegistry", ReadOnlyRegistry())
mns.serve("MnsInfo", MnsInfoEvents())
msac_store.serve(mns)
# What SME registers as its own (plan tasks S1.12, S1.14): the registry's classes, TS 28.319's, the subscriptions (S1.13), and SubNetwork for reads of
# its container, all within `SubNetwork=SMO` (its mnsScope), so R1 Termination routes a read of `SubNetwork=SMO` here and every other SubNetwork to
# the producers of the network.
OWN_CLASSES = (*REGISTRY_CLASSES, *msac_store.CLASSES, *SUBSCRIPTION_CLASSES, "SubNetwork")


def _row(dn: str, attributes: dict) -> ManagedObject:
    last = parse_dn(dn)[-1]
    return RegistryObject(dn=dn, object_class=last.class_name, moi_id=last.moi_id, parent_dn=parent_of(dn), depth=depth_of(dn), attributes=attributes)


def ensure_registry(db: Session) -> None:
    """Writes what SME's store must hold and does not (the registry's container, the MnsRegistry, SME's own registration as `OWN_CLASSES` within
    `SubNetwork=SMO`, the five D15 roles) and commits; nothing when all is there. SME's registration is rewritten when it differs (an older release's)."""
    changed = False
    if db.get(RegistryObject, SUBNETWORK_DN) is None:
        db.add(_row(SUBNETWORK_DN, {}))
        changed = True
    if db.get(RegistryObject, REGISTRY_DN) is None:
        db.add(_row(REGISTRY_DN, {}))
        changed = True
    for dn, resource in registration(MODULE, OWN_CLASSES, default_address(MODULE), scope=[SUBNETWORK_DN]):
        row = db.get(RegistryObject, dn)
        if row is None:
            db.add(_row(dn, resource["attributes"]))
            changed = True
        elif row.attributes != resource["attributes"]:
            row.attributes = resource["attributes"]
            changed = True
    if changed:
        db.flush()
    changed = msac_store.ensure_d15_roles(db, RegistryObject) or changed
    if changed:
        db.commit()


def registry_session(db: Session = Depends(get_session)) -> Iterator[Session]:
    """The router's session: `get_session`'s, after `ensure_registry`."""
    ensure_registry(db)
    yield db


router = provmns_router(mns, get_session=registry_session)
