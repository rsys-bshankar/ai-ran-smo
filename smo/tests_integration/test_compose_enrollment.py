"""PR-SEC-14: the compose file gives every SMO module the enrollment secret, and no rApp.

A module that registers at SME without the secret is recorded as an rApp, is refused the internal scope and cannot call its peers: the whole stack
fails to come ready. `docker compose config` accepts that mistake (it did once: a service written as `environment: *db_env` was missed by a scripted
edit), so this reads the resolved file.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
SECRET_FILE = "/run/secrets/enrollment_secret"
# services that never call another module through R1 (or are not ours): they hold no identity at SME
NO_IDENTITY = {"postgres", "pgbouncer", "db-backup", "migrate", "r1-termination", "mock-o1-adaptor", "gui", "edge", "edge-tls", "netconf-lab", "netconf-lab-replay",
               "tempo", "loki", "fluent-bit", "grafana"}


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return dict(env) if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_every_smo_module_mounts_the_secret_and_names_its_file():
    """Every SMO module (not an rApp or an exempt service) mounts the enrollment secret and names its file, so it registers at SME as a module."""
    missing = []
    for name, service in _services().items():
        if name in NO_IDENTITY or name.endswith(("-rapp", "-rapp-worker")):      # an rApp's worker is the rApp (call flow 22e)
            continue
        mounted = "enrollment_secret" in (service.get("secrets") or [])
        named = _env(service).get("SMO_ENROLLMENT_SECRET_FILE") == SECRET_FILE
        if not (mounted and named):
            missing.append(f"{name}: mounts the secret={mounted}, names the file={named}")
    assert missing == []


def test_no_rapp_holds_the_secret_and_each_declares_itself_one():
    """No rApp (nor an rApp's worker) holds the enrollment secret or names its file, and each sets `SMO_IDENTITY_KIND=rapp`."""
    wrong = []
    for name, service in _services().items():
        if not name.endswith(("-rapp", "-rapp-worker")):
            continue
        env = _env(service)
        if "enrollment_secret" in (service.get("secrets") or []) or "SMO_ENROLLMENT_SECRET_FILE" in env:
            wrong.append(f"{name} has the enrollment secret")
        if env.get("SMO_IDENTITY_KIND") != "rapp":
            wrong.append(f"{name} does not set SMO_IDENTITY_KIND=rapp")
    assert wrong == []


def test_the_secret_is_declared_and_comes_from_the_script_that_creates_it():
    """The compose secret `enrollment_secret` is the file the init script creates."""
    assert yaml.safe_load(COMPOSE.read_text())["secrets"]["enrollment_secret"]["file"] == "./secrets/enrollment_secret"
    assert "create_secret enrollment_secret" in (COMPOSE.parent / "scripts" / "init_secrets.sh").read_text()
