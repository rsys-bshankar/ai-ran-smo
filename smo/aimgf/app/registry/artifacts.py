"""Model artifacts in the object store (plan task S2.5): the format gate, the object key, and the streamed upload and download.

An artifact's bytes live in the object store's `models` bucket (`smo_shared.objectstore`, D10), never in the database: `model_artifact` keeps the key,
the SHA-256 digest, the format and the size. Which formats an upload may have is the setting SMO_AIMGF_ARTIFACT_FORMATS (comma separated):

    onnx          an ONNX ModelProto (`.onnx`): its top-level protobuf fields are walked and must be ModelProto's, with `ir_version` and `graph`
    safetensors   a Safetensors file (`.safetensors`): the 8-byte header length, a JSON header of tensors whose data offsets cover the rest exactly
    zip-legacy    a file named `.zip`, the rule every caller uploaded under before S2.5 (the name, not the content); accepted with a deprecation
                  warning in the answer and the audit row, until the callers move to ONNX or Safetensors (plan rows S7.1, S7.13) and the value leaves
                  the default

Default: `onnx,safetensors,zip-legacy`. Pickle is refused whatever the setting says (direction document Part 2 §1, ADR 0010: loading a pickle runs
code): a file that starts as a pickle does, a name ending `.pkl`, `.pickle`, `.joblib`, `.pt`, `.pth` or `.ckpt`, and a zip holding a member that is
one (a PyTorch `.pt` is a zip holding `data.pkl`). A value the setting does not know, `pickle` among them, stops AIMgF at start-up.

Memory: the upload arrives spooled to a temporary file (Starlette's UploadFile); it is read in CHUNK (1 MiB) pieces to hash and check it and again to
send it, and a download is passed on one CHUNK at a time. The only whole-artifact read is the TS 29.482 discovery answer that carries the model inline
(`mlModel`, base64 in JSON), and only for an artifact of at most INLINE_LIMIT (16 MiB); a larger one is listed without its bytes. A Safetensors header
is parsed whole and refused above SAFETENSORS_HEADER_LIMIT (8 MiB).
"""

import base64
import hashlib
import json
import logging
import os
import struct
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from typing import BinaryIO, TypeVar

import httpx

from smo_shared import objectstore
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.objectstore import CHUNK, Download

log = logging.getLogger(__name__)

USE = "models"                                  # the object store's bucket for model artifacts
FORMATS = ("onnx", "safetensors", "zip-legacy")
DEFAULT_FORMATS = "onnx,safetensors,zip-legacy"
EXTENSIONS = {"onnx": ".onnx", "safetensors": ".safetensors", "zip-legacy": ".zip"}
MEDIA_TYPES = {"onnx": "application/octet-stream", "safetensors": "application/octet-stream", "zip-legacy": "application/zip"}
PICKLE_NAMES = (".pkl", ".pickle", ".joblib", ".pt", ".pth", ".ckpt")
INLINE_LIMIT = 16 << 20
SAFETENSORS_HEADER_LIMIT = 8 << 20
ZIP_MEMBER_LIMIT = 10_000
DEPRECATION = ("zip-legacy: a .zip artifact is deprecated; upload ONNX (.onnx) or Safetensors (.safetensors). SMO_AIMGF_ARTIFACT_FORMATS stops "
               "accepting zip-legacy once the callers have moved (plan rows PR-CONV-S7.1, S7.13)")
_PART_2 = "direction document Part 2 §1, ADR 0010"
# ModelProto's top-level fields (onnx.proto3): number -> protobuf wire type (0 varint, 2 length-delimited)
_MODEL_PROTO = {1: 0, 2: 2, 3: 2, 4: 2, 5: 0, 6: 2, 7: 2, 8: 2, 14: 2, 20: 2, 25: 2, 26: 2}


T = TypeVar("T")


class Refused(ValueError):
    """An upload whose format is refused; the message is the reason the caller reads."""


def accepted_formats(environ: Mapping[str, str] = os.environ) -> frozenset[str]:
    """The formats SMO_AIMGF_ARTIFACT_FORMATS accepts (DEFAULT_FORMATS when unset); ValueError for an empty list, `pickle` or a name it does not know."""
    raw = environ.get("SMO_AIMGF_ARTIFACT_FORMATS")
    names = [n.strip().lower() for n in (DEFAULT_FORMATS if raw is None else raw).split(",") if n.strip()]
    if "pickle" in names:
        raise ValueError(f"SMO_AIMGF_ARTIFACT_FORMATS names pickle, which is never accepted ({_PART_2})")
    unknown = sorted(set(names) - set(FORMATS))
    if unknown or not names:
        raise ValueError(f"SMO_AIMGF_ARTIFACT_FORMATS must name one or more of {', '.join(FORMATS)}; got {raw!r}")
    return frozenset(names)


def _is_pickle(head: bytes) -> bool:
    """Whether `head` starts a pickle of protocol 2 or later (PROTO opcode 0x80 and the protocol number), the form every current writer uses."""
    return len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5


def _varint(f: BinaryIO) -> int | None:
    """One protobuf varint from `f`; None at the end of the file, Refused when it is cut off or longer than ten bytes."""
    value = 0
    for shift in range(0, 70, 7):
        byte = f.read(1)
        if not byte:
            if shift == 0:
                return None
            raise Refused("not ONNX: the file ends inside a protobuf field")
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value
    raise Refused("not ONNX: a protobuf varint longer than ten bytes")


def _is_onnx(f: BinaryIO, size: int) -> bool:
    """Whether the `size` bytes of `f` are an ONNX ModelProto: every top-level field one of ModelProto's with its wire type, `ir_version` and `graph`
    present, the last field ending at the end of the file. Length-delimited fields are skipped, not read."""
    f.seek(0)
    seen = set()
    try:
        while (tag := _varint(f)) is not None:
            number, wire = tag >> 3, tag & 7
            if _MODEL_PROTO.get(number) != wire:
                return False
            seen.add(number)
            if wire == 0:
                if _varint(f) is None:
                    return False
            else:
                length = _varint(f)
                if length is None or f.tell() + length > size:
                    return False
                f.seek(length, os.SEEK_CUR)
    except Refused:
        return False
    return {1, 7} <= seen and f.tell() == size


def _is_safetensors(f: BinaryIO, size: int) -> bool:
    """Whether the `size` bytes of `f` are a Safetensors file: a little-endian u64 header length, a JSON object header (at most
    SAFETENSORS_HEADER_LIMIT) whose tensors have a dtype, a shape and data offsets, and whose offsets end exactly where the file does."""
    f.seek(0)
    raw = f.read(8)
    if len(raw) != 8:
        return False
    (length,) = struct.unpack("<Q", raw)
    if not 2 <= length <= SAFETENSORS_HEADER_LIMIT or 8 + length > size:
        return False
    try:
        header = json.loads(f.read(length).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(header, dict):
        return False
    end = 0
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(tensor, dict) or not isinstance(tensor.get("dtype"), str) or not isinstance(tensor.get("shape"), list):
            return False
        offsets = tensor.get("data_offsets")
        if not (isinstance(offsets, list) and len(offsets) == 2 and all(isinstance(o, int) for o in offsets) and 0 <= offsets[0] <= offsets[1]):
            return False
        end = max(end, offsets[1])
    return end == size - 8 - length


def _zip_holds_pickle(f: BinaryIO) -> bool:
    """Whether the zip in `f` holds a member that is a pickle, by name or by its first two bytes; Refused when it is not a readable zip."""
    f.seek(0)
    try:
        with zipfile.ZipFile(f) as archive:
            members = archive.infolist()
            if len(members) > ZIP_MEMBER_LIMIT:
                raise Refused(f"zip-legacy: more than {ZIP_MEMBER_LIMIT} members")
            for member in members:
                if member.is_dir():
                    continue
                if member.filename.lower().endswith(PICKLE_NAMES):
                    return True
                with archive.open(member) as opened:
                    if _is_pickle(opened.read(2)):
                        return True
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
        raise Refused(f"zip-legacy: not a readable zip ({type(exc).__name__})") from None
    return False


def detect(f: BinaryIO, size: int, filename: str) -> str:
    """The format of the `size` bytes of `f` uploaded as `filename`: `onnx`, `safetensors` or `zip-legacy`; Refused with the reason for a pickle (by
    name, content or zip member), for anything else, and for a file whose name's extension is not its format's. zip-legacy keeps the rule from before
    S2.5, the name ending `.zip`, plus the pickle checks: a `.zip` that is a zip has every member checked, one that is not is taken as it was then."""
    name = filename.lower()
    f.seek(0)
    head = f.read(4)
    if name.endswith(PICKLE_NAMES) or _is_pickle(head):
        raise Refused(f"pickle is refused: loading a pickle runs code ({_PART_2}); export the model to ONNX or save it as Safetensors")
    is_zip = head.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if is_zip and _zip_holds_pickle(f):
        raise Refused(f"the zip holds a pickle, which is refused: loading a pickle runs code ({_PART_2})")
    if name.endswith(".zip"):
        return "zip-legacy"
    if is_zip:
        found = "zip-legacy"
    elif _is_safetensors(f, size):
        found = "safetensors"
    elif _is_onnx(f, size):
        found = "onnx"
    else:
        raise Refused("only ONNX (.onnx) and Safetensors (.safetensors) are accepted; the file is neither")
    if not name.endswith(EXTENSIONS[found]):
        raise Refused(f"the file is {found} but its name does not end in {EXTENSIONS[found]}")
    return found


def inspect(f: BinaryIO, filename: str, environ: Mapping[str, str] = os.environ) -> tuple[int, str, str]:
    """(size, SHA-256, format) of the upload in `f`: read in CHUNK pieces, its format detected and checked against SMO_AIMGF_ARTIFACT_FORMATS (Refused
    when not accepted); `f` is left at its start for the upload."""
    accepted = accepted_formats(environ)
    digest, size = hashlib.sha256(), 0
    f.seek(0)
    while chunk := f.read(CHUNK):
        digest.update(chunk)
        size += len(chunk)
    found = detect(f, size, filename)
    if found not in accepted:
        raise Refused(f"{found} is not accepted here: SMO_AIMGF_ARTIFACT_FORMATS is {','.join(sorted(accepted))}")
    f.seek(0)
    return size, digest.hexdigest(), found


def key(model_id: uuid.UUID, artifact_id: uuid.UUID, fmt: str) -> str:
    """The object key of an artifact: `<model id>/<artifact id><extension>` in the `models` bucket."""
    return f"{model_id}/{artifact_id}{EXTENSIONS.get(fmt, '')}"


def store() -> "objectstore.ObjectStore | objectstore.LocalStore":
    """The object store the environment names (`smo_shared.objectstore.open_store`); ValueError naming the setting that is missing."""
    return objectstore.open_store()


def chunks_of(f: BinaryIO) -> Iterator[bytes]:
    """The bytes of `f`, from where it stands, CHUNK at a time."""
    while chunk := f.read(CHUNK):
        yield chunk


def put(f: BinaryIO, object_key: str, size: int, sha256: str, fmt: str) -> None:
    """Sends the upload in `f` to the store at `object_key`, streamed, with its SHA-256 (which the store checks) and its format's media type."""
    store().put_stream(USE, object_key, chunks_of(f), size, sha256, MEDIA_TYPES[fmt])
    log.info("model artifact stored", extra={"artifactFormat": fmt, "sizeBytes": size, "sha256": sha256})


def open_artifact(object_key: str, sha256: str) -> Download:
    """The stored object at `object_key` as a Download; IntegrityError when the store holds it with another SHA-256 than the row's (nothing is read)."""
    download = store().open_stream(USE, object_key)
    if download.sha256 is not None and download.sha256.lower() != sha256:
        download.close()
        raise objectstore.IntegrityError(f"GET {USE}: the stored object's SHA-256 is not the one recorded for the artifact")
    return download


def inline(object_key: str, sha256: str, size: int) -> str | None:
    """The artifact base64-encoded for an answer that carries it inline (TS 29.482 `mlModel`), or None when it is larger than INLINE_LIMIT."""
    if size > INLINE_LIMIT:
        return None
    download = open_artifact(object_key, sha256)
    return base64.b64encode(b"".join(download.chunks)).decode()


def delete_quietly(keys: list[str]) -> None:
    """Removes the objects at `keys`, logging (not raising) a store that refuses: the rows are already gone, an orphan object only costs space."""
    for object_key in keys:
        try:
            store().delete(USE, object_key)
        except (objectstore.ObjectStoreError, ValueError, OSError, httpx.HTTPError) as exc:
            log.warning("model artifact object not removed", extra={"error": type(exc).__name__})


def store_call(call: Callable[[], T]) -> T:
    """`call()`'s result, with the object store's failures as the SMO's errors: not configured or not answering 503 ENDPOINT_UNREACHABLE; a refusal,
    or bytes that do not match their digest, 502 UPSTREAM_FAILED (the detail names the store's answer, never a key)."""
    try:
        return call()
    except Refused:
        raise
    except ValueError as exc:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"the object store is not configured: {exc}") from None
    except httpx.TransportError as exc:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"the object store did not answer ({type(exc).__name__})") from None
    except objectstore.ObjectStoreError as exc:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"the object store: {exc}") from None
