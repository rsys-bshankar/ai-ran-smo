"""The 1,000-cell topology TE&IV is measured on (plan tasks S3.19 to S3.23; ADR 0013 section 11.4's tree, in TE&IV's own forms): a SubNetwork
`SubNetwork=SMO-RAN` (TE&IV's MnS scope) with a CCOFunction, `cells` NRCellDU three to a gNB, each gNB a ManagedElement with a GNBDUFunction and a
GNBCUCPFunction; per cell an NRSectorCarrier with a CommonBeamformingFunction on the DU side, and on the CU side an NRCellCU with an NRFreqRelation,
the four cell-level SON functions and 8 NRCellRelation (adjacentNRCellRef to cells of nearby gNBs, in a ring, so two-hop neighbourhoods overlap as in a
real grid; the first relation of each cell says the neighbour covers it for energy saving). 18,004 objects at 1,000 cells. TS 28.541 attribute names
and values throughout, including attributes TE&IV holds as decorators (administrativeState, operationalState, cellState, plmnInfoInfoList, ...), so
the decorator path of S3.21 is loaded and read too. Values are plausible, not real, and the same for a seed.
"""

import random

from app.ingest import Moi    # teiv/ on the path (budget.py, the tests)

SUBNETWORK = "SubNetwork=SMO-RAN"
PER_GNB = 3
NEIGHBOURS = 8


def me_dn(gnb: int) -> str:
    """The ManagedElement of gNB number `gnb`."""
    return f"{SUBNETWORK},ManagedElement=gNB-{gnb:04d}"


def du_cell(gnb: int, cell: int) -> str:
    """The NRCellDU `cell` (1..3) of gNB `gnb`."""
    return f"{me_dn(gnb)},GNBDUFunction=1,NRCellDU={cell}"


def cu_cell(gnb: int, cell: int) -> str:
    """The NRCellCU `cell` (1..3) of gNB `gnb`."""
    return f"{me_dn(gnb)},GNBCUCPFunction=1,NRCellCU={cell}"


def synthetic(cells: int = 1000, seed: int = 7) -> list:
    """The tree of the module description, as ingest.Moi objects."""
    rnd = random.Random(seed)  # noqa: S311 (synthetic test data)
    out = [Moi(SUBNETWORK, {"dnPrefix": "", "userLabel": "smo-ran", "setOfMcc": ["001"], "priorityLabel": 1}),
           Moi(f"{SUBNETWORK},CCOFunction=1", {"cCOControl": True}, parent_dn=SUBNETWORK)]
    gnbs = (cells + PER_GNB - 1) // PER_GNB
    cus: list[str] = []
    for g in range(gnbs):
        me = me_dn(g)
        out.append(Moi(me, {"vendorName": "smo-lab", "swVersion": "1.0", "userLabel": f"gNB-{g:04d}"}))
        du, cu = f"{me},GNBDUFunction=1", f"{me},GNBCUCPFunction=1"
        out.append(Moi(du, {"gnbDuId": g, "gnbId": 1000 + g, "gnbIdLength": 22, "gnbDuName": f"du-{g}", "userLabel": f"du-{g}"}))
        out.append(Moi(cu, {"gnbCuName": f"cu-{g}", "gnbId": 1000 + g, "gnbIdLength": 22, "plmnId": {"mcc": "001", "mnc": "01"}}))
        for c in range(1, PER_GNB + 1):
            n = g * PER_GNB + c - 1
            if n >= cells:
                break
            sc = f"{du},NRSectorCarrier={c}"
            out.append(Moi(sc, {"arfcnDL": 630000 + c, "arfcnUL": 630000 + c, "bSChannelBwDL": 100, "bSChannelBwUL": 100, "txDirection": "DL_AND_UL",
                                "configuredMaxTxPower": 40000}))
            out.append(Moi(f"{sc},CommonBeamformingFunction=1", {"coverageShape": 1, "digitalAzimuth": 1200 * c - 2400, "digitalTilt": 30}))
            out.append(Moi(du_cell(g, c), {"cellLocalId": c, "nrPci": n % 1008, "nRTAC": 100 + g % 50,
                                           "administrativeState": rnd.choice(["UNLOCKED", "UNLOCKED", "LOCKED"]), "operationalState": "ENABLED",
                                           "cellState": "ACTIVE", "ssbFrequency": 630000 + c,
                                           "plmnInfoInfoList": [{"plmnId": {"mcc": "001", "mnc": "01"}, "snssai": {"sst": 1}}],
                                           "nRSectorCarrierRef": [sc]}))
            cell = cu_cell(g, c)
            cus.append(cell)
            out.append(Moi(cell, {"cellLocalId": c, "plmnInfoList": [{"plmnId": {"mcc": "001", "mnc": "01"}}]}))
            out.append(Moi(f"{cell},NRFreqRelation=1", {"offsetMO": {"rsrpOffsetSsb": 0}, "cellReselectionPriority": 5, "pMax": 23, "qOffsetFreq": 0,
                                                        "qRxLevMin": -120, "tReselectionNr": 1}))
            out.append(Moi(f"{cell},CESManagementFunction=1", {"cesSwitch": True, "energySavingState": "isNotEnergySaving",
                                                               "intraRatEsActivationOriginalCellLoadParameters": {"loadThreshold": 10, "timeDuration": 900}}))
            out.append(Moi(f"{cell},DESManagementFunction=1", {"desSwitch": False, "energySavingState": "isNotEnergySaving", "isProbingCapable": "no"}))
            out.append(Moi(f"{cell},DMROFunction=1", {"dmroControl": True, "maximumDeviationHoTriggerLow": -6, "maximumDeviationHoTriggerHigh": 6,
                                                      "minimumTimeBetweenHoTriggerChange": 60, "tstoreUEcntxt": 50}))
            out.append(Moi(f"{cell},DLBOFunction=1", {"dlboControl": True, "maximumDeviationHoTriggerLow": -4, "maximumDeviationHoTriggerHigh": 4,
                                                      "minimumTimeBetweenHoTriggerChange": 60}))
    for n, cell in enumerate(cus):
        for k in range(1, NEIGHBOURS + 1):
            target = cus[(n + k * PER_GNB + (k % PER_GNB)) % len(cus)]
            out.append(Moi(f"{cell},NRCellRelation={k}", {"nRTCI": k, "adjacentNRCellRef": target, "nRFreqRelationRef": f"{cell},NRFreqRelation=1",
                                                          "isHOAllowed": True, "isRemoveAllowed": True, "isESCoveredBy": "YES" if k == 1 else "NO",
                                                          "cellIndividualOffset": {"rsrpOffsetSsb": 0}}))
    return out
