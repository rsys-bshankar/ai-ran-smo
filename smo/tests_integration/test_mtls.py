"""PR-SEC-2: mutual TLS between the services: the certificate script and its rotation, the compose override, the image command, and the chart.

The handshakes themselves (a server started with the options `smo_shared.mtls` prints, a client with and without a certificate) are in `shared/tests/test_mtls.py`; the
full stack over mTLS is the CI job `compose-mtls`. The render tests here need `helm` and are skipped without it (CI's `helm` job has it).
"""

import importlib.util
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
CHART = SMO_ROOT / "deploy" / "helm" / "smo"
OVERRIDE = SMO_ROOT / "docker-compose.mtls.yml"
COMPOSE = SMO_ROOT / "docker-compose.yml"

_spec = importlib.util.spec_from_file_location("mtls_certs", SMO_ROOT / "scripts" / "mtls_certs.py")
certs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(certs)

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
EXEMPT_FROM_MTLS = {"mock-o1-adaptor", "gui", "migrate", "postgres", "pgbouncer", "netconf-lab", "edge-tls"}


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """A certificate directory made once for the module by `mtls_certs.init` with a 30 day lifetime: a CA and one certificate per service."""
    root = tmp_path_factory.mktemp("mtls")
    assert certs.init(root, 30).startswith("created")
    return root


def _handshake(server_dir: Path, client_dir: Path, present: bool = True, trust: Path | None = None, hostname: str | None = None) -> bool:
    """A real TLS handshake in memory between a server context (requires a client certificate) and a client context; True when both sides complete."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(server_dir / "tls.crt", server_dir / "tls.key")
    server.load_verify_locations(cafile=server_dir / "ca.crt")
    server.verify_mode = ssl.CERT_REQUIRED
    client = ssl.create_default_context(cafile=str((trust or client_dir) / "ca.crt"))
    if present:
        client.load_cert_chain(client_dir / "tls.crt", client_dir / "tls.key")
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname=hostname or server_dir.name)
    s = server.wrap_bio(s_in, s_out, server_side=True)
    done = {"c": False, "s": False}
    try:
        for _ in range(20):
            for name, side, inbound, outbound_of_peer in (("c", c, c_in, s_out), ("s", s, s_in, c_out)):
                if done[name]:
                    continue
                data = outbound_of_peer.read()
                if data:
                    inbound.write(data)
                try:
                    side.do_handshake()
                    done[name] = True
                except ssl.SSLWantReadError:
                    pass
            if all(done.values()):
                break
        # TLS 1.3: the client finishes first; the server sees a missing or bad client certificate on its last read
        if done["c"] and not done["s"]:
            s_in.write(c_out.read())
            s.do_handshake()
            done["s"] = True
    except ssl.SSLError:
        return False
    return all(done.values())


# ---------------------------------------------------------------------------------------------------------------------------- the script

def test_every_service_gets_a_certificate_signed_by_the_ca_with_its_name_and_both_usages(pki):
    """Each service's certificate is directly issued by the CA, is not itself a CA, is named after the service, can be used as a client, and as a
    server only when the service serves.
    """
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca = x509.load_pem_x509_certificate((pki / "ca" / "ca.crt").read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    for name in certs.SERVICES:
        cert = x509.load_pem_x509_certificate((pki / name / "tls.crt").read_bytes())
        cert.verify_directly_issued_by(ca)
        assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == name
        usages = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.CLIENT_AUTH in usages
        assert (ExtendedKeyUsageOID.SERVER_AUTH in usages) == (name in certs.SERVERS), name
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        assert name in names and (("localhost" in names) == (name in certs.SERVERS)), name


def test_the_ca_key_stays_private_and_the_service_files_are_readable_by_a_container(pki):
    """The CA key and the directories above it are private (0600 and 0700), the service files are readable (0644) so a container running as another
    user can read them, and a service directory never holds the CA key.
    """
    assert stat.S_IMODE((pki / "ca" / "ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE(pki.stat().st_mode) == 0o700 and stat.S_IMODE((pki / "ca").stat().st_mode) == 0o700
    for name in ("tls.crt", "tls.key", "ca.crt"):
        assert stat.S_IMODE((pki / "sme" / name).stat().st_mode) == 0o644
    assert not (pki / "sme" / "ca.key").exists(), "a service never holds the CA key"


def test_a_rerun_keeps_what_is_there_and_force_replaces_it(tmp_path):
    """Running `init` again keeps the existing certificates and only `force` replaces them."""
    assert certs.init(tmp_path, 30).startswith("created")
    before = (tmp_path / "sme" / "tls.crt").read_bytes()
    assert certs.init(tmp_path, 30).startswith("kept") and (tmp_path / "sme" / "tls.crt").read_bytes() == before
    assert certs.init(tmp_path, 30, force=True).startswith("created") and (tmp_path / "sme" / "tls.crt").read_bytes() != before


def test_services_talk_to_each_other_and_a_client_certificate_works_but_a_missing_one_or_another_ca_does_not(pki, tmp_path):
    """A real TLS handshake succeeds between two services and with a client certificate, and fails with no client certificate, with a certificate
    from another PKI, and when the server's certificate is not from the CA the client trusts.
    """
    assert _handshake(pki / "sme", pki / "dme")
    certs.new_client(pki, "operator", 30)
    assert _handshake(pki / "sme", pki / "clients" / "operator")
    assert not _handshake(pki / "sme", pki / "dme", present=False)
    other = tmp_path / "other"
    certs.init(other, 30)
    assert not _handshake(pki / "sme", other / "dme", trust=pki / "sme")        # trusts our CA, presents one of the other PKI
    assert not _handshake(pki / "sme", pki / "dme", trust=other / "sme")        # presents ours, but the server's is not the CA it trusts


def test_extra_names_end_up_on_every_server_certificate(tmp_path, monkeypatch):
    """The names and addresses in `SMO_MTLS_NAMES` are added to every server certificate as DNS and IP subject alternative names."""
    from cryptography import x509
    monkeypatch.setenv("SMO_MTLS_NAMES", "smo.example.test, 10.0.0.5")
    certs.init(tmp_path, 30)
    san = x509.load_pem_x509_certificate((tmp_path / "sme" / "tls.crt").read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "smo.example.test" in san.get_values_for_type(x509.DNSName)
    assert any(str(ip) == "10.0.0.5" for ip in san.get_values_for_type(x509.IPAddress))


def test_renewing_the_leaves_keeps_the_ca_and_every_service_still_agrees(tmp_path):
    """Renewing replaces the service certificates and keeps the CA, so the services still agree and a service that has not restarted still talks to
    one that has; the expiry check flags a certificate at 100 days but not at 30.
    """
    certs.init(tmp_path, 30)
    old_ca, old_leaf = (tmp_path / "ca" / "ca.crt").read_bytes(), (tmp_path / "sme" / "tls.crt").read_bytes()
    assert certs.renew(tmp_path, 90).startswith("renewed")
    assert (tmp_path / "ca" / "ca.crt").read_bytes() == old_ca and (tmp_path / "sme" / "tls.crt").read_bytes() != old_leaf
    assert _handshake(tmp_path / "sme", tmp_path / "dme")
    assert certs.status(tmp_path, 30)[1] is False and certs.status(tmp_path, 100)[1] is True        # 90 days left: fine at 30, expiring at 100
    # a service left on the old certificate (not yet restarted) still talks to one that has the new one: same CA
    (tmp_path / "old").mkdir()
    for name in ("tls.crt", "tls.key", "ca.crt"):
        (tmp_path / "old" / name).write_bytes((tmp_path / "sme" / name).read_bytes())
    assert _handshake(tmp_path / "old", tmp_path / "dme", hostname="sme")


def test_the_ca_rotates_in_three_phases_and_no_phase_breaks_a_pair_of_services_on_different_phases(tmp_path):
    """A CA rotation runs in three phases (trust: the bundle holds the old and the new CA; issue: new certificates from the new CA; retire: the old
    CA is dropped), a pair of services one phase apart can still handshake in both directions so a rolling restart never breaks traffic, the
    finished rotation refuses what only the old CA vouches for, and repeating a phase is reported, not applied.
    """
    certs.init(tmp_path, 30)

    def snapshot(label: str, *services: str) -> Path:
        target = tmp_path / "snap" / label
        for service in services:
            (target / service).mkdir(parents=True)
            for name in ("tls.crt", "tls.key", "ca.crt"):
                (target / service / name).write_bytes((tmp_path / service / name).read_bytes())
        return target

    before = snapshot("before", "sme", "dme")                              # restarted before the rotation began
    assert certs.rotate_ca(tmp_path, "trust", 30).startswith("trust")
    trusting = snapshot("trusting", "sme", "dme")                          # old leaf, ca.crt = old + new
    assert (tmp_path / "sme" / "ca.crt").read_bytes().count(b"BEGIN CERTIFICATE") == 2
    assert certs.rotate_ca(tmp_path, "trust", 30).startswith("a rotation is already")
    assert certs.rotate_ca(tmp_path, "issue", 30).startswith("issue")
    issued = snapshot("issued", "sme", "dme")                              # new leaf from the new CA, ca.crt = new + old
    assert certs.rotate_ca(tmp_path, "retire", 30).startswith("retire")
    retired = snapshot("retired", "sme", "dme")                            # new leaf, ca.crt = new only
    assert (tmp_path / "sme" / "ca.crt").read_bytes().count(b"BEGIN CERTIFICATE") == 1
    # a rolling restart runs one service ahead of another by at most one phase: every such pair keeps working
    for ahead, behind in ((trusting, before), (issued, trusting), (retired, issued)):
        for a, b in (("sme", "dme"), ("dme", "sme")):
            assert _handshake(ahead / a, behind / b), (ahead.name, behind.name, a, b)
            assert _handshake(behind / a, ahead / b), (behind.name, ahead.name, a, b)
    # and the finished rotation refuses what only the old CA vouches for
    assert not _handshake(retired / "sme", before / "dme")
    assert certs.rotate_ca(tmp_path, "retire", 30).startswith("nothing to retire")


def test_status_exits_non_zero_for_an_expiring_certificate(tmp_path):
    """`mtls_certs.py status` exits 1 and names the services when a certificate is within the warning window, and exits 0 with a shorter
    `--warn-days`.
    """
    certs.init(tmp_path, 10)
    result = subprocess.run([sys.executable, str(SMO_ROOT / "scripts" / "mtls_certs.py"), "status", "--dir", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1 and "EXPIRING" in result.stdout and "sme" in result.stdout
    ok = subprocess.run([sys.executable, str(SMO_ROOT / "scripts" / "mtls_certs.py"), "status", "--dir", str(tmp_path), "--warn-days", "5"], capture_output=True, text=True)
    assert ok.returncode == 0


def test_the_command_line_refuses_to_work_without_a_ca(tmp_path):
    """The command line exits 2 and points at `init` when asked to renew in a directory that has no CA."""
    result = subprocess.run([sys.executable, str(SMO_ROOT / "scripts" / "mtls_certs.py"), "renew", "--dir", str(tmp_path / "none")], capture_output=True, text=True)
    assert result.returncode == 2 and "init" in result.stderr


# ----------------------------------------------------------------------------------------------------------------- the compose override

def _compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _override_services() -> dict:
    return yaml.safe_load(OVERRIDE.read_text())["services"]


def test_the_override_covers_every_service_the_script_makes_a_certificate_for_and_nothing_else():
    """The compose mTLS override lists exactly the services the script makes certificates for plus the two workers and the GUI backend, and every
    built service without it is named as exempt on purpose.
    """
    override = set(_override_services())
    assert override == (set(certs.SERVERS) | {"ran-nf-oam-worker", "mdaf-worker", "rapp-mgmt-worker", "aimgf-worker", "energy-saving-rapp-worker", "gui-bff"})
    compose = _compose_services()
    assert override <= set(compose)
    built = {name for name, spec in compose.items() if "build" in spec and "profiles" not in spec}
    assert built - override == EXEMPT_FROM_MTLS & built, "a service built from the Dockerfile is neither mTLS nor named as exempt: " + str(built - override - EXEMPT_FROM_MTLS)


def test_a_server_mounts_its_own_directory_read_only_and_is_probed_over_tls():
    """Each participant turns mTLS on and mounts its own certificate directory read-only; servers are health-checked over TLS, clients have no
    check, and the GUI backend does not serve TLS.
    """
    for name, spec in _override_services().items():
        assert spec["environment"]["SMO_MTLS"] == "on"
        assert spec["volumes"] == [f"./certs/mtls/{name}:/run/mtls:ro"], name
        if name in certs.SERVERS:
            assert spec["healthcheck"]["test"][:4] == ["CMD", "python", "-m", "smo_shared.mtls"] and "probe" in spec["healthcheck"]["test"], name
        else:
            assert "healthcheck" not in spec, f"{name} has no HTTP server to probe"
    assert _override_services()["gui-bff"]["environment"]["SMO_MTLS_SERVE"] == "off"
    assert "SMO_MTLS_SERVE" not in _override_services()["sme"]["environment"]


def test_the_default_compose_file_has_no_mtls_in_it():
    """The default compose file has no mTLS settings at all, so mTLS is switched on only by adding the override file."""
    assert "SMO_MTLS" not in COMPOSE.read_text() and "mtls" not in COMPOSE.read_text().lower().replace("docker-compose.mtls.yml", "")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_compose_accepts_the_two_files_together():
    """`docker compose config` accepts the default file and the override together and the merged result turns mTLS on for the services and not for
    the mock adaptor. Skipped without docker or without the generated secrets.
    """
    env = {**os.environ, "COMPOSE_FILE": f"{COMPOSE}:{OVERRIDE}"}
    result = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=SMO_ROOT, capture_output=True, text=True, env=env)
    if "secrets" in result.stderr and "no such file" in result.stderr.lower():
        pytest.skip("secrets not created in this checkout (scripts/init_secrets.sh)")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["sme"]["environment"]["SMO_MTLS"] == "on" and "SMO_MTLS" not in services["mock-o1-adaptor"].get("environment", {})
    assert services["r1-termination"]["healthcheck"]["test"][-2:] == ["probe", "/ready"]
    assert any(v["target"] == "/run/mtls" for v in services["sme"]["volumes"])


# ----------------------------------------------------------------------------------------------------------------------- the image command

def _cmd() -> str:
    line = next(l for l in (SMO_ROOT / "Dockerfile").read_text().splitlines() if l.startswith("CMD "))
    return json.loads(line[len("CMD "):])[-1]


def test_the_image_starts_plain_without_mtls_and_with_the_options_with_it(tmp_path):
    """The image's command, with `exec uvicorn` replaced by `echo`, adds no TLS option without `SMO_MTLS` and the client-certificate-required
    options with it.
    """
    command = _cmd().replace("exec uvicorn", "echo uvicorn")
    env = {**os.environ, "PYTHONPATH": str(SMO_ROOT / "shared"), "UVICORN_WORKERS": "1", "UVICORN_GRACEFUL_SHUTDOWN_SECONDS": "5"}
    env.pop("SMO_MTLS", None)
    plain = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env)
    assert plain.returncode == 0 and "--ssl" not in plain.stdout and "--no-access-log" in plain.stdout
    certs.init(tmp_path, 30)
    on = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env={**env, "SMO_MTLS": "on", "SMO_MTLS_CERT_FILE": str(tmp_path / "sme" / "tls.crt"),
                                                                                   "SMO_MTLS_KEY_FILE": str(tmp_path / "sme" / "tls.key"), "SMO_MTLS_CA_FILE": str(tmp_path / "sme" / "ca.crt")})
    assert on.returncode == 0 and "--ssl-cert-reqs 2" in on.stdout and "--ssl-ca-certs" in on.stdout and on.stdout.startswith("uvicorn app.main:app")


def test_the_image_does_not_start_when_mtls_is_on_and_a_file_is_missing(tmp_path):
    """Fail closed: never plain HTTP because a certificate did not arrive."""
    command = _cmd().replace("exec uvicorn", "echo STARTED uvicorn")
    env = {**os.environ, "PYTHONPATH": str(SMO_ROOT / "shared"), "SMO_MTLS": "on", "UVICORN_WORKERS": "1", "UVICORN_GRACEFUL_SHUTDOWN_SECONDS": "5",
           "SMO_MTLS_CERT_FILE": str(tmp_path / "missing.crt"), "SMO_MTLS_KEY_FILE": str(tmp_path / "missing.key"), "SMO_MTLS_CA_FILE": str(tmp_path / "missing-ca.crt")}
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and "STARTED" not in result.stdout and "missing.crt" in result.stderr


# ------------------------------------------------------------------------------------------------------------------------------ the chart

def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def test_the_chart_is_off_by_default_and_names_how_each_module_takes_part():
    """The chart has mTLS and cert-manager off by default and assigns each module a mode: off (the mock adaptor and the GUI), client (the GUI
    backend) or server (the services and the two workers).
    """
    values = _values()
    assert values["mtls"]["enabled"] is False and values["mtls"]["certManager"]["enabled"] is False
    modes = {name: {**values["moduleDefaults"], **spec}["mtls"] for name, spec in values["modules"].items()}
    assert {n for n, m in modes.items() if m == "off"} == {"mock-o1-adaptor", "gui"}
    assert {n for n, m in modes.items() if m == "client"} == {"gui-bff"}
    assert {n for n, m in modes.items() if m == "server"} == set(certs.SERVERS) | {"ran-nf-oam-worker", "mdaf-worker", "rapp-mgmt-worker", "aimgf-worker", "energy-saving-rapp-worker"}      # a worker is rendered as a client


def test_the_chart_and_the_compose_override_agree_on_who_takes_part():
    """The modules that take part in mTLS in the chart are exactly those in the compose override."""
    values = _values()
    in_chart = {name for name, spec in values["modules"].items() if {**values["moduleDefaults"], **spec}["mtls"] != "off"}
    assert in_chart == set(_override_services())


def _render(*args: str) -> list[dict]:
    result = subprocess.run(["helm", "template", "smo", str(CHART), "-n", "smo", "--kube-version", "1.30.0", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _container(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]["containers"][0]


@helm
def test_rendered_by_default_there_is_no_mtls_anywhere():
    """Rendered with default values the chart has no certificate, no mTLS variable or volume, and plain HTTP readiness probes. Needs helm."""
    docs = _render()
    assert not [d for d in docs if d["kind"] == "Certificate"]
    for deployment in (d for d in docs if d["kind"] == "Deployment"):
        container = _container(deployment)
        assert "SMO_MTLS" not in {e["name"] for e in container.get("env") or []}, deployment["metadata"]["name"]
        assert "mtls" not in {v["name"] for v in deployment["spec"]["template"]["spec"]["volumes"]}
        if "readinessProbe" in container and "httpGet" in container["readinessProbe"]:
            assert container["readinessProbe"]["httpGet"]["path"] == "/ready"


@helm
def test_with_mtls_each_participant_mounts_its_secret_serves_what_its_mode_says_and_is_probed_by_exec():
    """With mTLS on, each participant mounts its own `<module>-mtls` Secret read-only at /run/mtls with mTLS on, the GUI backend does not serve
    TLS, and the mock adaptor and GUI are left out. Needs helm.
    """
    docs = _render("--set", "mtls.enabled=true")
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    for name, deployment in deployments.items():
        container, env = _container(deployment), {e["name"]: e.get("value") for e in (_container(deployment).get("env") or [])}
        volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
        if name in ("mock-o1-adaptor", "gui"):
            assert "SMO_MTLS" not in env and "mtls" not in volumes, name
            continue
        assert env["SMO_MTLS"] == "on" and volumes["mtls"]["secret"]["secretName"] == f"{name}-mtls", name
        assert {"name": "mtls", "mountPath": "/run/mtls", "readOnly": True} in container["volumeMounts"]
        if name == "gui-bff":
            assert env["SMO_MTLS_SERVE"] == "off"
        else:
            assert "SMO_MTLS_SERVE" not in env
        if name in certs.SERVERS:
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                command = container[probe]["exec"]["command"]
                assert command[:5] == ["python", "-m", "smo_shared.mtls", "probe", command[4]] and command[5] == "8000", (name, probe)
            assert container["readinessProbe"]["exec"]["command"][4] == "/ready" and container["livenessProbe"]["exec"]["command"][4] == "/live"
    assert "httpGet" in _container(deployments["mock-o1-adaptor"])["readinessProbe"]                      # not an mTLS server: probed as before


@helm
def test_cert_manager_mode_makes_a_ca_chain_and_one_certificate_per_participant():
    """With cert-manager mode the chart makes a self-signed issuer, a CA certificate and an issuer from it, then one certificate per participant
    with the usages, names and renewal window the chart promises. Needs helm.
    """
    docs = _render("--set", "mtls.enabled=true", "--set", "mtls.certManager.enabled=true")
    kinds = [(d["kind"], d["metadata"]["name"]) for d in docs if d["apiVersion"].split("/")[0] == "cert-manager.io"]
    assert ("Issuer", "smo-mtls-selfsigned") in kinds and ("Certificate", "smo-mtls-ca") in kinds and ("Issuer", "smo-mtls-ca") in kinds
    certificates = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Certificate" and d["metadata"]["name"] != "smo-mtls-ca"}
    values = _values()
    expected = {f"{n}-mtls" for n, s in values["modules"].items() if {**values["moduleDefaults"], **s}["mtls"] != "off"}
    assert set(certificates) == expected
    sme = certificates["sme-mtls"]["spec"]
    assert sme["secretName"] == "sme-mtls" and set(sme["usages"]) == {"digital signature", "server auth", "client auth"}
    assert {"sme", "sme.smo.svc", "sme.smo.svc.cluster.local", "localhost"} == set(sme["dnsNames"])
    assert sme["issuerRef"]["name"] == "smo-mtls-ca" and sme["renewBefore"] == "720h"


@helm
def test_cert_manager_mode_can_use_an_issuer_of_your_own_and_needs_its_name():
    """With `createCA=false` the chart makes no issuer and uses the named one, and fails to render when no issuer name is given. Needs helm."""
    docs = _render("--set", "mtls.enabled=true", "--set", "mtls.certManager.enabled=true", "--set", "mtls.certManager.createCA=false",
                   "--set", "mtls.certManager.issuerRef.name=corp-ca", "--set", "mtls.certManager.issuerRef.kind=ClusterIssuer")
    assert not [d for d in docs if d["kind"] == "Issuer"]
    assert next(d for d in docs if d["kind"] == "Certificate")["spec"]["issuerRef"]["name"] == "corp-ca"
    failed = subprocess.run(["helm", "template", "smo", str(CHART), "--kube-version", "1.30.0", "--set", "mtls.enabled=true", "--set", "mtls.certManager.enabled=true",
                             "--set", "mtls.certManager.createCA=false"], capture_output=True, text=True)
    assert failed.returncode != 0 and "issuerRef.name" in failed.stderr


@helm
def test_without_cert_manager_the_chart_makes_no_certificates_and_the_secrets_are_the_operators():
    """Without cert-manager the chart makes no Certificate, Issuer or `-mtls` Secret: those Secrets are the operator's to create. Needs helm."""
    docs = _render("--set", "mtls.enabled=true")
    assert not [d for d in docs if d["kind"] in ("Certificate", "Issuer")]
    assert not [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-mtls")]


@helm
def test_the_gateway_ingress_speaks_https_to_the_gateway_only_when_given_a_client_secret():
    """The gateway's Ingress speaks HTTPS to the gateway, with the client Secret, only when `mtls.ingressClientSecret` is set; the GUI's Ingress
    never does. Needs helm.
    """
    base = ["--set", "ingress.enabled=true", "--set", "ingress.r1.host=r1.example.test", "--set", "ingress.gui.host=gui.example.test", "--set", "mtls.enabled=true"]
    plain = {d["metadata"]["name"]: d for d in _render(*base) if d["kind"] == "Ingress"}
    assert "nginx.ingress.kubernetes.io/backend-protocol" not in plain["r1"]["metadata"].get("annotations", {})
    docs = {d["metadata"]["name"]: d for d in _render(*base, "--set", "mtls.ingressClientSecret=smo/ingress-client") if d["kind"] == "Ingress"}
    annotations = docs["r1"]["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/backend-protocol"] == "HTTPS" and annotations["nginx.ingress.kubernetes.io/proxy-ssl-secret"] == "smo/ingress-client"
    assert "nginx.ingress.kubernetes.io/backend-protocol" not in docs["gui"]["metadata"].get("annotations", {})


@helm
def test_lint_is_clean_with_mtls_on():
    """`helm lint` is clean with mTLS and cert-manager on. Needs helm."""
    result = subprocess.run(["helm", "lint", str(CHART), "--kube-version", "1.30.0", "--set", "mtls.enabled=true", "--set", "mtls.certManager.enabled=true"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
