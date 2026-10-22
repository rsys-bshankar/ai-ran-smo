"""Package signatures at onboarding (PR-RAPP-1.3 to 1.5): a signed package is verified against the trust store, and a signed package can be required.

Only the network fetch and the NFO call are faked; the CSAR bytes, the signature and the trust-store directory named by the environment are real. Fixtures: `client` (the app
over an in-memory SQLite), `publisher` (a trust store with one publisher, `acme`, and its private key, set through ONBOARDING_TRUST_STORE) and the autouse `nfo` (fakes NFO and
clears the two policy variables). Run: `cd smo/onboarding && PYTHONPATH=.:../shared python -m pytest tests/test_signing.py -q`.
"""

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Table, Uuid, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import csar_signing as cs
from smo_shared.db import Base, get_session

from app.main import app
from app.models import ApplicationPackage, Artifact, PackageUsageRegistration

FILES = {
    "TOSCA-Metadata/TOSCA.meta": b"Entry-Definitions: Definitions/main.yaml\n",
    "Definitions/main.yaml": b"application_name: Signed_rApp\napplication_version: 1.0.0\nprovider: Acme\n",
    "manifest.yaml": b"rappManifest: {manifestVersion: '1.0'}\n",
}


class Fetched:
    """The fake of a fetched package: just `content` and a `raise_for_status` that does nothing."""
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class NfoCreated:
    """The fake of NFO's 201 answer to CreateDescriptor, with a fresh descriptor id."""
    status_code = 201

    def json(self):
        return {"nfDeploymentDescriptorId": str(uuid.uuid4())}


def zipped(files) -> bytes:
    """Zips `files` (name -> bytes) into CSAR bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def client():
    """A TestClient of the app over an in-memory SQLite (one shared connection) with the three Onboarding tables; the dependency override is removed after the test.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if "nf_deployment_descriptor" not in Base.metadata.tables:
        Table("nf_deployment_descriptor", Base.metadata, Column("nf_deployment_descriptor_id", Uuid, primary_key=True))
    Base.metadata.create_all(engine, tables=[ApplicationPackage.__table__, Artifact.__table__, PackageUsageRegistration.__table__])
    factory = sessionmaker(bind=engine)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    """A trust store directory with one publisher, `acme`, and that publisher's private key."""
    private_pem, public_pem = cs.generate_keypair()
    store = tmp_path / "trust"
    store.mkdir()
    (store / "acme.pub").write_bytes(public_pem)
    monkeypatch.setenv("ONBOARDING_TRUST_STORE", str(store))
    return cs.load_private_key(private_pem)


@pytest.fixture(autouse=True)
def nfo(monkeypatch):
    """Autouse: fakes the NFO call to answer 201 and removes the two signing environment variables, so each test starts with no policy."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: NfoCreated())
    monkeypatch.delenv("ONBOARDING_TRUST_STORE", raising=False)
    monkeypatch.delenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", raising=False)


def onboard(client, monkeypatch, content: bytes) -> tuple[dict, dict]:
    """Fakes the fetch of `content`, posts the package and returns (the POST answer, the onboarding-status body)."""
    monkeypatch.setattr("app.main.httpx.get", lambda location, timeout=None, **kw: Fetched(content))
    answer = client.post("/packages", json={"location": "http://example/pkg.csar"}).json()
    return answer, client.get(f"/packages/{answer['packageId']}/onboarding-status").json()


def package_view(client, package_id) -> dict:
    return next(p for p in client.get("/packages").json()["items"] if p["packageId"] == package_id)


def tampered(data: bytes, change) -> bytes:
    """Re-zips `data` after `change(files)` has altered the dict of its files, to simulate a package changed after signing."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {i.filename: z.read(i.filename) for i in z.infolist()}
    change(files)
    return zipped(files)


# ------------------------------------------------------------------------------------------------------------------------ nothing set

def test_with_nothing_set_a_signed_or_unsigned_package_onboards_as_before_and_is_marked_validated(client, monkeypatch):
    """With no trust store and no requirement, signed and unsigned packages both onboard and signatureVerified is true (it then only means 'validated').
    """
    key = cs.load_private_key(cs.generate_keypair()[0])
    for content in (zipped(FILES), cs.sign_csar(zipped(FILES), key)):
        answer, status = onboard(client, monkeypatch, content)
        assert status["state"] == "AVAILABLE" and "failureReason" not in answer
        assert package_view(client, answer["packageId"])["signatureVerified"] is True          # the legacy meaning: validated, nothing checked against a key


def test_a_policy_flag_alone_with_no_trust_store_refuses_every_package_and_says_why(client, monkeypatch):
    """Requiring signatures with no trust store refuses every package with the reason: a misconfiguration must not mean 'accept all'."""
    monkeypatch.setenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", "true")
    answer, status = onboard(client, monkeypatch, zipped(FILES))
    assert status["state"] == "FAILED" and "no trusted publisher keys are configured" in answer["failureReason"]


# --------------------------------------------------------------------------------------------------------------------- with a trust store

def test_a_package_signed_by_a_trusted_publisher_onboards_and_is_verified(client, monkeypatch, publisher):
    """A package signed by a publisher in the trust store onboards and signatureVerified is true."""
    answer, status = onboard(client, monkeypatch, cs.sign_csar(zipped(FILES), publisher))
    assert status["state"] == "AVAILABLE"
    assert package_view(client, answer["packageId"])["signatureVerified"] is True


def test_an_unsigned_package_is_accepted_but_not_marked_verified_when_the_policy_is_off(client, monkeypatch, publisher):
    """With a trust store but signing not required, an unsigned package onboards with signatureVerified false."""
    answer, status = onboard(client, monkeypatch, zipped(FILES))
    assert status["state"] == "AVAILABLE"
    assert package_view(client, answer["packageId"])["signatureVerified"] is False


def test_an_unsigned_package_is_refused_when_signing_is_required(client, monkeypatch, publisher):
    """With signing required, an unsigned package ends FAILED with 'package signature: the package is not signed'."""
    monkeypatch.setenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", "true")
    answer, status = onboard(client, monkeypatch, zipped(FILES))
    assert status["state"] == "FAILED" and answer["failureReason"].startswith("package signature: the package is not signed")


def test_a_signed_package_is_still_accepted_when_signing_is_required(client, monkeypatch, publisher):
    """A signed package passes when signing is required; the setting is read case-insensitively (`TRUE`)."""
    monkeypatch.setenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", "TRUE")
    assert onboard(client, monkeypatch, cs.sign_csar(zipped(FILES), publisher))[1]["state"] == "AVAILABLE"


# Runs with signing required and not required. Each case changes the signed package after signing and names the message that failureReason must contain: a file
# changed, a file added, a file removed.
@pytest.mark.parametrize("require", ["false", "true"])
@pytest.mark.parametrize("change, message", [
    (lambda f: f.__setitem__("manifest.yaml", b"changed\n"), "manifest.yaml does not match its signed digest"),
    (lambda f: f.__setitem__("payload.py", b"print(1)\n"), "payload.py is not covered by the signed digest list"),
    (lambda f: f.pop("manifest.yaml"), "manifest.yaml is in the signed digest list but not in the package"),
], ids=["tampered", "added", "removed"])
def test_a_package_changed_after_signing_is_refused_whatever_the_policy(client, monkeypatch, publisher, require, change, message):
    """A changed, added or removed file fails the package with the rule broken, whether or not signing is required."""
    monkeypatch.setenv("ONBOARDING_REQUIRE_SIGNED_PACKAGES", require)
    answer, status = onboard(client, monkeypatch, tampered(cs.sign_csar(zipped(FILES), publisher), change))
    assert status["state"] == "FAILED"
    assert message in answer["failureReason"] and answer["failureReason"].startswith("package signature: ")


def test_a_package_signed_by_an_unknown_publisher_is_refused(client, monkeypatch, publisher):
    """A signature from a key that is not in the trust store is refused as an unknown publisher."""
    stranger = cs.load_private_key(cs.generate_keypair()[0])
    answer, status = onboard(client, monkeypatch, cs.sign_csar(zipped(FILES), stranger))
    assert status["state"] == "FAILED" and "unknown publisher" in answer["failureReason"]


def test_a_signature_made_with_the_wrong_key_under_a_trusted_key_id_is_refused(client, monkeypatch, publisher):
    """A signature that names a trusted publisher but was made with another key does not verify."""
    stranger = cs.load_private_key(cs.generate_keypair()[0])

    def forge(files):
        files[cs.SIGNATURE_FILE] = json.dumps({**json.loads(files[cs.SIGNATURE_FILE]),
                                               "signature": json.loads(cs.sign_digests(files[cs.DIGEST_FILE], stranger))["signature"]}).encode()
    answer, status = onboard(client, monkeypatch, tampered(cs.sign_csar(zipped(FILES), publisher), forge))
    assert status["state"] == "FAILED" and "publisher acme" in answer["failureReason"] and "does not verify" in answer["failureReason"]


def test_a_digest_list_without_a_signature_is_refused_even_when_the_policy_is_off(client, monkeypatch, publisher):
    """A package that carries a digest list but no signature is refused even when signing is not required."""
    answer, status = onboard(client, monkeypatch, tampered(cs.sign_csar(zipped(FILES), publisher), lambda f: f.pop(cs.SIGNATURE_FILE)))
    assert status["state"] == "FAILED" and "no signature" in answer["failureReason"]


def test_a_refused_signature_does_not_block_the_next_attempt(client, monkeypatch, publisher):
    """A refused package leaves no state behind: the correctly signed package onboards next."""
    bad = tampered(cs.sign_csar(zipped(FILES), publisher), lambda f: f.__setitem__("manifest.yaml", b"x"))
    assert onboard(client, monkeypatch, bad)[1]["state"] == "FAILED"
    assert onboard(client, monkeypatch, cs.sign_csar(zipped(FILES), publisher))[1]["state"] == "AVAILABLE"


def test_the_trust_store_is_read_for_each_package_so_a_new_key_needs_no_restart(client, monkeypatch, publisher, tmp_path):
    """A key added to the trust-store directory is honoured by the next package without a restart."""
    newcomer = cs.load_private_key(cs.generate_keypair()[0])
    signed = cs.sign_csar(zipped(FILES), newcomer)
    assert onboard(client, monkeypatch, signed)[1]["state"] == "FAILED"
    (tmp_path / "trust" / "newcomer.pub").write_bytes(cs.public_pem(newcomer.public_key()))
    assert onboard(client, monkeypatch, signed)[1]["state"] == "AVAILABLE"


# Two broken trust stores: a path that does not exist and an empty directory.
@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_a_trust_store_that_cannot_be_used_fails_the_package_with_a_reason_and_never_accepts(client, monkeypatch, tmp_path, kind):
    """An unusable trust store fails the package with a reason that does not contain the mounted path, and never falls back to accepting."""
    target = tmp_path / kind
    if kind == "empty":
        target.mkdir()
    monkeypatch.setenv("ONBOARDING_TRUST_STORE", str(target))
    key = cs.load_private_key(cs.generate_keypair()[0])
    answer, status = onboard(client, monkeypatch, cs.sign_csar(zipped(FILES), key))
    assert status["state"] == "FAILED" and answer["failureReason"].startswith("the trust store cannot be used")
    assert str(tmp_path) not in answer["failureReason"]                        # a name, not the path the operator mounted


def test_the_committed_sample_packages_verify_against_the_demo_publisher_key():
    """The four sample CSARs committed under samples/ are signed by the demo publisher whose public key is in samples/demo-signing."""
    samples = next(p / "samples" for p in Path(__file__).resolve().parents if (p / "samples" / "build_csar.py").is_file())      # not a fixed number of parents: the mutation job runs one level deeper
    trust = cs.load_trust_store(samples / "demo-signing")
    csars = sorted(samples.glob("*.csar"))
    assert len(csars) == 4
    for csar in csars:
        assert cs.verify_csar(csar.read_bytes(), trust).publisher == "demo-publisher", csar.name
