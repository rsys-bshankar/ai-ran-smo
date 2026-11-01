### PR-CONV-S1.7 — SME's MnS registry: discovery by class, api-cat and optimisation domain; ServiceReady and NotifyServiceChange

**Built in the TS 28.622 form directly.** The plan row asked for a TS 28.533 registry (class → producer) first; it was built as PR-CONV-S1.12's
registry instead, so there is no conversion to do later (`history.d/PR-CONV-S1.12.md`).

**Discovery.** A flat getMOIAttributes of the registry (`smo_shared.mns.mns_registry.read`, every page) joined into `MnsEntry` values
(`entries`), filtered by `discover(class_name=, api_cat=, domain=)`: the class from the MgmtDataInfo the MnsInfo names, api-cat as the mnsType
(PROV_MNS, FAULT_SUPERVISION_MNS, ...) or one of the mnsCapability values (NR_PROVISIONING, ML_MODEL_MANAGEMENT, ...), the optimisation domain from
the TS 28.537 values in supportedManagementData. No route of SME's own: the ProvMnS filter grammar has no array membership, and a private
discovery route would fail the interface gate (ADR 0011), so the filtering is the library's, over the standard read.

**Events.** On the SMO event bus, in the commit of the change (`docs/EVENTS.md`): `org.oran.smo.mnsregistry.ServiceReady` when an MnsInfo is
created, `org.oran.smo.mnsregistry.NotifyServiceChange` with `changeType` MODIFIED or REMOVED; the data is the MnsInfo. Tested on the table bus,
where a vetoed subtree delete publishes nothing (the in-memory bus is not transactional).

**Conformance against the corrected SME contract.** SME's contract (`specs/smo-schema-bundle/openapi/modules/sme.yaml`, S0.7) lists the three
classes over ProvMnS; `scripts/check_contract_drift.py` now finds them served (SME's contract-only count fell from 28 to 19) and the schemathesis
contract test of SME exercises the four routes. It found that the router did not document its TS 28.532 error bodies and that the SMO's OpenAPI
hook replaced its 422 with the ProblemDetails envelope; the router now declares 400, 403, 404, 409, 412, 415 and 422 with the TS 28.532 body and
`x-smo-error-body`, which the hook respects (`smo_shared/openapi_security.py`).

**Not done.** Delivery of the two events to CAPIF subscribers and the 1 s bound (S4.6).
