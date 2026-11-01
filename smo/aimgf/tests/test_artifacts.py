"""Tests of model artifacts in the object store (plan task S2.5, `app/registry/artifacts.py` and the artifact routes of `app/registry/routes.py`): the
format gate (ONNX, Safetensors, zip-legacy; pickle refused whatever the setting), the setting SMO_AIMGF_ARTIFACT_FORMATS and its check at start-up,
the streamed upload and download with the SHA-256 digest, the deprecation warning of a zip-legacy upload in the answer and the audit row, the
store's failures, the delete of a model's objects and the TS 29.482 inline answer's size limit.

Run: `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_artifacts.py -q`. The store is smo_shared.objectstore's in-memory LocalStore
(the suite's SMO_ALLOW_SQLITE_FALLBACK selects it) or, where a test says so, a directory; the S3 client itself is tested in shared/tests/test_objectstore.py.
"""

import base64
import hashlib
import io
import json
import pickle
import struct
import uuid
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from smo_shared import audit, objectstore

from app.main import app
from app.registry import artifacts
from app.registry.models import ModelArtifact

from test_registry import client, db_session_factory  # noqa: F401 (fixtures)

ONNX = b"\x08\x08" + b"\x12\x04test" + b"\x3a\x06\x0a\x01x\x12\x01y" + b"\x42\x02\x10\x11"   # ir_version 8, producer, a graph, an opset
SAFETENSORS_HEADER = json.dumps({"w": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}, "__metadata__": {"k": "v"}}).encode()
SAFETENSORS = struct.pack("<Q", len(SAFETENSORS_HEADER)) + SAFETENSORS_HEADER + b"\x00" * 8
PICKLE = pickle.dumps({"weights": [1, 2, 3]}, protocol=4)


def _zip(**members: bytes) -> bytes:
    """A zip archive holding `members` (name -> bytes)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


def _detect(data: bytes, name: str) -> str:
    return artifacts.detect(io.BytesIO(data), len(data), name)


@pytest.fixture(autouse=True)
def empty_store():
    """Every test starts with an empty in-memory store and leaves none behind."""
    objectstore._MEMORY.clear()
    yield
    objectstore._MEMORY.clear()


def _model(client) -> str:
    """Registers a model of a random type and returns its id."""
    resp = client.post("/models", json={"modelType": "t-" + uuid.uuid4().hex[:8], "version": "1"})
    assert resp.status_code == 201, resp.text
    return resp.json()["modelId"]


def _objects() -> dict:
    return objectstore._MEMORY.get("smo-models", {})


def test_onnx_and_safetensors_are_recognised_by_their_content():
    """A ModelProto with ir_version and a graph is onnx; a header-and-buffer file whose offsets cover the rest exactly is safetensors."""
    assert _detect(ONNX, "m.onnx") == "onnx"
    assert _detect(SAFETENSORS, "m.safetensors") == "safetensors"
    assert _detect(SAFETENSORS, "M.SAFETENSORS") == "safetensors"


@pytest.mark.parametrize("data, name, reason", [
    (PICKLE, "m.onnx", "pickle is refused"),                                   # a pickle named as anything else
    (b"\x08\x08", "m.pkl", "pickle is refused"),                               # by name alone
    (ONNX, "model.pt", "pickle is refused"),
    (_zip(**{"archive/data.pkl": b"x"}), "torch.zip", "holds a pickle"),       # a PyTorch .pt is a zip with data.pkl
    (_zip(weights=PICKLE), "m.zip", "holds a pickle"),                         # a member that is a pickle by its content
    (b"just text", "m.onnx", "neither"),
    (ONNX[:-1], "m.onnx", "neither"),                                          # cut off inside a field
    (b"\x08\x08", "m.onnx", "neither"),                                        # no graph
    (b"\x08\x08\x3a\x7f", "m.onnx", "neither"),                                # a graph longer than the file
    (b"\x08" + b"\xff" * 11, "m.onnx", "neither"),                             # a varint of more than ten bytes
    (b"\x58\x01\x3a\x00\x08\x01", "m.onnx", "neither"),                        # field 11 is not ModelProto's
    (b"\x0a\x00\x3a\x00", "m.onnx", "neither"),                                # ir_version with the wrong wire type
    (SAFETENSORS[:-1], "m.safetensors", "neither"),                            # offsets past the end
    (struct.pack("<Q", 2) + b"[]", "m.safetensors", "neither"),                # a header that is not an object
    (struct.pack("<Q", 4) + b"\xff\xfe{}", "m.safetensors", "neither"),        # not UTF-8
    (struct.pack("<Q", 9 << 20) + b"{}", "m.safetensors", "neither"),          # a header over the limit
    (struct.pack("<Q", 2) + b"{}" + b"x", "m.safetensors", "neither"),         # a buffer the header does not describe
    (b"1234", "m.safetensors", "neither"),
    (ONNX, "m.safetensors", "does not end in .onnx"),
    (SAFETENSORS, "m.onnx", "does not end in .safetensors"),
    (_zip(a=b"x"), "m.onnx", "does not end in .zip"),
])
def test_pickle_and_anything_but_the_accepted_formats_is_refused_with_the_reason(data, name, reason):
    """Pickle by name, content or zip member; content that is neither ONNX nor Safetensors; an extension that is not the content's: each refused
    with its reason."""
    with pytest.raises(artifacts.Refused, match=reason):
        _detect(data, name)


@pytest.mark.parametrize("bad", [
    {"dtype": 1, "shape": [], "data_offsets": [0, 0]}, {"dtype": "F32", "shape": 1, "data_offsets": [0, 0]}, {"dtype": "F32", "shape": []},
    {"dtype": "F32", "shape": [], "data_offsets": [2, 1]}, {"dtype": "F32", "shape": [], "data_offsets": [0, "1"]}, "not a tensor"])
def test_a_safetensors_header_with_a_malformed_tensor_is_not_safetensors(bad):
    """A tensor entry without a string dtype, a list shape and two ordered integer offsets makes the file not Safetensors."""
    header = json.dumps({"w": bad}).encode()
    with pytest.raises(artifacts.Refused, match="neither"):
        _detect(struct.pack("<Q", len(header)) + header, "m.safetensors")


def test_a_zip_legacy_upload_keeps_the_rule_from_before_s2_5():
    """A file named .zip is zip-legacy whatever it holds (the name was the only check before S2.5), unless it is a pickle; a zip that cannot be read,
    or holds too many members, is refused."""
    assert _detect(b"pkzip-bytes", "m.zip") == "zip-legacy"
    assert _detect(_zip(**{"model.json": b"{}", "dir/": b""}), "m.ZIP") == "zip-legacy"
    with pytest.raises(artifacts.Refused, match="pickle"):
        _detect(PICKLE, "m.zip")
    with pytest.raises(artifacts.Refused, match="not a readable zip"):
        _detect(b"PK\x03\x04broken", "m.zip")


def test_a_zip_with_more_members_than_the_limit_is_refused(monkeypatch):
    """ZIP_MEMBER_LIMIT bounds how many members are opened."""
    monkeypatch.setattr(artifacts, "ZIP_MEMBER_LIMIT", 1)
    with pytest.raises(artifacts.Refused, match="more than 1 members"):
        _detect(_zip(a=b"1", b=b"2"), "m.zip")


@pytest.mark.parametrize("value, expected", [
    (None, {"onnx", "safetensors", "zip-legacy"}), ("onnx", {"onnx"}), (" Safetensors , onnx ,", {"onnx", "safetensors"})])
def test_the_setting_names_the_accepted_formats(value, expected):
    """Unset is the default (all three); a list is read case-insensitively, spaces and empty items ignored."""
    assert artifacts.accepted_formats({} if value is None else {"SMO_AIMGF_ARTIFACT_FORMATS": value}) == expected


@pytest.mark.parametrize("value, match", [("pickle", "never accepted"), ("onnx,pickle", "never accepted"), ("onnx,h5", "one or more of"), ("", "one or more of"),
                                          (" , ", "one or more of")])
def test_a_setting_naming_pickle_an_unknown_format_or_none_is_refused(value, match):
    """pickle is never accepted; an unknown name and an empty list are configuration errors."""
    with pytest.raises(ValueError, match=match):
        artifacts.accepted_formats({"SMO_AIMGF_ARTIFACT_FORMATS": value})


def test_a_setting_naming_pickle_stops_aimgf_at_start_up(monkeypatch):
    """The lifespan reads the setting first, so AIMgF does not start with a setting it cannot honour."""
    monkeypatch.setenv("SMO_AIMGF_ARTIFACT_FORMATS", "onnx,pickle")
    with pytest.raises(ValueError, match="pickle"), TestClient(app):
        pass


def test_inspect_reads_the_upload_a_chunk_at_a_time_and_checks_the_setting():
    """inspect hashes and sizes the upload without reading more than CHUNK at once, and refuses a format the setting leaves out; the file is left at
    its start."""
    reads = []

    class Counting(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    data = ONNX + b"\x32" + bytes([127]) + b"d" * 127 + b"\x32\x80\x80\x01" + b"e" * (1 << 14)        # a doc_string, then one of 16 KiB
    f = Counting(data)
    assert artifacts.inspect(f, "m.onnx", {}) == (len(data), hashlib.sha256(data).hexdigest(), "onnx")
    assert f.tell() == 0 and all(0 < size <= objectstore.CHUNK for size in reads)
    with pytest.raises(artifacts.Refused, match="onnx is not accepted here: SMO_AIMGF_ARTIFACT_FORMATS is safetensors"):
        artifacts.inspect(io.BytesIO(ONNX), "m.onnx", {"SMO_AIMGF_ARTIFACT_FORMATS": "safetensors"})


def test_an_onnx_upload_is_stored_with_its_digest_and_streamed_back(client, db_session_factory):
    """201 with the format and the SHA-256; the bytes are in the `models` bucket at `<model>/<artifact>.onnx` and the row holds the key, the digest and
    the format; the download streams them back with Content-Length, Repr-Digest and ETag; no warning."""
    model_id = _model(client)
    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX, "application/octet-stream")})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    digest = hashlib.sha256(ONNX).hexdigest()
    assert (body["format"], body["sha256"], body["sizeBytes"], body["artifactVersion"]) == ("onnx", digest, len(ONNX), 1) and "warnings" not in body
    key = f"{model_id}/{body['artifactId']}.onnx"
    assert _objects()[key] == (ONNX, digest)
    with db_session_factory() as db:
        row = db.scalar(select(ModelArtifact).where(ModelArtifact.model_id == uuid.UUID(model_id)))
        assert (row.object_key, row.sha256, row.artifact_format, row.size_bytes, row.filename) == (key, digest, "onnx", len(ONNX), "m.onnx")
        entry = db.scalar(select(audit.AuditEntry).where(audit.AuditEntry.action == "UPLOAD_MODEL_ARTIFACT"))
        assert entry.detail == {"modelId": model_id, "artifactVersion": 1, "format": "onnx", "sha256": digest, "sizeBytes": len(ONNX)}
    download = client.get(f"/models/{model_id}/artifact/1")
    assert download.status_code == 200 and download.content == ONNX
    assert download.headers["content-type"] == "application/octet-stream" and download.headers["content-length"] == str(len(ONNX))
    assert download.headers["repr-digest"] == f"sha-256=:{base64.b64encode(hashlib.sha256(ONNX).digest()).decode()}:"
    assert download.headers["etag"] == f'"{digest}"' and download.headers["content-disposition"] == 'attachment; filename="m.onnx"'


def test_a_safetensors_upload_and_a_second_version(client):
    """A Safetensors file is stored under `.safetensors`; a second upload is version 2 and both versions read back."""
    model_id = _model(client)
    first = client.post(f"/models/{model_id}/artifact", files={"file": ("m.safetensors", SAFETENSORS)}).json()
    second = client.post(f"/models/{model_id}/artifact", files={"file": ("n.onnx", ONNX)}).json()
    assert (first["format"], first["artifactVersion"], second["artifactVersion"]) == ("safetensors", 1, 2)
    assert f"{model_id}/{first['artifactId']}.safetensors" in _objects()
    assert client.get(f"/models/{model_id}/artifact/1").content == SAFETENSORS and client.get(f"/models/{model_id}/artifact/2").content == ONNX


def test_a_zip_legacy_upload_carries_the_deprecation_warning_in_the_answer_and_the_audit_row(client, db_session_factory, caplog):
    """A .zip is accepted under the default setting with `warnings` naming its deprecation, the same warning in the audit row's detail, the media
    type application/zip on the way back, and the format in the log line."""
    model_id = _model(client)
    caplog.set_level("INFO", logger="app.registry.artifacts")
    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"pkzip-bytes", "application/zip")})
    assert resp.status_code == 201 and resp.json()["warnings"] == [artifacts.DEPRECATION] and resp.json()["format"] == "zip-legacy"
    assert "S7.1" in artifacts.DEPRECATION and "deprecated" in artifacts.DEPRECATION
    with db_session_factory() as db:
        entry = db.scalar(select(audit.AuditEntry).where(audit.AuditEntry.action == "UPLOAD_MODEL_ARTIFACT"))
        assert entry.detail["warnings"] == [artifacts.DEPRECATION] and entry.detail["format"] == "zip-legacy" and entry.result == "201"
        assert entry.target == f"/aimgf/models/{model_id}/artifact"
    assert [r.artifactFormat for r in caplog.records if r.getMessage() == "model artifact stored"] == ["zip-legacy"]
    download = client.get(f"/models/{model_id}/artifact/1")
    assert download.content == b"pkzip-bytes" and download.headers["content-type"] == "application/zip"


@pytest.mark.parametrize("formats", [None, "onnx,safetensors,zip-legacy", "zip-legacy"])
def test_a_pickle_is_refused_whatever_the_setting_and_nothing_is_stored(client, db_session_factory, monkeypatch, formats):
    """415 ARTIFACT_FORMAT_INVALID naming pickle and Part 2 §1, under every setting, with no object, no row and no artifact location written."""
    if formats is not None:
        monkeypatch.setenv("SMO_AIMGF_ARTIFACT_FORMATS", formats)
    model_id = _model(client)
    for name, data in (("model.pkl", PICKLE), ("model.zip", PICKLE), ("model.zip", _zip(**{"m/data.pkl": PICKLE}))):
        resp = client.post(f"/models/{model_id}/artifact", files={"file": (name, data)})
        assert resp.status_code == 415 and "ARTIFACT_FORMAT_INVALID" in resp.text and "pickle" in resp.text and "Part 2" in resp.text
    assert _objects() == {} and client.get(f"/models/{model_id}").json()["artifactLocation"] is None
    with db_session_factory() as db:
        assert db.scalar(select(ModelArtifact)) is None


def test_the_setting_decides_which_formats_an_upload_may_have(client, monkeypatch):
    """With SMO_AIMGF_ARTIFACT_FORMATS=onnx,safetensors a .zip is 415 with the setting named; ONNX still goes in; with no file name it is refused."""
    monkeypatch.setenv("SMO_AIMGF_ARTIFACT_FORMATS", "onnx,safetensors")
    model_id = _model(client)
    refused = client.post(f"/models/{model_id}/artifact", files={"file": ("model.zip", b"pkzip-bytes")})
    assert refused.status_code == 415 and "zip-legacy is not accepted here" in refused.text
    assert client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)}).status_code == 201


def test_a_store_kept_in_a_directory_holds_the_artifact(client, monkeypatch, tmp_path):
    """SMO_OBJECTSTORE_URL=file:///dir puts the bytes in a file under the bucket's directory; the download reads them from there."""
    monkeypatch.setenv("SMO_OBJECTSTORE_URL", f"file://{tmp_path}")
    model_id = _model(client)
    artifact_id = client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)}).json()["artifactId"]
    assert (tmp_path / "smo-models" / model_id / f"{artifact_id}.onnx").read_bytes() == ONNX and _objects() == {}
    assert client.get(f"/models/{model_id}/artifact/1").content == ONNX


def test_no_store_configured_is_503_and_a_store_refusing_is_502(client, monkeypatch):
    """Without SMO_OBJECTSTORE_URL (and outside the test opt-in) the upload is 503 ENDPOINT_UNREACHABLE naming the variable; a store that does not
    answer is 503, one that refuses 502 UPSTREAM_FAILED; nothing is written."""
    model_id = _model(client)
    monkeypatch.delenv("SMO_ALLOW_SQLITE_FALLBACK")
    monkeypatch.delenv("SMO_OBJECTSTORE_URL", raising=False)
    resp = client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)})
    assert resp.status_code == 503 and "ENDPOINT_UNREACHABLE" in resp.text and "SMO_OBJECTSTORE_URL" in resp.text
    for raised, status, code in ((httpx.ConnectError("refused"), 503, "ENDPOINT_UNREACHABLE"), (objectstore.ObjectStoreError("PUT smo-models: 403 AccessDenied"), 502, "UPSTREAM_FAILED")):
        def refuse(*args, error=raised, **kwargs):
            raise error
        monkeypatch.setattr(artifacts, "put", refuse)
        resp = client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)})
        assert resp.status_code == status and code in resp.text
    assert client.get(f"/models/{model_id}").json()["artifactLocation"] is None


def test_a_row_that_cannot_be_written_takes_its_object_with_it(client, monkeypatch):
    """When the commit fails after the object was stored (here the audit row raises), the object is removed again and the error propagates."""
    model_id = _model(client)

    def broken(*args, **kwargs):
        raise RuntimeError("audit down")
    monkeypatch.setattr(audit, "record", broken)
    with pytest.raises(RuntimeError, match="audit down"):
        client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)})
    assert _objects() == {}


def test_a_stored_object_that_does_not_match_its_row_is_not_served(client):
    """An object whose stored digest is not the row's is 502 before a byte is sent; one whose bytes no longer match its digest is cut off at the
    end; a missing object is 502 with the store's 404."""
    model_id = _model(client)
    artifact_id = client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)}).json()["artifactId"]
    key = f"{model_id}/{artifact_id}.onnx"
    _objects()[key] = (ONNX, "0" * 64)
    resp = client.get(f"/models/{model_id}/artifact/1")
    assert resp.status_code == 502 and "UPSTREAM_FAILED" in resp.text and key not in resp.text
    _objects()[key] = (ONNX[:-1] + b"\x00", hashlib.sha256(ONNX).hexdigest())
    with pytest.raises(objectstore.IntegrityError):
        client.get(f"/models/{model_id}/artifact/1")
    del _objects()[key]
    assert client.get(f"/models/{model_id}/artifact/1").status_code == 502


def test_open_artifact_streams_in_chunks_of_at_most_one_mib(client):
    """A 3 MiB artifact is read back as chunks of at most CHUNK; nothing reads it whole."""
    big = json.dumps({"w": {"dtype": "U8", "shape": [3 << 20], "data_offsets": [0, 3 << 20]}}).encode()
    data = struct.pack("<Q", len(big)) + big + b"\x01" * (3 << 20)
    model_id = _model(client)
    artifact_id = client.post(f"/models/{model_id}/artifact", files={"file": ("big.safetensors", data)}).json()["artifactId"]
    download = artifacts.open_artifact(f"{model_id}/{artifact_id}.safetensors", hashlib.sha256(data).hexdigest())
    sizes = [len(c) for c in download.chunks]
    assert sum(sizes) == len(data) and max(sizes) == objectstore.CHUNK


def test_deleting_a_model_removes_its_objects_and_a_store_that_refuses_only_logs(client, monkeypatch, caplog):
    """Deregistration deletes the rows, then the objects; a store that refuses the delete leaves an orphan and a warning, not an error."""
    model_id = _model(client)
    client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)})
    client.post(f"/models/{model_id}/artifact", files={"file": ("m.safetensors", SAFETENSORS)})
    assert len(_objects()) == 2
    assert client.delete(f"/models/{model_id}").status_code == 204 and _objects() == {}
    other = _model(client)
    client.post(f"/models/{other}/artifact", files={"file": ("m.onnx", ONNX)})

    class Refusing:
        def delete(self, use, key):
            raise objectstore.ObjectStoreError("DELETE smo-models: 403")
    monkeypatch.setattr(artifacts, "store", lambda: Refusing())
    assert client.delete(f"/models/{other}").status_code == 204
    assert any(r.getMessage() == "model artifact object not removed" for r in caplog.records) and len(_objects()) == 1


def test_the_inline_discovery_answer_carries_an_artifact_up_to_the_limit(client, monkeypatch):
    """TS 29.482 discovery with include-models carries the latest artifact, read from the store, base64 encoded when it is at most INLINE_LIMIT; a
    larger one is listed by its id alone; a store that does not answer is 503."""
    model_id = _model(client)
    client.post(f"/models/{model_id}/artifact", files={"file": ("m.onnx", ONNX)})

    def discover():
        return client.get("/models", params={"filt-criteria": json.dumps({"mlModelId": model_id}), "include-models": "true"})
    assert discover().json()["mlModels"] == [{"mlModelId": model_id, "mlModel": base64.b64encode(ONNX).decode()}]
    monkeypatch.setattr(artifacts, "INLINE_LIMIT", len(ONNX) - 1)
    assert discover().json()["mlModels"] == [{"mlModelId": model_id}]
    monkeypatch.setattr(artifacts, "INLINE_LIMIT", len(ONNX))
    assert discover().json()["mlModels"][0]["mlModel"]

    def unreachable(*args):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(artifacts, "open_artifact", unreachable)
    assert discover().status_code == 503
