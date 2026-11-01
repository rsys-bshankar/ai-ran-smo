"""The TLS edge (PR-SEC-1): the development certificate script, the edge nginx config and the compose profile."""

import re
import shutil
import socket
import ssl
import stat
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = SMO_ROOT / "scripts" / "make_dev_certs.sh"
NGINX = (SMO_ROOT / "edge" / "nginx.conf").read_text()

pytestmark_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def make_certs(directory: Path, *args: str, names: str = "") -> subprocess.CompletedProcess:
    """Runs the development certificate script into `directory` with a minimal environment (`names` as the extra names) and returns the finished
    process.
    """
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "SMO_TLS_NAMES": names}
    return subprocess.run([str(SCRIPT), *args, str(directory)], capture_output=True, text=True, env=env, timeout=60)


def openssl(*args: str) -> str:
    return subprocess.run(["openssl", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A certificate directory made once for the module with the extra name `smo.example.test` and the address 10.0.0.5."""
    directory = tmp_path_factory.mktemp("certs")
    result = make_certs(directory, names="smo.example.test,10.0.0.5")
    assert result.returncode == 0, result.stderr
    return directory


@pytestmark_openssl
def test_the_server_certificate_chains_to_the_ca_and_is_not_itself_a_ca(certs):
    """The server certificate verifies against the CA, is not a CA itself and is for server authentication. Needs openssl."""
    assert "OK" in openssl("verify", "-CAfile", str(certs / "ca.crt"), str(certs / "server.crt"))
    server = openssl("x509", "-in", str(certs / "server.crt"), "-noout", "-text")
    assert "CA:FALSE" in server and "TLS Web Server Authentication" in server
    assert "CA:TRUE" in openssl("x509", "-in", str(certs / "ca.crt"), "-noout", "-text")


@pytestmark_openssl
def test_it_names_every_host_the_edge_is_reached_by_and_the_extra_names_asked_for(certs):
    """The certificate names localhost, the two services behind the edge and 127.0.0.1, and the extra names the operator asked for. Needs openssl."""
    san = openssl("x509", "-in", str(certs / "server.crt"), "-noout", "-ext", "subjectAltName")
    for name in ("DNS:localhost", "DNS:r1-termination", "DNS:gui", "IP Address:127.0.0.1", "DNS:smo.example.test",
                 "IP Address:10.0.0.5"):
        assert name in san, name


@pytestmark_openssl
def test_it_is_valid_for_a_year_and_the_key_belongs_to_the_certificate(certs):
    """The certificate is valid for more than 300 and less than 400 days, and the private key is the one that belongs to it. Needs openssl."""
    assert subprocess.run(["openssl", "x509", "-in", str(certs / "server.crt"), "-noout", "-checkend", str(300 * 86400)]).returncode == 0
    assert subprocess.run(["openssl", "x509", "-in", str(certs / "server.crt"), "-noout", "-checkend", str(400 * 86400)]).returncode == 1
    cert_key = openssl("x509", "-in", str(certs / "server.crt"), "-noout", "-pubkey")
    private_key = openssl("pkey", "-in", str(certs / "server.key"), "-pubout")
    assert cert_key == private_key


@pytestmark_openssl
def test_the_ca_key_is_private_the_directory_is_owner_only_and_a_rerun_keeps_what_is_there(tmp_path):
    """The CA key is private (0600), the directory is owner-only (0700), a rerun keeps the existing files and `--force` replaces them. Needs
    openssl.
    """
    directory = tmp_path / "certs"
    assert make_certs(directory).returncode == 0
    assert stat.S_IMODE((directory / "ca.key").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    before = (directory / "server.crt").read_bytes()
    again = make_certs(directory)
    assert again.stdout.startswith("kept:") and (directory / "server.crt").read_bytes() == before
    forced = make_certs(directory, "--force")
    assert forced.stdout.startswith("created:") and (directory / "server.crt").read_bytes() != before


def _client_context() -> ssl.SSLContext:
    """A verifying client that will not speak anything older than TLS 1.2."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


@pytestmark_openssl
def test_a_real_tls_handshake_works_for_a_client_that_trusts_the_ca_and_fails_for_one_that_does_not(certs):
    """A real TLS handshake with the generated certificate works for clients that trust the CA and fails for a client that does not trust it. Needs
    openssl.
    """
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(certs / "server.crt", certs / "server.key")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    def serve():
        for _ in range(4):                                   # three trusted clients and one that is not
            connection, _ = listener.accept()
            try:
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.sendall(b"hello")
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    trusting = _client_context()
    trusting.load_verify_locations(cafile=str(certs / "ca.crt"))
    for name in ("localhost", "r1-termination", "smo.example.test"):          # each name the certificate carries
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, trusting.wrap_socket(raw, server_hostname=name) as tls:
            assert tls.recv(5) == b"hello" and tls.version() in ("TLSv1.2", "TLSv1.3")
    with pytest.raises(ssl.SSLCertVerificationError):                          # a client with the system trust store
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            untrusting = _client_context()
            untrusting.load_default_certs()                                    # the system store, which does not have our CA
            untrusting.wrap_socket(raw, server_hostname="localhost")
    listener.close()


# ---------------------------------------------------------------- the edge nginx config

def test_the_edge_offers_only_tls_1_2_and_1_3_with_the_certificate_from_the_mounted_secrets():
    """The edge nginx config offers TLS 1.2 and 1.3 only, reads its certificate and key from the mounted secrets, and disables session tickets and
    the version header.
    """
    assert re.search(r"ssl_protocols\s+TLSv1\.2 TLSv1\.3;", NGINX)
    assert "TLSv1 " not in NGINX and "TLSv1.1" not in NGINX and "SSLv" not in NGINX
    assert "ssl_certificate     /run/secrets/tls_cert;" in NGINX and "ssl_certificate_key /run/secrets/tls_key;" in NGINX
    assert "ssl_session_tickets off;" in NGINX and "server_tokens       off;" in NGINX


def test_each_door_is_tls_sends_hsts_and_proxies_to_the_right_service():
    """The edge has two TLS-only listeners, 3443 to the GUI and 8443 to the gateway, each sending HSTS and the forwarded protocol, and no
    plain-HTTP listener.
    """
    servers = re.findall(r"server \{(.*?)\n\}", NGINX, re.S)
    assert len(servers) == 2
    by_port = {re.search(r"listen (\d+) ssl;", body).group(1): body for body in servers}
    assert set(by_port) == {"3443", "8443"}
    assert "proxy_pass http://gui:8080;" in by_port["3443"] and "proxy_pass http://r1-termination:8000;" in by_port["8443"]
    for body in servers:
        assert re.search(r'add_header Strict-Transport-Security "max-age=\d+; includeSubDomains" always;', body)
        assert "proxy_set_header X-Forwarded-Proto https;" in body
    assert not re.search(r"listen \d+;", NGINX), "a plain-HTTP listener in the TLS edge"


# ---------------------------------------------------------------- compose

def _compose() -> dict:
    return yaml.safe_load((SMO_ROOT / "docker-compose.yml").read_text())


def test_the_edge_is_only_in_the_tls_profile_publishes_two_ports_and_is_hardened():
    """The edge runs only in the `tls` profile, publishes just its two ports, drops all capabilities and gets its certificate as secrets; the
    services outside the default stack are exactly the listed profile services, so the default stack needs no certificate.
    """
    compose = _compose()
    edge = compose["services"]["edge-tls"]
    assert edge["profiles"] == ["tls"]
    assert sorted(edge["ports"]) == ["3443:3443", "8443:8443"]
    assert edge["cap_drop"] == ["ALL"] and "no-new-privileges:true" in edge["security_opt"]
    assert sorted(edge["secrets"]) == ["tls_cert", "tls_key"]
    assert compose["secrets"]["tls_cert"]["file"] == "./certs/server.crt" and compose["secrets"]["tls_key"]["file"] == "./certs/server.key"
    # the only services outside the default stack: the TLS edge, the NETCONF lab server (PR-SB-1.4, profile netconf-lab), the pooler,
    # the traces and logs stack (PR-OBS-3, PR-OBS-6: profiles tracing and logging), and Echo (PR-CONV-S1.6, profile echo: no certificate either)
    assert sorted(name for name, svc in compose["services"].items() if "profiles" in svc) == [
        "db-backup", "echo", "edge-tls", "fluent-bit", "grafana", "loki", "netconf-lab", "oran-sim-du", "pgbouncer", "tempo"], \
        "the default stack must not need a certificate"
    assert compose["services"]["netconf-lab"]["profiles"] == ["netconf-lab"]


def test_the_gui_session_cookie_is_secure_by_default_so_it_is_only_sent_over_the_https_door():
    """The GUI session cookie is Secure by default, so a browser sends it only over the HTTPS door."""
    environment = _compose()["services"]["gui-bff"]["environment"]
    assert environment["GUI_COOKIE_SECURE"] == "${GUI_COOKIE_SECURE:-true}"


def test_the_certificate_directory_is_git_ignored():
    """The `certs/` directory is git-ignored so generated keys are not committed."""
    assert "certs/" in (SMO_ROOT / ".gitignore").read_text().splitlines()


def test_the_edge_forwards_the_token_endpoint_to_sme_without_a_token_and_everything_else_to_r1():
    """PR-SEC-1.6: /bootstrap advertises <R1_PUBLIC_BASE_URL>/sme/oauth2/token; that one path goes straight to SME, which is where a consumer
    with no token yet must be able to get one. The edge may not forward any other /sme path around R1's token check."""
    conf = (SMO_ROOT / "edge" / "nginx.conf").read_text()
    exact = re.findall(r"location = (/\S+) \{[^}]*proxy_pass ([^;]+);", conf)
    assert ("/sme/oauth2/token", "http://sme:8000/oauth2/token") in exact
    to_sme = [path for path, target in exact if "sme:8000" in target]
    assert to_sme == ["/sme/oauth2/token"], f"the edge forwards these straight to SME, around R1's token check: {to_sme}"
    assert "R1_PUBLIC_BASE_URL" in (SMO_ROOT / "docker-compose.yml").read_text() and "sme" in _compose()["services"]["edge-tls"]["depends_on"]
