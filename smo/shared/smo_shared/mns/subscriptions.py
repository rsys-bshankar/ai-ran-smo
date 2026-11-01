"""One subscription manager for every ProvMnS producer (plan task S1.13): TS 28.622 NtfSubscriptionControl and HeartbeatControl, served over ProvMnS by
the router itself, delivering the router's notifications through the transactional outbox.

Every `ProvMnS` serves the two classes of the vendored TS 28.623 Subscription Control NRM (`TS28623_SubscriptionControlNrm.yaml`) unless it is built
with `subscriptions=False`:

  <a SubNetwork or ManagedElement>,NtfSubscriptionControl=<id>
      notificationRecipientAddress   where the notifications go (a URI the SSRF guard accepts, `smo_shared.webhook`; else 422 VETOED)
      notificationTypes              which notification types (absent or empty: all)
      scope                          which instances, relative to the subscription's parent (TS 28.623 Scope: scopeType and scopeLevel, as a
                                     getMOIAttributes scope; or a dataNodeSelector, `smo_shared.msac`'s selector subset). Absent: BASE_ALL
      notificationFilter             a condition on the notification (the router's filter grammar, `scope.parse_filter`, with `@` the
                                     notification body: `@.notificationType`, `@.attributeList.userLabel`, ...); not parsable: 422 VETOED
  ...,NtfSubscriptionControl=<id>,HeartbeatControl=<id>
      heartbeatNtfPeriod             seconds between two notifyHeartbeat to the subscription's recipient (0, the default: none)
      triggerHeartbeatNtf            write only: true sends one notifyHeartbeat now, and is not kept

Delivery. In the transaction of every change, after the router has published its notification on the event bus (`ProvMnS._notify`), `deliver`
finds the subscriptions of this producer's store that want it (type, scope, filter) and whose subscriber may read the instance it is about
(notification access, TS 28.319: the router's own checks, `RouterChecks.visible`, for the caller that created the subscription), and adds one
outbox row per subscription (`smo_shared.outbox.enqueue`). The row exists exactly when the change does and is sent after the commit, at least once,
SSRF-guarded at send time too (CLAUDE.md, "caller-supplied destination"). The subscriptions themselves (NtfSubscriptionControl, HeartbeatControl) are
not delivered to anyone: a subscriber would otherwise learn another's recipient address and filter.

Who subscribed. The caller that created a subscription is kept with it, out of sight: a hidden attribute (`SUBSCRIBER`, a key starting with `_`,
which no answer shows, no schema allows a caller to send, and a modify keeps; `service.public`). Delivery asks for that caller's notification
access, so a subscriber scoped to one SubNetwork is never told about another's instances, whatever its subscription's scope says.

Heartbeats. `triggerHeartbeatNtf` sends at once. Periodic heartbeats are a worker's job, never a request process's (`scripts/check_statelessness.py`):
`heartbeat_task(mns, session_factory)` is the `smo_shared.worker.Task` a module adds to its `app/tasks.py`; it sends every heartbeat that is due (its
period elapsed since the last one, kept in the hidden `LAST_HEARTBEAT`) and is safe to run again after a crash.
"""

import datetime
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import msac, outbox
from .. import scope as claims
from ..webhook import is_safe_webhook_destination
from .checks import Caller
from .dn import depth_of, parent_of, parse_dn, path_of
from .errors import MnsError
from .lifecycle import IMoiLifecycleHandler, Moi, MoiContext
from .scope import Scope, matches, parse_filter

if TYPE_CHECKING:          # the service builds the manager; the manager only reads what the service gives it
    from .service import ProvMnS

SUBSCRIPTION = "NtfSubscriptionControl"
HEARTBEAT = "HeartbeatControl"
CLASSES = (SUBSCRIPTION, HEARTBEAT)
SUBSCRIBER = "_subscriber"          # hidden: who created the subscription
LAST_HEARTBEAT = "_lastHeartbeat"   # hidden: when the last periodic heartbeat was sent (ISO 8601)
WRITE_ONLY = {HEARTBEAT: ("triggerHeartbeatNtf",)}
_MAX_NOTIFICATION_ID = 2**53 - 1


def owner_of(caller: Caller) -> dict:
    """The hidden record of who created a subscription."""
    return {"invoker": caller.invoker, "role": caller.role, "actingUser": caller.acting_user, "scope": claims.encode(caller.scope)}


def caller_of(owner: Any) -> Caller:
    """The caller a hidden owner record stands for; a record that is not one is a caller that may read nothing scoped (an empty claim)."""
    if not isinstance(owner, dict):
        return Caller(invoker=None, role=None, scope=claims.Scope(frozenset(), frozenset()))
    return Caller(invoker=owner.get("invoker"), role=owner.get("role"), acting_user=owner.get("actingUser"), scope=claims.decode(owner.get("scope")))


def in_scope(subscription_dn: str, scope: Any, dn: str) -> bool:
    """Whether the instance `dn` is in the scope of the subscription at `subscription_dn`: relative to the subscription's parent, a scopeType and
    scopeLevel as getMOIAttributes reads them, or a dataNodeSelector (`msac.selects`). No scope is BASE_ALL. A scope that is neither is nothing."""
    base = parent_of(subscription_dn)
    if base is None:
        return False
    if scope is None:
        scope = {"scopeType": "BASE_ALL"}
    if not isinstance(scope, dict):
        return False
    if "dataNodeSelector" in scope:
        selector = scope["dataNodeSelector"]
        return isinstance(selector, str) and msac.selector_supported(selector) and msac.selects(selector, "/" + path_of(dn))
    if not (dn == base or dn.startswith(base + ",")):
        return False
    level = scope.get("scopeLevel")
    scope_type = scope.get("scopeType", "BASE_ONLY")
    if not isinstance(level, int) or isinstance(level, bool):
        if scope_type in ("BASE_NTH_LEVEL", "BASE_SUBTREE"):
            return False                                    # these need a level, as a read's scope does
        level = 0                                           # BASE_ONLY and BASE_ALL ignore it
    parsed = Scope(scope_type, level)
    lowest, highest = parsed.depth_range(depth_of(base))
    return depth_of(dn) >= lowest and (highest is None or depth_of(dn) <= highest)


def wants(subscription: dict, notification_type: str, dn: str, body: dict) -> bool:
    """Whether the subscription (its public attributes and DN) asks for this notification: its type, its scope, its filter."""
    attributes = subscription["attributes"]
    types = attributes.get("notificationTypes")
    if types and notification_type not in types:
        return False
    if not in_scope(subscription["dn"], attributes.get("scope"), dn):
        return False
    condition = attributes.get("notificationFilter")
    return condition is None or matches(parse_filter(condition), body)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _check_subscription(moi: Moi) -> None:
    """422 VETOED unless the recipient is a destination the SSRF guard accepts and the filter parses."""
    if not is_safe_webhook_destination(moi.attributes.get("notificationRecipientAddress")):
        raise MnsError(422, "VETOED", "notificationRecipientAddress must be an http or https URL the SMO may call (not loopback, link-local or metadata)",
                       bad_attributes=["notificationRecipientAddress"], bad_objects=[moi.dn])
    condition = moi.attributes.get("notificationFilter")
    if condition is not None:
        try:
            parse_filter(condition)
        except MnsError:
            raise MnsError(422, "VETOED", "notificationFilter is not a condition this producer evaluates", bad_attributes=["notificationFilter"],
                           bad_objects=[moi.dn]) from None


class _SubscriptionHooks(IMoiLifecycleHandler):
    """NtfSubscriptionControl's hooks: the recipient and the filter are checked on every write."""

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Checks the new subscription."""
        _check_subscription(moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Checks the changed subscription."""
        _check_subscription(new)


class SubscriptionManager:
    """The subscriptions of one producer (see the module description); built by `ProvMnS`, which calls `stored`, `hidden_on_create` and `deliver`."""

    def __init__(self, mns: "ProvMnS"):
        """Serves the two classes on `mns` with their hooks."""
        self.mns = mns
        mns.serve(SUBSCRIPTION, _SubscriptionHooks())
        mns.serve(HEARTBEAT, _HeartbeatHooks(self))

    @staticmethod
    def stored(class_name: str, attributes: dict) -> dict:
        """The attributes as kept: a write-only attribute (`triggerHeartbeatNtf`) is not."""
        dropped = WRITE_ONLY.get(class_name, ())
        return {name: value for name, value in attributes.items() if name not in dropped}

    @staticmethod
    def hidden_on_create(class_name: str, caller: Caller) -> dict:
        """The hidden attributes a new instance starts with: a subscription's subscriber."""
        return {SUBSCRIBER: owner_of(caller)} if class_name == SUBSCRIPTION else {}

    def _subscriptions(self, db: Session) -> list:
        model = self.mns.model
        return list(db.scalars(select(model).where(model.object_class == SUBSCRIPTION).order_by(model.dn)))

    def deliver(self, db: Session, notification_type: str, dn: str, body: dict) -> int:
        """Adds an outbox row for every subscription that wants this notification and whose subscriber may read `dn`; returns how many."""
        from .service import public    # the service imports this module; the helper lives with the store logic there
        if parse_dn(dn)[-1].class_name in CLASSES:
            return 0                       # a subscription is nobody else's business (see the module description)
        count = 0
        for row in self._subscriptions(db):
            subscription = {"dn": row.dn, "attributes": public(row.attributes)}
            if not wants(subscription, notification_type, dn, body):
                continue
            if not self.mns.checks.visible(caller_of((row.attributes or {}).get(SUBSCRIBER)), dn):
                continue
            if outbox.enqueue(db, subscription["attributes"].get("notificationRecipientAddress"), body, module=self.mns.checks.module) is not None:
                count += 1
        return count

    def heartbeat(self, db: Session, heartbeat_dn: str, period: int) -> bool:
        """Adds one notifyHeartbeat to the outbox for the subscription that contains `heartbeat_dn`; False when there is no such subscription."""
        subscription_dn = parent_of(heartbeat_dn)
        row = db.get(self.mns.model, subscription_dn) if subscription_dn else None
        if row is None or row.object_class != SUBSCRIPTION:
            return False
        body = {"href": f"{self.mns.api_root}/ProvMnS/{_version()}/{path_of(heartbeat_dn)}", "notificationId": secrets.randbelow(_MAX_NOTIFICATION_ID) + 1,
                "notificationType": "notifyHeartbeat", "eventTime": _now().isoformat(), "systemDN": self.mns.system_dn, "heartbeatNtfPeriod": period}
        return outbox.enqueue(db, (row.attributes or {}).get("notificationRecipientAddress"), body, module=self.mns.checks.module) is not None

    def send_due_heartbeats(self, db: Session, now: datetime.datetime | None = None) -> int:
        """Sends every periodic heartbeat that is due (its period elapsed since the last one, or never sent), records when, commits; returns how many."""
        now = now or _now()
        model = self.mns.model
        sent = 0
        for row in list(db.scalars(select(model).where(model.object_class == HEARTBEAT).order_by(model.dn))):
            attributes = dict(row.attributes or {})
            period = attributes.get("heartbeatNtfPeriod") or 0
            if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
                continue
            last = attributes.get(LAST_HEARTBEAT)
            if last is not None and now < datetime.datetime.fromisoformat(last) + datetime.timedelta(seconds=period):
                continue
            if self.heartbeat(db, row.dn, period):
                row.attributes = {**attributes, LAST_HEARTBEAT: now.isoformat()}
                sent += 1
        db.commit()
        return sent


class _HeartbeatHooks(IMoiLifecycleHandler):
    """HeartbeatControl's hooks: `triggerHeartbeatNtf` true sends one notifyHeartbeat now, in the change's transaction (a `before_*` hook, the one
    that sees the attributes as sent: the write-only flag is never stored)."""

    def __init__(self, manager: "SubscriptionManager"):
        """Keeps the manager that sends the heartbeat."""
        self.manager = manager

    def _trigger(self, ctx: MoiContext, moi: Moi) -> None:
        if moi.attributes.get("triggerHeartbeatNtf") is True:
            self.manager.heartbeat(ctx.session, moi.dn, moi.attributes.get("heartbeatNtfPeriod") or 0)

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """A heartbeat now when asked."""
        self._trigger(ctx, moi)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """A heartbeat now when asked."""
        self._trigger(ctx, new)


def _version() -> str:
    from .service import PROVMNS_VERSION
    return PROVMNS_VERSION


def heartbeat_task(mns: "ProvMnS", session_factory: Callable[[], Session], name: str = "mns-heartbeats", interval_seconds: float = 5.0):
    """The worker task (`smo_shared.worker.Task`) that sends `mns`'s periodic heartbeats; a module adds it to its `app/tasks.py`."""
    from ..worker import Task

    def run() -> None:
        if mns.subscriptions is None:
            return
        with session_factory() as db:
            mns.subscriptions.send_due_heartbeats(db)

    return Task(name, interval_seconds, run)


