"""SME's TS 28.319 access control (plan task S1.14): Identity, Role and AccessRule as ProvMnS classes, the five D15 roles, and the AccessRules that decide
SME's own ProvMnS routes.

What it is: the hooks and rows of the three classes of the vendored MSAC NRM (`TS28319_MsacNrm.yaml`), served by SME's ProvMnS producer
(`app/mns_registry.py`) in its store `sme.mns_registry`, under `SubNetwork=SMO` (the NRM's OpenAPI declares no name-containment; SME's class registry
lets a SubNetwork contain them, `ClassRegistry.contain`):

  SubNetwork=SMO,Identity=<id>     identityType (USERNAME for a console user, MACHINEUSER for an invoker, ...), identityName (the caller's actor: the
                                   console's `smo-gui:<user>`, or an invoker id), roleList (Role DNs)
  SubNetwork=SMO,Role=<id>         roleName, accessRulesList (AccessRule DNs)
  SubNetwork=SMO,AccessRule=<id>   ruleName, dataNodeSelector (`smo_shared.msac`'s subset), operations (create, read, update, delete, exec), actions

The five roles of ADR 0008 D15 are written with their AccessRules the first time SME finds them missing (`ensure_d15_roles`; their rules:
`smo_shared/auth/roles.py`). A role may be changed but not deleted (422 VETOED).

Checks on a write (422 VETOED, naming the attribute): an Identity carries no `credential` (SME keeps no password: credentials stay with SME's invoker
registrations and the console's identity provider); an AccessRule's selector is one this build evaluates, its operations are TS 28.319's and its action
is ALLOW or DENY (a rule that could never match is refused, not kept).

Who may change them: SME's router decides with `AccessRuleAuthorizer` over these same rows (`local_rules`), read in the request's own session, so a
NOC_OPERATOR, whose role denies writes under `SubNetwork=SMO`, cannot give itself more. Other modules read them through R1 Termination
(`smo_shared.auth.SmeAccessRules`, `SMO_ACCESS_RULES=sme`).
"""

from sqlalchemy import select

from smo_shared import msac
from smo_shared.auth import access, roles
from smo_shared.mns import IMoiLifecycleHandler, MnsError, Moi, MoiContext
from smo_shared.mns.checks import SESSION
from smo_shared.mns.dn import depth_of, parent_of, parse_dn
from smo_shared.mns.service import resource

CLASSES = access.CLASSES
BASE_DN = access.BASE_DN


class IdentityHooks(IMoiLifecycleHandler):
    """Identity: no credential is kept."""

    @staticmethod
    def _check(moi: Moi) -> None:
        """422 VETOED for a credential."""
        if "credential" in moi.attributes:
            raise MnsError(422, "VETOED", "an Identity carries no credential here: invokers authenticate at SME's token endpoint, people at the console's "
                           "identity provider", bad_attributes=["credential"], bad_objects=[moi.dn])

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Refuses a credential."""
        self._check(moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Refuses a credential."""
        self._check(new)


class RoleHooks(IMoiLifecycleHandler):
    """Role: the five D15 roles are not deleted."""

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Refuses deleting a D15 role."""
        if moi.moi_id in roles.D15_ROLES:
            raise MnsError(422, "VETOED", f"{moi.moi_id} is one of the platform's five roles (D15): change its rules, do not delete it", bad_objects=[moi.dn])


class AccessRuleHooks(IMoiLifecycleHandler):
    """AccessRule: only a rule this build evaluates is kept."""

    @staticmethod
    def _check(moi: Moi) -> None:
        """422 VETOED, naming the attribute, for a selector, operations or action this build does not evaluate."""
        attributes = moi.attributes
        selector = attributes.get("dataNodeSelector")
        if not isinstance(selector, str) or not msac.selector_supported(selector):
            raise MnsError(422, "VETOED", "dataNodeSelector is an absolute path of Class=id segments, * a wildcard (/SubNetwork=A/*)",
                           bad_attributes=["dataNodeSelector"], bad_objects=[moi.dn])
        operations = attributes.get("operations")
        if not isinstance(operations, list) or not operations or not set(operations) <= set(msac.OPERATIONS):
            raise MnsError(422, "VETOED", f"operations is a non-empty list of {', '.join(msac.OPERATIONS)}", bad_attributes=["operations"],
                           bad_objects=[moi.dn])
        if attributes.get("actions") not in msac.ACTIONS:
            raise MnsError(422, "VETOED", "actions is ALLOW or DENY", bad_attributes=["actions"], bad_objects=[moi.dn])

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Checks the rule."""
        self._check(moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Checks the changed rule."""
        self._check(new)


def serve(mns) -> None:
    """Serves the three classes on SME's producer with their hooks, under a SubNetwork."""
    mns.registry.index_file("TS28319_MsacNrm.yaml")
    mns.registry.contain("SubNetwork", CLASSES)
    mns.serve("Identity", IdentityHooks())
    mns.serve("Role", RoleHooks())
    mns.serve("AccessRule", AccessRuleHooks())


def ensure_d15_roles(db, model) -> bool:
    """Writes the five D15 roles and their rules under `SubNetwork=SMO` when PLATFORM_ADMIN is missing (the container must exist); True when it did.
    Does not commit."""
    if db.get(model, f"{BASE_DN},Role=PLATFORM_ADMIN") is not None:
        return False
    for dn, body in roles.as_resources(BASE_DN):
        if db.get(model, dn) is None:
            last = parse_dn(dn)[-1]
            db.add(model(dn=dn, object_class=last.class_name, moi_id=last.moi_id, parent_dn=parent_of(dn), depth=depth_of(dn),
                         attributes=body["attributes"]))
    return True


def local_rules(model):
    """The source of SME's own decisions: Identity, Role and AccessRule read from `model`'s table in the operation's session (`checks.SESSION`); no
    session (outside an operation): none, so nobody is managed."""
    def load() -> list[dict]:
        db = SESSION.get()
        if db is None:
            return []
        rows = db.scalars(select(model).where(model.parent_dn == BASE_DN, model.object_class.in_(CLASSES)))
        return [resource(row) for row in rows]
    return access.LoadedAccessRules(load)
