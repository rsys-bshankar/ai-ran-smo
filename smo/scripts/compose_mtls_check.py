"""PR-SEC-2.2 / 2.3: the mutual-TLS stack (docker-compose.mtls.yml) refuses what it should and answers what it should.

Runs INSIDE the r1-termination container (it has a certificate, the CA, and every module resolves from there), fed on stdin by the CI job:

    docker compose -f docker-compose.yml -f docker-compose.mtls.yml exec -T r1-termination python3 - < scripts/compose_mtls_check.py

Checks, for every service that serves mTLS:
  * a client with the right certificate gets /live over https (200);
  * a client that verifies the server but presents NO certificate is refused;
  * a client whose certificate comes from ANOTHER CA (a throwaway one made here) is refused;
  * plain http on the same port gets no answer from the application;
and, through the gateway: /bootstrap names https addresses, and an unauthenticated routed call is 401 (the token gate is still there; the certificate is
in addition to it, never instead of it). The services that are not mTLS servers (mock-o1-adaptor, gui-bff) still answer plain HTTP.
The runbook replay (tests_integration/test_demo_runbook.py with SMO_MTLS=on) is the proof that the calls between modules work; this is the proof of the refusals.
"""
import datetime as dt
import os
import ssl
import sys
import tempfile

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smo_shared import mtls

SERVERS = [
    "r1-termination", "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam", "nfo", "focom", "aimgf", "ran-analytics", "mdaf",
    "intent-service", "so-smos", "sa-smos", "energy-saving-rapp", "mobility-optimization-rapp", "coverage-optimization-rapp", "traffic-steering-rapp",
]
PLAIN = ["mock-o1-adaptor", "gui-bff"]

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    """Prints one `ok` / `FAIL` line and remembers a failure; the script goes on and lists every failure at the end."""
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def refused(url: str, **kwargs) -> tuple[bool, str]:
    """True when the call did not get an HTTP answer from the application: the handshake or the connection failed."""
    try:
        response = httpx.get(url, timeout=10, **kwargs)
    except httpx.HTTPError as exc:
        return True, type(exc).__name__
    except ssl.SSLError as exc:
        return True, type(exc).__name__
    return False, f"answered {response.status_code}"


def foreign_identity(directory: str) -> tuple[str, str]:
    """A certificate for `runbook` from a CA nobody here trusts."""
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "foreign CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "r1-termination")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False).sign(ca_key, hashes.SHA256()))
    cert_path, key_path = os.path.join(directory, "foreign.crt"), os.path.join(directory, "foreign.key")
    with open(cert_path, "wb") as handle:
        handle.write(leaf.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


check("this container runs with SMO_MTLS=on", mtls.enabled())
_cert, _key, ca_file = mtls.files()
good = mtls.client_context()

with tempfile.TemporaryDirectory() as scratch:
    foreign_cert, foreign_key = foreign_identity(scratch)
    for host in SERVERS:
        url = f"https://{host}:8000/live"
        try:
            ok = httpx.get(url, verify=good, timeout=10).status_code == 200
            detail = ""
        except httpx.HTTPError as exc:
            ok, detail = False, repr(exc)
        check(f"{host}: a client with a certificate from the CA is answered", ok, detail)

        no_cert = ssl.create_default_context(cafile=ca_file)                        # verifies the server, presents nothing
        was_refused, why = refused(url, verify=no_cert)
        check(f"{host}: a client with no certificate is refused", was_refused, why)

        wrong = ssl.create_default_context(cafile=ca_file)
        wrong.load_cert_chain(foreign_cert, foreign_key)                           # presents a certificate the CA did not sign
        was_refused, why = refused(url, verify=wrong)
        check(f"{host}: a client whose certificate is from another CA is refused", was_refused, why)

        was_refused, why = refused(f"http://{host}:8000/live")
        check(f"{host}: plain http gets no answer from the application", was_refused, why)

for host in PLAIN:
    try:
        httpx.get(f"http://{host}:8000/", timeout=10)
        check(f"{host}: still plain HTTP (not an mTLS server by design)", True)
    except httpx.HTTPError as exc:
        check(f"{host}: still plain HTTP (not an mTLS server by design)", False, repr(exc))

response = httpx.get("https://r1-termination:8000/bootstrap", verify=good, timeout=10)
check("/bootstrap -> 200 over mTLS", response.status_code == 200, response.status_code)
check("/bootstrap advertises https addresses", response.status_code == 200 and "https://sme:8000/" in response.text and "http://sme" not in response.text,
      response.text[:200])
response = httpx.get("https://r1-termination:8000/sme/health", verify=good, timeout=10)
check("a valid certificate does not replace the token: an unauthenticated routed call is 401", response.status_code in (401, 403), response.status_code)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all mTLS checks passed")
