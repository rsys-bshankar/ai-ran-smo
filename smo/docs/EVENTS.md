# The SMO event bus

What happened in a module, published as an event other parts of the platform can follow (`PR-MSG-3`). The code is `shared/smo_shared/events.py`; the
feed is served by DME (`GET /dme/smo-events`); the operator console follows it to push its counts sooner (`GUI-1.5`, `gui-bff/app/events.py`).

## The envelope

A CloudEvents 1.0 event in its JSON form, plus three extension attributes. Schema: [`schemas/event-envelope.schema.json`](schemas/event-envelope.schema.json)
(with two examples).

| Attribute | Meaning |
|---|---|
| `specversion` | `1.0` |
| `id` | A UUID, unique per event |
| `source` | The module that published it: `/smo/<module>` |
| `type` | `org.oran.smo.<what>.<change>` |
| `subject` | The entity the event is about (its id), when it has one |
| `time` | When it was published, UTC |
| `datacontenttype` | `application/json` |
| `data` | What the type carries (below) |
| `smoseq` | Its position in the feed; a reader passes the last one it read as `after` |
| `smoregion`, `smotenant` | Where the managed element the event is about is, when it is one's: a scoped reader is given only the events inside its claim |
| `correlationid` | The `X-Correlation-ID` of the request or worker task (`task:<name>:<uuid>`) that published the event, when there was one (the name smo-teiv gives the same attribute; revision 0059, call flow 14c) |

## Types

| Type | Published by | `subject` | `data` |
|---|---|---|---|
| `org.oran.smo.alarm.raised` | RAN NF OAM, when an alarm row is inserted (unless it arrives already cleared) | the alarm id | `alarmId`, `managedElementRef`, `managedFunctionRef`, `severity`, `probableCause` |
| `org.oran.smo.alarm.cleared` | RAN NF OAM, when an alarm's severity becomes `cleared` (the clear route, a VES clear, any ORM change) | the alarm id | as above |
| `org.oran.smo.rapp-instance.state-changed` | rApp Management, at every transition of an instance's state machine | the instance id | `machine`, `from`, `event`, `to` |
| `org.oran.smo.rapp-instance.credentials-rotated` | rApp Management's worker, when it rotated an instance's secret on schedule (`RAPP_MGMT_SECRET_ROTATION_DAYS`, `SEC-15.19`) and the rApp took the new one | the instance id | `instanceId`, `oauthClientId`, `olderThanDays`; never a secret |
| `org.oran.smo.rapp-instance.credentials-rotation-failed` | rApp Management's worker, when a scheduled rotation did not complete | the instance id | as above, plus `reason` (`WORKLOAD_UNREACHABLE`: nothing changed; `SME_DID_NOT_ISSUE_A_SECRET`: nothing changed; `WORKLOAD_DID_NOT_TAKE_THE_SECRET`: the old secret is already invalid) and `oldSecretStillValid` |
| `org.oran.smo.o-cloud-resource.created` / `.deleted` | FOCOM, when an inventory resource is provisioned or deprovisioned (call flow 16), in the same commit as the inventory notifications | the resource id | `resourceTypeId`, `resourcePoolId` |
| `org.oran.smo.nf-deployment.state-changed` | NFO, at every transition of a deployment's state machine (call flow 15e: a workload that breaks is `to: ABNORMAL`), and when a deployment is removed (`event: DELETE_COMPLETE`, `to: DELETED`) | the deployment id (an rApp instance's `workloadRef`) | as above |
| `org.oran.smo.model-lifecycle.state-changed` | AIMgF, at every transition of a model's lifecycle and of its serving runtime (call flow 26: a decision, a run starting or ending, deploy, terminate, retire; `machine` is `ModelLifecycleState` or `RuntimeLifecycleState`) | the model id | as above |
| `org.oran.smo.data-job.state-changed` | DME (call flow 11), when a data job's status changes without its owner asking: `ACTIVE` to `NO_PRODUCER` when the last producer of its type deregisters, back to `ACTIVE` when one joins, and to `DELETED` when its type is deleted or the job is (call flow 12a: by its owner, the console or a module, also one of a consumer's jobs deleted together) | the data job id | `dataJobId`, `dmeTypeId`, `consumerId`, `createdBy`, `from`, `to`, `reason` |
| `org.oran.smo.write-config-job.state-changed` | RAN NF OAM, at every transition of a CM job (held for approval, approved, scheduled, started, done, halted, rejected) | the job id | as above |

The state-change events come from one hook (`StateMachine.fire` in `smo_shared/statemachine.py` calls `events.on_transition`): a machine whose `fire` is given
the entity in its context (`instance=`, `job=`) publishes `<table>.state-changed` about it, with no code in the module. A machine that is not given its entity
publishes nothing; giving it one is the whole change to add its events.

## How it is delivered

- **Published with the change.** `publish(session_or_connection, ...)` writes a row of the shared table `smo_event` (revision 0042) in the caller's
  transaction: an event exists exactly when its change was committed, and a rolled-back change has none.
- **Woken at commit.** On Postgres the same transaction runs `pg_notify('smo_events', <seq>)`, which Postgres delivers when it commits. A reader that waits
  (`wait_after`) `LISTEN`s on a connection of its own and is woken within a second. A connection through a transaction-pooling PgBouncer cannot listen (it
  accepts `LISTEN` and then gives the server connection to another client), so with `SMO_DB_POOLER=transaction` the reader polls every 0.5 s instead, unless
  `SMO_EVENT_LISTEN_URL` names a direct connection to listen on. A connection that refuses `LISTEN` falls back to polling the same way.
- **Read by position.** `GET /dme/smo-events?after=<seq>&limit=<n>&wait=<s>&types=<a,b>` answers `{"items": [envelopes, oldest first], "next": <seq>}`.
  Without `after` it answers no events and the newest position, where a reader that wants only what comes next starts. `wait` (at most 25 s, below the 30 s
  a module allows a call and R1 Termination's 60 s) holds the answer until an event is committed. `limit` is at most 500. A caller with a scope claim (a console user's,
  `GUI-5`) gets only the events inside it; `next` still moves past the others.
- **Internal only.** The feed names every module's entities, so R1 Termination refuses it to an rApp (`roles.py`, `INTERNAL_ONLY`): an rApp gets data
  through DME's data jobs.
- **A recent history, not an archive.** Events are kept `SMO_EVENT_RETENTION_HOURS` (default 24) and then deleted by the shared worker task `event-purge`
  (every `SMO_EVENT_PURGE_SECONDS`, default 300). A reader that was away longer than that starts again from the newest position and re-reads what it needs
  from the modules.
- **At least once, in order.** A reader that keeps its `next` sees every event once, in commit order of their sequence numbers. Two transactions that commit
  out of the order in which they took their numbers can make a reader that read in between skip the earlier one; the console uses the feed only as a cue
  to recompute, so it does not mind. A consumer that needs every event must re-read a little behind its position.

`SMO_EVENT_BUS` chooses the publisher: `table` (default), `memory` (an in-process list, for tests) or `off`. On a database without the table (a module's
SQLite unit tests) the table publisher keeps the event in memory, so a test of a change is not a test of the bus.

## Not built

- **Kafka** (`MSG-3.4`, `OPEN_ITEMS.md`): the publisher is an interface (`EventPublisher`) so a Kafka one can sit beside the table one; nothing needs it yet. Designed and decided (October 2026), not built: a relay that follows `smo_event`, not a Kafka `EventPublisher` ([adr/0006-msg-3-4-kafka-event-sink.md](adr/0006-msg-3-4-kafka-event-sink.md)).
- **Subscriptions with callbacks.** A reader follows the feed; nothing is pushed to a URL. A module that wants to tell an outside party uses the outbox
  (`docs/NOTIFICATIONS.md`).
