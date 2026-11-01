"""TS 28.552 performance measurements (counters) of the NR gNB, as data: the families the sample rApps and the converged plan use (DRB, RRU, PEE, HO
and RRC).

TS 28.552 publishes its measurement definitions only as specification text: there is no OpenAPI or YANG list of them in `specs/` (`specs/MnS/xsd/measData.xsd`
is the file format, not the names). So each counter below is written as data with the clause that defines it. The clause numbers are those of
TS 28.552 Release 19 as recorded by hand; the specification text is not in `specs/` and was not reachable when this was written, so they have not been
checked against the text (open item PR-CONV-S1.3a). `name` is the measurement name of the definition's "Measurement Name" item (TS 32.404 form:
`Family.Measurement[.subcounter]`; a trailing `.Cause`, `.5QI` or `.cause` means one subcounter per cause or 5QI); `unit` is what one value counts.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Counter:
    """One TS 28.552 measurement: its name, family (the part before the first dot; `MM` for handovers), the clause that defines it, its unit, the object
    class it is measured on, and what it measures."""

    name: str
    clause: str
    unit: str
    measured_on: str
    description: str

    @property
    def family(self) -> str:
        """The measurement family: the name's part before the first dot (DRB, RRU, PEE, MM, RRC)."""
        return self.name.split(".", 1)[0]


COUNTERS: tuple[Counter, ...] = (
    # 5.1.1.1 Packet delay
    Counter("DRB.AirIfDelayDl", "5.1.1.1.1", "0.1 ms", "NRCellDU", "Average delay DL air-interface"),
    Counter("DRB.RlcSduDelayDl", "5.1.1.1.2", "0.1 ms", "NRCellDU", "Average delay in RLC sublayer of gNB-DU (DL)"),
    # 5.1.1.2 Radio resource utilization
    Counter("RRU.PrbTotDl", "5.1.1.2.1", "%", "NRCellDU", "DL total PRB usage: mean percentage of PRBs used in the downlink"),
    Counter("RRU.PrbTotUl", "5.1.1.2.2", "%", "NRCellDU", "UL total PRB usage: mean percentage of PRBs used in the uplink"),
    Counter("RRU.PrbUsedDl", "5.1.1.2.5", "PRBs", "NRCellDU", "DL PRBs used for data traffic"),
    Counter("RRU.PrbAvailDl", "5.1.1.2.6", "PRBs", "NRCellDU", "DL total available PRBs"),
    Counter("RRU.PrbUsedUl", "5.1.1.2.7", "PRBs", "NRCellDU", "UL PRBs used for data traffic"),
    Counter("RRU.PrbAvailUl", "5.1.1.2.8", "PRBs", "NRCellDU", "UL total available PRBs"),
    # 5.1.1.3 UE throughput
    Counter("DRB.UEThpDl", "5.1.1.3.1", "kbit/s", "NRCellDU", "Average DL UE throughput in gNB"),
    Counter("DRB.UEThpUl", "5.1.1.3.3", "kbit/s", "NRCellDU", "Average UL UE throughput in gNB"),
    # 5.1.1.4 RRC connection number
    Counter("RRC.ConnMean", "5.1.1.4.1", "connections", "NRCellCU", "Mean number of RRC connections"),
    Counter("RRC.ConnMax", "5.1.1.4.2", "connections", "NRCellCU", "Maximum number of RRC connections"),
    Counter("RRC.InactiveConnMean", "5.1.1.4.3", "connections", "NRCellCU", "Mean number of stored inactive RRC connections"),
    Counter("RRC.InactiveConnMax", "5.1.1.4.4", "connections", "NRCellCU", "Maximum number of stored inactive RRC connections"),
    # 5.1.1.6 Mobility management: 5.1.1.6.1 inter-gNB handovers, 5.1.1.6.2 intra-gNB handovers
    Counter("MM.HoPrepInterReq", "5.1.1.6.1.1", "attempts", "GNBCUCPFunction", "Requested preparations for outgoing inter-gNB handovers"),
    Counter("MM.HoPrepInterSucc", "5.1.1.6.1.2", "successes", "GNBCUCPFunction", "Successful preparations for outgoing inter-gNB handovers"),
    Counter("MM.HoPrepInterFail.cause", "5.1.1.6.1.3", "failures", "GNBCUCPFunction", "Failed preparations for outgoing inter-gNB handovers, per cause"),
    Counter("MM.HoResAlloInterReq", "5.1.1.6.1.4", "attempts", "GNBCUCPFunction", "Requested resource allocations for incoming inter-gNB handovers"),
    Counter("MM.HoResAlloInterSucc", "5.1.1.6.1.5", "successes", "GNBCUCPFunction",
            "Successful resource allocations for incoming inter-gNB handovers"),
    Counter("MM.HoExeInterReq", "5.1.1.6.1.7", "attempts", "GNBCUCPFunction", "Requested executions of outgoing inter-gNB handovers"),
    Counter("MM.HoExeInterSucc", "5.1.1.6.1.8", "successes", "GNBCUCPFunction", "Successful executions of outgoing inter-gNB handovers"),
    Counter("MM.HoExeIntraReq", "5.1.1.6.2.1", "attempts", "GNBCUCPFunction", "Requested executions of intra-gNB handovers"),
    Counter("MM.HoExeIntraSucc", "5.1.1.6.2.2", "successes", "GNBCUCPFunction", "Successful executions of intra-gNB handovers"),
    # 5.1.1.10 DRB setup
    Counter("DRB.EstabAtt.5QI", "5.1.1.10.1", "attempts", "NRCellCU", "DRBs attempted to set up, per 5QI"),
    Counter("DRB.EstabSucc.5QI", "5.1.1.10.2", "successes", "NRCellCU", "DRBs successfully set up, per 5QI"),
    # 5.1.1.15 RRC connection establishment
    Counter("RRC.ConnEstabAtt.Cause", "5.1.1.15.1", "attempts", "NRCellCU", "Attempted RRC connection establishments, per establishment cause"),
    Counter("RRC.ConnEstabSucc.Cause", "5.1.1.15.2", "successes", "NRCellCU", "Successful RRC connection establishments, per establishment cause"),
    # 5.1.1.19 Power, energy and environmental (PEE)
    Counter("PEE.AvgPower", "5.1.1.19.1", "W", "ManagedElement", "Average power over the period"),
    Counter("PEE.MinPower", "5.1.1.19.2", "W", "ManagedElement", "Minimum power over the period"),
    Counter("PEE.MaxPower", "5.1.1.19.3", "W", "ManagedElement", "Maximum power over the period"),
    Counter("PEE.Energy", "5.1.1.19.4", "kWh", "ManagedElement", "Energy consumed over the period"),
    Counter("PEE.AvgTemperature", "5.1.1.19.5", "degree Celsius", "ManagedElement", "Average temperature over the period"),
    Counter("PEE.MinTemperature", "5.1.1.19.6", "degree Celsius", "ManagedElement", "Minimum temperature over the period"),
    Counter("PEE.MaxTemperature", "5.1.1.19.7", "degree Celsius", "ManagedElement", "Maximum temperature over the period"),
    Counter("PEE.Voltage", "5.1.1.19.8", "V", "ManagedElement", "Voltage"),
    Counter("PEE.Current", "5.1.1.19.9", "A", "ManagedElement", "Current"),
    Counter("PEE.Humidity", "5.1.1.19.10", "%", "ManagedElement", "Humidity"),
    # 5.1.2 gNB-CU-UP: PDCP data volume
    Counter("DRB.PdcpSduVolumeDL", "5.1.2.1.1", "Mbit", "NRCellCU", "DL cell PDCP SDU data volume"),
    Counter("DRB.PdcpSduVolumeUL", "5.1.2.1.2", "Mbit", "NRCellCU", "UL cell PDCP SDU data volume"),
)
