"""TS 28.554 end-to-end KPIs, as data: name, clause, unit, and the formula over TS 28.552 counters (ts28552.py), including the §6.7.1 energy-efficiency
KPI S3.18 computes.

Like TS 28.552, TS 28.554 defines its KPIs only as specification text (nothing in `specs/`), so each KPI is written as data with its clause; the clauses
are TS 28.554 Release 19 as recorded by hand and not checked against the text here (open item PR-CONV-S1.3a). A formula is an arithmetic expression
over counter names (`+ - * /`, parentheses, numbers; `evaluate` in `__init__.py` computes it): the counters of one object over one period, a per-cause or
per-5QI counter standing for the sum of its subcounters. Unit conversions are written into the formula, so the result is in `unit`.

Not here, recorded: the availability (§6.7.5) and quality (§6.7.6) companions of the energy-efficiency KPI that the plan names (G5); their formulas are
not recorded yet, and S3.18 adds them with PR-CONV-S1.3a.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Kpi:
    """One TS 28.554 KPI: its name, the clause that defines it, its unit, the formula over TS 28.552 counters, and what it measures."""

    name: str
    clause: str
    unit: str
    formula: str
    description: str


KPIS: tuple[Kpi, ...] = (
    Kpi("EE.DataVolume", "6.7.1", "bit/J",
        "(DRB.PdcpSduVolumeDL + DRB.PdcpSduVolumeUL) * 1000000 / (PEE.Energy * 3600000)",
        "Energy efficiency of the NG-RAN, data-volume based: the PDCP SDU data volume (DL and UL, Mbit) per energy consumed (PEE.Energy, kWh)"),
    Kpi("DRBAccessibility", "6.2.2", "%",
        "100 * (RRC.ConnEstabSucc.Cause / RRC.ConnEstabAtt.Cause) * (DRB.EstabSucc.5QI / DRB.EstabAtt.5QI)",
        "DRB accessibility for UE services: RRC connection establishment success times DRB setup success"),
    Kpi("InterGnbHoSuccessRate", "6.5.1", "%",
        "100 * MM.HoExeInterSucc / MM.HoExeInterReq",
        "Inter-gNB handover success rate: successful over requested executions of outgoing inter-gNB handovers"),
    Kpi("DlUeThroughput", "6.3.1", "kbit/s",
        "DRB.UEThpDl",
        "Downstream throughput for the NG-RAN: the average DL UE throughput"),
)
