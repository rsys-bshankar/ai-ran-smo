"""TE&IV's map between the 3GPP face and smo-teiv's model (plan tasks S3.19, S3.20, S3.21; ADR 0013 G1 to G4, section 11): one place that says, for each
TS 28.541 / TS 28.622 class TE&IV holds, which smo-teiv entity type stores it, in which domain, under which attribute names, and how each TS 28.541
reference attribute is a relationship. The reader (`reader.py`), the writer of ingestion events (`ingest.py`) and the decorator schema (`attrs.py`)
all read it, so the two directions cannot drift apart.

The DN and the id (G1). A TE&IV object's DN is its full 3GPP DN (`SubNetwork=...,ManagedElement=...,...`, TS 32.300), and smo-teiv keys it by
`urn:3gpp:dn:<DN>` (`teiv_id`); its only sourceId is that id, so every descendant of a DN X carries a sourceId that starts with `urn:3gpp:dn:X,`,
which is how a subtree is asked for (`reader.py`). Class names are TS 28.541's in the DN; smo-teiv's O-RAN names (ODUFunction for GNBDUFunction,
OCUCPFunction for GNBCUCPFunction) and its attribute names (`nRPCI` for TS 28.541's `nrPci`, `gNBDUId` for `gnbDuId`, ...) are mapped here.

What a class's attributes are, in three groups:

  native       an attribute of smo-teiv's model or of TE&IV's extension (teiv/model/smo-teiv-ext-*.yang), stored in smo-teiv's columns, so smo-teiv
               can filter on it (`scopeFilter /attributes[...]`). A TS 28.541 boolean is the extension's enumeration `true` / `false` (smo-teiv's
               schema generator has no YANG boolean, ADR 0013 section 11.5 c1): `to_teiv` and `from_teiv` convert.
  reference    a TS 28.541 attribute that names another object (NRCellDU.nRSectorCarrierRef, NRCellRelation.adjacentNRCellRef and nRFreqRelationRef):
               a relationship in smo-teiv, read back as the DN(s) it points at. adjacentNRCellRef is read from NEIGHBOR_CELL, which TE&IV writes
               with the neighbour as aSide (ADR 0013 section 11.5 b5: smo-teiv resolves a scope filter on a same-type relationship towards the aSide
               only) and with the NRCellRelation's DN as its third sourceId: reversing it gives the relation's adjacent cell.
  decorator    every other TS 28.541 attribute of the class (G3: administrativeState, operationalState, plmnInfoList, ...): smo-teiv's model has no
               column for it, so TE&IV keeps it as a decorator of TE&IV's own user-defined schema `smo-teiv-attrs` (attrs.py; DM R005 v06 Annex E),
               the data model's extension point for data beside an entity's attributes.

An attribute smo-teiv holds that TS 28.541 does not have (smo-teiv's `nCI` of NRCellDU, `frequencyDL` of NRSectorCarrier, ...) is not shown.
"""

from dataclasses import dataclass, field

TEIV_PREFIX = "urn:3gpp:dn:"
BOOLEAN = "boolean"       # a native attribute stored as the extension's enumeration Boolean_Type (`true` / `false`)


@dataclass(frozen=True)
class TeivClass:
    """One TS 28.541 class as smo-teiv holds it: the YANG module and entity type, the domain it is read in, and its native attributes (TS name ->
    (smo-teiv name, kind), kind BOOLEAN or "")."""

    name: str
    module: str
    entity_type: str
    domain: str
    native: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """`module:type`, the key of the class's entities in smo-teiv's answers and ingestion events."""
        return f"{self.module}:{self.entity_type}"


def _same(*names: str) -> dict[str, tuple[str, str]]:
    return {n: (n, "") for n in names}


def _bools(*names: str) -> dict[str, tuple[str, str]]:
    return {n: (n, BOOLEAN) for n in names}


RAN, OAM, EXT_RAN, EXT_OAM = "o-ran-smo-teiv-ran", "o-ran-smo-teiv-oam", "smo-teiv-ext-ran", "smo-teiv-ext-oam"
CLASSES: dict[str, TeivClass] = {c.name: c for c in (
    # smo-teiv 0.3.0's own model (O-RAN TE&IV-DM R004 v02.00 / R005 v04.00), with its names mapped to TS 28.541's
    TeivClass("ManagedElement", OAM, "ManagedElement", "OAM"),
    TeivClass("GNBDUFunction", RAN, "ODUFunction", "RAN", {"gnbDuId": ("gNBDUId", ""), "gnbId": ("gNBId", ""), "gnbIdLength": ("gNBIdLength", "")}),
    TeivClass("GNBCUCPFunction", RAN, "OCUCPFunction", "RAN", {"gnbCuName": ("gNBCUName", ""), "gnbId": ("gNBId", ""),
                                                             "gnbIdLength": ("gNBIdLength", "")}),
    TeivClass("NRCellDU", RAN, "NRCellDU", "RAN", {"cellLocalId": ("cellLocalId", ""), "nrPci": ("nRPCI", ""), "nRTAC": ("nRTAC", "")}),
    TeivClass("NRCellCU", RAN, "NRCellCU", "RAN", _same("cellLocalId")),
    TeivClass("NRSectorCarrier", RAN, "NRSectorCarrier", "RAN", _same("arfcnDL", "arfcnUL", "bSChannelBwDL")),
    # TE&IV's extension (teiv/model/, ADR 0013 section 11.3): TS 28.541 / TS 28.622 names, TS 28.541 booleans as Boolean_Type
    TeivClass("SubNetwork", EXT_OAM, "SubNetwork", "EXT_OAM", _same("dnPrefix", "userLabel", "userDefinedNetworkType", "setOfMcc", "priorityLabel")),
    TeivClass("NRCellRelation", EXT_RAN, "NRCellRelation", "EXT_RAN", {**_same("nRTCI", "cellIndividualOffset", "isESCoveredBy"),
                                                                        **_bools("isRemoveAllowed", "isHOAllowed", "isENDCAllowed", "isMLBAllowed")}),
    TeivClass("NRFreqRelation", EXT_RAN, "NRFreqRelation", "EXT_RAN", _same("offsetMO", "cellReselectionPriority", "pMax", "qOffsetFreq", "qRxLevMin",
                                                                            "tReselectionNr")),
    TeivClass("CommonBeamformingFunction", EXT_RAN, "CommonBeamformingFunction", "EXT_RAN", _same("coverageShape", "digitalAzimuth", "digitalTilt")),
    TeivClass("CESManagementFunction", EXT_RAN, "CESManagementFunction", "EXT_RAN",
              {**_same("intraRatEsActivationOriginalCellLoadParameters", "energySavingState", "energySavingControl"), **_bools("cesSwitch")}),
    TeivClass("DESManagementFunction", EXT_RAN, "DESManagementFunction", "EXT_RAN",
              {**_same("intraRatEsActivationOriginalCellLoadParameters", "energySavingState", "isProbingCapable"), **_bools("desSwitch")}),
    TeivClass("DMROFunction", EXT_RAN, "DMROFunction", "EXT_RAN",
              {**_same("maximumDeviationHoTriggerLow", "maximumDeviationHoTriggerHigh", "minimumTimeBetweenHoTriggerChange", "tstoreUEcntxt"),
               **_bools("dmroControl")}),
    TeivClass("DLBOFunction", EXT_RAN, "DLBOFunction", "EXT_RAN",
              {**_same("maximumDeviationHoTriggerLow", "maximumDeviationHoTriggerHigh", "minimumTimeBetweenHoTriggerChange"), **_bools("dlboControl")}),
    TeivClass("CCOFunction", EXT_RAN, "CCOFunction", "EXT_RAN", _bools("cCOControl")),
)}

# (parent class, child class) -> the smo-teiv relationship (module:TYPE) that carries the TS 28.622 name-containment, aSide the parent
CONTAINMENT: dict[tuple[str, str], str] = {
    ("SubNetwork", "SubNetwork"): f"{EXT_OAM}:SUBNETWORK_CONTAINS_SUBNETWORK",
    ("SubNetwork", "ManagedElement"): f"{EXT_OAM}:SUBNETWORK_CONTAINS_MANAGEDELEMENT",
    ("SubNetwork", "CCOFunction"): f"{EXT_OAM}:SUBNETWORK_CONTAINS_CCOFUNCTION",
    ("ManagedElement", "GNBDUFunction"): "o-ran-smo-teiv-rel-oam-ran:MANAGEDELEMENT_MANAGES_ODUFUNCTION",
    ("ManagedElement", "GNBCUCPFunction"): "o-ran-smo-teiv-rel-oam-ran:MANAGEDELEMENT_MANAGES_OCUCPFUNCTION",
    ("GNBDUFunction", "NRCellDU"): f"{RAN}:ODUFUNCTION_PROVIDES_NRCELLDU",
    ("GNBDUFunction", "NRSectorCarrier"): f"{RAN}:ODUFUNCTION_PROVIDES_NRSECTORCARRIER",
    ("GNBCUCPFunction", "NRCellCU"): f"{RAN}:OCUCPFUNCTION_PROVIDES_NRCELLCU",
    ("NRCellCU", "NRCellRelation"): f"{EXT_RAN}:NRCELLCU_CONTAINS_NRCELLRELATION",
    ("NRCellCU", "NRFreqRelation"): f"{EXT_RAN}:NRCELLCU_CONTAINS_NRFREQRELATION",
    ("NRCellCU", "CESManagementFunction"): f"{EXT_RAN}:NRCELLCU_CONTAINS_CESMANAGEMENTFUNCTION",
    ("NRCellCU", "DESManagementFunction"): f"{EXT_RAN}:NRCELLCU_CONTAINS_DESMANAGEMENTFUNCTION",
    ("NRCellCU", "DMROFunction"): f"{EXT_RAN}:NRCELLCU_CONTAINS_DMROFUNCTION",
    ("NRCellCU", "DLBOFunction"): f"{EXT_RAN}:NRCELLCU_CONTAINS_DLBOFUNCTION",
    ("NRSectorCarrier", "CommonBeamformingFunction"): f"{EXT_RAN}:NRSECTORCARRIER_CONTAINS_COMMONBEAMFORMINGFUNCTION",
}
# The CCOFunction's TS 28.541 parent is the SubNetwork (one each, ADR 0013 section 11.3); every other child's parent is its DN's own prefix.


@dataclass(frozen=True)
class Reference:
    """A TS 28.541 reference attribute held as a relationship: its type (module:TYPE), the domain it is read in, whether the attribute's object is the
    aSide (`own_side`), whether it is multi-valued, and, for NEIGHBOR_CELL, the sourceId position that names the object the attribute belongs to."""

    relationship: str
    domain: str
    own_side: str               # "aSide" or "bSide": where the object holding the attribute is
    many: bool = False
    owner_source: int | None = None    # NEIGHBOR_CELL: the attribute's object is not a side but the relationship's sourceId at this index

    @property
    def relationship_type(self) -> str:
        """The relationship type without its module, as smo-teiv's API paths name it."""
        return self.relationship.split(":", 1)[1]


NEIGHBOR_CELL = f"{EXT_RAN}:NRCELLCU_NEIGHBOR_CELL_NRCELLCU"
CO_COVERAGE_OVERLAY = f"{EXT_RAN}:NRCELLDU_CO_COVERAGE_OVERLAY_NRCELLDU"
REFERENCES: dict[tuple[str, str], Reference] = {
    ("NRCellDU", "nRSectorCarrierRef"): Reference(f"{RAN}:NRCELLDU_USES_NRSECTORCARRIER", "RAN", "aSide", many=True),
    ("NRCellRelation", "nRFreqRelationRef"): Reference(f"{EXT_RAN}:NRCELLRELATION_USES_NRFREQRELATION", "EXT_RAN", "aSide"),
    # NEIGHBOR_CELL: aSide the neighbour (the relation's adjacent cell), bSide the cell holding the relation, sourceIds[2] the NRCellRelation (S3.20)
    ("NRCellRelation", "adjacentNRCellRef"): Reference(NEIGHBOR_CELL, "EXT_RAN", "owner", owner_source=2),
}


def teiv_id(dn: str) -> str:
    """smo-teiv's id of the object at `dn` (G1: one to one)."""
    return TEIV_PREFIX + dn


def dn_of(identifier: str) -> str:
    """The DN of a smo-teiv id; ValueError for an id that is not TE&IV's."""
    if not identifier.startswith(TEIV_PREFIX):
        raise ValueError(f"not a TE&IV id: {identifier!r}")
    return identifier[len(TEIV_PREFIX):]


def class_of(dn: str) -> str:
    """The class of the last RDN of `dn`."""
    return dn.rsplit(",", 1)[-1].split("=", 1)[0]


def to_teiv(cls: TeivClass, name: str, value):
    """A TS 28.541 attribute value as smo-teiv's column holds it (a boolean as the Boolean_Type text)."""
    if cls.native[name][1] == BOOLEAN and isinstance(value, bool):
        return "true" if value else "false"
    return value


def from_teiv(cls: TeivClass, teiv_name: str, value):
    """(TS 28.541 name, value) of a smo-teiv attribute of `cls`, or None when TS 28.541 has no such attribute (it is not shown)."""
    for name, (stored, kind) in cls.native.items():
        if stored == teiv_name:
            if kind == BOOLEAN and value in ("true", "false"):
                return name, value == "true"
            return name, value
    return None


def children(parent: str) -> list[str]:
    """The classes TE&IV holds that a `parent` contains directly (CONTAINMENT, plus the ones whose parent is the DN prefix)."""
    return sorted({child for (p, child) in CONTAINMENT if p == parent})


def descendants(parent: str) -> list[str]:
    """Every class that can lie below an object of class `parent`, at any depth."""
    seen: list[str] = []
    todo = children(parent)
    while todo:
        cls = todo.pop()
        if cls not in seen:
            seen.append(cls)
            todo.extend(children(cls))
    return sorted(seen)
