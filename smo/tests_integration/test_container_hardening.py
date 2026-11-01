"""Containers run unprivileged (PR-SEC-13.1, 13.3): a non-root user, no capabilities, no privilege escalation."""

import re
from pathlib import Path

import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (SMO_ROOT / "Dockerfile").read_text()


# The one service built here from someone else's source: the O-RAN-SC simulator of the profile `oran-sim` (ADR 0012 section 5). Its daemons (sshd, vsftpd,
# netopeer2) run as root, so after cap_drop ALL it gets back exactly these capabilities, and its root filesystem holds sysrepo's datastores and the PM files.
# It drops everything else and sets no-new-privileges like every service; nothing else may have an exception.
# smo-teiv's Postgres (profile teiv, S3.23; teiv/Dockerfile.db is upstream's PostGIS image with a generated schema): its socket directory is a second tmpfs, since
# the image's entrypoint initialises the database through a socket there whatever the server is told (unix_socket_directories); nothing else is writable.
EXTRA_SCRATCH = {"teiv-db": ["/var/run/postgresql"]}
THIRD_PARTY_BUILDS = {"oran-sim-du": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "SYS_CHROOT", "AUDIT_WRITE", "NET_BIND_SERVICE", "NET_RAW"]}


def _compose() -> dict:
    text = (SMO_ROOT / "docker-compose.yml").read_text()
    return yaml.safe_load(text)["services"]


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", text)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_the_image_ends_as_a_numeric_non_root_user_before_it_starts_the_service():
    """The Dockerfile's last `USER` is a numeric non-root uid, so the runtime can check it, and comes before `CMD`."""
    instructions = _instructions(DOCKERFILE)
    users = [i for i, line in enumerate(instructions) if line.startswith("USER ")]
    assert users, "the Dockerfile never drops root"
    last = instructions[users[-1]].split()[1]
    uid = last.split(":")[0]
    assert uid.isdigit() and int(uid) > 0, f"USER {last}: a numeric non-root uid lets the runtime check it"
    assert users[-1] < next(i for i, line in enumerate(instructions) if line.startswith("CMD ")), "CMD runs as root"


def test_the_two_directories_a_service_writes_exist_and_belong_to_that_user():
    """The directories a service writes (`/data`, `/srv/packages`, `/srv/scratch`) are made and owned by the service user in the image, and compose
    mounts volumes at the first two.
    """
    run = " ".join(line for line in _instructions(DOCKERFILE) if line.startswith("RUN "))
    assert re.search(r"mkdir -p /data /srv/packages /srv/scratch", run) and re.search(r"chown smo:smo /data /srv/packages /srv/scratch", run)
    compose = (SMO_ROOT / "docker-compose.yml").read_text()
    assert "gui_bff_data:/data" in compose and "smo_packages:/srv/packages" in compose


def test_every_service_we_build_drops_every_capability_and_cannot_gain_privileges():
    """Every service built from this repository drops all capabilities and sets `no-new-privileges`."""
    services = _compose()
    built = {name: svc for name, svc in services.items() if "build" in svc}
    assert len(built) >= 25, "the compose file lost services"
    for name, svc in built.items():
        assert svc.get("cap_drop") == ["ALL"], f"{name} keeps Linux capabilities"
        assert "no-new-privileges:true" in svc.get("security_opt", []), f"{name} can gain privileges"


def test_every_service_we_build_has_a_read_only_root_filesystem_with_memory_scratch_space():
    """PR-SEC-13.2: nothing a service writes may land in the image layer. /tmp is a tmpfs; the writable places that must survive are volumes."""
    built = {name: svc for name, svc in _compose().items() if ("build" in svc or name == "edge-tls") and name not in THIRD_PARTY_BUILDS}
    for name, svc in built.items():
        assert svc.get("read_only") is True, f"{name} can write to its root filesystem"
        assert svc.get("tmpfs") == ["/tmp"] + EXTRA_SCRATCH.get(name, []), f"{name} has no scratch space"
    # the runbook copies CSARs into the one volume that is meant for it, not into the tmpfs (docker cp cannot write there)
    assert "smo_scratch:/srv/scratch" in " ".join(_compose()["r1-termination"]["volumes"])
    runbook = (SMO_ROOT / "DEMO_RUNBOOK.md").read_text()
    assert "r1-termination:/tmp" not in runbook and "--directory /tmp" not in runbook


def test_no_service_asks_for_more_privilege_than_the_default():
    """No service is privileged, shares the host network, PID or IPC namespace, mounts the Docker socket or adds a capability; the O-RAN-SC simulator adds exactly
    the capabilities THIRD_PARTY_BUILDS lists for it."""
    for name, svc in _compose().items():
        assert not svc.get("privileged"), name
        assert svc.get("network_mode") != "host" and svc.get("pid") != "host" and svc.get("ipc") != "host", name
        assert not any("docker.sock" in str(v) for v in svc.get("volumes", [])), name
        assert svc.get("cap_add", []) == THIRD_PARTY_BUILDS.get(name, []), f"{name} adds capabilities {svc.get('cap_add')}"


def test_postgres_is_left_with_its_defaults_because_its_entrypoint_drops_privileges_itself():
    """The Postgres service has no `cap_drop`, because its image entrypoint starts as root and drops privileges itself."""
    postgres = _compose()["postgres"]
    assert "cap_drop" not in postgres
