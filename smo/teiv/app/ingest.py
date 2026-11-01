"""TE&IV's writes to smo-teiv in smo-teiv's forms (plan tasks S3.20, S3.21; ADR 0013 section 7, D6: TE&IV is the only producer of smo-teiv's topic): a
tree of TS 28.541 / TS 28.622 objects becomes smo-teiv ingestion CloudEvents (`ran-logical-topology.merge`, one topic, smo.teiv.ingestion.v1) and
`updateDecorator` calls of TE&IV's decorator schema. The ProvMnS write path of S3.4 will call it; today the load test (`teiv/loadtest/`) and the
tests do, with the same map the reader uses (model.py), so what TE&IV writes is what it reads back.

What one object becomes:

  the entity        `urn:3gpp:dn:<DN>`, its one sourceId the same id (the reader's subtree condition), its native attributes under smo-teiv's names
                    (a boolean as Boolean_Type text)
  its containment   the CONTAINMENT relationship from its parent (its DN's prefix, or `parent_dn` for a CCOFunction under its SubNetwork)
  its references    NRCellDU.nRSectorCarrierRef -> NRCELLDU_USES_NRSECTORCARRIER, NRCellRelation.nRFreqRelationRef -> NRCELLRELATION_USES_NRFREQRELATION,
                    and NRCellRelation.adjacentNRCellRef -> NRCELLRELATION_ADJACENT_NRCELLCU, NEIGHBOR_CELL with the neighbour as aSide and the
                    relation's DN as third sourceId (S3.20), and CO_COVERAGE_OVERLAY from the neighbour's NRCellDU to the cell's when isESCoveredBy is YES
  its decorators    every other TS 28.541 attribute, encoded (attrs.encode), in updateDecorator bodies: one per distinct set of values, for the
                    entities that share it, at most DECORATOR_BATCH of them (smo-teiv's limit); smo-teiv's ingestion ignores decorators in an event,
                    so they are written through the exposure API
A relationship id may hold only RFC 3986 unreserved and sub-delim characters plus `:`, and a relationship needs two sourceIds (ADR 0013 section 4).
"""

import datetime
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import attrs, model

MAX_ENTITIES_PER_EVENT = 300            # an event stays well under Kafka's default 1 MB message
DECORATOR_BATCH = 100                   # smo-teiv's consumer-data.batch-size: the most entities one updateDecorator may name
RELATIONSHIPS_PER_EVENT = 1000


@dataclass
class Moi:
    """One object to write: its DN, its TS 28.541 attributes, and `parent_dn` when its parent is not its DN's prefix (a CCOFunction)."""

    dn: str
    attributes: dict = field(default_factory=dict)
    parent_dn: str | None = None

    @property
    def cls(self) -> str:
        """Its class (the last RDN's)."""
        return model.class_of(self.dn)


def _rel_id(kind: str, a: str, b: str) -> str:
    return f"urn:oran:smo:teiv:{kind.split(':', 1)[1]}={a};{b}"


def encode(mois: Iterable[Moi]) -> tuple[list[dict], list[dict]]:
    """(event bodies, updateDecorator bodies) for `mois` (module description). Entities first, at most MAX_ENTITIES_PER_EVENT to an event, each
    relationship once both its ends are written, the rest in relationship-only events at the end. ValueError for a class TE&IV does not hold."""
    mois = list(mois)
    by_dn = {m.dn: m for m in mois}
    events: list[dict] = []
    entities: dict[str, list] = defaultdict(list)
    relationships: list[tuple[str, dict]] = []
    decorators: dict[str, list[str]] = defaultdict(list)
    count = 0

    def rel(kind: str, a: str, b: str, extra: str | None = None) -> None:
        sources = [model.teiv_id(a), model.teiv_id(b)] + ([model.teiv_id(extra)] if extra else [])
        relationships.append((kind, {"id": _rel_id(kind, a, b), "aSide": model.teiv_id(a), "bSide": model.teiv_id(b), "sourceIds": sources}))

    for m in mois:
        if m.cls not in model.CLASSES:
            raise ValueError(f"TE&IV holds no {m.cls} ({m.dn})")
        cls = model.CLASSES[m.cls]
        native, decor = {}, {}
        for name, value in m.attributes.items():
            ref = model.REFERENCES.get((m.cls, name))
            if ref is not None:
                for target in value if isinstance(value, list) else [value]:
                    if target not in by_dn:
                        continue
                    if ref.owner_source is not None:          # adjacentNRCellRef: ADJACENT, and NEIGHBOR_CELL reversed (S3.20)
                        cell = m.dn.rsplit(",", 1)[0]
                        rel(f"{model.EXT_RAN}:NRCELLRELATION_ADJACENT_NRCELLCU", m.dn, target)
                        rel(model.NEIGHBOR_CELL, target, cell, extra=m.dn)
                        if m.attributes.get("isESCoveredBy") == "YES" and _du(target) in by_dn and _du(cell) in by_dn:
                            rel(model.CO_COVERAGE_OVERLAY, _du(target), _du(cell), extra=m.dn)
                    else:
                        rel(ref.relationship, m.dn, target)
            elif name in cls.native:
                native[cls.native[name][0]] = model.to_teiv(cls, name, value)
            else:
                decor[attrs.key(name)] = attrs.encode(value)
        entity = {"id": model.teiv_id(m.dn), "sourceIds": [model.teiv_id(m.dn)]}
        if native:
            entity["attributes"] = native
        entities[cls.qualified].append(entity)
        if decor:
            decorators[json.dumps(decor, sort_keys=True)].append(model.teiv_id(m.dn))
        parent = m.parent_dn or (m.dn.rsplit(",", 1)[0] if "," in m.dn else None)
        if parent and parent in by_dn:
            kind = model.CONTAINMENT.get((model.class_of(parent), m.cls))
            if kind:
                rel(kind, parent, m.dn)
        count += 1
        if count >= MAX_ENTITIES_PER_EVENT:
            events.append({"entities": [{k: v} for k, v in entities.items()], "relationships": []})
            entities, count = defaultdict(list), 0
    if count:
        events.append({"entities": [{k: v} for k, v in entities.items()], "relationships": []})
    for i in range(0, len(relationships), RELATIONSHIPS_PER_EVENT):
        chunk: dict[str, list] = defaultdict(list)
        for kind, body in relationships[i:i + RELATIONSHIPS_PER_EVENT]:
            chunk[kind].append(body)
        events.append({"entities": [], "relationships": [{k: v} for k, v in chunk.items()]})
    decorator_calls = [{"operation": "merge", "decorators": json.loads(values), "entityIds": ids[i:i + DECORATOR_BATCH]}
                       for values, ids in decorators.items() for i in range(0, len(ids), DECORATOR_BATCH)]
    return events, decorator_calls


def _du(cu_cell: str) -> str:
    """The NRCellDU DN of the cell whose NRCellCU DN is given (TE&IV's trees name both sides of a cell alike: GNBCUCPFunction=1,NRCellCU=n and
    GNBDUFunction=1,NRCellDU=n of one ManagedElement)."""
    return cu_cell.replace("GNBCUCPFunction=1,NRCellCU=", "GNBDUFunction=1,NRCellDU=")


def cloud_event_line(body: dict, ce_type: str = "ran-logical-topology.merge", source: str = "teiv") -> str:
    """One event as a line for `kafka-console-producer --property parse.headers=true` (headers `k:::v` joined by `,,,`, then `,,,` and the body)."""
    headers = {"ce_specversion": "1.0", "ce_id": str(uuid.uuid4()), "ce_source": source, "ce_type": ce_type, "content-type": "application/yang-data+json",
               "ce_time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "ce_dataschema": "https://teiv:8080/schemas/v1/r1-topology"}
    return ",".join(f"{k}:::{v}" for k, v in headers.items()) + ",,," + json.dumps(body, separators=(",", ":"))
