"""The event-bus relay to Kafka (ADR 0006 slice 2, plan task S1.10, D9): a shared worker task that follows `smo_event` after commit and produces each
event to a Kafka topic, in `seq` order, keyed by its entity.

Why a relay and not an `EventPublisher` (ADR 0006 section 3.1, Q1): a Kafka produce cannot join the caller's Postgres transaction, so a publisher that
produced directly would send an event for a change that later rolled back, or lose one between the commit and the produce. `TablePublisher` stays the
only in-transaction publisher; `smo_event` is the buffer, and this relay its second reader.

One pass (`relay_once`): read the events after the relay's position (`smo_event_relay.last_seq`) that are at least `SMO_KAFKA_RELAY_SETTLE_SECONDS` old
(a transaction that took a lower `seq` but commits later has had time to commit: the skip hazard of docs/EVENTS.md), produce them, flush, and move the
position over the events the broker acknowledged without a gap. An event whose message is larger than `SMO_KAFKA_MAX_MESSAGE_BYTES` is skipped (logged,
counted) and the position moves past it, so it never blocks the head of the line. A crash between the flush and the position update sends the batch
again: consumers de-duplicate on the envelope's `id`. Delivery is at least once, in order per key.

Switched on by `SMO_KAFKA_RELAY=true` on a worker (compose: the DME worker, profile `kafka`), with `SMO_EVENT_BUS=table` and the `kafka` extra in the
image (`WITH_KAFKA=1`). Without the client library the worker refuses to start and says which build argument is missing: losing events silently would be
worse. Nothing here runs in a request; Kafka is in no module's `/ready` (ADR 0006 section 3.8).

Metrics: `smo_kafka_produce_total{outcome}` (ok, error, oversize), `smo_kafka_produce_duration_seconds` (one flush), `smo_kafka_up` (1 after a flush the
broker acknowledged, 0 after one it did not), and the query gauges `smo_event_relay_lag_events` / `smo_event_relay_lag_seconds` (how far the newest
event is ahead of the position, and the age of the oldest unsent one).
"""

import datetime
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import events
from .kafka_settings import KafkaSettings, message_for, redacted

log = logging.getLogger("smo.kafka")

PRODUCED = Counter("smo_kafka_produce_total", "Events the Kafka relay produced, by outcome: ok (acknowledged), error (not acknowledged; sent again), "
                   "oversize (larger than SMO_KAFKA_MAX_MESSAGE_BYTES; skipped).", ["outcome"])
FLUSH = Histogram("smo_kafka_produce_duration_seconds", "Time the Kafka relay's producer took to flush one batch (produce to last acknowledgement).")
UP = Gauge("smo_kafka_up", "1 when the Kafka relay's last flush was acknowledged by the broker, 0 when it was not.")
PASS_SECONDS = 15.0                       # a relay pass drains batches for at most this long, well inside the worker's one-minute heartbeat


class KafkaSink(Protocol):
    """What the relay produces through: the real client (`ConfluentSink`) or a test's fake. `produce` queues one message and later calls
    `on_delivery(error)` with None for an acknowledged message; `flush(timeout)` waits for every queued one and returns how many are still undelivered."""

    def produce(self, topic: str, key: bytes, value: bytes, headers: list[tuple[str, bytes]], on_delivery: Callable[[object | None], None]) -> None: ...

    def flush(self, timeout: float) -> int: ...


class RelayStartError(RuntimeError):
    """The relay was switched on but cannot run: a setting is missing, or the image has no Kafka client (build with WITH_KAFKA=1)."""


class ConfluentSink:
    """`KafkaSink` over confluent-kafka's `Producer` (imported here only: the `kafka` extra). The producer's own log lines go to the `smo.kafka` logger,
    and its configuration is logged by key with the secrets masked."""

    def __init__(self, settings: KafkaSettings, producer_factory=None):
        """Builds the producer from `settings` with `producer_factory` (default confluent-kafka's `Producer`); RelayStartError when the client is absent."""
        if producer_factory is None:
            try:
                from confluent_kafka import Producer
            except ImportError as exc:
                raise RelayStartError("SMO_KAFKA_RELAY is on but this image has no Kafka client: build it with WITH_KAFKA=1 "
                                      "(docker compose: SMO_WITH_KAFKA=1)") from exc
            producer_factory = Producer
        config = settings.producer_config()
        log.info("Kafka relay producer", extra={"config": redacted(config)})
        self._producer = producer_factory({**config, "logger": log})

    def produce(self, topic, key, value, headers, on_delivery) -> None:
        """Queues one message; `on_delivery` gets the client's error or None (confluent-kafka calls back with (error, message))."""
        self._producer.produce(topic, value=value, key=key, headers=headers, on_delivery=lambda error, _message: on_delivery(error))
        self._producer.poll(0)

    def flush(self, timeout: float) -> int:
        """Waits up to `timeout` seconds for every queued message; returns how many are still queued."""
        return self._producer.flush(timeout)


def relay_name(settings: KafkaSettings) -> str:
    """The relay's row in `smo_event_relay`: `kafka:<topic>`, so a new topic starts from the beginning of the feed's window."""
    return f"kafka:{settings.topic}"


def position(db: Session, name: str) -> int:
    """The `last_seq` of the relay `name`, 0 when it has not run yet."""
    return db.scalar(select(events.SmoEventRelay.last_seq).where(events.SmoEventRelay.name == name)) or 0


def _advance(db: Session, name: str, seq: int, now: datetime.datetime) -> None:
    """Moves the relay `name`'s position up to `seq` (making its row the first time); never backwards. The caller commits."""
    row = db.get(events.SmoEventRelay, name)
    if row is None:
        db.add(events.SmoEventRelay(name=name, last_seq=seq, updated_at=now))
    elif seq > row.last_seq:
        row.last_seq, row.updated_at = seq, now


def relay_once(db: Session, sink: KafkaSink, settings: KafkaSettings, now: datetime.datetime | None = None) -> dict:
    """One batch: produce the settled events after the relay's position, flush, and move the position over the acknowledged ones (see the module
    docstring). Commits the position itself; returns {"sent", "skipped", "failed", "position"}."""
    now = now or datetime.datetime.now(datetime.UTC)
    name = relay_name(settings)
    after = position(db, name)
    settled = now - datetime.timedelta(seconds=settings.settle_seconds)
    rows = list(db.scalars(select(events.SmoEvent).where(events.SmoEvent.seq > after, events.SmoEvent.time <= settled)
                           .order_by(events.SmoEvent.seq).limit(settings.batch)))
    outcomes: dict[int, str] = {}
    started = time.monotonic()
    for row in rows:
        key, value, headers = message_for(events.view(row))
        if len(value) > settings.max_message_bytes:
            log.error("Kafka relay: event larger than SMO_KAFKA_MAX_MESSAGE_BYTES skipped", extra={"seq": row.seq, "type": row.type, "bytes": len(value)})
            outcomes[row.seq] = "oversize"
            continue
        outcomes[row.seq] = "pending"

        def delivered(error, seq=row.seq) -> None:
            outcomes[seq] = "ok" if error is None else "error"
        try:
            sink.produce(settings.topic, key, value, headers, delivered)
        except Exception as exc:                          # noqa: BLE001 (a full local queue or a closed producer: stop here, send again next pass)
            log.warning("Kafka relay: produce refused: %s", type(exc).__name__)
            outcomes[row.seq] = "error"
            break
    if any(state == "pending" for state in outcomes.values()):
        sink.flush(settings.delivery_timeout_ms / 1000)
        FLUSH.observe(time.monotonic() - started)
    moved_to, sent, skipped, failed = after, 0, 0, 0
    for seq in sorted(outcomes):
        state = outcomes[seq]
        if state in ("pending", "error"):
            failed = sum(1 for s in outcomes.values() if s in ("pending", "error"))
            break
        moved_to = seq
        sent += state == "ok"
        skipped += state == "oversize"
    PRODUCED.labels("ok").inc(sent)
    PRODUCED.labels("oversize").inc(skipped)
    PRODUCED.labels("error").inc(failed)
    if sent or failed:
        UP.set(0 if failed else 1)
    if failed:
        log.warning("Kafka relay: the broker did not acknowledge every event; they are sent again on the next pass", extra={"failed": failed})
    if moved_to > after:
        _advance(db, name, moved_to, now)
    db.commit()
    return {"sent": sent, "skipped": skipped, "failed": failed, "position": moved_to, "read": len(rows)}


def lag(db: Session, settings: KafkaSettings, now: datetime.datetime | None = None) -> tuple[int, float]:
    """(events after the relay's position, age in seconds of the oldest of them, 0 when none): what the two lag gauges show."""
    after = position(db, relay_name(settings))
    behind = db.scalar(select(func.count()).select_from(events.SmoEvent).where(events.SmoEvent.seq > after)) or 0
    oldest = db.scalar(select(func.min(events.SmoEvent.time)).where(events.SmoEvent.seq > after))
    if oldest is None:
        return behind, 0.0
    oldest = oldest if oldest.tzinfo else oldest.replace(tzinfo=datetime.UTC)
    return behind, max(0.0, ((now or datetime.datetime.now(datetime.UTC)) - oldest).total_seconds())


def register_lag_metrics(settings: KafkaSettings, session_factory=None) -> None:
    """Registers `smo_event_relay_lag_events` and `smo_event_relay_lag_seconds` (by topic), read when Prometheus scrapes the worker (cached, as every
    query gauge, smo_shared/metrics.py)."""
    from .metrics import register_query_gauge
    topic = (settings.topic,)
    register_query_gauge("smo_event_relay_lag_events", "Events in smo_event the Kafka relay has not sent yet, by topic.", ["topic"],
                         lambda db: [(topic, lag(db, settings)[0])], session_factory)
    register_query_gauge("smo_event_relay_lag_seconds", "Age of the oldest event the Kafka relay has not sent yet (0 when none), by topic.", ["topic"],
                         lambda db: [(topic, lag(db, settings)[1])], session_factory)


def relay_on(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether `SMO_KAFKA_RELAY` switches the relay on."""
    return (environ.get("SMO_KAFKA_RELAY") or "").strip().lower() in ("1", "true", "yes", "on")


def kafka_relay_task(environ: Mapping[str, str] = os.environ, sink_factory=ConfluentSink):
    """The shared worker task `kafka-relay` (one runner database-wide), or None when `SMO_KAFKA_RELAY` is off. Raises RelayStartError, so the worker
    does not start, when the relay is on but `SMO_EVENT_BUS` is not `table`, a setting is wrong, or the image has no Kafka client."""
    from .worker import Task
    if not relay_on(environ):
        return None
    if (environ.get("SMO_EVENT_BUS") or "table").strip().lower() != "table":
        raise RelayStartError("SMO_KAFKA_RELAY needs SMO_EVENT_BUS=table: the relay follows the smo_event table")
    try:
        settings = KafkaSettings.from_env(environ)
    except ValueError as exc:
        raise RelayStartError(str(exc)) from exc
    sink = sink_factory(settings)
    from .db import SessionLocal
    register_lag_metrics(settings)

    def relay() -> None:
        deadline = time.monotonic() + PASS_SECONDS
        while True:
            with SessionLocal() as db:
                done = relay_once(db, sink, settings)
            if done["read"] < settings.batch or done["failed"] or time.monotonic() > deadline:
                return

    try:
        interval = max(0.1, float(environ.get("SMO_KAFKA_RELAY_INTERVAL_SECONDS") or 1.0))
    except ValueError:
        raise RelayStartError("SMO_KAFKA_RELAY_INTERVAL_SECONDS must be a number of seconds") from None
    return Task("kafka-relay", interval, relay, shared=True)
