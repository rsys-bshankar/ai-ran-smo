"""S3 access for the SMO's object store (D10, plan task S1.10): one bucket per use, every object stored and read back with its SHA-256.

The store is SeaweedFS by default (compose profile `objectstore`) or any S3 API (MinIO, profile `minio`; a cloud bucket). Nothing uses this module
yet: S2.5 moves model artifacts out of the database into it, the CSAR store and the cold Parquet tier (S3.11) follow. Three uses, three buckets:

    models        model artifacts (ONNX, Safetensors; S2.5)      SMO_OBJECTSTORE_BUCKET_MODELS        default smo-models
    csars         rApp packages                                  SMO_OBJECTSTORE_BUCKET_CSARS         default smo-csars
    cold-parquet  telemetry older than the warm tier (S3.11)     SMO_OBJECTSTORE_BUCKET_COLD_PARQUET  default smo-cold-parquet

Why httpx and not boto3: httpx is already in every image (`requirements/runtime.txt`), the SMO needs six calls (create a bucket, put, get, head,
delete, list), and boto3 with botocore would add about 90 MB unpacked to every image for that. The one thing boto3 would bring that is hard to get
right is AWS Signature Version 4; `sign()` below implements it from the published algorithm with the standard library and is tested against the
example in AWS's documentation (shared/tests/test_objectstore.py). Not done here, on purpose: multipart upload (one PUT carries up to 5 GiB, more than
any artifact the SMO stores today; S2.5 adds multipart if a model grows past it), virtual-hosted bucket addressing (path style works on SeaweedFS,
MinIO and AWS), presigned URLs.

Integrity: `put` sends the body's SHA-256 as `x-amz-content-sha256` (the server refuses a body that does not match it) and stores it as the object's
metadata `x-amz-meta-sha256`; `get` hashes what it read and raises `IntegrityError` when it differs from that metadata.

Settings (environment; the keys by the `*_FILE` convention of `smo_shared.secretfile`, so compose and the chart mount them as files):
    SMO_OBJECTSTORE_URL                  the S3 endpoint, e.g. http://objectstore:8333 (required)
    SMO_OBJECTSTORE_REGION               the signing region (default us-east-1; SeaweedFS and MinIO accept any)
    SMO_OBJECTSTORE_ACCESS_KEY(_FILE)    the access key id (required)
    SMO_OBJECTSTORE_SECRET_KEY(_FILE)    the secret access key (required)
    SMO_OBJECTSTORE_CA_FILE              a CA bundle for an https endpoint (unset: the system's)
    SMO_OBJECTSTORE_TIMEOUT_SECONDS      per request (default 60)
    SMO_OBJECTSTORE_WAIT_SECONDS         how long the two commands below wait for a store that is still starting (default 60)

    python -m smo_shared.objectstore ensure-buckets      create the three buckets if missing (compose's objectstore-init runs this)
    python -m smo_shared.objectstore roundtrip           put an object in each bucket, read it back, compare the SHA-256, delete it (the CI job)
"""

import datetime
import hashlib
import hmac
import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

import httpx

from smo_shared.secretfile import read_secret

USES = {"models": ("SMO_OBJECTSTORE_BUCKET_MODELS", "smo-models"), "csars": ("SMO_OBJECTSTORE_BUCKET_CSARS", "smo-csars"),
        "cold-parquet": ("SMO_OBJECTSTORE_BUCKET_COLD_PARQUET", "smo-cold-parquet")}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
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
        # config-ref: SMO_OBJECTSTORE_BUCKET_MODELS, SMO_OBJECTSTORE_BUCKET_CSARS, SMO_OBJECTSTORE_BUCKET_COLD_PARQUET
        buckets = {use: (environ.get(var) or default).strip() for use, (var, default) in USES.items()}
        return cls(endpoint=endpoint, access_key=access, secret_key=secret, region=(environ.get("SMO_OBJECTSTORE_REGION") or "us-east-1").strip(),
                   ca_file=environ.get("SMO_OBJECTSTORE_CA_FILE") or None, timeout=timeout, buckets=buckets)


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

    def bucket(self, use: str) -> str:
        """The bucket name of `use`; an unknown use is a ValueError."""
        if use not in self.settings.buckets:
            raise ValueError(f"unknown object store use {use!r}: one of {', '.join(sorted(self.settings.buckets))}")
        return self.settings.buckets[use]

    def _request(self, method: str, path: str, *, body: bytes = b"", headers: Mapping[str, str] | None = None, query: str = "",
                 ok: tuple[int, ...] = (200,)) -> httpx.Response:
        """One signed request to `path` (bucket and key, not yet encoded) with `body`; the answer when its status is in `ok`, else ObjectStoreError with the
        method, the bucket, the status and S3's error code (never the key)."""
        url = f"{self.settings.endpoint}{_uri_encode(path)}" + (f"?{query}" if query else "")
        signed = sign(method, url, headers or {}, hashlib.sha256(body).hexdigest(), self.settings.access_key, self.settings.secret_key,
                      self.settings.region, self._clock())
        signed.pop("host")                                # httpx sets it from the URL, the same value
        response = self.http.request(method, url, content=body or None, headers=signed)
        if response.status_code not in ok:
            code = ""
            try:
                code = _xml(response.content).findtext("Code") or ""
            except (ET.ParseError, ObjectStoreError):
                pass
            raise ObjectStoreError(f"{method} {path.split('/', 2)[1] if path.count('/') > 1 else path}: {response.status_code} {code}".rstrip())
        return response

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


def roundtrip(store: ObjectStore, size: int = 1 << 20) -> list[dict]:
    """For each use: put `size` random bytes, read them back, compare the SHA-256 both ways, check head and list, delete; a dict per use. Raises on
    any mismatch (what the compose job proves)."""
    out = []
    for use in store.settings.buckets:
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


def wait_for(store: ObjectStore, seconds: float, sleep=time.sleep) -> list[str]:
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
    store = ObjectStore(Settings.from_env())
    names = wait_for(store, float(os.environ.get("SMO_OBJECTSTORE_WAIT_SECONDS") or 60))
    if args == ["ensure-buckets"]:
        print("buckets: " + ", ".join(names))
    else:
        for row in roundtrip(store):
            print(f"{row['use']}: {row['bytes']} bytes through {row['bucket']}, sha256 {row['sha256']} both ways")
    return 0


if __name__ == "__main__":
    sys.exit(main())
