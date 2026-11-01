"""Tests for smo_shared.objectstore (D10, plan task S1.10): the SigV4 signature against AWS's published example (and against botocore when it is
installed), the settings and their secret files, and the client's calls against an in-memory S3 behind httpx.MockTransport. The round trip against a
real SeaweedFS is the compose job "Object store profile round trip" (scripts/objectstore_roundtrip.sh)."""

import datetime
import hashlib
import re
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from smo_shared import objectstore as osx

NOW = datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC)
AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_the_signature_is_the_one_in_aws_documentation():
    """AWS's "GET Object" example of Signature Version 4 (header-based auth): the signature, credential scope and signed headers are AWS's own."""
    headers = osx.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"}, osx.EMPTY_SHA256, AWS_KEY, AWS_SECRET,
                       "us-east-1", NOW)
    assert headers["authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                                        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                                        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert headers["x-amz-date"] == "20130524T000000Z" and headers["host"] == "examplebucket.s3.amazonaws.com"


def test_a_put_with_metadata_and_a_list_query_sign_as_botocore_signs_them():
    """With botocore installed (not in the dev lock; the check is skipped without it), a PUT with metadata and a ListObjectsV2 query with an encoded
    prefix carry the same signature as botocore's S3SigV4Auth gives them."""
    botocore_auth = pytest.importorskip("botocore.auth")
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    body = b"hello world"
    cases = [("PUT", "http://objectstore:8333/smo-models/a%20b/m.onnx", {"content-type": "application/octet-stream", "x-amz-meta-sha256": "ab"}, body),
             ("GET", "http://objectstore:8333/smo-models?list-type=2&prefix=round%2Ftrip", {}, b"")]
    for method, url, headers, data in cases:
        payload = hashlib.sha256(data).hexdigest()
        ours = osx.sign(method, url, headers, payload, AWS_KEY, AWS_SECRET, "us-east-1", NOW)
        request = AWSRequest(method=method, url=url, data=data, headers={**headers, "x-amz-content-sha256": payload, "x-amz-date": "20130524T000000Z"})
        request.context["timestamp"] = "20130524T000000Z"                  # botocore's signing instant, so both sign the same moment
        signer = botocore_auth.S3SigV4Auth(Credentials(AWS_KEY, AWS_SECRET), "s3", "us-east-1")
        theirs = signer.signature(signer.string_to_sign(request, signer.canonical_request(request)), request)
        assert ours["authorization"].split("Signature=")[1] == theirs, url


def test_settings_come_from_the_environment_and_the_keys_from_files(tmp_path):
    """The endpoint and region from variables, both keys from their `_FILE` forms, the bucket names overridable per use; repr shows no key."""
    (tmp_path / "access").write_text("smo-access\n")
    (tmp_path / "secret").write_text("smo-secret\n")
    settings = osx.Settings.from_env({"SMO_OBJECTSTORE_URL": "http://objectstore:8333/", "SMO_OBJECTSTORE_ACCESS_KEY_FILE": str(tmp_path / "access"),
                                      "SMO_OBJECTSTORE_SECRET_KEY_FILE": str(tmp_path / "secret"), "SMO_OBJECTSTORE_BUCKET_CSARS": "pkgs"})
    assert (settings.endpoint, settings.access_key, settings.secret_key, settings.region) == ("http://objectstore:8333", "smo-access", "smo-secret", "us-east-1")
    assert dict(settings.buckets) == {"models": "smo-models", "csars": "pkgs", "cold-parquet": "smo-cold-parquet"}
    assert "smo-secret" not in repr(settings) and "smo-access" not in repr(settings)


@pytest.mark.parametrize("environ, missing", [
    ({}, "SMO_OBJECTSTORE_URL"),
    ({"SMO_OBJECTSTORE_URL": "http://s3:1"}, "SMO_OBJECTSTORE_ACCESS_KEY"),
    ({"SMO_OBJECTSTORE_URL": "http://s3:1", "SMO_OBJECTSTORE_ACCESS_KEY": "a"}, "SMO_OBJECTSTORE_SECRET_KEY"),
    ({"SMO_OBJECTSTORE_URL": "ftp://s3:1", "SMO_OBJECTSTORE_ACCESS_KEY": "a", "SMO_OBJECTSTORE_SECRET_KEY": "b"}, "http or https"),
    ({"SMO_OBJECTSTORE_URL": "http://s3:1", "SMO_OBJECTSTORE_ACCESS_KEY": "a", "SMO_OBJECTSTORE_SECRET_KEY": "b", "SMO_OBJECTSTORE_TIMEOUT_SECONDS": "x"},
     "SMO_OBJECTSTORE_TIMEOUT_SECONDS"),
])
def test_a_missing_or_bad_setting_is_named(environ, missing):
    """Each required setting missing, a non-HTTP endpoint and a bad timeout are refused with the variable named."""
    with pytest.raises(ValueError, match=missing):
        osx.Settings.from_env(environ)


class FakeS3:
    """An in-memory S3 behind httpx.MockTransport: buckets, objects with their metadata, ListObjectsV2 paged two keys at a time. It checks that every
    request is signed with the test keys and that a PUT body hashes to its x-amz-content-sha256, as S3 does."""

    def __init__(self, corrupt: bool = False):
        self.buckets: dict[str, dict[str, tuple[bytes, dict]]] = {}
        self.corrupt = corrupt
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=key/")
        body = request.content
        assert hashlib.sha256(body).hexdigest() == request.headers["x-amz-content-sha256"]
        parts = urlsplit(str(request.url))
        bucket, _, key = unquote(parts.path).lstrip("/").partition("/")
        self.calls.append(f"{request.method} {bucket}/{key}" + (f"?{parts.query}" if parts.query else ""))
        if not key:
            if request.method == "PUT":
                if bucket in self.buckets:
                    return httpx.Response(409, content=b"<Error><Code>BucketAlreadyOwnedByYou</Code></Error>")
                self.buckets[bucket] = {}
                return httpx.Response(200)
            return self._list(bucket, dict(p.split("=", 1) for p in parts.query.split("&")))
        objects = self.buckets.setdefault(bucket, {})
        if request.method == "PUT":
            objects[key] = (body, {"x-amz-meta-sha256": request.headers["x-amz-meta-sha256"]})
            return httpx.Response(200)
        if key not in objects:
            return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
        data, meta = objects[key]
        if request.method == "DELETE":
            del objects[key]
            return httpx.Response(204)
        data = data[:-1] + b"X" if self.corrupt else data
        return httpx.Response(200, content=b"" if request.method == "HEAD" else data, headers={**meta, "content-length": str(len(data)), "etag": '"e"'})

    def _list(self, bucket: str, query: dict) -> httpx.Response:
        keys = sorted(k for k in self.buckets.get(bucket, {}) if k.startswith(unquote(query.get("prefix", ""))))
        start = int(unquote(query.get("continuation-token", "0")))
        page, more = keys[start:start + 2], start + 2 < len(keys)
        xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in page)
        token = f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else ""
        return httpx.Response(200, content=(f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>{str(more).lower()}'
                                            f"</IsTruncated>{xml}{token}</ListBucketResult>").encode())


def _store(fake: FakeS3) -> osx.ObjectStore:
    settings = osx.Settings(endpoint="http://objectstore:8333", access_key="key", secret_key="secret")
    return osx.ObjectStore(settings, client=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: NOW)


def test_put_get_head_list_delete_by_use_with_the_sha256():
    """A put stores the SHA-256 as metadata and returns it; get returns the bytes; head gives size and SHA-256; list pages; delete removes; head of
    a missing key is None."""
    fake = FakeS3()
    store = _store(fake)
    assert store.ensure_buckets() == ["smo-models", "smo-csars", "smo-cold-parquet"]
    assert store.ensure_buckets() == ["smo-models", "smo-csars", "smo-cold-parquet"]            # a second run: 409 BucketAlreadyOwnedByYou is fine
    digest = store.put("models", "m/one.onnx", b"onnx bytes")
    assert digest == hashlib.sha256(b"onnx bytes").hexdigest()
    assert store.get("models", "m/one.onnx") == b"onnx bytes"
    assert store.head("models", "m/one.onnx") == {"size": 10, "sha256": digest, "etag": "e"}
    for name in ("m/two", "m/three", "other"):
        store.put("models", name, b"x")
    assert store.list_keys("models", "m/") == ["m/one.onnx", "m/three", "m/two"]
    assert sum(c.startswith("GET smo-models/?list-type=2") for c in fake.calls) == 2
    store.delete("models", "m/one.onnx")
    assert store.head("models", "m/one.onnx") is None


def test_a_corrupt_object_raises_and_a_missing_one_is_an_error_without_its_key():
    """Bytes that do not hash to the stored SHA-256 raise IntegrityError; a GET of a missing key raises ObjectStoreError with the status and S3's
    code but not the key."""
    store = _store(FakeS3(corrupt=True))
    store.put("csars", "pkg.csar", b"package")
    with pytest.raises(osx.IntegrityError):
        store.get("csars", "pkg.csar")
    with pytest.raises(osx.ObjectStoreError, match="404 NoSuchKey") as error:
        store.get("csars", "secret-name.csar")
    assert "secret-name" not in str(error.value)


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "./a", "a\nb"])
def test_a_key_outside_the_bucket_or_with_a_control_character_is_refused(key):
    """Empty keys, absolute keys, `.` / `..` segments and control characters are refused before any request; so is an unknown use."""
    store = _store(FakeS3())
    with pytest.raises(ValueError):
        store.put("models", key, b"x")
    with pytest.raises(ValueError, match="unknown object store use"):
        store.bucket("logs")


def test_the_roundtrip_command_proves_each_bucket_and_cleans_up(monkeypatch, capsys):
    """`roundtrip` puts, reads back, heads, lists and deletes an object in each of the three buckets and prints the SHA-256; a bad argument is
    usage (exit 2)."""
    fake = FakeS3()
    real = osx.ObjectStore
    monkeypatch.setattr(osx, "ObjectStore", lambda settings: real(settings, client=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: NOW))
    monkeypatch.setenv("SMO_OBJECTSTORE_URL", "http://objectstore:8333")
    monkeypatch.setenv("SMO_OBJECTSTORE_ACCESS_KEY", "key")
    monkeypatch.setenv("SMO_OBJECTSTORE_SECRET_KEY", "secret")
    assert osx.main(["roundtrip"]) == 0
    out = capsys.readouterr().out
    assert len(re.findall(r"sha256 [0-9a-f]{64} both ways", out)) == 3
    assert all(not objects for objects in fake.buckets.values())
    assert osx.main(["ensure-buckets"]) == 0 and "smo-models, smo-csars, smo-cold-parquet" in capsys.readouterr().out
    assert osx.main(["nope"]) == 2


def test_the_commands_wait_for_a_store_that_is_still_starting_but_not_for_a_refusal():
    """Two connection errors, then the store answers: `wait_for` tries again (sleeping 2 s each time) and returns the buckets; a 403 is raised at once."""
    fake, refused = FakeS3(), {"n": 0}

    def transport(request):
        if refused["n"] < 2:
            refused["n"] += 1
            raise httpx.ConnectError("connection refused")
        return fake(request)
    store = osx.ObjectStore(osx.Settings(endpoint="http://s3:1", access_key="key", secret_key="secret"),
                            client=httpx.Client(transport=httpx.MockTransport(transport)), clock=lambda: NOW)
    pauses = []
    assert osx.wait_for(store, 60, sleep=pauses.append) == ["smo-models", "smo-csars", "smo-cold-parquet"] and pauses == [2, 2]
    denied = osx.ObjectStore(osx.Settings(endpoint="http://s3:1", access_key="key", secret_key="secret"),
                             client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, content=b"<Error><Code>SignatureDoesNotMatch</Code></Error>"))))
    with pytest.raises(osx.ObjectStoreError, match="403 SignatureDoesNotMatch"):
        osx.wait_for(denied, 60, sleep=pauses.append)
    with pytest.raises(httpx.ConnectError):
        osx.wait_for(osx.ObjectStore(store.settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))),
                     0, sleep=pauses.append)


def test_an_answer_with_a_dtd_is_refused_before_it_is_parsed():
    """A list answer that declares an entity (an expansion attack's shape) is refused, not parsed; S3 never sends a DTD."""
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><ListBucketResult><Contents><Key>&a;</Key></Contents></ListBucketResult>'
    store = osx.ObjectStore(osx.Settings(endpoint="http://s3:1", access_key="key", secret_key="secret"),
                            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=evil))), clock=lambda: NOW)
    with pytest.raises(osx.ObjectStoreError, match="DTD"):
        store.list_keys("models")


def test_a_streamed_put_and_read_hold_one_chunk_and_check_the_sha256():
    """`put_stream` sends chunks whose size and SHA-256 are known beforehand (signed, stored as metadata); `open_stream` reads the object back chunk by
    chunk, each no larger than the chunk size asked for; `get_stream` releases the connection at the end of its block."""
    fake = FakeS3()
    store = _store(fake)
    data = bytes(range(256)) * 40
    digest = hashlib.sha256(data).hexdigest()
    assert store.put_stream("models", "m/big.onnx", (data[i:i + 1000] for i in range(0, len(data), 1000)), len(data), digest) == digest
    assert fake.buckets["smo-models"]["m/big.onnx"] == (data, {"x-amz-meta-sha256": digest})
    download = store.open_stream("models", "m/big.onnx", chunk_size=4096)
    assert (download.size, download.sha256) == (len(data), digest)
    chunks = list(download.chunks)
    assert b"".join(chunks) == data and max(map(len, chunks)) <= 4096
    with store.get_stream("models", "m/big.onnx") as again:
        assert b"".join(again.chunks) == data


def test_a_streamed_put_that_does_not_match_its_sha256_or_size_is_abandoned():
    """Chunks that hash to another SHA-256, or add up to another size, raise IntegrityError and nothing is stored."""
    fake = FakeS3()
    store = _store(fake)
    with pytest.raises(osx.IntegrityError):
        store.put_stream("models", "m/x", [b"abc"], 3, hashlib.sha256(b"abd").hexdigest())
    with pytest.raises(osx.IntegrityError):
        store.put_stream("models", "m/x", [b"abc"], 4, hashlib.sha256(b"abc").hexdigest())
    assert "m/x" not in fake.buckets.get("smo-models", {})


def test_a_streamed_read_of_a_corrupt_object_raises_at_the_end_and_a_missing_one_at_once():
    """Bytes that do not hash to the stored SHA-256 raise IntegrityError after the last chunk; a missing key raises ObjectStoreError (404) when opened."""
    store = _store(FakeS3(corrupt=True))
    store.put("models", "m/c", b"payload")
    download = store.open_stream("models", "m/c")
    with pytest.raises(osx.IntegrityError):
        list(download.chunks)
    with pytest.raises(osx.ObjectStoreError, match="404 NoSuchKey"):
        store.open_stream("models", "m/absent")


@pytest.fixture(params=["memory", "file"])
def local(request, tmp_path):
    """A LocalStore of each kind, its buckets made; the memory one starts empty."""
    osx._MEMORY.clear()
    store = osx.LocalStore(None if request.param == "memory" else tmp_path)
    store.ensure_buckets()
    yield store
    osx._MEMORY.clear()


def test_a_local_store_has_the_s3_clients_methods(local):
    """put, get, head, list, delete, the streamed forms and the round trip behave as the S3 client's; a missing key is a 404 and an unknown use refused."""
    digest = local.put("models", "m/one.onnx", b"onnx bytes")
    assert digest == hashlib.sha256(b"onnx bytes").hexdigest() and local.get("models", "m/one.onnx") == b"onnx bytes"
    assert local.head("models", "m/one.onnx") == {"size": 10, "sha256": digest, "etag": digest}
    data = b"z" * 5000
    local.put_stream("models", "m/two", (data[i:i + 1024] for i in range(0, 5000, 1024)), 5000, hashlib.sha256(data).hexdigest())
    with local.get_stream("models", "m/two", chunk_size=1024) as download:
        chunks = list(download.chunks)
    assert b"".join(chunks) == data and max(map(len, chunks)) == 1024
    assert local.list_keys("models", "m/") == ["m/one.onnx", "m/two"] and local.list_keys("models", "x") == []
    local.delete("models", "m/one.onnx")
    local.delete("models", "m/one.onnx")
    assert local.head("models", "m/one.onnx") is None
    with pytest.raises(osx.ObjectStoreError, match="404"):
        local.get("models", "m/one.onnx")
    with pytest.raises(ValueError):
        local.bucket("logs")
    with pytest.raises(ValueError):
        local.put("models", "../escape", b"x")
    assert [row["use"] for row in osx.roundtrip(local, size=64)] == ["models", "csars", "cold-parquet"]


def test_a_local_store_refuses_a_put_that_does_not_match_and_a_file_changed_on_disk(local, tmp_path):
    """A streamed put whose bytes do not match its SHA-256 stores nothing; in a directory, a file whose bytes changed after it was stored raises
    IntegrityError when read."""
    with pytest.raises(osx.IntegrityError):
        local.put_stream("models", "m/bad", [b"abc"], 3, osx.EMPTY_SHA256)
    assert local.head("models", "m/bad") is None and local.list_keys("models") == []
    if local.root is not None:
        local.put("models", "m/file", b"original")
        (tmp_path / "smo-models" / "m" / "file").write_bytes(b"tampered")
        with pytest.raises(osx.IntegrityError):
            local.get("models", "m/file")


def test_open_store_chooses_the_backend_from_the_settings(tmp_path):
    """memory:// and file:///dir are LocalStores (memory shared by every instance of the process); no URL is memory only with the test opt-in; an
    http URL is the S3 client; a relative file:// path and no URL without the opt-in are refused."""
    osx._MEMORY.clear()
    first, second = osx.open_store({"SMO_OBJECTSTORE_URL": "memory://"}), osx.open_store({"SMO_ALLOW_SQLITE_FALLBACK": "1"})
    assert isinstance(first, osx.LocalStore) and first.root is None and second.root is None
    first.put("models", "k", b"shared")
    assert second.get("models", "k") == b"shared"
    on_disk = osx.open_store({"SMO_OBJECTSTORE_URL": f"file://{tmp_path}", "SMO_OBJECTSTORE_BUCKET_MODELS": "mine"})
    assert on_disk.root == tmp_path and on_disk.bucket("models") == "mine"
    s3 = osx.open_store({"SMO_OBJECTSTORE_URL": "http://s3:1", "SMO_OBJECTSTORE_ACCESS_KEY": "a", "SMO_OBJECTSTORE_SECRET_KEY": "b"})
    assert isinstance(s3, osx.ObjectStore) and s3.uses() == ["models", "csars", "cold-parquet"]
    with pytest.raises(ValueError, match="absolute"):
        osx.open_store({"SMO_OBJECTSTORE_URL": "file://relative/dir"})
    with pytest.raises(ValueError, match="SMO_OBJECTSTORE_URL"):
        osx.open_store({"SMO_ALLOW_SQLITE_FALLBACK": "0"})
    osx._MEMORY.clear()
