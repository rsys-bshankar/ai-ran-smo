---
section: Added
---
- **The MnS registry and routing ProvMnS by class through R1 (plan tasks S1.7, S1.8, S1.12): milestone M1.** SME serves the TS 28.622 MnS registry
  over ProvMnS at `/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1` (MnsRegistry, read only; MnsInfo and TS 28.537 MgmtDataInfo, one of each per
  producer), and publishes `org.oran.smo.mnsregistry.ServiceReady` and `.NotifyServiceChange` on the event bus (`docs/EVENTS.md`). Every module
  that serves ProvMnS registers itself there at start-up through R1 Termination (Echo today; `SMO_MNS_REGISTRATION=off` skips it,
  `SMO_MNS_ADDRESS` changes the address it registers). R1 Termination now routes `/ProvMnS/{version}/{LDN}` to the producer of the LDN's class
  that the registry names, with no entry in its route table (`R1_MNS_REGISTRY_CACHE_SECONDS`, default 10, is how long it keeps its copy of the
  registry): a 404 `NO_ROUTE` when nobody serves the class, a 503 `MNS_REGISTRY_UNAVAILABLE` when the registry cannot be read. An rApp may read
  a ProvMnS resource through R1 and change none. `GET /bootstrap` also lists `so-smos` and `aimgf` (a client that picks the first token
  endpoint, as the SDKs do, is unaffected). **Schema:** revision `sme_0001` (the first of the `sme` branch: `alembic_version` now reads
  `sme_0001`) creates `sme.mns_registry`, and SME's role gets the event feed and the audit chain; no secret or setting to change.
