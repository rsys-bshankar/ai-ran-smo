### PR-CONV-S3.21 — The TS 28.541 attributes in TE&IV as decorators of TE&IV's own schema, and an object version with its If-Match check, ready for writes (in part: the rest is PR-CONV-S3.21a)

The rest, the attributes RAN NF OAM writes arriving through TE&IV's write path and a write carried out, is PR-CONV-S3.21a (it needs S3.4).

**The attributes.** smo-teiv's columns hold only what its model declares. Every other TS 28.541 attribute of the 15 classes (94 names: administrativeState,
operationalState, cellState, plmnInfoInfoList, ssbFrequency, the SON functions' settings, ...) is a decorator of `smo-teiv-attrs`, TE&IV's decorator
schema (CIMI R005 v07.00 section 4.5.3, the data model's way to add data to an entity): one string leaf per attribute, generated from the shared class
registry (`teiv/app/attrs.py`, `python -m app.attrs --write`; a test fails when `app/smo-teiv-attrs.yang` is stale), loaded through
createUserDefinedSchema (as `application/yang`, the only media type smo-teiv takes) by the readiness probe when it is absent, so a replica takes no
traffic before it is. A value is stored as itself when it is
plain text and otherwise as `j:` + base64url of its JSON, so no value TE&IV writes holds a character smo-teiv would paste into its SQL (ADR 0013 d2); a
plain-text decorator can be filtered on in smo-teiv. `ingest.encode` writes the decorators as updateDecorator calls of at most 100 entities (smo-teiv's
batch size), grouped by equal values, because smo-teiv's ingestion ignores the decorators of an event (d4).

**The version (G7).** smo-teiv's `lastModified` does not change when a decorator does (d3), so the version is a digest of the object's attributes in
TS 28.541 form (`reader.version`, 16 hex characters). A single-object getMOIAttributes answers it as `ETag`; modifyMOIAttributes, createMOI and deleteMOI
check the caller, the class, the instance and `If-Match` (RFC 9110: `*`, a list, weak tags) and answer **412 STALE_VERSION** with the current ETag for a
stale one, then 501 WRITE_NOT_AVAILABLE until S3.4's write path. A test changes a decorator and sees the version change.
