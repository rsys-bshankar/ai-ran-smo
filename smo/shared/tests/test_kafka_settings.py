"""Tests for smo_shared.kafka_settings (ADR 0006 slice 2): the settings and what is refused, the producer configuration, the topic naming rule (and the
topic reserved for smo-teiv), the redaction of secrets, and the Kafka message one event becomes."""

import json
import logging

import pytest

from smo_shared import kafka_settings as ks
from smo_shared.secretfile import SecretConflict

BASE = {"SMO_KAFKA_BOOTSTRAP_SERVERS": "kafka:9092", "MODULE": "dme"}


def test_the_defaults_are_tls_the_events_topic_and_the_adr_numbers():
    """With only the servers set: SSL, SCRAM-SHA-512, topic smo.events.v1, batch 500, settle 3 s, 30 s delivery timeout, 1 MiB messages, client smo-dme."""
    s = ks.KafkaSettings.from_env(BASE)
    assert (s.security_protocol, s.sasl_mechanism, s.topic, s.batch, s.settle_seconds) == ("SSL", "SCRAM-SHA-512", "smo.events.v1", 500, 3.0)
    assert (s.delivery_timeout_ms, s.max_message_bytes, s.client_id) == (30000, 1048576, "smo-dme")


def test_the_producer_is_idempotent_acks_all_and_verifies_tls():
    """The producer configuration: idempotence, acks=all, five in flight (ordered with idempotence), the host name checked under TLS, the TLS files."""
    s = ks.KafkaSettings.from_env({**BASE, "SMO_KAFKA_TLS_CA_FILE": "/ca.pem", "SMO_KAFKA_TLS_CERT_FILE": "/c.pem", "SMO_KAFKA_TLS_KEY_FILE": "/k.pem"})
    config = s.producer_config()
    assert config["enable.idempotence"] is True and config["acks"] == "all" and config["max.in.flight.requests.per.connection"] == 5
    assert config["ssl.endpoint.identification.algorithm"] == "https"
    assert (config["ssl.ca.location"], config["ssl.certificate.location"], config["ssl.key.location"]) == ("/ca.pem", "/c.pem", "/k.pem")
    assert "sasl.mechanism" not in config and config["message.max.bytes"] > s.max_message_bytes


def test_sasl_ssl_takes_the_password_from_its_file_and_it_is_never_shown(tmp_path):
    """SASL_SSL with SCRAM: the password from SMO_KAFKA_SASL_PASSWORD_FILE goes into the configuration, `redacted` masks it, and repr never shows it."""
    (tmp_path / "pw").write_text("sentinel-password\n")
    s = ks.KafkaSettings.from_env({**BASE, "SMO_KAFKA_SECURITY_PROTOCOL": "sasl_ssl", "SMO_KAFKA_SASL_USERNAME": "relay",
                                   "SMO_KAFKA_SASL_PASSWORD_FILE": str(tmp_path / "pw")})
    config = s.producer_config()
    assert config["sasl.password"] == "sentinel-password" and config["sasl.mechanism"] == "SCRAM-SHA-512"
    assert ks.redacted(config)["sasl.password"] == "***" and "sentinel-password" not in repr(s)
    assert "sentinel-password" not in json.dumps(ks.redacted(config))


@pytest.mark.parametrize("extra, message", [
    ({"SMO_KAFKA_BOOTSTRAP_SERVERS": ""}, "SMO_KAFKA_BOOTSTRAP_SERVERS"),
    ({"SMO_KAFKA_SECURITY_PROTOCOL": "TLS"}, "SMO_KAFKA_SECURITY_PROTOCOL must be one of"),
    ({"SMO_KAFKA_SASL_MECHANISM": "GSSAPI"}, "SMO_KAFKA_SASL_MECHANISM must be one of"),
    ({"SMO_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT", "SMO_KAFKA_SASL_MECHANISM": "PLAIN"}, "PLAIN sends the password in the clear"),
    ({"SMO_KAFKA_SECURITY_PROTOCOL": "SASL_SSL"}, "needs SMO_KAFKA_SASL_USERNAME"),
    ({"SMO_KAFKA_RELAY_BATCH": "501"}, "SMO_KAFKA_RELAY_BATCH must be between 1 and 500"),
    ({"SMO_KAFKA_RELAY_BATCH": "many"}, "SMO_KAFKA_RELAY_BATCH must be a whole number"),
    ({"SMO_KAFKA_RELAY_SETTLE_SECONDS": "-1"}, "0 or more"),
    ({"SMO_KAFKA_RELAY_SETTLE_SECONDS": "soon"}, "number of seconds"),
    ({"SMO_KAFKA_EVENTS_TOPIC": "Events"}, "does not follow"),
    ({"SMO_KAFKA_EVENTS_TOPIC": "smo.teiv.ingestion.v1"}, "TE&IV's alone"),
])
def test_what_is_not_allowed_is_refused_with_the_variable_named(extra, message):
    """Missing servers, an unknown protocol or mechanism, PLAIN without TLS, SASL without credentials, bad numbers and bad or reserved topics."""
    with pytest.raises(ks.KafkaSettingsError, match=message):
        ks.KafkaSettings.from_env({**BASE, **extra})


def test_plaintext_is_allowed_with_a_warning_and_a_password_twice_is_a_conflict(caplog):
    """PLAINTEXT (the compose lab broker) starts with a warning; a password given both inline and as a file fails closed."""
    with caplog.at_level(logging.WARNING, logger="smo.kafka"):
        s = ks.KafkaSettings.from_env({**BASE, "SMO_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"})
    assert "ssl.endpoint.identification.algorithm" not in s.producer_config() and "PLAINTEXT" in caplog.text
    with pytest.raises(SecretConflict):
        ks.KafkaSettings.from_env({**BASE, "SMO_KAFKA_SASL_PASSWORD": "a", "SMO_KAFKA_SASL_PASSWORD_FILE": "/x"})


@pytest.mark.parametrize("topic, ok", [("smo.events.v1", True), ("smo.ves.in.v1", True), ("smo.dme.pm-rop.v12", True), ("smo.events", False),
                                       ("smo.events.v0", False), ("events.v1", False), ("smo..v1", False), ("smo.teiv.x.v1", False)])
def test_the_topic_rule(topic, ok):
    """smo.<owner>[.<stream>].v<N>, lower case, N from 1; smo.teiv.* is reserved for smo-teiv's ingestion (D6)."""
    if ok:
        assert ks.check_topic(topic) == topic
    else:
        with pytest.raises(ks.KafkaSettingsError):
            ks.check_topic(topic)


def test_an_event_becomes_its_envelope_keyed_by_subject_with_cloudevents_headers():
    """Key = subject (else source); value = the envelope as compact JSON; headers ce_id, ce_type, ce_source, ce_time, ce_specversion, smoseq, content-type."""
    event = {"specversion": "1.0", "id": "e-1", "source": "/smo/ran-nf-oam", "type": "org.oran.smo.alarm.raised", "time": "2026-11-01T00:00:00+00:00",
             "datacontenttype": "application/json", "data": {"a": 1}, "subject": "alarm-7", "smoseq": 42}
    key, value, headers = ks.message_for(event)
    assert key == b"alarm-7" and json.loads(value) == event
    assert dict(headers) == {"ce_specversion": b"1.0", "ce_id": b"e-1", "ce_type": b"org.oran.smo.alarm.raised", "ce_source": b"/smo/ran-nf-oam",
                             "ce_time": b"2026-11-01T00:00:00+00:00", "smoseq": b"42", "content-type": ks.CONTENT_TYPE.encode()}
    assert ks.message_for({k: v for k, v in event.items() if k != "subject"})[0] == b"/smo/ran-nf-oam"
