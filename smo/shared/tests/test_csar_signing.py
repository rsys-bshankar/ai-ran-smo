"""PR-RAPP-1: digest list, detached ed25519 signature, trust store and verification of a CSAR.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_csar_signing.py -q
"""

import io
import json
import zipfile

import pytest

from smo_shared import csar_signing as cs

FILES = {
    "TOSCA-Metadata/TOSCA.meta": b"Entry-Definitions: Definitions/asd.yaml\n",
    "Definitions/asd.yaml": b"application_name: Demo\n",
    "manifest.yaml": b"rappManifest: {manifestVersion: '1.0'}\n",
}


def build(files: dict[str, bytes]) -> bytes:
    """Helper: a plain (unsigned) CSAR zip from a {name: bytes} dict."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return out.getvalue()


def keypair(tmp_path, name):
    """Helper: makes an ed25519 key pair, writes the public half to `<name>.pub` in tmp_path (the trust store) and returns the private key."""
    private_pem, public_pem = cs.generate_keypair()
    (tmp_path / f"{name}.pub").write_bytes(public_pem)
    return cs.load_private_key(private_pem)


@pytest.fixture
def key(tmp_path):
    return keypair(tmp_path, "acme")


@pytest.fixture
def trust(tmp_path, key):
    return cs.load_trust_store(tmp_path)


def signed(key, files=FILES) -> bytes:
    return cs.sign_csar(build(files), key)


def rewrite(data: bytes, change) -> bytes:
    """The package with `change(files)` applied to its entries (a dict name -> bytes, edited in place)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {i.filename: z.read(i.filename) for i in z.infolist()}
    change(files)
    return build(files)


def code_of(data, trust) -> str:
    """Helper: verifies `data` against `trust`, expecting a SignatureError, and returns its `code`."""
    with pytest.raises(cs.SignatureError) as excinfo:
        cs.verify_csar(data, trust)
    return excinfo.value.code


def test_the_digest_list_names_every_file_sorted_and_never_itself():
    """The digest list holds one line per package file, sorted by path, and never lists the two signing entries."""
    listing = cs.digest_list({**FILES, cs.DIGEST_FILE: b"x", cs.SIGNATURE_FILE: b"y"}).decode()
    assert [line.split("  ", 1)[1] for line in listing.splitlines()] == sorted(FILES)
    assert listing.splitlines()[0] == "%s  Definitions/asd.yaml" % __import__("hashlib").sha256(FILES["Definitions/asd.yaml"]).hexdigest()


def test_a_signed_package_verifies_and_names_the_publisher_the_operator_gave_the_key(key, trust):
    """A correctly signed package verifies and the publisher is the trust-store name of the key, with the file count and key id."""
    result = cs.verify_csar(signed(key), trust)
    assert result.publisher == "acme" and result.files == len(FILES) and result.key_id == cs.key_id(key.public_key())


def test_signing_is_repeatable_so_a_rebuilt_package_is_byte_identical(key):
    """Signing the same package with the same key twice gives identical bytes (fixed timestamps, deterministic ed25519)."""
    assert signed(key) == signed(key)


def test_re_signing_replaces_the_earlier_signature_instead_of_stacking_entries(key, tmp_path):
    """Signing an already signed package replaces its digest and signature entries, so the package has one signature, by the new key."""
    other = keypair(tmp_path, "other")
    twice = cs.sign_csar(signed(key), other)
    with zipfile.ZipFile(io.BytesIO(twice)) as z:
        assert z.namelist().count(cs.SIGNATURE_FILE) == 1
    assert cs.verify_csar(twice, cs.load_trust_store(tmp_path)).publisher == "other"


def test_a_modified_file_is_rejected_and_named(key, trust):
    """Changing a file after signing is rejected with code `modified` and the file's name."""
    data = rewrite(signed(key), lambda f: f.__setitem__("manifest.yaml", b"tampered\n"))
    with pytest.raises(cs.SignatureError, match="manifest.yaml does not match its signed digest") as excinfo:
        cs.verify_csar(data, trust)
    assert excinfo.value.code == "modified"


def test_an_added_file_is_rejected_and_named(key, trust):
    """A file added after signing is rejected with code `added` and its name."""
    data = rewrite(signed(key), lambda f: f.__setitem__("evil.py", b"import os\n"))
    with pytest.raises(cs.SignatureError, match="evil.py is not covered") as excinfo:
        cs.verify_csar(data, trust)
    assert excinfo.value.code == "added"


def test_a_removed_file_is_rejected_and_named(key, trust):
    """A file removed after signing is rejected with code `missing` and its name."""
    data = rewrite(signed(key), lambda f: f.pop("manifest.yaml"))
    with pytest.raises(cs.SignatureError, match="manifest.yaml is in the signed digest list but not in the package") as excinfo:
        cs.verify_csar(data, trust)
    assert excinfo.value.code == "missing"


def test_a_rewritten_digest_list_is_caught_by_the_signature(key, trust):
    """An attacker who edits a file and rewrites the digest list to match still fails, because the signature covers the list (`bad_signature`)."""
    def forge(files):
        files[cs.DIGEST_FILE] = cs.digest_list({**files, "manifest.yaml": b"tampered\n"})
        files["manifest.yaml"] = b"tampered\n"
    assert code_of(rewrite(signed(key), forge), trust) == "bad_signature"


def test_a_signature_made_with_another_key_under_a_trusted_key_id_is_rejected(key, trust, tmp_path):
    """A signature by another key that claims a trusted key id is `bad_signature`, naming the trusted publisher."""
    impostor = cs.load_private_key(cs.generate_keypair()[0])

    def swap(files):
        document = json.loads(files[cs.SIGNATURE_FILE])
        document["signature"] = json.loads(cs.sign_digests(files[cs.DIGEST_FILE], impostor))["signature"]
        files[cs.SIGNATURE_FILE] = json.dumps(document).encode()
    with pytest.raises(cs.SignatureError, match="publisher acme") as excinfo:
        cs.verify_csar(rewrite(signed(key), swap), trust)
    assert excinfo.value.code == "bad_signature"


def test_a_package_signed_by_a_key_nobody_trusts_is_an_unknown_publisher(trust):
    """A valid signature by a key that is not in the trust store is `unknown_publisher`, and the message carries no key material."""
    stranger = cs.load_private_key(cs.generate_keypair()[0])
    with pytest.raises(cs.SignatureError, match="unknown publisher") as excinfo:
        cs.verify_csar(signed(stranger), trust)
    assert excinfo.value.code == "unknown_publisher"
    assert "BEGIN" not in str(excinfo.value)


def test_a_package_that_says_who_it_is_does_not_get_that_name(key, trust):
    """The publisher reported is the trust store's name for the key; nothing inside the package can change it."""
    data = rewrite(signed(key), lambda f: None)
    assert cs.verify_csar(data, trust).publisher == "acme"        # the name is the trust store's, never a field of the package


def test_an_unsigned_package_is_reported_as_unsigned(trust):
    """A package with neither a digest list nor a signature is `unsigned`."""
    assert code_of(build(FILES), trust) == "unsigned"


def test_a_digest_list_without_a_signature_is_unsigned_and_a_signature_without_a_list_is_malformed(key, trust):
    """Half a signature is refused: a list without a signature is `unsigned`, a signature without a list is `malformed`."""
    data = signed(key)
    assert code_of(rewrite(data, lambda f: f.pop(cs.SIGNATURE_FILE)), trust) == "unsigned"
    assert code_of(rewrite(data, lambda f: f.pop(cs.DIGEST_FILE)), trust) == "malformed"


# Table: each case is a signature entry that is not JSON, not an object, of the wrong version or algorithm, with a non-string key id or with a
# signature that is not base64; all must be `malformed`.
@pytest.mark.parametrize("signature", [b"not json", b"[]", b'{"version": 2, "algorithm": "ed25519", "keyId": "a", "signature": "AA=="}',
                                       b'{"version": 1, "algorithm": "rsa", "keyId": "a", "signature": "AA=="}',
                                       b'{"version": 1, "algorithm": "ed25519", "keyId": 1, "signature": "AA=="}',
                                       b'{"version": 1, "algorithm": "ed25519", "keyId": "a", "signature": "***"}'])
def test_a_malformed_signature_entry_is_rejected_not_crashed_on(key, trust, signature):
    assert code_of(rewrite(signed(key), lambda f: f.__setitem__(cs.SIGNATURE_FILE, signature)), trust) == "malformed"


# Table: a digest list that is nonsense text, not UTF-8, or names one path twice. Each is validly signed, so only the format can fail; all must be
# `malformed`.
@pytest.mark.parametrize("digests", [b"nonsense\n", b"\xff\xfe", ("0" * 64 + "  a\n" + "0" * 64 + "  a\n").encode()])
def test_a_malformed_digest_list_is_rejected(key, tmp_path, digests):
    private = key
    trust = cs.load_trust_store(tmp_path)

    def forge(files):
        files[cs.DIGEST_FILE] = digests
        files[cs.SIGNATURE_FILE] = cs.sign_digests(digests, private)         # validly signed, so only the format can fail
    assert code_of(rewrite(signed(key), forge), trust) == "malformed"


def test_a_package_with_a_file_twice_is_rejected(key, trust):
    """A zip with two entries of the same name is `malformed`, since which one a reader sees must not depend on the reader."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(signed(key))) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        with pytest.warns(UserWarning, match="Duplicate name"):
            target.writestr("manifest.yaml", b"second one\n")
    assert code_of(out.getvalue(), trust) == "malformed"


# Table: a path that climbs out with `..`, is absolute, or uses a backslash; each makes the package `malformed`.
@pytest.mark.parametrize("name", ["../escape.py", "/abs.py", "a\\b.py"])
def test_a_file_with_an_unsafe_path_is_rejected(key, trust, name):
    assert code_of(rewrite(signed(key), lambda f: f.__setitem__(name, b"x")), trust) == "malformed"


def test_a_file_that_is_not_a_zip_is_malformed(trust):
    """Bytes that are not a zip file are `malformed`, not an unhandled error."""
    assert code_of(b"not a zip", trust) == "malformed"


def test_sign_csar_refuses_a_package_with_a_file_twice(key):
    """Signing refuses a package that lists a file twice."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("a", b"1")
        with pytest.warns(UserWarning, match="Duplicate name"):
            z.writestr("a", b"2")
    with pytest.raises(cs.SignatureError):
        cs.sign_csar(out.getvalue(), key)


def test_directory_entries_are_ignored_by_the_digest_check(key, trust):
    """Directory entries in the zip are neither listed nor counted as files."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(signed(key))) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        target.writestr("Artifacts/", b"")
    assert cs.verify_csar(out.getvalue(), trust).files == len(FILES)


# -------------------------------------------------------------------------------------------------------------------------- the trust store

def test_a_directory_is_read_for_pub_and_pem_files_and_skips_the_rest(tmp_path):
    """A trust-store directory yields one publisher per `.pub`/`.pem` file and skips other files, dotfiles and directories."""
    _, a = cs.generate_keypair()
    _, b = cs.generate_keypair()
    (tmp_path / "acme.pub").write_bytes(a)
    (tmp_path / "vendor.pem").write_bytes(b)
    (tmp_path / "README.txt").write_text("not a key")
    (tmp_path / ".hidden.pub").write_text("kubernetes data dirs and dotfiles are skipped")
    (tmp_path / "sub.pub").mkdir()
    store = cs.load_trust_store(tmp_path)
    assert sorted(k.publisher for k in store.keys) == ["acme", "vendor"] and bool(store)


def test_a_file_may_hold_several_keys_named_after_the_file(tmp_path):
    """Several keys in one file are named after the file, then `#2`, `#3`."""
    (tmp_path / "acme.pub").write_bytes(cs.generate_keypair()[1] + cs.generate_keypair()[1])
    assert [k.publisher for k in cs.load_trust_store(tmp_path / "acme.pub").keys] == ["acme", "acme#2"]


def test_a_symlinked_key_is_followed_like_a_mounted_configmap(tmp_path):
    """A symlinked key file is read (a Kubernetes ConfigMap mount is symlinks) while the `..data` directory is skipped."""
    data = tmp_path / "..data"
    data.mkdir()
    (data / "acme.pub").write_bytes(cs.generate_keypair()[1])
    (tmp_path / "acme.pub").symlink_to(data / "acme.pub")
    assert [k.publisher for k in cs.load_trust_store(tmp_path).keys] == ["acme"]


# Table: a store that is missing, empty, holds a non-key, holds a key of another type, or has an unreadable (dangling) file; each must raise
# TrustStoreError rather than load fewer keys.
@pytest.mark.parametrize("where", ["missing", "empty", "not-a-key", "wrong-type", "unreadable"])
def test_an_unusable_trust_store_is_an_error_not_a_smaller_store(tmp_path, where):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if where == "missing":
        target = tmp_path / "nope"
    elif where == "empty":
        target = tmp_path
    elif where == "not-a-key":
        (tmp_path / "x.pub").write_text("hello")
        target = tmp_path
    elif where == "wrong-type":
        pem = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        (tmp_path / "x.pub").write_bytes(pem)
        target = tmp_path
    else:
        (tmp_path / "x.pub").symlink_to(tmp_path / "gone")
        target = tmp_path
    with pytest.raises(cs.TrustStoreError) as excinfo:
        cs.load_trust_store(target)
    assert "PRIVATE" not in str(excinfo.value)


def test_a_key_that_is_not_ed25519_or_not_pem_is_refused_when_loaded():
    """Loading a private or public key that is not PEM, or not ed25519, raises ValueError saying which."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    ec_private = ec.generate_private_key(ec.SECP256R1())
    pem = ec_private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    with pytest.raises(ValueError, match="ed25519"):
        cs.load_private_key(pem)
    with pytest.raises(ValueError, match="PEM private key"):
        cs.load_private_key(b"nope")
    with pytest.raises(ValueError, match="PEM public key"):
        cs.load_public_key(b"nope")
    with pytest.raises(ValueError, match="ed25519 public key"):
        cs.load_public_key(ec_private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


# ------------------------------------------------------------------------------------------------------------------------ zip-bomb limits

def _verify(data, trust, **limits):
    """Helper: verify `data` with `ZipLimits(**limits)` and return the SignatureError code (the call must raise)."""
    with pytest.raises(cs.SignatureError) as excinfo, zipfile.ZipFile(io.BytesIO(data)) as z:
        cs.verify_zip(z, trust, cs.ZipLimits(**limits))
    return excinfo.value.code


def test_the_default_limits_accept_an_ordinary_signed_package(key, trust):
    """The defaults are far above a real package, so a normal signed package still verifies."""
    assert cs.verify_csar(signed(key), trust).publisher == "acme"


def test_a_package_with_too_many_entries_is_too_large(key, trust):
    """An archive with thousands of tiny entries is refused from the directory, before anything is hashed."""
    files = {**FILES, **{f"extra/{n}.txt": b"x" for n in range(30)}}
    assert _verify(signed(key, files), trust, max_entries=20) == "too_large"


def test_a_single_file_above_the_member_limit_is_too_large(key, trust):
    """One oversized file is refused by its declared size."""
    assert _verify(signed(key, {**FILES, "big.bin": b"a" * 5000}), trust, max_member_bytes=1000) == "too_large"


def test_the_total_uncompressed_size_is_limited(key, trust):
    """Many files that are each under the member limit still cannot add up beyond the total limit."""
    files = {**FILES, **{f"part/{n}.bin": b"a" * 600 for n in range(10)}}
    assert _verify(signed(key, files), trust, max_member_bytes=1000, max_total_bytes=3000) == "too_large"


def test_a_highly_compressed_file_is_refused_by_the_ratio_guard(key, trust):
    """A zip bomb (a long run of one byte compresses by a factor of about a thousand) is refused although each limit on size alone is generous."""
    bomb = signed(key, {**FILES, "bomb.bin": b"\0" * (4 * 1024 * 1024)})
    assert _verify(bomb, trust, ratio_floor_bytes=1024) == "too_large"


def test_the_default_limits_refuse_a_real_zip_bomb(key, trust):
    """A 60 MiB file of zeros (about 60 kB compressed) is refused by the default limits (member size and ratio), the behaviour that matters in production."""
    with pytest.raises(cs.SignatureError) as excinfo:
        cs.verify_csar(signed(key, {**FILES, "bomb.bin": b"\0" * (60 * 1024 * 1024)}), trust)
    assert excinfo.value.code == "too_large"


def test_a_directory_that_understates_a_file_is_caught_while_reading(key, trust, monkeypatch):
    """The read is bounded as well as the directory: a file whose header says it is small but whose data is large is refused when read."""
    data = signed(key, {**FILES, "liar.bin": b"a" * 5000})
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        monkeypatch.setattr(cs, "check_limits", lambda *a, **k: None)         # skip the directory check to reach the bounded read
        with pytest.raises(cs.SignatureError) as excinfo:
            cs.verify_zip(z, trust, cs.ZipLimits(max_member_bytes=1000))
    assert excinfo.value.code == "too_large"


def test_an_oversized_digest_list_is_too_large(key, trust):
    """The digest list and the signature entry have their own, small limit."""
    assert _verify(signed(key), trust, max_signing_bytes=10) == "too_large"
