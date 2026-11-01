### PR-CONV-S3.19 — TE&IV's ProvMnS read facade over smo-teiv: getMOIAttributes with scope and filter in the TS 28.532 shape, the TS 28.541 classes and DNs mapped, the filter translated, what smo-teiv cannot carry refused with the reason

**What was built.** `teiv/` became a module: `teiv/app/` mounts the shared ProvMnS router of `smo_shared.mns` on `TeivProvMnS`, whose store is O-RAN-SC
smo-teiv 0.3.0's exposure API (`app/client.py`, the only client of it in the SMO), so the URI, the error bodies, the hierarchical and flat answers,
paging and the S1.5 checks (caller and scope, MSAC, trace, metrics) are the shared ones. At start-up TE&IV registers its 15 classes in SME's MnS registry
(`install_self_registration`, S1.12) under its MnS scope `TEIV_MNS_SCOPE` (`SubNetwork=SMO-RAN`), so R1 Termination routes `/ProvMnS/...` of those classes
to it by class name with no static route (S1.8): `tests_integration/test_teiv_through_r1.py` reads Q1 to Q5 through R1 that way.

**The map (G1)** is `app/model.py`: a DN is smo-teiv's id `urn:3gpp:dn:<DN>` and its only sourceId; the classes ManagedElement, GNBDUFunction
(smo-teiv's ODUFunction), GNBCUCPFunction (OCUCPFunction), NRCellDU, NRCellCU, NRSectorCarrier, and the extension's SubNetwork, NRCellRelation,
NRFreqRelation, CommonBeamformingFunction, CESManagementFunction, DESManagementFunction, DMROFunction, DLBOFunction and CCOFunction; native attributes
under their TS 28.541 names (`nrPci` for `nRPCI`, `gnbDuId` for `gNBDUId`, `gnbCuName` for `gNBCUName`), smo-teiv's columns TS 28.541 lacks not shown,
references from relationships (nRSectorCarrierRef, nRFreqRelationRef, adjacentNRCellRef).

**The read (G8)** is `app/reader.py`: the base (an entity read), one list per class that can lie in the scope's depth range, one per reference attribute
asked for, all sent together; each with a sourceIds condition for "below the base" made of at most two short terms (smo-teiv compares
`"CD_sourceIds"::text LIKE '%x%'` through a trigram index, and the trigrams all ids share are what make it slow), a superset TE&IV then keeps exact by
DN. The TS 28.532 filter is evaluated by TE&IV on every candidate, so the answer is exactly the filter's; its top-level class and attribute equalities go
down first (`/attributes[@column=v]`, `/decorators[@smo-teiv-attrs:a='v']`). Refused with the reason, never answered wrongly: 422 CLASS_NOT_SUPPORTED,
INVALID_DN (behind the shared parser's 400), 422 NOT_EXPRESSIBLE over `TEIV_MAX_SCOPE_OBJECTS` candidates, 503 TEIV_UNAVAILABLE with Retry-After for
smo-teiv down or slower than `TEIV_READ_TIMEOUT_SECONDS`, 502 TEIV_REFUSED for smo-teiv refusing TE&IV's own request.

**The WG10 door** (`app/wg10.py`): the 16 operations of O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00 as routes with their standard operationIds, passed to
smo-teiv with only the standard's parameters; a scoped or MSAC-managed caller is sent to ProvMnS (403 SCOPE_DENIED: smo-teiv's answers cannot be cut to a
claim); TE&IV's own decorator schema and decorators are refused (403 TEIV_OWNED); a decorator name or value with `'`, `"` or `\` is refused (400
INVALID_DECORATOR_VALUE), because smo-teiv 0.3.0 writes the value into its SQL text unescaped, an SQL injection found here (ADR 0013 section 12.5 d2,
offered upstream with S3.25); the writes are audited. Routing `/teiv/...` through R1 and TE&IV's service in compose and the chart are the wiring pull
request that follows.

**The contract.** `teiv.yaml` names `teiv` as its code module and lists the 15 classes TE&IV registers (GNBCUUPFunction, which TE&IV does not map yet,
moved to `x-smo-pending`); `docs/openapi/teiv.json` is TE&IV's live schema (checked by `tests_integration/test_teiv_through_r1.py` until
`scripts/generate_openapi_specs.py` lists the module). teiv's contract drift went from 0 code-only / 56 contract-only to **0 / 0** (84 operations
agreed: the generic ProvMnS route serving the 15 classes' four operations, and the 16 WG10 routes); its interface baseline was empty and stays empty
(24 routes, all bundle operations). The WG10 answers' `required` members the standard declares and smo-teiv does not are present in every 0.3.0 answer
(13 domains, the RAN entity and relationship types).

**Measured** (`teiv/loadtest/queries.py`, 1,000 cells, through TE&IV, 200 runs, every answer checked: 0 wrong), p95: Q1 18.8 ms, Q3 14.4 ms, Q5 75.1 ms
(and Q2 20.8, Q4 113.6: S3.20). The first port gave Q5 965 ms; what brought it down is ADR 0013 section 12.4.

**Not taken.** Reading a one-level scope by containment association (`/contained-by-nrCellCu[@id=...]`) with a btree index on smo-teiv's `REL_FK_*`
columns: 4.6 ms against 8.8 ms for a cell's relations, but such a read misses an object whose containment edge was never written, which the sourceIds
condition never does. A cache of smo-teiv's answers in TE&IV: it would answer stale after a write TE&IV did not make.
