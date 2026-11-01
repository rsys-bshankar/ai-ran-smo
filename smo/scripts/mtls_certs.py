#!/usr/bin/env python3
"""A development CA and one certificate per service, for mutual TLS between the services (PR-SEC-2.1, 2.5).

    scripts/mtls_certs.py init   [--dir DIR] [--days N] [--force]     a CA and a certificate for every service (and the `runbook` client)
    scripts/mtls_certs.py renew  [--dir DIR] [--days N]               new certificates from the same CA (leaf rotation)
    scripts/mtls_certs.py rotate-ca trust|issue|retire [--dir DIR]    replace the CA without a moment when two services disagree about it
    scripts/mtls_certs.py client NAME [--dir DIR] [--days N]          a client-only certificate (an operator, an external rApp, a probe)
    scripts/mtls_certs.py status [--dir DIR] [--warn-days N]          days left on every certificate and on the CA; exit 1 when one is under N (default 30)
    SMO_MTLS_NAMES="smo.example.test,10.0.0.5" scripts/mtls_certs.py init      extra names (DNS or IP) on every service certificate

DIR defaults to smo/certs/mtls (git-ignored). Layout:

    DIR/ca/ca.crt, ca.key          the signing CA (10 years). ca.key is mode 0600: it can make a certificate every service trusts. Keep it OFF the hosts that run the stack
    DIR/ca/next.crt, next.key      while a CA rotation is under way (rotate-ca trust ... issue)
    DIR/ca/previous.crt            the CA being retired (rotate-ca issue ... retire)
    DIR/<service>/tls.crt, tls.key, ca.crt     what a service mounts at /run/mtls (docker-compose.mtls.yml). ca.crt is the CA, or old and new CA during a rotation
    DIR/clients/<name>/...         the same three files for a client-only certificate
    DIR/postgres/tls.crt, tls.key, ca.crt      the database's server certificate (names: postgres, localhost, 127.0.0.1, ::1, SMO_MTLS_NAMES) and the CA bundle the clients
                                   verify it against (docker-compose.pgtls.yml, PR-SEC-2.4). Postgres refuses a key file that is group or world readable, so that overlay copies it

A service certificate is ECDSA P-256, valid 90 days by default, with the service's name, `localhost`, 127.0.0.1 and ::1 as subject alternative names and the
extended key usages serverAuth AND clientAuth: the one certificate is the service's server certificate and its client certificate. Names for Kubernetes
(`<service>.<namespace>.svc`, `.svc.cluster.local`) are added with SMO_MTLS_NAMES, or use the chart's cert-manager mode, which needs none of this.

The key files are mode 0644 inside a 0700 directory, so the unprivileged user (uid 10001) of a container that bind-mounts its own directory can read them
and no other account on this host can. Not for production: a deployment brings its own CA and its own issuance (cert-manager, Vault PKI, an enterprise CA),
mounted the same way (docs/ARCHITECTURE.md, mutual TLS between services; deploy/helm/smo/README.md).

Rotation, with no downtime (a server loads its certificate at start, so each step ends with a rolling restart; clients reread theirs when the file changes):
  leaf certificates only:   renew, then restart the services one at a time (or `docker compose up -d --force-recreate`, `kubectl rollout restart`)
  the CA:                   rotate-ca trust   (every ca.crt now holds old + new CA)   -> restart everything
                            rotate-ca issue   (new leaf certificates from the new CA) -> restart everything
                            rotate-ca retire  (ca.crt holds the new CA only)          -> restart everything
"""

import argparse
import datetime as dt
import ipaddress
import os
import shutil
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SMO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = SMO_ROOT / "certs" / "mtls"

# The compose services that take part (docker-compose.mtls.yml gives each one its directory). Not here: mock-o1-adaptor (a stand-in for a network function
# outside the SMO, plain HTTP), gui-bff and gui (the browser's side; the BFF is a client only, and has a certificate), the databases, the edge.
SERVERS = ("r1-termination", "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam", "nfo", "focom", "mlmr", "aimgf", "mllf", "ran-analytics", "mdaf", "echo",
           "intent-service", "so-smos", "sa-smos", "energy-saving-rapp", "mobility-optimization-rapp", "coverage-optimization-rapp", "traffic-steering-rapp")
CLIENT_ONLY = ("ran-nf-oam-worker", "mdaf-worker", "dme-worker", "rapp-mgmt-worker", "aimgf-worker", "energy-saving-rapp-worker", "mobility-optimization-rapp-worker",
               "coverage-optimization-rapp-worker", "traffic-steering-rapp-worker", "gui-bff", "runbook")   # no server port (a worker), a plain server (the BFF), or a test driver
SERVICES = SERVERS + CLIENT_ONLY
# A server certificate for the database (PR-SEC-2.4): docker-compose.pgtls.yml mounts DIR/postgres. It is not a service of the mTLS overlay (it is not in SERVERS or
# SERVICES, which the overlay and its tests enumerate): Postgres is verified by the clients (sslmode=verify-full against ca.crt), it does not ask them for a certificate.
DATABASES = ("postgres",)

CA_DAYS = 3650
LEAF_DAYS = 90
_SKEW = dt.timedelta(minutes=5)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def make_ca(days: int = CA_DAYS, common_name: str = "AI-RAN SMO development mTLS CA") -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A new self-signed ECDSA P-256 CA and its key: a CA certificate (path length 0, so it can sign leaves but no sub-CA) valid for `days` (default ten years).

        Backdated by five minutes (`_SKEW`) so a host whose clock is slightly behind still accepts it. Nothing is written; the caller stores the pair.
    """
    key = new_key()
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AI-RAN SMO development"), x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _now()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _SKEW).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    """A subject-alternative-name extension from `names`: an IP address becomes an IP entry, anything else a DNS entry. Duplicates are dropped, order is kept."""
    entries: list[x509.GeneralName] = []
    for name in dict.fromkeys(names):          # unique, in order
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def extra_names() -> list[str]:
    return [n.strip() for n in os.environ.get("SMO_MTLS_NAMES", "").split(",") if n.strip()]


def make_leaf(ca_cert: x509.Certificate, ca_key, name: str, days: int = LEAF_DAYS, server: bool = True) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A certificate for `name`. `server=False` makes a client-only one (no names, no serverAuth)."""
    key = new_key()
    now = _now()
    usages = [ExtendedKeyUsageOID.CLIENT_AUTH] + ([ExtendedKeyUsageOID.SERVER_AUTH] if server else [])
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AI-RAN SMO development"), x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - _SKEW).not_valid_after(now + dt.timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
                                            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False))
    if server:
        builder = builder.add_extension(_san([name, "localhost", "127.0.0.1", "::1", *extra_names()]), critical=False)
    else:
        builder = builder.add_extension(_san([name]), critical=False)       # the name is still the identity (CN and SAN agree)
    return builder.sign(ca_key, hashes.SHA256()), key


# ---------------------------------------------------------------------------------------------------------------------------- files

def _write(path: Path, data: bytes, mode: int) -> None:
    """Writes `data` to `path` with permissions `mode`, creating parent directories.

        Written to `<path>.tmp` and renamed over the target, so a service that rereads its certificate when the file changes never sees half a file. The explicit
        `chmod` after the write makes the mode exact whatever the process umask is.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)               # a reader never sees half a file


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _load_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _service_dirs(root: Path) -> list[Path]:
    """Every directory that holds a tls.crt: the services and the clients."""
    return sorted(p.parent for p in root.glob("*/tls.crt")) + sorted(p.parent for p in (root / "clients").glob("*/tls.crt"))


def _write_leaf(directory: Path, cert: x509.Certificate, key, ca_bundle: bytes) -> None:
    """Writes `tls.crt`, `tls.key` and `ca.crt` of one service or client into `directory`.

        The directory is 0755 and the files 0644, including the key, on purpose: a container's unprivileged user must read its own key, and the parent `DIR` is 0700 so no
        other account on the host can reach it. The CA key is never written here.
    """
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)  # noqa: S103 (a container's unprivileged user must traverse its own directory; the parent is 0700)
    _write(directory / "tls.crt", cert_pem(cert), 0o644)
    _write(directory / "tls.key", key_pem(key), 0o644)
    _write(directory / "ca.crt", ca_bundle, 0o644)


def _ca_bundle(root: Path) -> bytes:
    """What every ca.crt holds: the signing CA, plus the one being retired or introduced."""
    ca = root / "ca"
    parts = [(ca / "ca.crt").read_bytes()]
    for extra in ("next.crt", "previous.crt"):
        if (ca / extra).exists():
            parts.append((ca / extra).read_bytes())
    return b"".join(parts)


def init(root: Path, days: int, force: bool = False) -> str:
    """Creates the CA (`ca/ca.crt`, `ca/ca.key` mode 0600) and a certificate for every service, client and the database; returns a one-line message.

        Without `force` an existing CA is left alone and the message says `kept` (nothing is written). With `force` a new CA replaces it, any rotation files
        (`next.*`, `previous.crt`) are removed, and every certificate is new, so every service must restart. `days` is the validity of the leaf certificates only.
    """
    ca = root / "ca"
    if (ca / "ca.crt").exists() and not force:
        return f"kept: {root} (already has a CA; --force makes a new one and new certificates, `renew` keeps the CA)"
    _private_dir(root)
    _private_dir(ca)
    if force:
        for stale in ("next.crt", "next.key", "previous.crt"):
            (ca / stale).unlink(missing_ok=True)
    ca_cert, ca_key = make_ca()
    _write(ca / "ca.key", key_pem(ca_key), 0o600)
    _write(ca / "ca.crt", cert_pem(ca_cert), 0o644)
    for service in SERVICES + DATABASES:
        cert, key = make_leaf(ca_cert, ca_key, service, days, server=service in SERVERS or service in DATABASES)
        _write_leaf(root / service, cert, key, cert_pem(ca_cert))
    return f"created: {root}  (CA, {len(SERVICES)} service certificates and one for the database, {days} days)"


def renew(root: Path, days: int) -> str:
    """Issues new leaf certificates for every service and client found under `root`, from the CA already in use, and rewrites their `ca.crt` with the current bundle.

        The name and whether a certificate is a server one are read back from the old certificate (a server certificate is recognised by `localhost` among its DNS names),
        so a client-only certificate stays client-only. Servers load their certificate at start: restart them afterwards.
    """
    ca_cert, ca_key = _load_cert(root / "ca" / "ca.crt"), _load_key(root / "ca" / "ca.key")
    bundle = _ca_bundle(root)
    done = 0
    for directory in _service_dirs(root):
        name = _load_cert(directory / "tls.crt").subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        was_server = any(isinstance(e, x509.DNSName) and e.value == "localhost" for e in _load_cert(directory / "tls.crt").extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value)
        cert, key = make_leaf(ca_cert, ca_key, name, days, server=was_server)
        _write_leaf(directory, cert, key, bundle)
        done += 1
    return f"renewed: {done} certificates, {days} days, from the CA already in use"


def new_client(root: Path, name: str, days: int) -> str:
    """Issues a client-only certificate (no serverAuth, no extra names) for `name` under `clients/<name>`, signed by the current CA."""
    ca_cert, ca_key = _load_cert(root / "ca" / "ca.crt"), _load_key(root / "ca" / "ca.key")
    cert, key = make_leaf(ca_cert, ca_key, name, days, server=False)
    _write_leaf(root / "clients" / name, cert, key, _ca_bundle(root))
    return f"created: {root / 'clients' / name}  (client certificate for {name!r}, {days} days)"


def rotate_ca(root: Path, phase: str, days: int) -> str:
    """One phase of replacing the CA without a moment when two services disagree about it; returns a message saying what was done and what to do next.

        `trust` makes a second CA (`ca/next.*`) and puts old and new into every `ca.crt`. `issue` makes the new CA the signing one, keeps the old as `previous.crt`, and
        renews every leaf from the new CA (the bundles still hold both). `retire` deletes `previous.crt` so every `ca.crt` holds the new CA only. Each phase is followed
        by a restart of every service. A phase out of order changes nothing and the message says which phase to run first. Raises `SystemExit` for an unknown phase.
    """
    ca = root / "ca"
    if phase == "trust":
        if (ca / "next.crt").exists():
            return "a rotation is already under way (ca/next.crt exists): run `rotate-ca issue`, or delete ca/next.* to abandon it"
        cert, key = make_ca(common_name=f"AI-RAN SMO development mTLS CA {_now():%Y%m%d%H%M%S}")
        _write(ca / "next.key", key_pem(key), 0o600)
        _write(ca / "next.crt", cert_pem(cert), 0o644)
        bundle = _ca_bundle(root)
        for directory in _service_dirs(root):
            _write(directory / "ca.crt", bundle, 0o644)
        return "trust: every ca.crt now holds the old and the new CA; restart every service, then run `rotate-ca issue`"
    if phase == "issue":
        if not (ca / "next.crt").exists():
            return "nothing to issue from: run `rotate-ca trust` first"
        shutil.copyfile(ca / "ca.crt", ca / "previous.crt")
        os.replace(ca / "next.crt", ca / "ca.crt")
        os.replace(ca / "next.key", ca / "ca.key")
        os.chmod(ca / "ca.key", 0o600)
        message = renew(root, days)
        return f"issue: {message}; the CA bundle still holds both; restart every service, then run `rotate-ca retire`"
    if phase == "retire":
        if not (ca / "previous.crt").exists():
            return "nothing to retire: run `rotate-ca issue` first"
        (ca / "previous.crt").unlink()
        bundle = _ca_bundle(root)
        for directory in _service_dirs(root):
            _write(directory / "ca.crt", bundle, 0o644)
        return "retire: ca.crt holds the new CA only; restart every service"
    raise SystemExit(f"unknown phase {phase!r}")


def _not_after(certificate) -> dt.datetime:
    """The expiry as an aware UTC time: `not_valid_after_utc` from cryptography 42, the naive `not_valid_after` before it."""
    value = getattr(certificate, "not_valid_after_utc", None)
    return value if value is not None else certificate.not_valid_after.replace(tzinfo=dt.timezone.utc)


def status(root: Path, warn_days: int) -> tuple[str, bool]:
    """Days left on the CA and on every leaf certificate as `(report, any_under_warn_days)`; the CA line counts the earliest expiry in its file (old and new during a rotation)."""
    lines, bad = [], False
    now = _now()
    items = [("CA", root / "ca" / "ca.crt")] + [(d.name if d.parent == root else f"clients/{d.name}", d / "tls.crt") for d in _service_dirs(root)]
    for label, path in items:
        left = (min(_not_after(c) for c in x509.load_pem_x509_certificates(path.read_bytes())) - now).days
        flag = left < warn_days
        bad = bad or flag
        lines.append(f"{'EXPIRING' if flag else 'ok      '} {label:30s} {left:5d} days")
    return "\n".join(lines), bad


def main(argv: list[str] | None = None) -> int:
    """Command-line entry for `init`, `renew`, `client`, `rotate-ca` and `status`. Returns 2 when a command other than `init` finds no CA in the directory,
        1 when `status` finds a certificate under `--warn-days`, else 0.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "renew", "client"):
        p = sub.add_parser(name)
        p.add_argument("--dir", type=Path, default=argparse.SUPPRESS)
        p.add_argument("--days", type=int, default=LEAF_DAYS)
        if name == "init":
            p.add_argument("--force", action="store_true")
        if name == "client":
            p.add_argument("name")
    p = sub.add_parser("rotate-ca")
    p.add_argument("phase", choices=["trust", "issue", "retire"])
    p.add_argument("--dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--days", type=int, default=LEAF_DAYS)
    p = sub.add_parser("status")
    p.add_argument("--dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--warn-days", type=int, default=30)
    args = parser.parse_args(argv)
    root = args.dir
    if args.command != "init" and not (root / "ca" / "ca.crt").exists():
        print(f"no CA in {root}: run `{Path(sys.argv[0]).name} init` first", file=sys.stderr)
        return 2
    if args.command == "init":
        print(init(root, args.days, args.force))
    elif args.command == "renew":
        print(renew(root, args.days))
    elif args.command == "client":
        print(new_client(root, args.name, args.days))
    elif args.command == "rotate-ca":
        print(rotate_ca(root, args.phase, args.days))
    else:
        text, bad = status(root, args.warn_days)
        print(text)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
