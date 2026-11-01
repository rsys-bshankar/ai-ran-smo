"""Tests of TE&IV's packaging of smo-teiv (plan task S3.23, D19, ADR 0013 section 9): in docker-compose.yml's profile `teiv` smo-teiv's database and API are on
the internal network `teiv` only, which no service joins but smo-teiv's and the shared broker, they publish no port, and no other service names them or
TE&IV's topic; the Kafka relay refuses that topic; the compose profile and the chart's `teiv` values agree on images, topic, heaps and memory limits."""

import re
from pathlib import Path

import pytest
import yaml

SMO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((SMO / "docker-compose.yml").read_text())
VALUES = yaml.safe_load((SMO / "deploy" / "helm" / "smo" / "values.yaml").read_text())
SERVICES = COMPOSE["services"]
TEIV = {"teiv-db", "teiv-kafka-init", "teiv-ingestion", "teiv-exposure"}
NAMES = re.compile(r"\bteiv-db\b|\bteiv-exposure\b|smo\.teiv\.|/topology-inventory/")


def _networks(service: dict) -> set[str]:
    """The networks a compose service joins (`default` when it names none)."""
    nets = service.get("networks") or ["default"]
    return set(nets if isinstance(nets, list) else nets)


def test_the_teiv_services_are_the_profile_and_on_the_internal_network_only():
    """The four smo-teiv services are in the profile `teiv` and on `teiv` alone; `teiv` is internal; none publishes a port."""
    assert COMPOSE["networks"]["teiv"]["internal"] is True
    for name in TEIV:
        assert SERVICES[name]["profiles"] == ["teiv"]
        assert _networks(SERVICES[name]) == {"teiv"}
        assert "ports" not in SERVICES[name]


def test_no_service_but_smo_teivs_and_the_broker_joins_the_network():
    """Only the broker is on both networks (the DME worker's relay reaches it on default, smo-teiv's ingestion on teiv)."""
    on_teiv = {name for name, svc in SERVICES.items() if "teiv" in _networks(svc)}
    assert on_teiv == TEIV | {"kafka"}
    assert _networks(SERVICES["kafka"]) == {"default", "teiv"} and "teiv" in SERVICES["kafka"]["profiles"]


def test_no_other_service_names_smo_teivs_database_api_or_topic():
    """No environment, command or entrypoint of a service outside the profile mentions teiv-db, teiv-exposure, smo.teiv.* or smo-teiv's API path."""
    for name, svc in SERVICES.items():
        if name in TEIV:
            continue
        text = yaml.safe_dump({k: svc.get(k) for k in ("environment", "command", "entrypoint", "healthcheck")})
        assert not NAMES.search(text), name


def test_the_relay_cannot_be_pointed_at_teivs_topic():
    """smo_shared.kafka_settings reserves smo.teiv.* (ADR 0006): the event bus's relay is refused that topic."""
    from smo_shared import kafka_settings as ks
    with pytest.raises(ks.KafkaSettingsError, match="TE&IV"):
        ks.check_topic("smo.teiv.ingestion.v1")


def test_the_chart_runs_what_compose_runs():
    """Same image names and tag, topic and consumer group, heap options and memory limits in the chart's `teiv` values as in the compose profile."""
    t = VALUES["teiv"]
    assert t["enabled"] is False
    assert SERVICES["teiv-ingestion"]["image"].endswith(f"/{t['image']}:{t['tag']}")
    assert SERVICES["teiv-db"]["image"].endswith(f"/{t['dbImage']}:{t['tag']}")
    app = SERVICES["teiv-ingestion"]["environment"]["SPRING_APPLICATION_JSON"]
    assert f'"name":"{t["kafka"]["topic"]}"' in app and f'"group-id":"{t["kafka"]["consumerGroup"]}"' in app
    assert t["kafka"]["topic"] in SERVICES["teiv-kafka-init"]["command"][0]
    for process in ("ingestion", "exposure"):
        svc = SERVICES[f"teiv-{process}"]
        assert svc["environment"]["JAVA_TOOL_OPTIONS"] == t[process]["javaOptions"]
        assert svc["deploy"]["resources"]["limits"]["memory"].rstrip("m") == t[process]["resources"]["limits"]["memory"].rstrip("Mi")
    assert SERVICES["teiv-db"]["deploy"]["resources"]["limits"]["memory"].rstrip("m") == t["db"]["resources"]["limits"]["memory"].rstrip("Mi")


def test_the_images_are_built_from_the_pinned_tag():
    """Both images are built from smo-teiv's 0.3.0 tag commit (ADR 0013 section 2), the same source for the database and the processes."""
    commit = "ee898469d2b9a3de5047ac6675f097ca72e97884"
    for name in ("teiv-db", "teiv-ingestion", "teiv-exposure"):
        assert SERVICES[name]["build"]["additional_contexts"]["teiv"].endswith(f"#{commit}}}")
