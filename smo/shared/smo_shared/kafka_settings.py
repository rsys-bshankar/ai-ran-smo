"""The pure half of the event-bus relay to Kafka (ADR 0006 slice 2, plan task S1.10, D9): its settings, the producer configuration they make, the
topic naming rule, and the Kafka message one stored event becomes. No Kafka client is imported here (smo_shared/kafka_relay.py does that, and only
when the relay is on), so this module is tested, and mutation-tested, without a broker.

Settings (environment; ADR 0006 section 3.7; the two secrets by the `*_FILE` convention of `smo_shared.secretfile`):
    SMO_KAFKA_BOOTSTRAP_SERVERS        host:port[,host:port...] (required when the relay is on)
    SMO_KAFKA_SECURITY_PROTOCOL        SSL (default) | SASL_SSL | SASL_PLAINTEXT | PLAINTEXT (a warning at start: the compose lab profile only)
    SMO_KAFKA_SASL_MECHANISM           SCRAM-SHA-512 (default) | SCRAM-SHA-256 | PLAIN (refused without TLS)
    SMO_KAFKA_SASL_USERNAME, SMO_KAFKA_SASL_PASSWORD(_FILE)
    SMO_KAFKA_TLS_CA_FILE, SMO_KAFKA_TLS_CERT_FILE, SMO_KAFKA_TLS_KEY_FILE, SMO_KAFKA_TLS_KEY_PASSWORD(_FILE)
    SMO_KAFKA_CLIENT_ID                default smo-<MODULE>
    SMO_KAFKA_EVENTS_TOPIC             default smo.events.v1
    SMO_KAFKA_RELAY_BATCH              events per batch, default 500 (1..500)
    SMO_KAFKA_RELAY_SETTLE_SECONDS     how old an event must be before it is sent, default 3 (ADR 0006 section 3.2, the skip hazard)
    SMO_KAFKA_DELIVERY_TIMEOUT_MS      default 30000
    SMO_KAFKA_MAX_MESSAGE_BYTES        default 1048576; a larger event is skipped, logged and counted, never retried

Topic names: `smo.<owner>[.<stream>].v<N>`, lower case: `<owner>` is the one part of the platform that writes the topic, so a per-topic (per-prefix)
ACL can give each owner's principal Write on `smo.<owner>.` and nothing else (PR-CONV-S1.15). `smo.events.v1` is the event bus's (this relay's) topic;
`smo.ves.in.v1` will be the VES input of SB-7.6 (ADR 0006 slice 4); smo-teiv's ingestion topic, when S3.23 puts smo-teiv on the same broker, is
`smo.teiv.<...>` and TE&IV's alone (D6): the relay refuses a topic under `smo.teiv.`, so no setting can point the platform's events into it.
"""

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .secretfile import read_secret

log = logging.getLogger("smo.kafka")

PROTOCOLS = ("SSL", "SASL_SSL", "SASL_PLAINTEXT", "PLAINTEXT")
MECHANISMS = ("SCRAM-SHA-512", "SCRAM-SHA-256", "PLAIN")
DEFAULT_TOPIC = "smo.events.v1"
TOPIC_RULE = re.compile(r"^smo\.[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*\.v[1-9][0-9]*$")
RESERVED_PREFIXES = ("smo.teiv.",)          # smo-teiv's ingestion (S3.23): written by TE&IV only (D6)
MAX_BATCH = 500
CONTENT_TYPE = "application/cloudevents+json; charset=utf-8"


class KafkaSettingsError(ValueError):
    """A Kafka setting is missing or not allowed; the message names the variable, never a secret's value."""


def check_topic(topic: str) -> str:
    """`topic` when it follows the naming rule of the module docstring and is not reserved; KafkaSettingsError otherwise."""
    if not TOPIC_RULE.match(topic):
        raise KafkaSettingsError(f"SMO_KAFKA_EVENTS_TOPIC {topic!r} does not follow smo.<owner>[.<stream>].v<N> (lower case)")
    if topic.startswith(RESERVED_PREFIXES):
        raise KafkaSettingsError(f"SMO_KAFKA_EVENTS_TOPIC {topic!r} is under smo.teiv.: smo-teiv's ingestion topic is TE&IV's alone (D6)")
    return topic


def _int(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    """`environ[name]` as a whole number between `low` and `high`, or `default` when unset; KafkaSettingsError naming `name` otherwise."""
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise KafkaSettingsError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise KafkaSettingsError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class KafkaSettings:
    """The relay's settings, read once at start (`from_env`); `sasl_password` and `tls_key_password` never appear in `repr` or in a log line."""
    bootstrap_servers: str
    security_protocol: str = "SSL"
    sasl_mechanism: str = "SCRAM-SHA-512"
    sasl_username: str | None = None
    sasl_password: str | None = field(default=None, repr=False)
    tls_ca_file: str | None = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    tls_key_password: str | None = field(default=None, repr=False)
    client_id: str = "smo"
    topic: str = DEFAULT_TOPIC
    batch: int = MAX_BATCH
    settle_seconds: float = 3.0
    delivery_timeout_ms: int = 30000
    max_message_bytes: int = 1048576

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "KafkaSettings":
        """The settings of the module docstring; a missing or forbidden one raises KafkaSettingsError naming it (a value and its `_FILE` both set raise
        `secretfile.SecretConflict`). PLAINTEXT is allowed with a warning."""
        servers = (environ.get("SMO_KAFKA_BOOTSTRAP_SERVERS") or "").strip()
        if not servers:
            raise KafkaSettingsError("SMO_KAFKA_BOOTSTRAP_SERVERS is not set")
        protocol = (environ.get("SMO_KAFKA_SECURITY_PROTOCOL") or "SSL").strip().upper()
        if protocol not in PROTOCOLS:
            raise KafkaSettingsError(f"SMO_KAFKA_SECURITY_PROTOCOL must be one of {', '.join(PROTOCOLS)}")
        mechanism = (environ.get("SMO_KAFKA_SASL_MECHANISM") or "SCRAM-SHA-512").strip().upper()
        if mechanism not in MECHANISMS:
            raise KafkaSettingsError(f"SMO_KAFKA_SASL_MECHANISM must be one of {', '.join(MECHANISMS)}")
        if protocol.startswith("SASL") and mechanism == "PLAIN" and protocol != "SASL_SSL":
            raise KafkaSettingsError("SMO_KAFKA_SASL_MECHANISM=PLAIN sends the password in the clear: only with SMO_KAFKA_SECURITY_PROTOCOL=SASL_SSL")
        username = (environ.get("SMO_KAFKA_SASL_USERNAME") or "").strip() or None
        password = read_secret("SMO_KAFKA_SASL_PASSWORD", environ)
        if protocol.startswith("SASL") and not (username and password):
            raise KafkaSettingsError(f"SMO_KAFKA_SECURITY_PROTOCOL={protocol} needs SMO_KAFKA_SASL_USERNAME and SMO_KAFKA_SASL_PASSWORD (or _FILE)")
        if protocol == "PLAINTEXT":
            log.warning("Kafka relay over PLAINTEXT: events and any credentials travel unencrypted (a lab broker on the compose network only)")
        settle = (environ.get("SMO_KAFKA_RELAY_SETTLE_SECONDS") or "").strip()
        try:
            settle_seconds = float(settle) if settle else 3.0
        except ValueError:
            raise KafkaSettingsError("SMO_KAFKA_RELAY_SETTLE_SECONDS must be a number of seconds") from None
        if settle_seconds < 0:
            raise KafkaSettingsError("SMO_KAFKA_RELAY_SETTLE_SECONDS must be 0 or more")
        return cls(
            bootstrap_servers=servers, security_protocol=protocol, sasl_mechanism=mechanism, sasl_username=username, sasl_password=password,
            tls_ca_file=environ.get("SMO_KAFKA_TLS_CA_FILE") or None, tls_cert_file=environ.get("SMO_KAFKA_TLS_CERT_FILE") or None,
            tls_key_file=environ.get("SMO_KAFKA_TLS_KEY_FILE") or None, tls_key_password=read_secret("SMO_KAFKA_TLS_KEY_PASSWORD", environ),
            client_id=(environ.get("SMO_KAFKA_CLIENT_ID") or f"smo-{environ.get('MODULE') or 'worker'}").strip().replace("/", "-"),
            topic=check_topic((environ.get("SMO_KAFKA_EVENTS_TOPIC") or DEFAULT_TOPIC).strip()),
            batch=_int(environ, "SMO_KAFKA_RELAY_BATCH", MAX_BATCH, 1, MAX_BATCH), settle_seconds=settle_seconds,
            delivery_timeout_ms=_int(environ, "SMO_KAFKA_DELIVERY_TIMEOUT_MS", 30000, 1000, 600000),
            max_message_bytes=_int(environ, "SMO_KAFKA_MAX_MESSAGE_BYTES", 1048576, 1024, 64 * 1048576))

    def producer_config(self) -> dict[str, object]:
        """The librdkafka configuration of the relay's producer: idempotent, acks from every in-sync replica, ordered per partition (ADR 0006
        section 3.2), TLS verified with the host name whenever TLS is on (never switchable), and the client's own log at warning and above."""
        config: dict[str, object] = {
            "bootstrap.servers": self.bootstrap_servers, "client.id": self.client_id, "security.protocol": self.security_protocol,
            "enable.idempotence": True, "acks": "all", "max.in.flight.requests.per.connection": 5,
            "delivery.timeout.ms": self.delivery_timeout_ms, "message.max.bytes": self.max_message_bytes + 65536, "linger.ms": 5,
        }
        if self.security_protocol in ("SSL", "SASL_SSL"):
            config["ssl.endpoint.identification.algorithm"] = "https"
            for key, value in (("ssl.ca.location", self.tls_ca_file), ("ssl.certificate.location", self.tls_cert_file),
                               ("ssl.key.location", self.tls_key_file), ("ssl.key.password", self.tls_key_password)):
                if value:
                    config[key] = value
        if self.security_protocol.startswith("SASL"):
            config.update({"sasl.mechanism": self.sasl_mechanism, "sasl.username": self.sasl_username, "sasl.password": self.sasl_password})
        return config


SECRET_KEYS = ("sasl.password", "ssl.key.password")


def redacted(config: Mapping[str, object]) -> dict[str, object]:
    """`config` with the secret values replaced by `***`: what may be logged."""
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in config.items()}


def message_for(event: Mapping) -> tuple[bytes, bytes, list[tuple[str, bytes]]]:
    """(key, value, headers) of the Kafka message for one stored event's envelope (`events.view`; ADR 0006 section 3.3): the key is the subject (the
    entity, so one entity's events keep their order on one partition), else the source; the value is the envelope itself as UTF-8 JSON; the headers
    are the CloudEvents binary-mode names plus `smoseq` and the content type, so a consumer can filter without parsing the value."""
    key = str(event.get("subject") or event["source"]).encode("utf-8")
    value = json.dumps(event, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    headers = [("ce_specversion", str(event["specversion"])), ("ce_id", str(event["id"])), ("ce_type", str(event["type"])),
               ("ce_source", str(event["source"])), ("ce_time", str(event["time"])), ("smoseq", str(event.get("smoseq", ""))),
               ("content-type", CONTENT_TYPE)]
    return key, value, [(name, text.encode("utf-8")) for name, text in headers]
