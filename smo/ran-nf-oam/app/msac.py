"""TS 28.319 MSAC (management service access control): Identity / Role / AccessRule.

SA-RANOAM-1. Replaces the old presence check of `msacRole` with role-based
access control evaluated per sub-change before anything is dispatched.

The three IOCs are REST resources with the spec's attribute names
(`TS28319_MsacNrm.yaml`), flat ids as in every NRM module here:
Identity.roleList holds Role ids, Role.accessRulesList holds AccessRule ids.
`credential` is write-only: only a salted scrypt hash is stored. It is checked when a caller presents it: `POST /config-jobs` for a requester that is an Identity with a
credential must carry it in the `X-MSAC-Credential` header (`verify_identity_credential`, SEC-15.6); an Identity with no credential is not asked for one.

Evaluation (`authorize`): the requester's roles come from the Identity named
by `requestedBy` plus the Role named by `msacRole`, if either exists. Every
AccessRule of those roles whose `dataNodeSelector` matches the target and
whose `operations` include the operation applies; DENY beats ALLOW; no
matching rule means refused. A requester with neither a registered Identity
nor a defined Role is unmanaged and falls back to the legacy gate in
`main.py`.

Deviation, recorded: `dataNodeSelector` is a Jex expression (TS 32.161). This
build accepts the absolute-path subset of the basic profile: `/Class=id/...`
with `*` as a wildcard, matched against the target's DN path. Any other
expression is refused at creation rather than stored and never matching.
"""

import hashlib
import hmac
import os
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import invoker_id
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .ldn import parse_ldn
from .models import MsacAccessRule, MsacIdentity, MsacRole

router = APIRouter(prefix="/msac")

# The request header that carries the credential of the Identity a write names in `requestedBy` (SEC-15.6). A header, not a body field, so a refused request's validation error and
# the stored request never hold it.
CREDENTIAL_HEADER = "X-MSAC-Credential"

OPERATIONS = {"create", "read", "update", "delete", "exec"}
# edit-config operation (RFC 6241) -> the MSAC operation it needs
CONFIG_OPERATION = {"merge": "update", "replace": "update", "create": "create", "delete": "delete", "remove": "delete"}


def hash_credential(secret: str) -> str:
    """A salted scrypt hash of an Identity credential, as `scrypt$<salt hex>$<digest hex>` (16 random salt bytes, n=2**14, r=8, p=1). The only form in which a credential is stored.
    """
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"


def check_credential(secret: str, stored: str | None) -> bool:
    """Whether `secret` matches a hash made by `hash_credential`; False when nothing is stored or what is stored is not such a hash (never an error).

    The digests are compared with `hmac.compare_digest`, so the time taken does not tell how many leading bytes of a guess were right. The scrypt cost is paid for every attempt
    whatever the outcome.
    """
    if not stored:
        return False
    try:
        scheme, salt, digest = stored.split("$")
        expected = bytes.fromhex(digest)
        derived = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:  # not `scrypt$<hex>$<hex>`: a damaged row refuses everyone, it does not make the route answer 500
        return False
    return scheme == "scrypt" and hmac.compare_digest(derived, expected)


def verify_identity_credential(db: Session, requested_by: str, presented: str | None) -> None:
    """Refuses (403 `MSAC_ACCESS_DENIED`) a write that names an Identity which has a credential, unless that credential is `presented`.

    `requestedBy` is the caller's own word, so without this the name of an Identity with wide rules is all it takes to use them. An Identity with no stored credential, and a requester
    that is no Identity at all (a Role named by `msacRole`, or nobody: the legacy gate), are not asked for one, so a deployment that never set a credential sees no change. A missing
    and a wrong credential get the same answer, which does not repeat either value. Called by `POST /config-jobs` only: a rollback, an approval's job and a KPI-guard revert are
    made from a record whose first request was checked.
    """
    identity = db.scalars(select(MsacIdentity).where(MsacIdentity.identity_name == requested_by)).first()
    if identity is None or not identity.credential_hash:
        return
    if presented is None or not check_credential(presented, identity.credential_hash):
        raise framework_error(FrameworkError.MSAC_ACCESS_DENIED, detail=f"{requested_by} has a credential: send it in the {CREDENTIAL_HEADER} header")


_SEGMENT = re.compile(r"^[A-Za-z_*][A-Za-z0-9_*]*=[^/\[\]@'\"]+$|^\*$")


def _check_selector(selector: str) -> None:
    """Refuses a `dataNodeSelector` that is not an absolute path of `Class=id` segments (with `*` wildcards): 422 SCHEMA_VALIDATION_FAILED. An expression this build cannot evaluate is rejected at creation rather than stored and never matched.
    """
    segments = selector[1:].split("/") if selector.startswith("/") else None
    if segments and segments[-1] == "":
        segments = segments[:-1]  # a trailing slash
    if segments is None or not all(_SEGMENT.match(seg) for seg in segments):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                              detail=f"dataNodeSelector {selector!r}: only absolute Jex paths of Class=id segments with '*' wildcards are supported")


def target_path(managed_element_ref: str, managed_function_ref: str | None) -> str:
    """The DN path of a change target: `/SubNetwork=A/ManagedElement=ME-1/NRCellDU=101`."""
    function = managed_function_ref or ""
    if "=" in managed_element_ref:  # the ME ref is itself a DN
        rdns = [f"{c}={i}" for c, i in parse_ldn(managed_element_ref)]
    else:
        rdns = [f"ManagedElement={managed_element_ref}"]
    if function:
        rdns += [f"{c}={i}" for c, i in parse_ldn(function)] if "=" in function else [function]
    return "/" + "/".join(rdns)


def _rules_of(db: Session, roles: list[MsacRole]) -> list[MsacAccessRule]:
    ids = {uuid.UUID(str(r)) for role in roles for r in role.access_rules_list}
    return [rule for i in ids if (rule := db.get(MsacAccessRule, i)) is not None]


def resolve_roles(db: Session, requested_by: str, msac_role: str | None) -> tuple[bool, list[MsacRole]]:
    """(managed?, roles). Managed means an Identity or a defined Role was found."""
    identity = db.scalars(select(MsacIdentity).where(MsacIdentity.identity_name == requested_by)).first()
    roles = [r for r in (db.get(MsacRole, uuid.UUID(str(i))) for i in (identity.role_list if identity else [])) if r is not None]
    named = db.scalars(select(MsacRole).where(MsacRole.role_name == msac_role)).first() if msac_role else None
    if named is not None and named not in roles:
        roles.append(named)
    return identity is not None or named is not None, roles


def _selects(selector: str, target: str) -> bool:
    pattern = re.escape(selector.rstrip("/") or "/*").replace(r"\*", ".*")
    return re.fullmatch(pattern, target) is not None


def authorize(db: Session, roles: list[MsacRole], target: str, operation: str) -> bool:
    """Whether the roles allow `operation` on the DN path `target`: the rules of the roles that select the target and list the operation are collected; any DENY refuses, otherwise at least one ALLOW is needed, and no matching rule means refused.
    """
    matched = [r for r in _rules_of(db, roles) if operation in r.operations and _selects(r.data_node_selector, target)]
    if any(r.actions == "DENY" for r in matched):
        return False
    return any(r.actions == "ALLOW" for r in matched)


def reach_on() -> bool:
    """MGT-2: `RAN_NF_OAM_MSAC_REACH` (off by default): the access rules that guard CM writes also guard the reads and the other changes of `MGT-2`. Read at each call."""
    return os.environ.get("RAN_NF_OAM_MSAC_REACH", "").strip().lower() in ("1", "true", "yes", "on")


def _managed_roles(db: Session, request: Request) -> tuple[str | None, list[MsacRole]] | None:
    """(the caller's invoker id, its roles) when the switch is on and the caller is a registered Identity; `None` for every caller that is not asked."""
    if not reach_on():
        return None
    requester = invoker_id(request)
    managed, roles = resolve_roles(db, requester, None) if requester else (False, [])
    return (requester, roles) if managed else None


def unreadable_elements(db: Session, request: Request, column) -> list[str]:
    """MGT-2.5/2.6: the elements named in `column` (of the rows a route would list) that a managed caller may not `read`; none for a caller that is not asked
    (switch off, not a registered Identity, not through the gateway). A list route leaves out the rows of these elements: filtered, never refused, as the scope is."""
    asked = _managed_roles(db, request)
    if asked is None:
        return []
    _, roles = asked
    return [ref for ref in db.scalars(select(column).distinct()).all() if not authorize(db, roles, target_path(ref, None), "read")]


def readable(stmt: Select, db: Session, request: Request, column) -> Select:
    """`stmt` without the rows whose element (`column`) a managed caller may not `read` (MGT-2.6); unchanged for a caller that is not asked."""
    hidden = unreadable_elements(db, request, column)
    return stmt.where(column.not_in(hidden)) if hidden else stmt


def may_read_everywhere(db: Session, request: Request) -> bool:
    """MGT-2.5/2.6: whether a caller may `read` the whole network (the root, which only a rule on `/*` selects); true for a caller that is not asked."""
    asked = _managed_roles(db, request)
    return asked is None or authorize(db, asked[1], "/", "read")


def may_read(db: Session, request: Request, managed_element_ref: str) -> bool:
    """MGT-2.6: whether a managed caller may `read` the element (true for a caller that is not asked). For the quiet routes (a delete answers 204 either way)."""
    asked = _managed_roles(db, request)
    return asked is None or authorize(db, asked[1], target_path(managed_element_ref, None), "read")


# ---------------------------------------------------------------- resources

# Request body of `POST /msac/access-rules`. `operations` and `componentCData` are stored sorted and de-duplicated.
class AccessRuleAttributes(BaseModel):
    ruleName: str
    dataNodeSelector: str
    operations: list[Literal["create", "read", "update", "delete", "exec"]]
    actions: Literal["ALLOW", "DENY"]
    componentCData: list[str] = []


# Request and response attributes of a Role. `accessRulesList` holds AccessRule ids that must exist.
class RoleAttributes(BaseModel):
    roleName: str
    accessRulesList: list[uuid.UUID] = []


# Request attributes of an Identity. `credential` is write-only: it is hashed on the way in and never returned (`_identity_view` omits it).
class IdentityAttributes(BaseModel):
    identityType: Literal["USERNAME", "EMAIL_ADDRESS", "PHONE_NUMBER", "IP_ADDRESS", "MACHINEUSER"]
    identityName: str
    credential: str | None = None
    roleList: list[uuid.UUID] = []


def _rule_view(r: MsacAccessRule) -> dict:
    return {"id": str(r.rule_id), "attributes": {"ruleName": r.rule_name, "dataNodeSelector": r.data_node_selector,
            "operations": r.operations, "actions": r.actions, "componentCData": r.component_c_data}}


def _role_view(r: MsacRole) -> dict:
    return {"id": str(r.role_id), "attributes": {"roleName": r.role_name, "accessRulesList": [str(i) for i in r.access_rules_list]}}


def _identity_view(i: MsacIdentity) -> dict:
    return {"id": str(i.identity_id), "attributes": {"identityType": i.identity_type, "identityName": i.identity_name,
            "roleList": [str(r) for r in i.role_list]}}  # no credential, ever


def _get(db: Session, model, object_id: uuid.UUID, name: str):
    row = db.get(model, object_id)
    if row is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such {name} {object_id}")
    return row


def _require_refs(db: Session, model, ids: list[uuid.UUID], name: str) -> None:
    missing = [str(i) for i in ids if db.get(model, i) is None]
    if missing:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"{name} ref(s) not found: {missing}")


@router.post("/access-rules", status_code=201)
def create_access_rule(body: AccessRuleAttributes, db: Session = Depends(get_session)):
    # The selector is checked first (422 when it is not supported); 201 with the stored rule.
    _check_selector(body.dataNodeSelector)
    row = MsacAccessRule(rule_name=body.ruleName, data_node_selector=body.dataNodeSelector, operations=sorted(set(body.operations)),
                         actions=body.actions, component_c_data=sorted(set(body.componentCData)))
    db.add(row)
    db.commit()
    return _rule_view(row)


@router.get("/access-rules")
def list_access_rules(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, unfiltered.
    page = paginate(db, select(MsacAccessRule), limit, offset)
    return {**page, "items": [_rule_view(r) for r in page["items"]]}


@router.get("/access-rules/{rule_id}")
def get_access_rule(rule_id: uuid.UUID, db: Session = Depends(get_session)):
    # 404 NRM_OBJECT_NOT_FOUND when the id is unknown.
    return _rule_view(_get(db, MsacAccessRule, rule_id, "AccessRule"))


@router.delete("/access-rules/{rule_id}", status_code=204)
def delete_access_rule(rule_id: uuid.UUID, db: Session = Depends(get_session)):
    # 204 whether or not the rule existed. A rule that exists is also removed from every role that listed it, in the same commit.
    row = db.get(MsacAccessRule, rule_id)
    if row is not None:
        for role in db.scalars(select(MsacRole)).all():  # a deleted rule leaves every role that listed it
            role.access_rules_list = [i for i in role.access_rules_list if str(i) != str(rule_id)]
        db.delete(row)
        db.commit()


@router.post("/roles", status_code=201)
def create_role(body: RoleAttributes, db: Session = Depends(get_session)):
    # 422 SCHEMA_VALIDATION_FAILED when a listed AccessRule does not exist or the role name is taken; 201 with the role.
    _require_refs(db, MsacAccessRule, body.accessRulesList, "AccessRule")
    if db.scalars(select(MsacRole).where(MsacRole.role_name == body.roleName)).first() is not None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"role {body.roleName!r} already exists")
    row = MsacRole(role_name=body.roleName, access_rules_list=[str(i) for i in body.accessRulesList])
    db.add(row)
    db.commit()
    return _role_view(row)


@router.get("/roles")
def list_roles(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, unfiltered.
    page = paginate(db, select(MsacRole), limit, offset)
    return {**page, "items": [_role_view(r) for r in page["items"]]}


@router.get("/roles/{role_id}")
def get_role(role_id: uuid.UUID, db: Session = Depends(get_session)):
    # 404 NRM_OBJECT_NOT_FOUND when the id is unknown.
    return _role_view(_get(db, MsacRole, role_id, "Role"))


@router.put("/roles/{role_id}")
def replace_role(role_id: uuid.UUID, body: RoleAttributes, db: Session = Depends(get_session)):
    # Replaces the name and the rule list. 404 for an unknown role, 422 for an unknown AccessRule. The new name is not checked against other roles here, so renaming onto a taken name reaches the database's unique constraint instead of the 422 that create gives.
    row = _get(db, MsacRole, role_id, "Role")
    _require_refs(db, MsacAccessRule, body.accessRulesList, "AccessRule")
    row.role_name, row.access_rules_list = body.roleName, [str(i) for i in body.accessRulesList]
    db.commit()
    return _role_view(row)


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role_id: uuid.UUID, db: Session = Depends(get_session)):
    # 204 whether or not the role existed. A role that exists is also removed from every identity's role list, in the same commit.
    row = db.get(MsacRole, role_id)
    if row is not None:
        for identity in db.scalars(select(MsacIdentity)).all():
            identity.role_list = [i for i in identity.role_list if str(i) != str(role_id)]
        db.delete(row)
        db.commit()


@router.post("/identities", status_code=201)
def create_identity(body: IdentityAttributes, db: Session = Depends(get_session)):
    # 422 SCHEMA_VALIDATION_FAILED when a listed Role does not exist or the identity name is taken; the credential, when given, is stored only as a scrypt hash. 201 with the identity (no credential).
    _require_refs(db, MsacRole, body.roleList, "Role")
    if db.scalars(select(MsacIdentity).where(MsacIdentity.identity_name == body.identityName)).first() is not None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"identity {body.identityName!r} already exists")
    row = MsacIdentity(identity_type=body.identityType, identity_name=body.identityName,
                       credential_hash=hash_credential(body.credential) if body.credential else None,
                       role_list=[str(i) for i in body.roleList])
    db.add(row)
    db.commit()
    return _identity_view(row)


@router.get("/identities")
def list_identities(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Paged list, unfiltered; no credential in any item.
    page = paginate(db, select(MsacIdentity), limit, offset)
    return {**page, "items": [_identity_view(i) for i in page["items"]]}


@router.get("/identities/{identity_id}")
def get_identity(identity_id: uuid.UUID, db: Session = Depends(get_session)):
    # 404 NRM_OBJECT_NOT_FOUND when the id is unknown; no credential in the answer.
    return _identity_view(_get(db, MsacIdentity, identity_id, "Identity"))


@router.put("/identities/{identity_id}")
def replace_identity(identity_id: uuid.UUID, body: IdentityAttributes, db: Session = Depends(get_session)):
    # Replaces type, name and role list; a credential in the body replaces the stored hash, an omitted one keeps it. 404 for an unknown identity, 422 for an unknown Role. As for roles, a renamed identity is not checked against the other names here, so a taken name reaches the unique constraint.
    row = _get(db, MsacIdentity, identity_id, "Identity")
    _require_refs(db, MsacRole, body.roleList, "Role")
    row.identity_type, row.identity_name, row.role_list = body.identityType, body.identityName, [str(i) for i in body.roleList]
    if body.credential:  # omitted = keep the stored credential
        row.credential_hash = hash_credential(body.credential)
    db.commit()
    return _identity_view(row)


@router.delete("/identities/{identity_id}", status_code=204)
def delete_identity(identity_id: uuid.UUID, db: Session = Depends(get_session)):
    # 204 whether or not the identity existed.
    row = db.get(MsacIdentity, identity_id)
    if row is not None:
        db.delete(row)
        db.commit()
