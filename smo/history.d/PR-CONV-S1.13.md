### PR-CONV-S1.13 — One subscription manager: TS 28.622 NtfSubscriptionControl and HeartbeatControl in every ProvMnS producer

**What was built.** `smo_shared/mns/subscriptions.py`. Every `ProvMnS` serves the two classes of the vendored TS 28.623 Subscription Control NRM unless
built with `subscriptions=False` (or given a class registry without that NRM): a `NtfSubscriptionControl` under a SubNetwork or ManagedElement
(`notificationRecipientAddress`, `notificationTypes`, `scope`, `notificationFilter`) and its `HeartbeatControl` (`heartbeatNtfPeriod`,
`triggerHeartbeatNtf`). The router's own notifications (notifyMOICreation, notifyMOIAttributeValueChanges, notifyMOIDeletion) are delivered to the
subscriptions that want them, in the change's transaction, through the transactional outbox (`smo_shared.outbox`, SSRF-guarded at enqueue and at
send, CLAUDE.md "caller-supplied destination"), so a delivery exists exactly when the change committed and is sent at least once. No module code:
Echo gets it by mounting the router, and only registers the two classes so R1 Termination routes them to it.

**Decisions.**
- *Scope* is relative to the subscription's parent, read as a getMOIAttributes scope (scopeType, scopeLevel), or a `dataNodeSelector` in
  `smo_shared.msac`'s subset; no scope is BASE_ALL. *The filter* is the router's filter grammar (`mns/scope.py`) on the notification body (`@` is
  the notification: `@.notificationType`, `@.attributeList.userLabel`); a filter that does not parse is refused at write, not ignored at delivery.
- *Notification access* (S1.14): the subscriber is kept with its subscription as a hidden attribute (a name starting with `_`: no class schema
  declares one, so a caller can never send it, no answer shows it, and a modify keeps it; `service.public` / `hidden`), and a notification about an
  instance is delivered only when that subscriber may read the instance (the router's own `RouterChecks.visible`). A subscriber scoped to one
  SubNetwork that subscribes to `/*` hears of that SubNetwork only.
- *Subscriptions are nobody's notifications*: creating or changing a subscription notifies no subscription, so a subscriber never learns another's
  recipient address or filter.
- *Heartbeats*: `triggerHeartbeatNtf` true sends one notifyHeartbeat at once and is not kept (write only; the service keeps what is stored apart from
  what the hooks see). Periodic heartbeats are sent by `send_due_heartbeats`, offered as the worker task `heartbeat_task(mns, session_factory)` for a
  module's `app/tasks.py` (a request process runs no scheduler, `scripts/check_statelessness.py`); the last send is a hidden attribute, so a task run
  twice sends once per period.

**Proved.** `tests_integration/test_subscriptions_and_access_rules.py`, through R1 Termination with Echo and SME (the M1 stack, now
`tests_integration/echo_stack.py`): a type-and-filter subscription receives only the matching creation, a BASE_ONLY one only its parent's change,
a trigger sends a heartbeat at once and the periodic one is sent when due and not again within its period; a loopback, metadata or `file:` recipient
is refused. `shared/tests/test_mns_subscriptions.py`: 33 tests (types, every scope form, the filter, rollback, hidden subscriber, heartbeats).

**Not done.** Echo has no worker process, so its periodic heartbeats run only where a worker calls `heartbeat_task` (compose and the chart give
Echo none: a cross-lane change, for when a module with subscribers needs it). With more than one unscoped producer serving the subscription
classes, R1 Termination's routing by class cannot tell which a subscription belongs to: it should route the two classes by the class of the
subscription's parent (r1-termination, outside this lane: reported, not changed). Alarm and performance notifications reach subscriptions when
their modules move onto the router (S3.15, S3.16).
