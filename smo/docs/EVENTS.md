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
| `org.oran.smo.alarm.raised` | RAN NF OAM, when an alarm row is inserted (unless it arrives already cleared) | the alarm id | `alarmId`, `managedElementRef`, `managedFunctionRef`, `severity`, `probableCause`, `ackState`, `ackUserId` |
| `org.oran.smo.alarm.cleared` | RAN NF OAM, when an alarm's severity becomes `cleared` (the clear route, a VES clear, any ORM change) | the alarm id | as above |
| `org.oran.smo.alarm.acknowledged` / `.unacknowledged` | RAN NF OAM, when an alarm's ack state changes (call flow 20b) | the alarm id | as above, with `ackState`, `ackUserId` |
| `org.oran.smo.vendor-capability.declared` / `.deleted` | RAN NF OAM, when a vendor's capability entry is declared or replaced (`PUT /vendor-capabilities/{vendor}`, `POST /vendor-onboarding`) or deleted (call flow 21b) | the vendor name | declared: the entry (`supportedServices`, `conformanceMode`, `supportedVendorModes`, `schemaRef`, `specSchemaRef`, `discoveryUri`, `updatedAt`); deleted: `vendorName`, `elementsLeftUnchecked` |
| `org.oran.smo.cm-schema.loaded` | RAN NF OAM, when a CM schema is loaded (`POST /cm-schemas`, `POST /vendor-onboarding`) | `name@revision` | `schemaName`, `revision`, `classCount` |
| `org.oran.smo.rapp-instance.state-changed` | rApp Management, at every transition of an instance's state machine | the instance id | `machine`, `from`, `event`, `to` |
| `org.oran.smo.rapp-instance.credentials-rotated` | rApp Management's worker, when it rotated an instance's secret on schedule (`RAPP_MGMT_SECRET_ROTATION_DAYS`, `SEC-15.19`) and the rApp took the new one | the instance id | `instanceId`, `oauthClientId`, `olderThanDays`; never a secret |
| `org.oran.smo.rapp-instance.credentials-rotation-failed` | rApp Management's worker, when a scheduled rotation did not complete | the instance id | as above, plus `reason` (`WORKLOAD_UNREACHABLE`: nothing changed; `SME_DID_NOT_ISSUE_A_SECRET`: nothing changed; `WORKLOAD_DID_NOT_TAKE_THE_SECRET`: the old secret is already invalid) and `oldSecretStillValid` |
| `org.oran.smo.software-job.state-changed` | RAN NF OAM, at every status change of a software management job (call flow 19), in the same commit | the job id | `managedElementRef`, `from`, `to`, `phase`, `reason`, `campaignId` |
| `org.oran.smo.o-cloud-resource.created` / `.deleted` | FOCOM, when an inventory resource is provisioned or deprovisioned (call flow 16), in the same commit as the inventory notifications | the resource id | `resourceTypeId`, `resourcePoolId` |
| `org.oran.smo.nf-deployment.state-changed` | NFO, at every transition of a deployment's state machine (call flow 15e: a workload that breaks is `to: ABNORMAL`), and when a deployment is removed (`event: DELETE_COMPLETE`, `to: DELETED`) | the deployment id (an rApp instance's `workloadRef`) | as above |
| `org.oran.smo.model-lifecycle.state-changed` | AIMgF, at every transition of a model's lifecycle and of its serving runtime (call flow 26: a decision, a run starting or ending, deploy, terminate, retire; `machine` is `ModelLifecycleState` or `RuntimeLifecycleState`) | the model id | as above |
| `org.oran.smo.data-job.state-changed` | DME (call flow 11), when a data job's status changes without its owner asking: `ACTIVE` to `NO_PRODUCER` when the last producer of its type deregisters, back to `ACTIVE` when one joins, and to `DELETED` when its type is deleted or the job is (call flow 12a: by its owner, the console or a module, also one of a consumer's jobs deleted together) | the data job id | `dataJobId`, `dmeTypeId`, `consumerId`, `createdBy`, `from`, `to`, `reason` |
| `org.oran.smo.write-config-job.state-changed` | RAN NF OAM, at every transition of a CM job (held for approval, approved, scheduled, started, done, halted, rejected) | the job id | as above |
| `org.oran.smo.provmns.notifyMOICreation` / `.notifyMOIAttributeValueChanges` / `.notifyMOIDeletion` | Any module that mounts the ProvMnS router (`smo_shared.mns`, none until S1.6), in the commit of a createMOI, of a createMOI or modifyMOIAttributes that changed attributes, and of a deleteMOI (one per deleted instance, each before its container); a write that changes nothing publishes nothing | the instance's DN | the TS 28.532 notification body (`TS28532_ProvMnS.yaml`): `href`, `notificationId`, `notificationType`, `eventTime`, `systemDN`, `sourceIndicator`; `attributeList` (creation, deletion; left out when empty) or `attributeListValueChanges` ([new values, old values] of the changed attributes) |

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

## Kafka (the relay, ADR 0006 slices 1–3)

A second sink beside the table: every event committed to `smo_event` also appears on a Kafka topic, in order per entity
([adr/0006-msg-3-4-kafka-event-sink.md](adr/0006-msg-3-4-kafka-event-sink.md), D9, plan task S1.10). Code: `shared/smo_shared/kafka_relay.py` (the relay) and
`kafka_settings.py` (its settings and the message mapping).

- **A relay, not a publisher.** A Kafka produce cannot be part of the caller's Postgres transaction, so the relay is not an `EventPublisher`: `TablePublisher`
  stays the only in-transaction publisher, and a shared worker task `kafka-relay` (one runner for the whole database) follows the table after commit. The table is
  the buffer: with Kafka down, writers are unaffected and events wait. Kafka is in no module's `/ready`.
- **Switching it on.** `SMO_KAFKA_RELAY=true` on a worker, with `SMO_EVENT_BUS=table` and an image built with the client (`WITH_KAFKA=1`, the `kafka` extra,
  `requirements/kafka.txt`: confluent-kafka). Compose: the DME worker, profile `kafka` (one KRaft broker, PLAINTEXT on the compose network; `DEMO_RUNBOOK.md`):
  `SMO_WITH_KAFKA=1 SMO_KAFKA_RELAY=true docker compose --profile kafka up -d --build`. A worker told to relay that cannot (no client, a setting missing)
  refuses to start and says why. The settings are in `docs/CONFIGURATION.md` (`SMO_KAFKA_*`).
- **Topic, key, message.** One topic, `smo.events.v1` (`SMO_KAFKA_EVENTS_TOPIC`). The key is the event's `subject` (else its `source`), so one entity's events
  keep `smoseq` order on one partition; different entities may interleave. The value is the envelope above, unchanged, as UTF-8 JSON. Headers in CloudEvents binary
  mode: `ce_id`, `ce_type`, `ce_source`, `ce_time`, `ce_specversion`, plus `smoseq` and `content-type: application/cloudevents+json`, so a consumer can filter
  without parsing. An event larger than `SMO_KAFKA_MAX_MESSAGE_BYTES` (1 MiB) is skipped, logged and counted, never retried.
- **Topic names per owner.** `smo.<owner>[.<stream>].v<N>`: the owner is the one part of the platform that writes the topic, so an ACL per prefix gives each
  owner's principal Write on its own topics and nothing else (PR-CONV-S1.15). `smo.events.v1` is the event bus's; `smo.ves.in.v1` will be the VES input
  (SB-7.6, ADR 0006 slice 4). The broker is the one S3.23 will share with smo-teiv; smo-teiv's ingestion topic (`smo.teiv.…`) is TE&IV's alone (D6): the relay
  refuses a topic under `smo.teiv.`, and no module but TE&IV is given it.
- **Position, duplicates, ordering.** The relay keeps its position in Postgres (`smo_event_relay`, revision 0066: `kafka:<topic>` → the highest `seq` the broker
  acknowledged without a gap), moved after the acknowledgement. A crash between the two, or a database restore (which rewinds the position), sends events
  again: **at least once**, and a consumer de-duplicates on the envelope `id`. The relay reads only events older than `SMO_KAFKA_RELAY_SETTLE_SECONDS` (3), so a
  transaction that took a lower `seq` but committed later is not skipped (the hazard of "At least once, in order" above); the cost is about 3 s of lag plus the
  worker's tick (5 s). The producer is idempotent with `acks=all`.
- **Kafka down longer than the retention.** While the relay is on, `event-purge` keeps events past `SMO_EVENT_RETENTION_HOURS` that the relay has not sent, up
  to `SMO_KAFKA_RELAY_MAX_HOLD_HOURS` (72) past their time (ADR 0006 Q2); after that they are lost to Kafka (not to the feed's own readers, which have the
  same 24 h window). The gauges `smo_event_relay_lag_events` and `smo_event_relay_lag_seconds` show the backlog; the alert on them is ADR 0006 slice 5.
- **Internal only.** The topic names every module's entities and Kafka ACLs are per topic, not per region or tenant, so it is platform-internal, like the
  feed: never grant an rApp or a tenant Read on it.
- **Security.** `SMO_KAFKA_SECURITY_PROTOCOL` is `SSL` unless set; `SASL_SSL` with SCRAM-SHA-512 is the production choice; `PLAINTEXT` starts with a warning
  (the lab broker); `PLAIN` is refused without TLS; the host name is always verified under TLS. The SASL password and the TLS key passphrase are secrets
  (`*_FILE`, `docs/SECRETS.md`), never logged: the producer configuration is logged with them masked.

## Not built

- **Kafka, the rest of ADR 0006:** slice 4 (SB-7.6: VES events read from `smo.ves.in.v1`) and slice 5 (the chart's `kafka:` values, the relay-lag alert and its
  runbook, an optional egress rule); a SASL_SSL listener in the compose profile before the `compose-kafka` job becomes required (Q5).
- **Subscriptions with callbacks.** A reader follows the feed; nothing is pushed to a URL. A module that wants to tell an outside party uses the outbox
  (`docs/NOTIFICATIONS.md`).
