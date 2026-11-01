"""PR-SEC-2.4: Postgres over TLS, and every client verifying it (sslmode=verify-full).

Without Docker, what is proved here: the compose overlay (docker-compose.pgtls.yml) reaches every service that opens a database connection and weakens none of them;
the certificate script makes the database a server certificate; and, against a REAL Postgres started for the test (initdb and postgres from the system, as an
unprivileged user), a client with the right CA connects over TLS, one with another CA or another host name is refused, and a plain connection is refused by the
server (scripts/pg_tls_check.py). The chart's render tests need `helm`. What only CI can show is the container itself (the compose job `compose-pgtls`, the kind
job): that the image's entrypoint accepts the command of the overlay and that the pods start on the Secret.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
CHART = SMO_ROOT / "deploy" / "helm" / "smo"
OVERLAY = SMO_ROOT / "docker-compose.pgtls.yml"
COMPOSE = SMO_ROOT / "docker-compose.yml"
HBA = SMO_ROOT / "pgtls" / "pg_hba.conf"

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


certs = _load("mtls_certs", SMO_ROOT / "scripts" / "mtls_certs.py")
checker = _load("pg_tls_check", SMO_ROOT / "scripts" / "pg_tls_check.py")


def _rules(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# ---------------------------------------------------------------------------------------------------------------------------- the overlay

def test_the_overlay_reaches_every_service_that_opens_a_database_connection_and_no_other():
    """The pgtls compose overlay lists every service that has `SMO_DATABASE_URL` and no other besides postgres and pgbouncer, so a service added to
    compose cannot be left on plain text.
    """
    base = yaml.safe_load(COMPOSE.read_text())["services"]
    uses_database = {name for name, spec in base.items() if "SMO_DATABASE_URL" in (spec.get("environment") or {})}
    overlay = yaml.safe_load(OVERLAY.read_text())["services"]
    assert uses_database <= set(overlay), f"a service with a database connection that the overlay leaves plain: {sorted(uses_database - set(overlay))}"
    assert set(overlay) - uses_database == {"postgres", "pgbouncer"}


def test_a_client_verifies_the_certificate_by_default_and_reads_the_ca_read_only():
    """Every database client in the overlay verifies the certificate and its name (`verify-full`, overridable with `SMO_DB_SSLMODE`) against a CA
    file mounted read-only, and pgbouncer verifies its server connection the same way.
    """
    overlay = yaml.safe_load(OVERLAY.read_text())["services"]
    for name, spec in overlay.items():
        if name in ("postgres", "pgbouncer"):
            continue
        assert spec["environment"]["PGSSLMODE"] == "${SMO_DB_SSLMODE:-verify-full}", name
        assert spec["environment"]["PGSSLROOTCERT"] == "/run/pgtls/ca.crt", name
        assert spec["volumes"] == ["./certs/mtls/postgres/ca.crt:/run/pgtls/ca.crt:ro"], name
    assert overlay["pgbouncer"]["environment"]["PGBOUNCER_SERVER_TLS_SSLMODE"] == "verify-full"
    assert overlay["pgbouncer"]["environment"]["PGBOUNCER_SERVER_TLS_CA_FILE"] == "/run/pgtls/ca.crt"


def test_the_overlay_does_not_put_a_weaker_mode_in_any_url_or_turn_verification_off():
    """The overlay's code (comments aside) names no weaker SSL mode and carries no database URL, which stay in docker-compose.yml: libpq reads
    `PGSSLMODE` and `PGSSLROOTCERT` instead.
    """
    text = OVERLAY.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for weaker in ("sslmode=disable", "sslmode=allow", "sslmode=prefer", "sslmode=require", "PGSSLMODE: disable", "PGSSLMODE: require", "PGSSLMODE: prefer"):
        assert weaker not in code
    assert "SMO_DATABASE_URL" not in code, "the URLs stay in docker-compose.yml; libpq reads PGSSLMODE and PGSSLROOTCERT"


def test_postgres_serves_tls_only_with_the_hba_file_and_a_key_only_it_can_read():
    """The overlay starts Postgres with TLS on, TLS 1.2 or later, its certificate and key, the pg_hba file, and a copy of the key that only the
    postgres user can read; it also keeps the slow-statement setting, because the overlay's command replaces the base command.
    """
    overlay = yaml.safe_load(OVERLAY.read_text())["services"]["postgres"]
    command = " ".join(overlay["command"])
    for needed in ("ssl=on", "ssl_cert_file=/run/pgtls/tls.crt", "ssl_key_file=/run/pgtls/tls.key", "hba_file=/etc/smo/pg_hba.conf", "ssl_min_protocol_version=TLSv1.2",
                   "install -m 0600 -o postgres -g postgres /run/pgtls-src/tls.key", "log_min_duration_statement="):
        assert needed in command, needed
    assert "./certs/mtls/postgres:/run/pgtls-src:ro" in overlay["volumes"] and "./pgtls/pg_hba.conf:/etc/smo/pg_hba.conf:ro" in overlay["volumes"]
    base_command = " ".join(yaml.safe_load(COMPOSE.read_text())["services"]["postgres"]["command"])
    assert "log_min_duration_statement" in base_command, "the overlay replaces the base command, so it must keep what the base command sets"


def test_the_hba_file_accepts_the_network_only_as_tls():
    """pg_hba.conf allows local sockets, requires TLS and a SCRAM password for network connections, and rejects network connections that are not
    TLS.
    """
    rules = _rules(HBA.read_text())
    assert rules == [["local", "all", "all", "trust"], ["hostssl", "all", "all", "all", "scram-sha-256"], ["host", "all", "all", "all", "reject"]]


def test_the_default_stack_is_unchanged():
    """The default compose file does not mention the overlay or any `PGSSL` setting, so TLS is on only when the overlay is added."""
    assert "pgtls" not in COMPOSE.read_text() and "PGSSL" not in COMPOSE.read_text()


def test_pgbouncer_refuses_to_verify_without_a_ca_and_keeps_prefer_by_default():
    """The pgbouncer entrypoint keeps `prefer` as its default server TLS mode and exits when a verifying mode is asked for without a CA file."""
    entrypoint = (SMO_ROOT / "pgbouncer" / "entrypoint.sh").read_text()
    assert "server_tls_sslmode = ${PGBOUNCER_SERVER_TLS_SSLMODE:-prefer}" in entrypoint
    assert "verify-ca|verify-full) [ -n \"$server_ca_line\" ]" in entrypoint and "exit 1" in entrypoint


# ---------------------------------------------------------------------------------------------------------------------------- the certificate

def test_the_script_makes_the_database_a_server_certificate_that_names_postgres(tmp_path):
    """The certificate script gives the database a server certificate from the CA, valid for `postgres` and `localhost`, with the CA file beside
    it; `postgres` is deliberately not in the lists the mTLS overlay and its tests enumerate.
    """
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    certs.init(tmp_path, 30)
    cert = x509.load_pem_x509_certificate((tmp_path / "postgres" / "tls.crt").read_bytes())
    ca = x509.load_pem_x509_certificate((tmp_path / "ca" / "ca.crt").read_bytes())
    cert.verify_directly_issued_by(ca)
    assert ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "postgres" in names and "localhost" in names
    assert (tmp_path / "postgres" / "ca.crt").read_bytes() == (tmp_path / "ca" / "ca.crt").read_bytes()
    assert "postgres" not in certs.SERVICES and "postgres" not in certs.SERVERS, "the mTLS overlay and its tests enumerate those"
    certs.renew(tmp_path, 30)                                   # a renewal keeps it a server certificate
    renewed = x509.load_pem_x509_certificate((tmp_path / "postgres" / "tls.crt").read_bytes())
    assert "localhost" in renewed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert renewed.serial_number != cert.serial_number


# ---------------------------------------------------------------------------------------------------------------------------- libpq's variables from the URL

def test_pg_env_takes_sslrootcert_from_the_url_and_leaves_the_environment_alone_without_one(tmp_path):
    """`smo_pg_env` takes the SSL mode and root certificate from the database URL's parameters, and leaves values already in the environment alone
    when the URL has none.
    """
    script = tmp_path / "t.sh"
    script.write_text(f'. "{SMO_ROOT / "scripts" / "pg_env.sh"}"\nsmo_pg_env || exit 1\necho "mode=${{PGSSLMODE:-}} root=${{PGSSLROOTCERT:-}}"\n')
    run = lambda url, **env: subprocess.run(["bash", str(script)], capture_output=True, text=True, env={"PATH": os.environ["PATH"], "SMO_DATABASE_URL": url, **env})  # noqa: E731
    both = run("postgresql+psycopg://u:p@h:5432/d?sslmode=verify-full&sslrootcert=/x/ca.crt")
    assert both.stdout.strip() == "mode=verify-full root=/x/ca.crt", both.stderr
    inherited = run("postgresql+psycopg://u:p@h:5432/d", PGSSLMODE="verify-full", PGSSLROOTCERT="/y/ca.crt")
    assert inherited.stdout.strip() == "mode=verify-full root=/y/ca.crt", inherited.stderr


# ---------------------------------------------------------------------------------------------------------------------------- a real server

needs_postgres = pytest.mark.skipif(checker.find_pg_bin() is None, reason="no initdb/postgres on this machine (CI's compose-pgtls job runs the real container)")


@needs_postgres
def test_against_a_real_postgres_the_right_ca_connects_and_a_wrong_ca_or_name_or_plain_is_refused():
    """Against a real Postgres started for the test, a client with the right CA connects and one with another CA, the wrong host name or no TLS is
    refused (four checks). Needs the Postgres binaries.
    """
    results = checker.spawn_and_check()
    failed = [text for ok, text in results if not ok]
    assert not failed, failed
    assert len(results) == 4


# ---------------------------------------------------------------------------------------------------------------------------- the chart

def _render(*args: str) -> list[dict]:
    result = subprocess.run(["helm", "template", "smo", str(CHART), "-n", "smo", "--kube-version", "1.30.0", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _by(docs, kind, name=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]


TLS_ON = ("--set", "postgres.tls.enabled=true", "--set", "postgres.tls.certManager.enabled=true", "--set", "postgres.tls.certManager.issuerRef.name=smo-mtls-ca")


@helm
def test_chart_default_has_no_postgres_tls_and_no_url_parameter():
    """Rendered with default values the chart has no pg_hba ConfigMap, no certificate, no TLS argument to Postgres (only the TimescaleDB preload and its
    telemetry off, D8) and no TLS parameter in any URL. Needs helm.
    """
    docs = _render()
    assert not _by(docs, "ConfigMap", "postgres-hba") and not [d for d in docs if d["kind"] == "Certificate"]
    sts = _by(docs, "StatefulSet", "postgres")[0]
    assert sts["spec"]["template"]["spec"]["containers"][0]["args"] == ["-c", "shared_preload_libraries=timescaledb", "-c", "timescaledb.telemetry_level=off"]
    text = yaml.safe_dump_all(docs)
    assert "sslrootcert" not in text and "pg-tls" not in text


@helm
def test_chart_tls_on_postgres_serves_tls_only_with_a_certificate_from_the_issuer():
    """With TLS on, the Postgres StatefulSet starts with the TLS options, mounts the `postgres-tls` Secret group-readable with the matching
    fsGroup, and uses the same pg_hba rules as the compose overlay. Needs helm.
    """
    docs = _render(*TLS_ON)
    sts = _by(docs, "StatefulSet", "postgres")[0]["spec"]["template"]["spec"]
    args = " ".join(sts["containers"][0]["args"])
    for needed in ("ssl=on", "ssl_cert_file=/run/pg-tls/tls.crt", "ssl_key_file=/run/pg-tls/tls.key", "hba_file=/etc/smo/pg_hba.conf", "ssl_min_protocol_version=TLSv1.2"):
        assert needed in args
    volumes = {v["name"]: v for v in sts["volumes"]}
    assert volumes["pg-tls"]["secret"]["secretName"] == "postgres-tls" and volumes["pg-tls"]["secret"]["defaultMode"] == 0o440
    assert sts["securityContext"]["fsGroup"] == 70
    hba = _by(docs, "ConfigMap", "postgres-hba")[0]["data"]["pg_hba.conf"]
    assert _rules(hba) == _rules(HBA.read_text()), "the chart's hba rules and the compose overlay's must be the same"
    certificate = _by(docs, "Certificate", "postgres-tls")[0]["spec"]
    assert certificate["issuerRef"]["name"] == "smo-mtls-ca" and certificate["usages"] == ["digital signature", "server auth"]
    assert {"postgres", "postgres.smo.svc", "postgres.smo.svc.cluster.local"} <= set(certificate["dnsNames"])


@helm
def test_chart_tls_on_every_database_client_verifies_and_mounts_only_the_ca():
    """With TLS on, every container that opens a database connection has a URL that verifies the certificate against the mounted CA, and mounts
    only the CA, not the server key. Needs helm.
    """
    docs = _render(*TLS_ON)
    checked = 0
    for doc in _by(docs, "Deployment") + _by(docs, "Job"):
        pod = doc["spec"]["template"]["spec"]
        for container in (pod.get("initContainers") or []) + pod["containers"]:
            urls = [e["value"] for e in (container.get("env") or []) if e["name"] == "SMO_DATABASE_URL"]
            if not urls:
                continue
            checked += 1
            assert all(u.endswith("/smo?sslmode=verify-full&sslrootcert=/run/pg-tls/ca.crt") for u in urls), (doc["metadata"]["name"], urls)
            assert {"name": "pg-tls-ca", "mountPath": "/run/pg-tls", "readOnly": True} in container["volumeMounts"], doc["metadata"]["name"]
        if any(e["name"] == "SMO_DATABASE_URL" for c in pod["containers"] for e in (c.get("env") or [])):
            volume = {v["name"]: v for v in pod["volumes"]}["pg-tls-ca"]["secret"]
            assert volume["secretName"] == "postgres-tls" and volume["items"] == [{"key": "ca.crt", "path": "ca.crt"}], "the key of the server never reaches a client"
    assert checked >= 20


@helm
def test_chart_tls_with_cert_manager_needs_an_issuer_and_does_not_touch_an_external_postgres():
    """cert-manager mode without an issuer name fails to render, and with an external Postgres the chart sets up no TLS of its own and passes the
    external SSL mode through. Needs helm.
    """
    result = subprocess.run(["helm", "template", "smo", str(CHART), "-n", "smo", "--kube-version", "1.30.0", "--set", "postgres.tls.enabled=true", "--set", "postgres.tls.certManager.enabled=true"],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "issuerRef.name" in result.stderr
    docs = _render("--set", "postgres.tls.enabled=true", "--set", "postgres.enabled=false", "--set", "postgres.external.host=db.example.com", "--set", "postgres.external.sslmode=verify-full")
    text = yaml.safe_dump_all(docs)
    assert "pg-tls" not in text and "sslmode=verify-full" in text and not _by(docs, "StatefulSet", "postgres")
