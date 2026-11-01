---
section: Changed
---
- **AIMgF keeps model artifacts in the object store, not the database (plan task S2.5; schema revision `aimgf_0002`).** An uploaded artifact is
  streamed to the object store's `models` bucket (SeaweedFS in the `objectstore` compose profile, or any S3) with its SHA-256, which every download
  checks and returns as `Repr-Digest` and `ETag`. Uploads accept ONNX (`.onnx`) and Safetensors (`.safetensors`), checked by content; a `.zip` is
  still accepted as the deprecated `zip-legacy`, with a `warnings` entry in the answer and the audit row; **a pickle is refused** (415
  `ARTIFACT_FORMAT_INVALID`) whatever the configuration. The new setting `SMO_AIMGF_ARTIFACT_FORMATS` (default `onnx,safetensors,zip-legacy`) chooses
  the formats. The upload answer adds `format` and `sha256`. TS 29.482 discovery with `include-models` carries an artifact inline up to 16 MiB.
  **Operators:** the compose stack now starts the object store (SeaweedFS, `objectstore` and `objectstore-init`) by default, no longer only in the
  `objectstore` profile. AIMgF and the `migrate` service need the object-store settings (`SMO_OBJECTSTORE_URL`, `SMO_OBJECTSTORE_ACCESS_KEY_FILE`,
  `SMO_OBJECTSTORE_SECRET_KEY_FILE`), which compose sets; with the Helm chart, point them at a store you run; revision `aimgf_0002` copies every stored artifact to the store, checks each against its digest and drops
  `model_artifact.content` (its downgrade reads them back). Without a store, artifact upload and download answer 503 and the upgrade of a database
  that holds artifacts stops before changing anything.
