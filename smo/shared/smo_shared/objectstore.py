"""S3 access for the SMO's object store (D10, plan task S1.10): one bucket per use, every object stored and read back with its SHA-256.

The store is SeaweedFS by default (compose profile `objectstore`) or any S3 API (MinIO, profile `minio`; a cloud bucket). AIMgF keeps its model
artifacts in it (S2.5); the CSAR store and the cold Parquet tier (S3.11) follow. Three uses, three buckets:

    models        model artifacts (ONNX, Safetensors; S2.5)      SMO_OBJECTSTORE_BUCKET_MODELS        default smo-models
    csars         rApp packages                                  SMO_OBJECTSTORE_BUCKET_CSARS         default smo-csars
    cold-parquet  telemetry older than the warm tier (S3.11)     SMO_OBJECTSTORE_BUCKET_COLD_PARQUET  default smo-cold-parquet

Why httpx and not boto3: httpx is already in every image (`requirements/runtime.txt`), the SMO needs six calls (create a bucket, put, get, head,
delete, list), and boto3 with botocore would add about 90 MB unpacked to every image for that. The one thing boto3 would bring that is hard to get
right is AWS Signature Version 4; `sign()` below implements it from the published algorithm with the standard library and is tested against the
example in AWS's documentation (shared/tests/test_objectstore.py). Not done here, on purpose: multipart upload (one PUT carries up to 5 GiB, a hundred
times the 50 MiB body limit of R1 Termination that every upload passes through), virtual-hosted bucket addressing (path style works on SeaweedFS,
MinIO and AWS), presigned URLs.

Streaming: `put_stream` sends a body from an iterable of chunks whose size and SHA-256 the caller already knows (S3 signs the payload's SHA-256, so it
is computed before the request: AIMgF hashes the upload as it spools to disk), and `open_stream` / `get_stream` read an object chunk by chunk (CHUNK,
1 MiB, at a time), hashing as they go. Neither holds more than one chunk of the object in memory.

Local backends, for tests and a single-process demo: `open_store()` returns this S3 client, or a `LocalStore` with the same methods when
`SMO_OBJECTSTORE_URL` is `memory://` (one process-wide dictionary) or `file:///a/directory` (objects under it, one file each). With no URL at all and
the unit tests' explicit opt-in `SMO_ALLOW_SQLITE_FALLBACK` (the in-memory database of `smo_shared.db`) switched on, it is `memory://`, so a test
process gets the in-memory store with the in-memory database; a production process without a URL still fails, naming the variable.

Integrity: `put` sends the body's SHA-256 as `x-amz-content-sha256` (the server refuses a body that does not match it) and stores it as the object's
metadata `x-amz-meta-sha256`; `get` hashes what it read and raises `IntegrityError` when it differs from that metadata.

Settings (environment; the keys by the `*_FILE` convention of `smo_shared.secretfile`, so compose and the chart mount them as files):
    SMO_OBJECTSTORE_URL                  the S3 endpoint, e.g. http://objectstore:8333 (required), or memory:// or file:///dir for a LocalStore
    SMO_OBJECTSTORE_REGION               the signing region (default us-east-1; SeaweedFS and MinIO accept any)
    SMO_OBJECTSTORE_ACCESS_KEY(_FILE)    the access key id (required)
    SMO_OBJECTSTORE_SECRET_KEY(_FILE)    the secret access key (required)
    SMO_OBJECTSTORE_CA_FILE              a CA bundle for an https endpoint (unset: the system's)
    SMO_OBJECTSTORE_TIMEOUT_SECONDS      per request (default 60)
    SMO_OBJECTSTORE_WAIT_SECONDS         how long the two commands below wait for a store that is still starting (default 60)

    python -m smo_shared.objectstore ensure-buckets      create the three buckets if missing (compose's objectstore-init runs this)
    python -m smo_shared.objectstore roundtrip           put an object in each bucket, read it back, compare the SHA-256, delete it (the CI job)
"""

import contextlib
import datetime
import hashlib
import json
import hmac
import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from smo_shared.secretfile import read_secret

USES = {"models": ("SMO_OBJECTSTORE_BUCKET_MODELS", "smo-models"), "csars": ("SMO_OBJECTSTORE_BUCKET_CSARS", "smo-csars"),
        "cold-parquet": ("SMO_OBJECTSTORE_BUCKET_COLD_PARQUET", "smo-cold-parquet")}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1 << 20                     # the most of an object `put_stream` and `open_stream` hold in memory at once (1 MiB)
_FALLBACK_VARIABLE = "SMO_ALLOW_SQLITE_FALLBACK"
_TRUE = frozenset({"1", "true", "yes", "on"})
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class ObjectStoreError(RuntimeError):
    """The store answered a request with an error; the message has the method, the path and the status, never a key or a signature."""


class IntegrityError(ObjectStoreError):
    """An object's bytes do not hash to the SHA-256 stored with it."""


@dataclass(frozen=True)
class Settings:
    """Where the store is and how to sign for it; `buckets` maps each use to its bucket name."""
    endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"
    ca_file: str | None = None
    timeout: float = 60.0
    buckets: Mapping[str, str] = field(default_factory=lambda: {use: default for use, (_, default) in USES.items()})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Settings":
        """The settings of the module docstring from `environ`; a required one missing raises ValueError naming the variable (never a value)."""
        endpoint = (environ.get("SMO_OBJECTSTORE_URL") or "").strip().rstrip("/")
        access = read_secret("SMO_OBJECTSTORE_ACCESS_KEY", environ)
        secret = read_secret("SMO_OBJECTSTORE_SECRET_KEY", environ)
        if not endpoint:
            raise ValueError("SMO_OBJECTSTORE_URL is not set")
        if not access:
            raise ValueError("SMO_OBJECTSTORE_ACCESS_KEY (or SMO_OBJECTSTORE_ACCESS_KEY_FILE) is not set")
        if not secret:
            raise ValueError("SMO_OBJECTSTORE_SECRET_KEY (or SMO_OBJECTSTORE_SECRET_KEY_FILE) is not set")
        if urlsplit(endpoint).scheme not in ("http", "https"):
            raise ValueError("SMO_OBJECTSTORE_URL must be an http or https URL")
        try:
            timeout = float(environ.get("SMO_OBJECTSTORE_TIMEOUT_SECONDS") or 60)
        except ValueError:
            raise ValueError("SMO_OBJECTSTORE_TIMEOUT_SECONDS must be a number of seconds") from None
        return cls(endpoint=endpoint, access_key=access, secret_key=secret, region=(environ.get("SMO_OBJECTSTORE_REGION") or "us-east-1").strip(),
                   ca_file=environ.get("SMO_OBJECTSTORE_CA_FILE") or None, timeout=timeout, buckets=buckets_from_env(environ))


def buckets_from_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Each use's bucket name: its variable of USES, else the default."""
    # config-ref: SMO_OBJECTSTORE_BUCKET_MODELS, SMO_OBJECTSTORE_BUCKET_CSARS, SMO_OBJECTSTORE_BUCKET_COLD_PARQUET
    return {use: (environ.get(var) or default).strip() for use, (var, default) in USES.items()}


@dataclass
class Download:
    """An object being read: its `size` and stored `sha256` (None when it has none), and `chunks`, which yields its bytes and raises IntegrityError
    after the last chunk when they do not hash to `sha256` or do not add up to `size`. `close()` releases the connection; reading to the end does too."""
    size: int
    sha256: str | None
    chunks: Iterator[bytes]
    close: Callable[[], None] = lambda: None


def _verified(chunks: Iterable[bytes], size: int, sha256: str | None, what: str, done: Callable[[], None] = lambda: None) -> Iterator[bytes]:
    """`chunks` passed through while hashed and counted; IntegrityError at the end when they do not match `size` and `sha256` (`what` names the bucket
    and operation in the message, never the key). `done` runs however the iteration ends."""
    digest, seen = hashlib.sha256(), 0
    try:
        for chunk in chunks:
            digest.update(chunk)
            seen += len(chunk)
            yield chunk
        if seen != size or (sha256 is not None and digest.hexdigest() != sha256.lower()):
            raise IntegrityError(f"{what}: the object's bytes are not the ones its size and SHA-256 describe")
    finally:
        done()


def _uri_encode(path: str) -> str:
    """S3's canonical URI: each byte but the unreserved characters percent-encoded, `/` kept (S3 does not encode the path twice)."""
    return quote(path, safe="/-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, url: str, headers: Mapping[str, str], payload_sha256: str, access_key: str, secret_key: str, region: str,
         now: datetime.datetime, service: str = "s3") -> dict[str, str]:
    """The headers a request needs for AWS Signature Version 4: `headers` plus `host`, `x-amz-date`, `x-amz-content-sha256` and `Authorization`.

    Every header given is signed. The path of `url` is signed as it stands, so it must already be URI-encoded the way it is sent (`ObjectStore`
    encodes it with `_uri_encode` once; S3 does not encode it again). The query string is canonicalised (each key and value decoded and encoded
    again, then sorted); `payload_sha256` is the hex SHA-256 of the body (or `UNSIGNED-PAYLOAD`). `now` is the signing time, in UTC.
    """
    parts = urlsplit(url)
    amz_date = now.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    host = parts.netloc
    out = {k.lower(): str(v).strip() for k, v in headers.items()}
    out.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256})
    query = []
    for pair in filter(None, parts.query.split("&")):
        key, _, value = pair.partition("=")
        query.append((quote(unquote(key), safe="-_.~"), quote(unquote(value), safe="-_.~")))
    canonical_query = "&".join(f"{k}={v}" for k, v in sorted(query))
    names = sorted(out)
    canonical_headers = "".join(f"{name}:{' '.join(out[name].split())}\n" for name in names)
    signed_headers = ";".join(names)
    canonical = "\n".join([method.upper(), parts.path or "/", canonical_query, canonical_headers, signed_headers, payload_sha256])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signing_key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), date), region), service), "aws4_request")
    signature = hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return out


def _xml(content: bytes) -> ET.Element:
    """An S3 answer's XML, parsed. S3 answers never carry a DTD, so one that does is refused before parsing: no entity can be declared, let alone expanded
    (the expansion attacks defusedxml exists for), and the standard library parser is then safe on what is left."""
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ObjectStoreError("the object store answered XML with a DTD, which S3 never sends: refused")
    return ET.fromstring(content)  # noqa: S314 — no DTD can reach this line (checked above)


def _check_key(key: str) -> str:
    """An object key the SMO may use: non-empty, no leading `/`, no `.` or `..` segment, no control character; returned unchanged."""
    if not key or key.startswith("/") or any(seg in (".", "..") for seg in key.split("/")) or any(ord(c) < 32 for c in key):
        raise ValueError("an object key must be a relative path without '.' or '..' segments or control characters")
    return key


class ObjectStore:
    """The SMO's S3 client: `put`, `get`, `head`, `delete`, `list_keys` by use (`models`, `csars`, `cold-parquet`), and `ensure_buckets`."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None, clock=lambda: datetime.datetime.now(datetime.UTC)):
        self.settings = settings
        self._clock = clock
        self.http = client or httpx.Client(timeout=settings.timeout, verify=settings.ca_file or True, follow_redirects=False)

    def uses(self) -> list[str]:
        """The uses this store has a bucket for."""
        return list(self.settings.buckets)

    def bucket(self, use: str) -> str:
        """The bucket name of `use`; an unknown use is a ValueError."""
        if use not in self.settings.buckets:
            raise ValueError(f"unknown object store use {use!r}: one of {', '.join(sorted(self.settings.buckets))}")
        return self.settings.buckets[use]

    def _signed(self, method: str, path: str, payload_sha256: str, headers: Mapping[str, str] | None = None, query: str = "") -> tuple[str, dict[str, str]]:
        """The URL of `path` (bucket and key, not yet encoded) and the signed headers of a request whose body hashes to `payload_sha256`."""
        url = f"{self.settings.endpoint}{_uri_encode(path)}" + (f"?{query}" if query else "")
        signed = sign(method, url, headers or {}, payload_sha256, self.settings.access_key, self.settings.secret_key, self.settings.region, self._clock())
        signed.pop("host")                                # httpx sets it from the URL, the same value
        return url, signed

    @staticmethod
    def _check(response: httpx.Response, method: str, path: str, ok: tuple[int, ...]) -> httpx.Response:
        """`response` when its status is in `ok`, else ObjectStoreError with the method, the bucket, the status and S3's error code (never the key)."""
        if response.status_code not in ok:
            code = ""
            try:
                code = _xml(response.read()).findtext("Code") or ""
            except (ET.ParseError, ObjectStoreError):
                pass
            raise ObjectStoreError(f"{method} {path.split('/', 2)[1] if path.count('/') > 1 else path}: {response.status_code} {code}".rstrip())
        return response

    def _request(self, method: str, path: str, *, body: bytes = b"", headers: Mapping[str, str] | None = None, query: str = "",
                 ok: tuple[int, ...] = (200,)) -> httpx.Response:
        """One signed request to `path` (bucket and key, not yet encoded) with `body`; the answer when its status is in `ok`, else ObjectStoreError with the
        method, the bucket, the status and S3's error code (never the key)."""
        url, signed = self._signed(method, path, hashlib.sha256(body).hexdigest(), headers, query)
        return self._check(self.http.request(method, url, content=body or None, headers=signed), method, path, ok)

    def ensure_buckets(self) -> list[str]:
        """Creates every use's bucket that does not exist yet (an existing one owned by these credentials is fine); returns the bucket names."""
        names = []
        for use in self.settings.buckets:
            name = self.bucket(use)
            response = self._request("PUT", f"/{name}", ok=(200, 409))
            if response.status_code == 409 and b"BucketAlreadyOwnedByYou" not in response.content and b"BucketAlreadyExists" not in response.content:
                raise ObjectStoreError(f"PUT {name}: 409")
            names.append(name)
        return names

    def put(self, use: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Stores `data` at `key` in `use`'s bucket with its SHA-256 as metadata; returns the SHA-256 (hex). The server checks the body against it."""
        digest = hashlib.sha256(data).hexdigest()
        self._request("PUT", f"/{self.bucket(use)}/{_check_key(key)}", body=data,
                      headers={"content-type": content_type, "x-amz-meta-sha256": digest})
        return digest

    def get(self, use: str, key: str) -> bytes:
        """The object at `key`; raises IntegrityError when its bytes do not hash to the SHA-256 stored with it, ObjectStoreError (404) when absent."""
        response = self._request("GET", f"/{self.bucket(use)}/{_check_key(key)}")
        stored = response.headers.get("x-amz-meta-sha256")
        if stored and hashlib.sha256(response.content).hexdigest() != stored.lower():
            raise IntegrityError(f"GET {self.bucket(use)}: the object's SHA-256 is not the one stored with it")
        return response.content

    def put_stream(self, use: str, key: str, chunks: Iterable[bytes], size: int, sha256: str, content_type: str = "application/octet-stream") -> str:
        """Stores the `size` bytes `chunks` yields at `key`, with `sha256` (their SHA-256, known beforehand: S3 signs it) as metadata; returns it. One
        chunk at a time is in memory. The server refuses a body that does not hash to `sha256`; so does this side, raising IntegrityError (the request
        is then abandoned) when the chunks do not add up to `size` and `sha256`."""
        path = f"/{self.bucket(use)}/{_check_key(key)}"
        url, signed = self._signed("PUT", path, sha256, {"content-type": content_type, "content-length": str(size), "x-amz-meta-sha256": sha256})
        body = _verified(chunks, size, sha256, f"PUT {self.bucket(use)}")
        self._check(self.http.request("PUT", url, content=body, headers=signed), "PUT", path, (200,))
        return sha256

    def open_stream(self, use: str, key: str, chunk_size: int = CHUNK) -> Download:
        """The object at `key` as a Download read `chunk_size` bytes at a time (ObjectStoreError, 404, when absent). Its chunks raise IntegrityError at
        the end when the bytes do not hash to the SHA-256 stored with the object; read them to the end or call `close()`."""
        path = f"/{self.bucket(use)}/{_check_key(key)}"
        url, signed = self._signed("GET", path, EMPTY_SHA256)
        response = self.http.send(self.http.build_request("GET", url, headers=signed), stream=True)
        try:
            self._check(response, "GET", path, (200,))
        except ObjectStoreError:
            response.close()
            raise
        size, stored = int(response.headers.get("content-length", 0)), response.headers.get("x-amz-meta-sha256")
        return Download(size=size, sha256=stored, chunks=_verified(response.iter_bytes(chunk_size), size, stored, f"GET {self.bucket(use)}", response.close),
                        close=response.close)

    @contextlib.contextmanager
    def get_stream(self, use: str, key: str, chunk_size: int = CHUNK) -> Iterator[Download]:
        """`open_stream` as a context manager: the connection is released when the block ends."""
        download = self.open_stream(use, key, chunk_size)
        try:
            yield download
        finally:
            download.close()

    def head(self, use: str, key: str) -> dict | None:
        """{size, sha256, etag} of the object at `key`, or None when there is none."""
        response = self._request("HEAD", f"/{self.bucket(use)}/{_check_key(key)}", ok=(200, 404))
        if response.status_code == 404:
            return None
        return {"size": int(response.headers.get("content-length", 0)), "sha256": response.headers.get("x-amz-meta-sha256"),
                "etag": response.headers.get("etag", "").strip('"')}

    def delete(self, use: str, key: str) -> None:
        """Removes the object at `key` (no error when it is already gone: S3 answers 204 either way)."""
        self._request("DELETE", f"/{self.bucket(use)}/{_check_key(key)}", ok=(200, 204))

    def list_keys(self, use: str, prefix: str = "") -> list[str]:
        """Every key in `use`'s bucket that starts with `prefix`, page by page (ListObjectsV2)."""
        keys, token = [], None
        while True:
            query = "list-type=2" + (f"&prefix={quote(prefix, safe='')}" if prefix else "") + (f"&continuation-token={quote(token, safe='')}" if token else "")
            root = _xml(self._request("GET", f"/{self.bucket(use)}", query=query).content)
            ns = _S3_NS if root.tag.startswith(_S3_NS) else ""
            keys += [k for k in (c.findtext(f"{ns}Key") for c in root.findall(f"{ns}Contents")) if k is not None]
            token = root.findtext(f"{ns}NextContinuationToken")
            if root.findtext(f"{ns}IsTruncated") != "true" or not token:
                return keys


_MEMORY: dict[str, dict[str, tuple[bytes, str]]] = {}         # memory://: every LocalStore of the process shares it, bucket -> key -> (bytes, sha256)


class LocalStore:
    """The methods of ObjectStore over a directory (`root`) or, with `root` None, the process-wide dictionary `_MEMORY`: for tests and a single-process
    demo, never a deployment (nothing is replicated, nothing is shared between processes but the directory). A file's SHA-256 is kept beside it, under
    `<root>/.sha256/<bucket>/<key>`, and checked on every read as the S3 client checks the stored metadata."""

    def __init__(self, root: Path | None = None, buckets: Mapping[str, str] | None = None):
        self.root = root
        self.buckets = dict(buckets or buckets_from_env({}))

    def uses(self) -> list[str]:
        """The uses this store has a bucket for."""
        return list(self.buckets)

    def bucket(self, use: str) -> str:
        """The bucket name of `use`; an unknown use is a ValueError."""
        if use not in self.buckets:
            raise ValueError(f"unknown object store use {use!r}: one of {', '.join(sorted(self.buckets))}")
        return self.buckets[use]

    def _paths(self, use: str, key: str) -> tuple[Path, Path]:
        """The file of the object at `key` and the file of its SHA-256 (a directory store only)."""
        root = self.root or Path(".")
        bucket, key = self.bucket(use), _check_key(key)
        return root / bucket / key, root / ".sha256" / bucket / key

    def ensure_buckets(self) -> list[str]:
        """Creates every use's bucket (a directory, or a dictionary) that does not exist yet; returns the bucket names."""
        for name in self.buckets.values():
            if self.root is None:
                _MEMORY.setdefault(name, {})
            else:
                (self.root / name).mkdir(parents=True, exist_ok=True)
        return list(self.buckets.values())

    def put(self, use: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Stores `data` at `key`; returns its SHA-256."""
        return self.put_stream(use, key, [data], len(data), hashlib.sha256(data).hexdigest(), content_type)

    def put_stream(self, use: str, key: str, chunks: Iterable[bytes], size: int, sha256: str, content_type: str = "application/octet-stream") -> str:
        """Stores the `size` bytes `chunks` yields at `key` with `sha256`; IntegrityError (and nothing stored) when they do not match it. A file is written
        beside its final name and renamed into place, so a reader never sees half of one."""
        body = _verified(chunks, size, sha256, f"PUT {self.bucket(use)}")
        if self.root is None:
            data = b"".join(body)
            _MEMORY.setdefault(self.bucket(use), {})[_check_key(key)] = (data, sha256)
            return sha256
        path, meta = self._paths(use, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        try:
            with partial.open("wb") as out:
                for chunk in body:
                    out.write(chunk)
            meta.write_text(json.dumps({"sha256": sha256, "contentType": content_type}))
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
        return sha256

    def open_stream(self, use: str, key: str, chunk_size: int = CHUNK) -> Download:
        """The object at `key` as a Download (ObjectStoreError, 404, when absent)."""
        if self.root is None:
            data, sha256 = self._memory(use, key)
            pieces = (data[i:i + chunk_size] for i in range(0, len(data), chunk_size))
            return Download(size=len(data), sha256=sha256, chunks=_verified(pieces, len(data), sha256, f"GET {self.bucket(use)}"))
        path, meta = self._paths(use, key)
        if not path.is_file():
            raise ObjectStoreError(f"GET {self.bucket(use)}: 404 NoSuchKey")
        handle = path.open("rb")
        stored: str | None = json.loads(meta.read_text())["sha256"] if meta.is_file() else None
        reads: Iterator[bytes] = iter(lambda: handle.read(chunk_size), b"")
        return Download(size=path.stat().st_size, sha256=stored, chunks=_verified(reads, path.stat().st_size, stored, f"GET {self.bucket(use)}", handle.close),
                        close=handle.close)

    def _memory(self, use: str, key: str) -> tuple[bytes, str]:
        found = _MEMORY.get(self.bucket(use), {}).get(_check_key(key))
        if found is None:
            raise ObjectStoreError(f"GET {self.bucket(use)}: 404 NoSuchKey")
        return found

    @contextlib.contextmanager
    def get_stream(self, use: str, key: str, chunk_size: int = CHUNK) -> Iterator[Download]:
        """`open_stream` as a context manager: the file is closed when the block ends."""
        download = self.open_stream(use, key, chunk_size)
        try:
            yield download
        finally:
            download.close()

    def get(self, use: str, key: str) -> bytes:
        """The object at `key`; IntegrityError when its bytes do not hash to its SHA-256, ObjectStoreError (404) when absent."""
        with self.get_stream(use, key) as download:
            return b"".join(download.chunks)

    def head(self, use: str, key: str) -> dict | None:
        """{size, sha256, etag} of the object at `key` (the etag is its SHA-256), or None when there is none."""
        try:
            download = self.open_stream(use, key)
        except ObjectStoreError:
            return None
        download.close()
        return {"size": download.size, "sha256": download.sha256, "etag": download.sha256 or ""}

    def delete(self, use: str, key: str) -> None:
        """Removes the object at `key` (no error when it is already gone)."""
        if self.root is None:
            _MEMORY.get(self.bucket(use), {}).pop(_check_key(key), None)
            return
        for path in self._paths(use, key):
            path.unlink(missing_ok=True)

    def list_keys(self, use: str, prefix: str = "") -> list[str]:
        """Every key in `use`'s bucket that starts with `prefix`, sorted."""
        if self.root is None:
            return sorted(k for k in _MEMORY.get(self.bucket(use), {}) if k.startswith(prefix))
        base = self.root / self.bucket(use)
        found = (p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and not p.name.endswith(".partial")) if base.is_dir() else ()
        return sorted(k for k in found if k.startswith(prefix))


def open_store(environ: Mapping[str, str] = os.environ) -> "ObjectStore | LocalStore":
    """The store `environ` names (module docstring): a LocalStore for `memory://` or `file:///dir`, or for no URL when the test opt-in
    `SMO_ALLOW_SQLITE_FALLBACK` is on; otherwise the S3 client of `Settings.from_env` (ValueError naming what is missing)."""
    url = (environ.get("SMO_OBJECTSTORE_URL") or "").strip()
    if not url and (environ.get(_FALLBACK_VARIABLE) or "").strip().lower() in _TRUE:
        url = "memory://"
    if url == "memory://":
        return LocalStore(None, buckets_from_env(environ))
    if url.startswith("file://"):
        parts = urlsplit(url)
        root = parts.path
        if parts.netloc not in ("", "localhost") or not root.startswith("/"):
            raise ValueError("SMO_OBJECTSTORE_URL file:// takes an absolute directory: file:///var/lib/smo/objects")
        return LocalStore(Path(root), buckets_from_env(environ))
    return ObjectStore(Settings.from_env(environ))


def roundtrip(store: "ObjectStore | LocalStore", size: int = 1 << 20) -> list[dict]:
    """For each use: put `size` random bytes, read them back, compare the SHA-256 both ways, check head and list, delete; a dict per use. Raises on
    any mismatch (what the compose job proves)."""
    out = []
    for use in store.uses():
        data = os.urandom(size)
        key = f"roundtrip/{uuid.uuid4().hex}.bin"
        sent = store.put(use, key, data)
        back = store.get(use, key)
        meta = store.head(use, key)
        listed = key in store.list_keys(use, "roundtrip/")
        if hashlib.sha256(back).hexdigest() != sent or back != data or not meta or meta["sha256"] != sent or meta["size"] != size or not listed:
            raise IntegrityError(f"round trip through {store.bucket(use)} did not return the object it stored")
        store.delete(use, key)
        if store.head(use, key) is not None:
            raise ObjectStoreError(f"DELETE {store.bucket(use)}: the object is still there")
        out.append({"use": use, "bucket": store.bucket(use), "bytes": size, "sha256": sent})
    return out


def wait_for(store: "ObjectStore | LocalStore", seconds: float, sleep=time.sleep) -> list[str]:
    """`store.ensure_buckets()`, tried again every 2 s while the store cannot be reached (a store still starting: MinIO restarts once while it configures
    itself), for up to `seconds`; then the last connection error is raised. An answer that is an error (a wrong key: 403) is raised at once."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return store.ensure_buckets()
        except httpx.TransportError as exc:
            if time.monotonic() >= deadline:
                raise
            print(f"object store not reachable yet ({type(exc).__name__}); trying again", flush=True)
            sleep(2)


def main(argv: list[str] | None = None) -> int:
    """`ensure-buckets` or `roundtrip` (module docstring) against the store the environment names; prints what it did, 0 on success."""
    args = sys.argv[1:] if argv is None else argv
    if args not in (["ensure-buckets"], ["roundtrip"]):
        print("usage: python -m smo_shared.objectstore ensure-buckets | roundtrip", file=sys.stderr)
        return 2
    store = open_store()
    names = wait_for(store, float(os.environ.get("SMO_OBJECTSTORE_WAIT_SECONDS") or 60))
    if args == ["ensure-buckets"]:
        print("buckets: " + ", ".join(names))
    else:
        for row in roundtrip(store):
            print(f"{row['use']}: {row['bytes']} bytes through {row['bucket']}, sha256 {row['sha256']} both ways")
    return 0


if __name__ == "__main__":
    sys.exit(main())
