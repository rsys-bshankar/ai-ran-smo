### PR-CONV-S3.20 — The model extension's 3GPP face: booleans-as-enumeration, the reversed NEIGHBOR_CELL side and the two-hop fan-out mapped back to TS 28.541 forms; Q2 and Q4 at p95 ≤ 150 ms at 1,000 cells through teiv/

Model work only, as D19's go amended the row: the two extension modules were already in `teiv/model/` and built into smo-teiv's images by S3.23. What
this adds is the face TS 28.541 callers see, in TE&IV's facade (`teiv/app/model.py`, `reader.py`, `ingest.py`):

- **Booleans.** smo-teiv's generator maps no YANG boolean (ADR 0013 c1), so the extension holds isHOAllowed, isRemoveAllowed, isENDCAllowed, isMLBAllowed,
  cesSwitch, dmroControl, ... as its enumeration `true` / `false`; TE&IV writes a TS 28.541 boolean as that text and reads it back as a boolean, and a
  filter `@.attributes.isHOAllowed == true` is TE&IV's to evaluate (it is not pushed down).
- **The reversed NEIGHBOR_CELL side.** NEIGHBOR_CELL is written with the neighbour as aSide, the cell holding the relation as bSide and the
  NRCellRelation's DN as third sourceId (c2's workaround, 11.5 b5); TE&IV's NRCellRelation.adjacentNRCellRef is read from it reversed: the relation is
  the sourceId, the adjacent cell the aSide. `ingest.encode` writes NRCELLRELATION_ADJACENT_NRCELLCU and NEIGHBOR_CELL together, and CO_COVERAGE_OVERLAY
  from the neighbour's NRCellDU to the cell's when isESCoveredBy is YES.
- **Two hops.** TS 28.532 has no two-hop read; the TS 28.541 form is the caller's fan-out: Q2 (the NRCellCU, BASE_NTH_LEVEL 1, filter NRCellRelation,
  attributes adjacentNRCellRef), then Q2 of each adjacent cell, sent together (11.5 b6).
- **The enum values.** smo-teiv refuses a user-defined schema while any loaded module has an `enum` without a `value`; every enum of
  `smo-teiv-ext-ran.yang` now has its value, and `model/check_extension.py` rule E8 holds it.

**Measured** (`teiv/loadtest/queries.py`, 1,000 cells, 8,000 relations, through TE&IV, 200 runs, 0 wrong answers; the Claude Code cloud container, 4
CPUs): **Q2 p95 20.8 ms, Q4 p95 113.6 ms** (p50 16.3 and 82.9). The first port answered Q4 at 473 ms; what it took (a rebuilt trigram index after the
bulk load, the short sourceIds condition, skipping references not selected, the read's requests sent at once, three TE&IV workers) is ADR 0013 section 12.4. D7 holds: Apache AGE is not
reconsidered, and section 9's fallback is not taken.
