#!/usr/bin/env python3
"""The Kafka profile's round trip (ADR 0006 slice 3, plan task S1.10): events published to the SMO's event bus (`smo_event`, through
`smo_shared.events.publish` in a committed transaction, as a module publishes) come out of the Kafka topic the relay writes, in order, keyed by their
entity, with the CloudEvents headers. The CI job "Kafka profile round trip" runs it in a container on the compose network:

    python scripts/kafka_roundtrip.py publish --subject S --first 0 --count 5     five events about S, data {"n": 0..4}, one transaction each
    python scripts/kafka_roundtrip.py expect --subject S --count 5 --timeout 90   read the topic from the start until S's events n = 0..count-1 have
                                                                                  all arrived; fail on a gap or a wrong order

Between a `publish` and an `expect` the job stops the broker, publishes more and starts it again: the relay keeps its position in Postgres and sends
what waited, still in order. A duplicate (the same envelope `id` twice, the relay's at-least-once) is counted, not a failure.

The database: SMO_DATABASE_URL (and SMO_DATABASE_PASSWORD_FILE), as for every service. The broker: SMO_KAFKA_BOOTSTRAP_SERVERS (default kafka:9092) and
SMO_KAFKA_EVENTS_TOPIC (default smo.events.v1), PLAINTEXT (the lab broker). Needs confluent-kafka (requirements/kafka.txt) for `expect`.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


def publish(subject: str, first: int, count: int) -> None:
    """`count` events of type org.oran.smo.roundtrip.published about `subject`, data {"n": first..}, each in its own committed transaction."""
    from smo_shared import events
    from smo_shared.db import session_scope
    for n in range(first, first + count):
        with session_scope() as db:
            events.publish(db, "roundtrip", "published", "/smo/kafka-roundtrip", subject, {"n": n})
    print(f"published {count} events about {subject} (n = {first}..{first + count - 1})", flush=True)


def expect(subject: str, count: int, timeout: float) -> int:
    """Reads the topic from the beginning until `subject`'s events n = 0..count-1 have arrived; checks key, headers, order and the envelope. Returns 0,
    or 1 with the reason."""
    from confluent_kafka import Consumer, KafkaException
    topic = os.environ.get("SMO_KAFKA_EVENTS_TOPIC", "smo.events.v1")
    consumer = Consumer({"bootstrap.servers": os.environ.get("SMO_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"), "group.id": f"smo-roundtrip-{uuid.uuid4()}",
                         "auto.offset.reset": "earliest", "enable.auto.commit": False})
    consumer.subscribe([topic])
    seen: list[int] = []
    ids: set[str] = set()
    duplicates, last_seq = 0, 0
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and len(seen) < count:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                if message.error().retriable():
                    continue
                raise KafkaException(message.error())
            if message.key() != subject.encode():
                continue
            envelope = json.loads(message.value())
            headers = {k: v.decode() for k, v in (message.headers() or [])}
            if envelope["id"] in ids:
                duplicates += 1
                continue
            ids.add(envelope["id"])
            problems = [what for what, ok in (
                ("ce_id header", headers.get("ce_id") == envelope["id"]), ("ce_type header", headers.get("ce_type") == envelope["type"]),
                ("smoseq header", headers.get("smoseq") == str(envelope.get("smoseq"))), ("content-type", "cloudevents" in headers.get("content-type", "")),
                ("type", envelope["type"] == "org.oran.smo.roundtrip.published"), ("subject", envelope.get("subject") == subject),
                ("seq order", envelope["smoseq"] > last_seq)) if not ok]
            if problems:
                print(f"FAIL: message {envelope['id']}: {', '.join(problems)} wrong", flush=True)
                return 1
            last_seq = envelope["smoseq"]
            seen.append(envelope["data"]["n"])
    finally:
        consumer.close()
    if seen != list(range(count)):
        print(f"FAIL: wanted n = 0..{count - 1} in order, read {seen}", flush=True)
        return 1
    print(f"read {count} events about {subject} in order from {topic} (seq up to {last_seq}; {duplicates} duplicates dropped by id)", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["publish", "expect"])
    parser.add_argument("--subject", required=True)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    if args.action == "publish":
        publish(args.subject, args.first, args.count)
        return 0
    return expect(args.subject, args.count, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
