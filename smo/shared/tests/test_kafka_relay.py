"""Tests for smo_shared.kafka_relay (ADR 0006 slice 2) against a fake producer and an in-memory SQLite feed: order and keys, the position moving only
over acknowledged events, a crash re-sending the same ids, the settle delay, an oversize event skipped, the lag, the start-up refusals, and the
`event-purge` hold. The real broker is the compose job "Kafka profile round trip" (scripts/kafka_roundtrip.py)."""

import datetime
import json
import logging
import uuid

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from smo_shared import events, kafka_relay
from smo_shared.db import Base
from smo_shared.kafka_settings import KafkaSettings

NOW = datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.UTC)


class FakeSink:
    """A KafkaSink that records what was produced and, on flush, acknowledges every message except those whose index is in `fail` (and none at all
    when `deliver` is false: a timed-out flush)."""

    def __init__(self, fail=(), deliver=True, refuse_after=None):
        self.fail, self.deliver, self.refuse_after = set(fail), deliver, refuse_after
        self.sent: list[tuple[str, bytes, dict, dict]] = []
        self._pending = []

    def produce(self, topic, key, value, headers, on_delivery):
        if self.refuse_after is not None and len(self.sent) >= self.refuse_after:
            raise BufferError("local queue full")
        self.sent.append((topic, key, json.loads(value), {k: v.decode() for k, v in headers}))
        self._pending.append((len(self.sent) - 1, on_delivery))

    def flush(self, timeout):
        if self.deliver:
            for index, callback in self._pending:
                callback("broker error" if index in self.fail else None)
            self._pending = []
        return len(self._pending)


def _db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[events.SmoEvent.__table__, events.SmoEventRelay.__table__])
    return Session(engine)


def _events(db: Session, n: int, age_seconds: float = 60, subject=lambda i: f"alarm-{i % 2}", size: int = 0, now=NOW) -> None:
    for i in range(n):
        db.execute(insert(events.SmoEvent).values(event_id=uuid.uuid4(), type="org.oran.smo.alarm.raised", source="/smo/ran-nf-oam",
                                                  subject=subject(i), time=now - datetime.timedelta(seconds=age_seconds), data={"i": i, "pad": "x" * size}))
    db.commit()


SETTINGS = KafkaSettings(bootstrap_servers="kafka:9092", security_protocol="PLAINTEXT", batch=500)


def test_events_go_out_in_order_keyed_by_entity_and_the_position_follows():
    """Five settled events are produced in seq order with key = subject and smoseq headers; the position moves to the last; a second pass sends nothing."""
    db, sink = _db(), FakeSink()
    _events(db, 5)
    assert kafka_relay.relay_once(db, sink, SETTINGS, NOW) == {"sent": 5, "skipped": 0, "failed": 0, "position": 5, "read": 5}
    assert [s[3]["smoseq"] for s in sink.sent] == ["1", "2", "3", "4", "5"] and {s[0] for s in sink.sent} == {"smo.events.v1"}
    assert [s[1] for s in sink.sent] == [b"alarm-0", b"alarm-1", b"alarm-0", b"alarm-1", b"alarm-0"]
    assert sink.sent[0][2]["id"] == sink.sent[0][3]["ce_id"] and sink.sent[0][2]["data"]["i"] == 0
    assert kafka_relay.position(db, "kafka:smo.events.v1") == 5
    assert kafka_relay.relay_once(db, sink, SETTINGS, NOW)["sent"] == 0 and len(sink.sent) == 5


def test_the_position_stops_before_the_first_unacknowledged_event_and_it_is_sent_again():
    """The third of five is not acknowledged: the position moves to 2 only; the next pass sends 3, 4 and 5 again with the same ids (duplicates, no loss)."""
    db = _db()
    _events(db, 5)
    first = FakeSink(fail={2})
    assert kafka_relay.relay_once(db, first, SETTINGS, NOW)["position"] == 2
    again = FakeSink()
    assert kafka_relay.relay_once(db, again, SETTINGS, NOW)["position"] == 5
    assert [s[2]["id"] for s in again.sent] == [s[2]["id"] for s in first.sent[2:]]


def test_a_flush_that_times_out_or_a_refused_produce_moves_nothing_past_it():
    """No acknowledgement at all leaves the position where it was; a produce refused after two events moves it over those two only."""
    db = _db()
    _events(db, 4)
    assert kafka_relay.relay_once(db, FakeSink(deliver=False), SETTINGS, NOW) == {"sent": 0, "skipped": 0, "failed": 4, "position": 0, "read": 4}
    assert kafka_relay.relay_once(db, FakeSink(refuse_after=2), SETTINGS, NOW)["position"] == 2


def test_a_crash_after_the_flush_sends_the_same_ids_again():
    """Acknowledged, but the position update is lost (rolled back, as a crash would): the next pass sends the same envelope ids."""
    db = _db()
    _events(db, 3)
    sink = FakeSink()
    original_commit = db.commit
    db.commit = db.rollback                                         # the crash: the position never lands
    kafka_relay.relay_once(db, sink, SETTINGS, NOW)
    db.commit = original_commit
    again = FakeSink()
    kafka_relay.relay_once(db, again, SETTINGS, NOW)
    assert [s[2]["id"] for s in again.sent] == [s[2]["id"] for s in sink.sent] and kafka_relay.position(db, "kafka:smo.events.v1") == 3


def test_an_event_younger_than_the_settle_delay_waits():
    """An event 1 s old is not sent with the 3 s settle delay; it is once it is old enough."""
    db, sink = _db(), FakeSink()
    _events(db, 1, age_seconds=1)
    assert kafka_relay.relay_once(db, sink, SETTINGS, NOW)["read"] == 0
    assert kafka_relay.relay_once(db, sink, SETTINGS, NOW + datetime.timedelta(seconds=3))["sent"] == 1


def test_an_oversize_event_is_skipped_counted_and_passed(caplog):
    """An event larger than SMO_KAFKA_MAX_MESSAGE_BYTES is not produced, is logged and counted, and the position moves past it to the next."""
    db, sink = _db(), FakeSink()
    _events(db, 1, size=2000)
    _events(db, 1)
    small = KafkaSettings(bootstrap_servers="k:1", security_protocol="PLAINTEXT", max_message_bytes=1024)
    before = kafka_relay.PRODUCED.labels("oversize")._value.get()
    with caplog.at_level(logging.ERROR, logger="smo.kafka"):
        assert kafka_relay.relay_once(db, sink, small, NOW) == {"sent": 1, "skipped": 1, "failed": 0, "position": 2, "read": 2}
    assert kafka_relay.PRODUCED.labels("oversize")._value.get() == before + 1 and "larger than" in caplog.text and len(sink.sent) == 1


def test_the_lag_counts_unsent_events_and_the_age_of_the_oldest():
    """Three unsent events a minute old: lag 3 and 60 s; after a pass, 0 and 0."""
    db = _db()
    _events(db, 3)
    assert kafka_relay.lag(db, SETTINGS, NOW) == (3, 60.0)
    kafka_relay.relay_once(db, FakeSink(), SETTINGS, NOW)
    assert kafka_relay.lag(db, SETTINGS, NOW) == (0, 0.0)


def test_the_task_is_off_by_default_and_refuses_to_start_when_it_cannot_run():
    """Off: no task. On: refused without SMO_EVENT_BUS=table, without servers, and without the Kafka client (the message names WITH_KAFKA=1)."""
    assert kafka_relay.kafka_relay_task({}) is None
    with pytest.raises(kafka_relay.RelayStartError, match="SMO_EVENT_BUS=table"):
        kafka_relay.kafka_relay_task({"SMO_KAFKA_RELAY": "true", "SMO_EVENT_BUS": "memory"})
    with pytest.raises(kafka_relay.RelayStartError, match="SMO_KAFKA_BOOTSTRAP_SERVERS"):
        kafka_relay.kafka_relay_task({"SMO_KAFKA_RELAY": "true"})

    def no_client(settings):
        return kafka_relay.ConfluentSink(settings, producer_factory=None)
    import builtins
    real_import = builtins.__import__

    def without_confluent(name, *args, **kwargs):
        if name.startswith("confluent_kafka"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    builtins.__import__ = without_confluent
    try:
        with pytest.raises(kafka_relay.RelayStartError, match="WITH_KAFKA=1"):
            kafka_relay.kafka_relay_task({"SMO_KAFKA_RELAY": "true", "SMO_KAFKA_BOOTSTRAP_SERVERS": "k:1", "SMO_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"},
                                         sink_factory=no_client)
    finally:
        builtins.__import__ = real_import


def test_the_task_is_shared_and_drains_through_the_sink(monkeypatch):
    """On, with a fake sink: a shared task named kafka-relay whose run (on the real clock) sends what the feed holds."""
    db = _db()
    _events(db, 2, now=datetime.datetime.now(datetime.UTC))
    monkeypatch.setattr(kafka_relay, "register_lag_metrics", lambda settings, session_factory=None: None)
    import smo_shared.db as smo_db
    monkeypatch.setattr(smo_db, "SessionLocal", lambda: Session(db.get_bind()))
    sink = FakeSink()
    task = kafka_relay.kafka_relay_task({"SMO_KAFKA_RELAY": "on", "SMO_KAFKA_BOOTSTRAP_SERVERS": "k:1", "SMO_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
                                         "SMO_KAFKA_RELAY_SETTLE_SECONDS": "0"}, sink_factory=lambda settings: sink)
    assert task.name == "kafka-relay" and task.shared
    task.fn()
    assert [s[3]["smoseq"] for s in sink.sent] == ["1", "2"]


def test_the_confluent_sink_logs_its_configuration_without_the_password(caplog):
    """ConfluentSink builds the producer from the settings (the factory gets the logger too) and logs the configuration with the secrets masked."""
    made = {}

    class Producer:
        def __init__(self, config):
            made.update(config)

        def produce(self, topic, value, key, headers, on_delivery):
            on_delivery(None, None)

        def poll(self, timeout):
            return 0

        def flush(self, timeout):
            return 0
    settings = KafkaSettings(bootstrap_servers="k:1", security_protocol="SASL_SSL", sasl_username="u", sasl_password="sentinel-password")
    with caplog.at_level(logging.INFO, logger="smo.kafka"):
        sink = kafka_relay.ConfluentSink(settings, producer_factory=Producer)
    acks = []
    sink.produce("smo.events.v1", b"k", b"v", [], acks.append)
    assert acks == [None] and sink.flush(1) == 0 and made["sasl.password"] == "sentinel-password" and made["logger"] is kafka_relay.log
    assert "sentinel-password" not in caplog.text and all("sentinel-password" not in str(r.__dict__) for r in caplog.records)


def test_the_purge_holds_unsent_events_while_the_relay_is_on_up_to_the_cap(monkeypatch):
    """Past the 24 h retention: with the relay off all go; on, the ones after the relay's position stay until SMO_KAFKA_RELAY_MAX_HOLD_HOURS (72)."""
    db = _db()
    for hours, n in ((80, 1), (30, 2)):
        _events(db, n, age_seconds=hours * 3600)
    db.add(events.SmoEventRelay(name="kafka:smo.events.v1", last_seq=1, updated_at=NOW))
    db.commit()
    monkeypatch.setenv("SMO_KAFKA_RELAY", "true")
    assert events.purge(db, NOW) == 1                    # seq 1: sent; seqs 2 and 3: 30 h old, unsent, held
    db.commit()
    monkeypatch.setenv("SMO_KAFKA_RELAY_MAX_HOLD_HOURS", "24")
    assert events.purge(db, NOW) == 2                    # the cap reached: gone even though unsent
    db.commit()
    _events(db, 1, age_seconds=30 * 3600)
    monkeypatch.delenv("SMO_KAFKA_RELAY")
    assert events.purge(db, NOW) == 1                    # the relay off: the retention alone
