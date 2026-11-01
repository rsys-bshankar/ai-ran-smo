### PR-CONV-S2.5 — Model artifacts out of the database into the object store; ONNX and Safetensors only

AIMgF's `model_artifact` no longer holds bytes. An artifact is an object in the object store's `models` bucket (`smo_shared.objectstore`, D10,
S1.10: SeaweedFS in compose's `objectstore` profile, any S3 otherwise) at `<modelId>/<artifactId><ext>`, stored with its SHA-256 as metadata; the
row keeps `object_key`, `sha256`, `artifact_format` (CHECK `onnx`, `safetensors`, `zip-legacy`) and `size_bytes` (`aimgf/app/registry/artifacts.py`,
`aimgf/README.md` 2.4 "Model artifacts in the object store").

**Format gate.** The setting `SMO_AIMGF_ARTIFACT_FORMATS` (comma separated; default `onnx,safetensors,zip-legacy`) says what an upload may be. ONNX
is recognised by walking its top-level protobuf fields (every one a ModelProto field with its wire type, `ir_version` and `graph` present, the last
ending at the end of the file; length-delimited fields are skipped, not read), Safetensors by its u64 header length and a JSON header of tensors
whose data offsets end exactly where the file does (header at most 8 MiB). No ONNX or Safetensors library was added: both checks are a few dozen lines
of standard library. `zip-legacy` keeps the rule from before S2.5, the name ending `.zip`, because every caller still uploads one (the four sample
rApps' JSON-in-zip models, the SDK's `store_model` default, the GUI's upload, `DEMO_RUNBOOK.md`, the integration tests); it answers with a
`warnings` entry and writes the same warning into the audit row (`UPLOAD_MODEL_ARTIFACT`, with format, digest and size), and every accepted upload
is logged with its format. Moving the callers and then dropping `zip-legacy` from the default is now part of plan rows `PR-CONV-S7.1` and
`PR-CONV-S7.13`. **Pickle is refused whatever the setting** (direction document Part 2 §1, ADR 0010 section 3.6): a pickle header (protocol 2 to
5), a name ending `.pkl`, `.pickle`, `.joblib`, `.pt`, `.pth` or `.ckpt`, and a zip holding a member that is one by name or content (a PyTorch `.pt`
is a zip holding `data.pkl`). The setting naming `pickle`, or a format it does not know, stops AIMgF at start-up. Anything else, and a name whose
extension is not its content's, is 415 `ARTIFACT_FORMAT_INVALID` with the reason. Not done: a protocol 0 or 1 pickle (text opcodes) is not
recognised as a pickle, but it is neither ONNX nor Safetensors and so is refused all the same, except inside a zip-legacy upload, whose content is
checked for pickle only; that gap closes with zip-legacy.

**Streaming and the memory bound.** `smo_shared.objectstore` gained `put_stream` (a body from chunks whose size and SHA-256 are known before the
request, since S3 signs the payload's SHA-256), `open_stream` / `get_stream` (chunks hashed as they are read, IntegrityError at the end on a
mismatch) and `CHUNK` = 1 MiB. The upload is the multipart file Starlette spools to disk; AIMgF reads it 1 MiB at a time to hash and check it, and
again to send it. The download is a `StreamingResponse` over the store's chunks, with `Content-Length`, `Repr-Digest` (RFC 9530) and `ETag`; the
stored object's digest is compared with the row's before the first byte (502 `UPSTREAM_FAILED`), and bytes that stop matching on the way cut the
response off rather than complete a wrong one. The stated size: no route reads more than 1 MiB of an artifact at once, except TS 29.482 discovery
with `include-models`, which must carry `mlModel` inline as base64 and does so only for an artifact of at most 16 MiB (`INLINE_LIMIT`); a larger one
is listed by `mlModelId` alone (`mlModel` is optional and has no by-reference form). Store failures: none configured or no answer 503
`ENDPOINT_UNREACHABLE`, a refusal 502. The object is written before its row; a row that cannot be written takes its object with it. Deregistering a
model deletes its objects after the commit (a store that refuses leaves an orphan and a warning log).

**Size and upload method.** R1 Termination's 50 MiB body limit stays. One S3 PUT carries up to 5 GiB, so multipart upload is not needed for anything
that limit admits, and was not built. A model above 50 MiB needs a presigned upload (the uploader PUTs to the store with a URL AIMgF signs; AIMgF
then checks digest and format before recording it). That needs the store reachable from outside (through the edge), a presigned-URL signer in
`smo_shared.objectstore` and two routes that must be bundle operations (ADR 0011): not in AIMgF's lane, so not done; it is the next step if models
outgrow 50 MiB.

**Migration `aimgf_0002`** (the `aimgf` branch, after `aimgf_0001`). Upgrade: adds the three columns; for each row, Postgres computes the SHA-256
(`sha256(content)`), the bytes are read 1 MiB per query (`substring`) and streamed to the store, the object is read back and hashed, then the row
records key, digest and format (from the file name; every row uploaded before S2.5 is zip-legacy); then the columns become NOT NULL, the check is
added and `content` is dropped. It prints the rows and bytes it moved. Downgrade: `content` is added back and filled from the store chunk by chunk
(`content || chunk`), checked in Postgres against the row's digest and size, made NOT NULL, and the three columns and the check are dropped; the
objects stay in the store, and upgrading again writes the same keys. A table without rows needs no store; with rows and none configured, the upgrade
stops before changing anything. Tested on Postgres 18 with five rows from 0 bytes to 2.5 MiB (3,671,019 bytes): after the upgrade every artifact is
readable from the store with the digest its row records, the downgrade restores `content` byte for byte, and a second upgrade works
(`aimgf/tests/test_artifact_migration.py`), against a directory store and against the compose profile's SeaweedFS 4.48. The same run through
AIMgF's routes against SeaweedFS stored, streamed back and deleted a 3 MiB Safetensors file.

**Test backend.** `smo_shared.objectstore.LocalStore` serves the same methods over `memory://` (one dictionary per process) or `file:///dir`, chosen
by `SMO_OBJECTSTORE_URL` through `open_store()`; with no URL and the tests' `SMO_ALLOW_SQLITE_FALLBACK` it is `memory://`, so the unit suites and
the integration mesh get the in-memory store with the in-memory database and need no change. This is the one change outside `aimgf/` (the helper
and its test).

**Wiring the module manifest must carry** (Session A's module manifests; not edited here): AIMgF and the `migrate` service get
`SMO_OBJECTSTORE_URL=http://objectstore:8333`, `SMO_OBJECTSTORE_ACCESS_KEY_FILE` and `SMO_OBJECTSTORE_SECRET_KEY_FILE` with the secrets
`objectstore_access_key` and `objectstore_secret_key`, and depend on `objectstore-init` having completed; the object store leaves the optional
`objectstore` profile for any stack that runs AIMgF (or AIMgF's artifact routes answer 503); the chart gets the same for its `aimgf` and migration
job. `SMO_AIMGF_ARTIFACT_FORMATS` needs no wiring while its default holds.
