"""The four TS 28.532 ProvMnS operations on a module's `moi_store` (plan tasks S1.1, S1.2, S1.4), independent of HTTP (`router.py` is the HTTP face).

    mns = ProvMnS(source="/smo/echo", system_dn="ManagementNode=echo", classes=["SubNetwork", "ManagedElement", "GNBDUFunction"])
    mns.serve("GNBDUFunction", MyHandler())         # the class's lifecycle hooks (lifecycle.py)

  create_moi(db, dn, body, if_match)          createMOI (HTTP PUT): creates the instance, or replaces its attributes when it exists
  get_moi_attributes(db, dn, ...)             getMOIAttributes (HTTP GET): scope, filter, attribute selection, keyset paging
  modify_moi_attributes(db, dn, media, body)  modifyMOIAttributes (HTTP PATCH): merge patch or JSON patch (patch.py)
  delete_moi(db, dn, if_match)                deleteMOI (HTTP DELETE): the instance and everything it contains

Each write runs in the caller's session and ends with its commit; any MnsError (the router's own, the registry's, a hook's veto) rolls it back first.
In order, a write: checks the class is served (422 CLASS_NOT_SUPPORTED) and the DN; checks containment (the parent exists, 422 PARENT_NOT_FOUND, and
its class may contain this one, 422 CONTAINMENT_VIOLATION); validates the attributes against the class's schema (registry.py, 422
SCHEMA_VALIDATION_FAILED); checks `If-Match` (412 STALE_VERSION when the instance is not at that version); calls the `before_*` hook; writes; flushes
(a concurrent writer surfaces here: `versioning.py`'s StaleDataError, or a duplicate key for two creates, is a 409 CONCURRENT_MODIFICATION); calls the
`after_*` hook; publishes the TS 28.532 notification on the event bus (`smo_shared.events`, type `org.oran.smo.provmns.<notificationType>`, in the same
transaction, so it exists exactly when the change does); commits.

Notifications are the bodies of the vendored OpenAPI: `NotifyMoiCreation` (`attributeList`: the new instance's attributes), `NotifyMoiAttributeValueChanges`
(`attributeListValueChanges`: [new values, old values] of the attributes that changed; a removed attribute's new value and an added one's old value are
null) and `NotifyMoiDeletion` (`attributeList`: the deleted instance's attributes), each with the `NotificationHeader` (`href` the instance's URI,
`notificationId`, `notificationType`, `eventTime`, `systemDN`) and `sourceIndicator` MANAGEMENT_OPERATION. A write that changes nothing publishes
nothing. Delivery to subscribers (NtfSubscriptionControl) is S1.13's; here the notifications go to the event bus only.
"""

import base64
import binascii
import datetime
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from .. import events
from .dn import SUBTREE_UPPER, depth_of, parent_of, parse_dn, path_of
from .errors import MnsError
from .checks import INTERNAL, SESSION, Caller, RouterChecks
from .lifecycle import DEFAULT_HANDLER, IMoiLifecycleHandler, Moi, MoiContext
from .patch import JSON_PATCH_TYPES, MERGE_PATCH_TYPES, apply_json_patch, apply_merge_patch
from .registry import ClassRegistry, default_registry
from .scope import Scope, matches, select as select_fields
from .store import ManagedObject
from .subscriptions import CLASSES as SUBSCRIPTION_CLASSES, SubscriptionManager

PROVMNS_VERSION = "19.2.0"          # D5: the one ProvMnS version every module serves (specs/smo-schema-bundle/openapi/common/provmns-root.yaml)
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
_MAX_NOTIFICATION_ID = 2**53 - 1    # NotificationId is an integer; this keeps it exact in every JSON reader


@dataclass(frozen=True)
class Page:
    """One page of getMOIAttributes: the selected resources in DN order and the cursor of the next page (None on the last)."""

    items: list[dict]
    next_cursor: str | None


def encode_cursor(dn: str) -> str:
    """The opaque cursor of the page after the instance `dn` (base64url of the DN)."""
    return base64.urlsafe_b64encode(dn.encode()).decode().replace("=", "")


def decode_cursor(cursor: str) -> str:
    """The DN a cursor stands for; one that is not a cursor this router issued is a 400 naming `cursor`."""
    try:
        dn = base64.urlsafe_b64decode(cursor + "==").decode()       # surplus padding is ignored
        parse_dn(dn)
    except (binascii.Error, UnicodeDecodeError, ValueError, MnsError):
        raise MnsError(400, "INVALID_QUERY", "cursor is not a cursor of this producer", bad_query_params=["cursor"]) from None
    return dn


def etag(version: int) -> str:
    """The ETag of an instance at `version` (its moiVersion)."""
    return f'"{version}"'


def public(attributes: dict | None) -> dict:
    """The attributes a caller sees: every one but the hidden ones the producer keeps for itself (a name starting with `_`, which no class schema
    declares, so a caller can never send one: subscriptions.py keeps a subscription's subscriber that way)."""
    return {name: value for name, value in (attributes or {}).items() if not name.startswith("_")}


def hidden(attributes: dict | None) -> dict:
    """The hidden attributes of a stored instance (see `public`); a modify keeps them."""
    return {name: value for name, value in (attributes or {}).items() if name.startswith("_")}


def resource(row: ManagedObject) -> dict:
    """The TS 28.532 `Resource` of a stored instance."""
    return {"id": row.moi_id, "objectClass": row.object_class, "objectInstance": row.dn, "attributes": public(row.attributes)}


def _moi(row: ManagedObject) -> Moi:
    return Moi(row.dn, row.object_class, row.moi_id, public(row.attributes), row.row_version)


def _check_if_match(if_match: str | None, row: ManagedObject | None, dn: str) -> None:
    """RFC 9110 If-Match: `*` needs the instance to exist; a list of ETags needs its moiVersion among them (weak tags compare equal). 412 otherwise."""
    if if_match is None:
        return
    if row is None:
        raise MnsError(412, "STALE_VERSION", f"{dn} does not exist, and If-Match names a version", bad_objects=[dn])
    if if_match.strip() == "*":
        return
    tags = [tag.strip().removeprefix("W/") for tag in if_match.split(",")]
    if etag(row.row_version) not in tags:
        raise MnsError(412, "STALE_VERSION", f"{dn} is at moiVersion {row.row_version}, not the one If-Match names", bad_objects=[dn],
                       headers={"ETag": etag(row.row_version)})


def _changes(old: dict, new: dict) -> tuple[dict, dict]:
    """The [new values, old values] of the attributes that differ; a missing value is null."""
    names = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name) or (name in old) != (name in new))
    return {name: new.get(name) for name in names}, {name: old.get(name) for name in names}


class ProvMnS:
    """One module's ProvMnS producer (see the module description). `classes` limits the served classes (None: every class the registry knows);
    `api_root` prefixes the `href` of notifications; `model` is the mapped class of the store (`ManagedObject`, the table `moi_store`, unless
    another was made with `store.store_model`); `subscriptions` serves NtfSubscriptionControl and HeartbeatControl and delivers to them
    (subscriptions.py; on unless False, when the registry knows the two classes)."""

    def __init__(self, *, source: str, system_dn: str, registry: ClassRegistry | None = None, classes: Iterable[str] | None = None,
                 api_root: str = "", checks: RouterChecks | None = None, model: type[ManagedObject] = ManagedObject, subscriptions: bool = True):
        """Keeps the event `source` (`/smo/<module>`), the `systemDN` of the notifications, the registry (the shared default when not given), the served
        classes and the cross-cutting checks (checks.py; by default: claim scope with no placement, no MSAC, audit on, the module named after `source`)."""
        self.source = source
        self.checks = checks if checks is not None else RouterChecks(module=source.rpartition("/")[2])
        self.system_dn = system_dn
        self.registry = registry if registry is not None else default_registry()
        self.api_root = api_root.rstrip("/")
        self.model = model
        self._served: set[str] | None = None if classes is None else set()
        self._handlers: dict[str, IMoiLifecycleHandler] = {}
        for name in classes or ():
            self.serve(name)
        known = all(name in self.registry for name in SUBSCRIPTION_CLASSES)      # a registry without the Subscription Control NRM has none
        self.subscriptions = SubscriptionManager(self) if subscriptions and known else None

    # ------------------------------------------------------------------ configuration

    def serve(self, class_name: str, handler: IMoiLifecycleHandler | None = None) -> None:
        """Serves `class_name` (which the registry must know: a ValueError at start-up otherwise) with `handler`'s hooks (none: the no-op ones)."""
        if class_name not in self.registry:
            raise ValueError(f"class {class_name!r} has no schema in the registry; register it before serving it")
        if self._served is not None:
            self._served.add(class_name)
        if handler is not None:
            self._handlers[class_name] = handler

    def served(self, class_name: str) -> bool:
        """Whether this producer serves `class_name`."""
        if self._served is None:
            return class_name in self.registry
        return class_name in self._served

    def _require_served(self, class_name: str) -> None:
        if not self.served(class_name):
            raise MnsError(422, "CLASS_NOT_SUPPORTED", f"class {class_name!r} is not served here, or has no registered schema", bad_objects=[class_name])

    def _handler(self, class_name: str) -> IMoiLifecycleHandler:
        return self._handlers.get(class_name, DEFAULT_HANDLER)

    # ------------------------------------------------------------------ notifications

    def _notify(self, db: Session, notification_type: str, dn: str, extra: dict[str, Any]) -> None:
        """Publishes one TS 28.532 notification on the event bus, in `db`'s transaction."""
        body = {"href": f"{self.api_root}/ProvMnS/{PROVMNS_VERSION}/{path_of(dn)}", "notificationId": secrets.randbelow(_MAX_NOTIFICATION_ID) + 1,
                "notificationType": notification_type, "eventTime": datetime.datetime.now(datetime.UTC).isoformat(), "systemDN": self.system_dn,
                "sourceIndicator": "MANAGEMENT_OPERATION", **extra}
        events.publish(db, "provmns", notification_type, self.source, subject=dn, data=body)
        if self.subscriptions is not None:
            self.subscriptions.deliver(db, notification_type, dn, body)

    # ------------------------------------------------------------------ the writes

    def _flush(self, db: Session, dn: str) -> None:
        """Flushes, turning a concurrent writer (a stale moiVersion, a duplicate DN) into a 409."""
        try:
            db.flush()
        except (StaleDataError, IntegrityError):
            raise MnsError(409, "CONCURRENT_MODIFICATION", f"{dn} was changed by another request while this one ran; repeat the request",
                           bad_objects=[dn]) from None

    def _run(self, db: Session, work):
        """Runs `work()` and commits (with `db` as the operation's session, `checks.SESSION`); rolls back and re-raises on any exception."""
        token = SESSION.set(db)
        try:
            result = work()
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            SESSION.reset(token)

    def create_moi(self, db: Session, dn: str, body: Any, if_match: str | None = None, caller: Caller = INTERNAL) -> tuple[dict, bool, int]:
        """createMOI: (resource, created, moiVersion). Creates the instance at `dn` from the Resource `body`, or, when it exists, replaces its
        attributes (a modify, with its hooks and notification). See the module description for the checks; `caller` is who asks (checks.py)."""
        def work():
            created, is_new, version = self._create(db, dn, body, if_match, caller)
            self.checks.record(db, caller, "createMOI", dn, 201 if is_new else 200, {"moiVersion": version})
            return created, is_new, version
        return self._run(db, work)

    def _create(self, db: Session, dn: str, body: Any, if_match: str | None, caller: Caller) -> tuple[dict, bool, int]:
        """createMOI's work inside the transaction (create_moi commits or rolls back around it)."""
        rdns = parse_dn(dn)
        self.checks.authorize(caller, "createMOI", dn)
        class_name, moi_id = rdns[-1].class_name, rdns[-1].moi_id
        self._require_served(class_name)
        attributes = self._resource_attributes(body, dn, class_name, moi_id)
        self.registry.validate(class_name, attributes)
        row = db.get(self.model, dn)
        _check_if_match(if_match, row, dn)
        ctx = MoiContext("createMOI", db, dn)
        if row is not None:
            if row.object_class != class_name:
                raise MnsError(409, "CLASS_MISMATCH", f"{dn} is a {row.object_class}", bad_objects=[dn])
            return self._replace(db, ctx, row, attributes), False, row.row_version
        parent_dn = parent_of(dn)
        parent_class = None
        if parent_dn is not None:
            parent = db.get(self.model, parent_dn)
            if parent is None:
                raise MnsError(422, "PARENT_NOT_FOUND", f"{parent_dn} does not exist", bad_objects=[parent_dn])
            parent_class = parent.object_class
        if not self.registry.may_contain(parent_class, class_name):
            where = f"under a {parent_class}" if parent_class else "at the top of the tree"
            raise MnsError(422, "CONTAINMENT_VIOLATION", f"a {class_name} may not be created {where}", bad_objects=[dn])
        moi = Moi(dn, class_name, moi_id, attributes, 0)
        handler = self._handler(class_name)
        handler.before_create(ctx, moi)
        kept = self._stored(class_name, attributes)
        extra = self.subscriptions.hidden_on_create(class_name, caller) if self.subscriptions is not None else {}
        row = self.model(dn=dn, object_class=class_name, moi_id=moi_id, parent_dn=parent_dn, depth=depth_of(dn), attributes={**kept, **extra})
        db.add(row)
        self._flush(db, dn)
        handler.after_create(ctx, _moi(row))
        self._notify(db, "notifyMOICreation", dn, {"attributeList": kept} if kept else {})
        return resource(row), True, row.row_version

    @staticmethod
    def _resource_attributes(body: Any, dn: str, class_name: str, moi_id: str) -> dict:
        """The attributes of a createMOI body, after checking it is one Resource for this DN (400 otherwise; contained objects are a 422)."""
        if not isinstance(body, dict):
            raise MnsError(400, "INVALID_BODY", "the body is a Resource (a JSON object)")
        if body.get("id") != moi_id:
            raise MnsError(400, "INVALID_BODY", f"id must be {moi_id!r}, the id of the URI", bad_attributes=["id"])
        if body.get("objectClass", class_name) != class_name:
            raise MnsError(400, "INVALID_BODY", f"objectClass must be {class_name!r}, the class of the URI", bad_attributes=["objectClass"])
        if body.get("objectInstance", dn) != dn:
            raise MnsError(400, "INVALID_BODY", f"objectInstance must be {dn!r}, the DN of the URI", bad_attributes=["objectInstance"])
        others = sorted(set(body) - {"id", "objectClass", "objectInstance", "attributes"})
        if others:
            raise MnsError(422, "INVALID_BODY", "createMOI creates one instance; create contained instances with their own createMOI",
                           bad_attributes=others)
        attributes = body.get("attributes", {})
        if not isinstance(attributes, dict):
            raise MnsError(400, "INVALID_BODY", "attributes must be an object", bad_attributes=["attributes"])
        return attributes

    def _stored(self, class_name: str, attributes: dict) -> dict:
        """The attributes as kept (a write-only one is not: subscriptions.py)."""
        return self.subscriptions.stored(class_name, attributes) if self.subscriptions is not None else attributes

    def _replace(self, db: Session, ctx: MoiContext, row: ManagedObject, attributes: dict) -> dict:
        """Changes a stored instance's attributes to `attributes`, with the modify hooks and notification; nothing at all when they are equal. The
        hooks see the attributes as sent; what is kept leaves out a write-only one and keeps the hidden ones; a write that changes nothing kept
        (only a write-only attribute) runs the hooks and notifies nothing."""
        old = _moi(row)
        if old.attributes == attributes:
            return resource(row)
        new = Moi(row.dn, row.object_class, row.moi_id, attributes, row.row_version)
        handler = self._handler(row.object_class)
        handler.before_modify(ctx, old, new)
        kept = self._stored(row.object_class, attributes)
        if kept == old.attributes:
            return resource(row)
        row.attributes = {**kept, **hidden(row.attributes)}
        self._flush(db, row.dn)
        handler.after_modify(ctx, old, _moi(row))
        new_values, old_values = _changes(old.attributes, kept)
        self._notify(db, "notifyMOIAttributeValueChanges", row.dn, {"attributeListValueChanges": [new_values, old_values]})
        return resource(row)

    def modify_moi_attributes(self, db: Session, dn: str, media_type: str, body: Any, if_match: str | None = None,
                              caller: Caller = INTERNAL) -> tuple[dict, int]:
        """modifyMOIAttributes: (resource, moiVersion) after applying the patch `body` of `media_type` (one of the four TS 28.532 patch media types,
        415 otherwise) to the instance at `dn` (404 when it does not exist)."""
        def work():
            changed, version = self._modify(db, dn, media_type, body, if_match, caller)
            self.checks.record(db, caller, "modifyMOIAttributes", dn, 200, {"moiVersion": version})
            return changed, version
        return self._run(db, work)

    def _modify(self, db: Session, dn: str, media_type: str, body: Any, if_match: str | None, caller: Caller) -> tuple[dict, int]:
        """modifyMOIAttributes' work inside the transaction."""
        rdns = parse_dn(dn)
        self.checks.authorize(caller, "modifyMOIAttributes", dn)
        self._require_served(rdns[-1].class_name)
        if media_type not in MERGE_PATCH_TYPES + JSON_PATCH_TYPES:
            raise MnsError(415, "UNSUPPORTED_MEDIA_TYPE", f"{media_type or 'no media type'}: a patch is one of {', '.join(MERGE_PATCH_TYPES + JSON_PATCH_TYPES)}")
        row = self._existing(db, dn)
        if row.object_class != rdns[-1].class_name:
            raise MnsError(409, "CLASS_MISMATCH", f"{dn} is a {row.object_class}", bad_objects=[dn])
        _check_if_match(if_match, row, dn)
        current = public(row.attributes)
        if media_type in MERGE_PATCH_TYPES:
            identity = {"id": row.moi_id, "objectClass": row.object_class, "objectInstance": row.dn}
            attributes = apply_merge_patch(identity, current, body)
        else:
            attributes = apply_json_patch(current, body)
        self.registry.validate(row.object_class, attributes)
        return self._replace(db, MoiContext("modifyMOIAttributes", db, dn), row, attributes), row.row_version

    def delete_moi(self, db: Session, dn: str, if_match: str | None = None, caller: Caller = INTERNAL) -> int:
        """deleteMOI: deletes the instance at `dn` (404 when it does not exist) and every instance it contains, each before the instance that contains
        it (reverse DN order: a contained instance's DN extends its container's), each with its hooks and its notifyMOIDeletion; returns how many were
        deleted. `If-Match` applies to the instance at `dn`. A subtree with an instance the caller may not delete (checks.py) is refused whole, naming
        only the base."""
        def work():
            count = self._delete(db, dn, if_match, caller)
            self.checks.record(db, caller, "deleteMOI", dn, 204, {"deleted": count})
            return count
        return self._run(db, work)

    def _delete(self, db: Session, dn: str, if_match: str | None, caller: Caller) -> int:
        """deleteMOI's work inside the transaction."""
        self.checks.authorize(caller, "deleteMOI", dn)
        self._require_served(parse_dn(dn)[-1].class_name)
        base = self._existing(db, dn)
        _check_if_match(if_match, base, dn)
        rows = list(db.scalars(select(self.model).where(self._subtree(dn, (base.depth, None))).order_by(self.model.dn.desc())))
        if not all(self._allowed(caller, "deleteMOI", row.dn) for row in rows):
            raise MnsError(403, "SCOPE_DENIED", f"{dn} contains instances outside the caller's reach", bad_objects=[dn])
        ctx = MoiContext("deleteMOI", db, dn)
        for row in rows:
            moi = _moi(row)
            handler = self._handler(row.object_class)
            handler.before_delete(ctx, moi)
            db.delete(row)
            self._flush(db, row.dn)
            handler.after_delete(ctx, moi)
            self._notify(db, "notifyMOIDeletion", row.dn, {"attributeList": moi.attributes} if moi.attributes else {})
        return len(rows)

    def _allowed(self, caller: Caller, operation: str, dn: str) -> bool:
        """Whether steps 1 and 2 of checks.py let `caller` do `operation` on `dn` (without raising)."""
        try:
            self.checks.authorize(caller, operation, dn)
        except MnsError:
            return False
        return True

    def _existing(self, db: Session, dn: str) -> ManagedObject:
        row = db.get(self.model, dn)
        if row is None:
            raise MnsError(404, "MOI_NOT_FOUND", f"{dn} does not exist", bad_objects=[dn])
        return row

    # ------------------------------------------------------------------ the read

    def _subtree(self, base_dn: str, depths: tuple[int, int | None]):
        """The SQL condition for the base and its descendants within `depths` (lowest, highest or None): the DN range [base, base + "-"), which holds
        exactly the base and the DNs that start with `base,` (dn.py), so the primary key serves it."""
        lowest, highest = depths
        model = self.model
        condition = and_(model.dn >= base_dn, model.dn < base_dn + SUBTREE_UPPER, model.depth >= lowest)
        return condition if highest is None else and_(condition, model.depth <= highest)

    def get_moi_attributes(self, db: Session, dn: str, scope: Scope | None = None, filter_tree: tuple | None = None,
                           attributes: list[str] | None = None, fields: list[str] | None = None, limit: int = DEFAULT_LIMIT,
                           cursor: str | None = None, caller: Caller = INTERNAL) -> Page:
        """getMOIAttributes: the instances the scope selects from the base at `dn` (404 when the base does not exist, or is outside the caller's
        reach) that pass the filter and that the caller may read (checks.py), in DN order, with the attribute selection applied, at most `limit` (1 to
        MAX_LIMIT, else a 400) after the `cursor`. Reads only; nothing is written."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise MnsError(400, "INVALID_QUERY", f"limit is an integer from 1 to {MAX_LIMIT}", bad_query_params=["limit"])
        token = SESSION.set(db)
        try:
            return self._read(db, dn, scope, filter_tree, attributes, fields, limit, cursor, caller)
        finally:
            SESSION.reset(token)

    def _read(self, db: Session, dn: str, scope: Scope | None, filter_tree: tuple | None, attributes: list[str] | None, fields: list[str] | None,
              limit: int, cursor: str | None, caller: Caller) -> Page:
        """getMOIAttributes' work, with `db` as the operation's session."""
        self.checks.authorize(caller, "getMOIAttributes", dn)
        scope = scope or Scope()
        self._existing(db, dn)
        after = decode_cursor(cursor) if cursor else None
        query = select(self.model).where(self._subtree(dn, scope.depth_range(depth_of(dn)))).order_by(self.model.dn)
        if after is not None:
            query = query.where(self.model.dn > after)
        unrestricted = caller.scope is None and self.checks.msac.provider(caller) is None
        if filter_tree is None and unrestricted:
            query = query.limit(limit + 1)              # one more than the page tells whether a next page exists
        found: list[dict] = []
        for row in db.scalars(query):
            item = resource(row)
            if matches(filter_tree, item) and (unrestricted or self.checks.visible(caller, row.dn)):
                found.append(item)
                if len(found) > limit:
                    break
        page = found[:limit]
        next_cursor = encode_cursor(page[-1]["objectInstance"]) if len(found) > limit else None
        return Page([select_fields(item, attributes, fields) for item in page], next_cursor)
