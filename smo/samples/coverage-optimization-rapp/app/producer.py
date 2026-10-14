"""Sample coverage data for the Coverage Optimization rApp (Wave 10.3,
W10.3-03).

  * `propagation` is a small, deterministic stand-in for the radio
    network. It turns a cluster's tilt and power settings, plus an
    injected fault, into each cell's problem shares and neighbour overlaps.
    The integration tests and demo.py feed it the **live** O1 settings read
    back from the mock adaptor. So every tilt or power change the rApp makes
    shows up in the next hour's PM, and the loop really closes.
  * `cell_counters` / `measurements` shape that as the PM reports an NF
    sends RAN NF OAM (`POST /ran-nf-oam/pm-reports`, counter type
    COVERAGE_PERFORMANCE). RAN NF OAM delivers them to DME.
  * `history` is training history in which tilt and power were varied, a
    cell at a time, so the sensitivities can be learned.
  * The Digital Twin producer registers `COVERAGE_PERFORMANCE_SIM`
    (sourceDomain DIGITAL_TWIN) and publishes clusters with a known
    injected fault, so emulation can score the optimiser's moves.

The propagation model is linear in each cell's reach. Reach is the uptilt t
in degrees below the baseline tilt, plus the power step p in dB over the
baseline power:

    weak_i      = W_i − 0.8 t_i − 1.5 p_i − Σ_j c_ij (0.3 t_j + 0.3 p_j)
    overshoot_i = O_i + 1.8 t_i + 0.6 p_i
    pollution_i = P_i + 0.2 (t_i + p_i) + Σ_j c_ij (1.2 t_j + 0.6 p_j)
    overlap_ij  = L_ij + 2.0 t_j + 1.0 p_j − 0.5 (t_i + p_i)

Here c_ij = L_ij / 10. Injected faults change the bases:
  * WEAK_COVERAGE at a cell raises its W;
  * OVERSHOOT at a cell raises its O. It also raises its neighbours' P and
    their overlap with it, because it reaches into them;
  * PILOT_POLLUTION at a cell raises its P and its overlap with every
    neighbour.
"""

import datetime
import hashlib
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from smo_shared import mtls

from .model.CoverageModel import MOVES
from .model.series import OVERLAP, OVERSHOOT, POLLUTION, POWER, TILT, TOTAL, WEAK

router = APIRouter()

SIM_PRODUCER_ID = "coverage-dt-producer"   # only when the caller's own invoker id cannot be told: DME takes an rApp's registration under that id alone (SEC-15.10b)
SIM_TYPE = {"namespace": "RAN", "name": "COVERAGE_PERFORMANCE_SIM", "version": "1.0.0",
            "typeName": "RAN.COVERAGE_PERFORMANCE_SIM"}
SELF_URL = mtls.http_url("http://coverage-optimization-rapp:8000")   # https:// with SMO_MTLS=on (PR-SEC-2): the address DME calls back

BASELINE_TILT = 60      # 6.0° (CommonBeamformingFunction.digitalTilt, 0.1°)
BASELINE_POWER = 43     # dBm (NRSectorCarrier.configuredMaxTxPower)
HEALTHY_SHARE = 2.0
BASE_OVERLAP = 10.0
SCENARIOS = ("HEALTHY", "WEAK_COVERAGE", "OVERSHOOT", "PILOT_POLLUTION")

# the sample cluster: four cells of one gNB and who neighbours whom
CELLS = ["301", "302", "303", "304"]
NEIGHBOURS = {"301": ["302", "303"], "302": ["301", "303", "304"], "303": ["301", "302", "304"], "304": ["302", "303"]}

_LOAD = [(0, 0.05), (5, 0.05), (8, 0.8), (12, 0.9), (18, 1.0), (22, 0.4), (24, 0.05)]


def load(hour: float) -> float:
    """The sample traffic load (0.05 to 1.0) at an hour of the day, interpolated between the points of the daily curve."""
    for (h0, v0), (h1, v1) in zip(_LOAD, _LOAD[1:]):
        if h0 <= hour <= h1:
            return v0 + (v1 - v0) * (hour - h0) / (h1 - h0)
    return 0.05


def _jitter(key: str, t: datetime.datetime, spread: float = 0.08) -> float:
    return (hashlib.sha256(f"{key}{t.isoformat()}".encode()).digest()[0] / 255 - 0.5) * spread


def _reach(settings: dict, cell: str) -> tuple[float, float]:
    tilt, power = settings.get(cell, (BASELINE_TILT, BASELINE_POWER))
    return -(float(tilt) - BASELINE_TILT) / 10.0, float(power) - BASELINE_POWER


def propagation(neighbours: dict[str, list[str]], settings: dict[str, tuple[float, float]],
                faults: dict[str, str] | None = None) -> dict[str, dict]:
    """{cell: {"shares": {...}, "overlaps": {nbr: %}}} for the given tilt/power
    `settings` ({cell: (digitalTilt, configuredMaxTxPower)}) and injected
    `faults` ({cell: scenario})."""
    faults = faults or {}
    base: dict[str, dict[str, Any]] = {c: {"W": HEALTHY_SHARE, "O": HEALTHY_SHARE, "P": HEALTHY_SHARE,
                "L": {j: BASE_OVERLAP for j in neighbours[c]}} for c in neighbours}
    for cell, scenario in faults.items():
        if scenario == "WEAK_COVERAGE":
            base[cell]["W"] = 12.0
        elif scenario == "OVERSHOOT":
            base[cell]["O"] = 12.0
            for j in neighbours[cell]:
                base[j]["P"] += 7.0
                base[j]["L"][cell] = base[j]["L"].get(cell, 0.0) + 10.0
        elif scenario == "PILOT_POLLUTION":
            base[cell]["P"] = 11.0
            for j in neighbours[cell]:
                base[cell]["L"][j] += 5.0
    out = {}
    for c, b in base.items():
        t, p = _reach(settings, c)
        nt = sum(b["L"][j] / 10.0 * _reach(settings, j)[0] for j in b["L"])
        np_ = sum(b["L"][j] / 10.0 * _reach(settings, j)[1] for j in b["L"])
        shares = {"WEAK_COVERAGE": b["W"] - 0.8 * t - 1.5 * p - 0.3 * nt - 0.3 * np_,
                  "OVERSHOOT": b["O"] + 1.8 * t + 0.6 * p,
                  "PILOT_POLLUTION": b["P"] + 0.2 * (t + p) + 1.2 * nt + 0.6 * np_}
        overlaps = {j: L + 2.0 * _reach(settings, j)[0] + 1.0 * _reach(settings, j)[1] - 0.5 * (t + p)
                    for j, L in b["L"].items()}
        out[c] = {"shares": {k: max(0.0, min(100.0, v)) for k, v in shares.items()},
                  "overlaps": {j: max(0.0, min(100.0, v)) for j, v in overlaps.items()}}
    return out


def cell_counters(cell: str, t: datetime.datetime, radio: dict, setting: tuple[float, float],
                  total: int | None = None) -> dict:
    """One cell's PM window: report counts from its shares, plus its CM snapshot."""
    n = total if total is not None else int(400 + 1600 * load(t.hour + t.minute / 60))
    counts: dict[str, float] = {TOTAL: n}
    for counter, key in ((WEAK, "WEAK_COVERAGE"), (OVERSHOOT, "OVERSHOOT"), (POLLUTION, "PILOT_POLLUTION")):
        counts[counter] = max(0, round(n * radio["shares"][key] / 100 * (1 + _jitter(cell + counter, t))))
    for j, ov in radio["overlaps"].items():
        counts[f"{OVERLAP}{j}"] = max(0, round(n * ov / 100 * (1 + _jitter(cell + j, t))))
    counts[TILT], counts[POWER] = setting
    return counts


def measurements(neighbours: dict[str, list[str]], settings: dict[str, tuple[float, float]], faults: dict[str, str],
                 t: datetime.datetime, overrides: dict[str, dict] | None = None, extra: dict | None = None) -> list[dict]:
    """PM measurements of every cell for the window starting at `t`."""
    radio = propagation(neighbours, settings, faults)
    out = []
    for c in neighbours:
        values = (overrides or {}).get(c) or cell_counters(c, t, radio[c], settings.get(c, (BASELINE_TILT, BASELINE_POWER)))
        out.append({"cellId": c, "values": values, "timestamp": t.isoformat(), **(extra or {})})
    return out


def exploration(cells: list[str], hours: int) -> list[dict[str, tuple[float, float]]]:
    """Per hour, the settings of a history in which, every 3 hours, one cell
    (in turn) was stepped one tilt or power step off its baseline, and the
    previously stepped cell went back to it."""
    out = []
    for h in range(hours):
        settings: dict[str, tuple[float, float]] = {c: (BASELINE_TILT, BASELINE_POWER) for c in cells}
        block = h // 3
        if block:
            cell = cells[block % len(cells)]
            d_tilt, d_power = MOVES[list(MOVES)[1 + hashlib.sha256(f"{cell}{block}".encode()).digest()[0] % 4]]
            settings[cell] = (BASELINE_TILT + 10 * d_tilt, BASELINE_POWER + d_power)
        out.append(settings)
    return out


def history(neighbours: dict[str, list[str]], start: datetime.datetime, hours: int,
            faults: dict[str, str] | None = None) -> list[dict]:
    """Training history: `hours` hourly windows for the cluster from `start`, with the settings varied by `exploration` so tilt and power effects
    can be learned.
    """
    cells = list(neighbours)
    return [m for h, settings in enumerate(exploration(cells, hours))
            for m in measurements(neighbours, settings, faults or {}, start + datetime.timedelta(hours=h))]


# ---------------------------------------------------------------- Digital Twin producer (DME callbacks)

# Request body of POST /sim-producer/publish: the managed element, the clusters to publish ({cluster: {scenario, faultCell}}), the first
# window and the number of hourly windows.
class SimPublishRequest(BaseModel):
    managedElementRef: str
    clusters: dict[str, dict]  # cluster id -> {"scenario": ..., "faultCell": "a" | "b" | "c" | "d"}
    start: datetime.datetime
    hours: int = 24


def sim_cluster(cluster: str) -> dict[str, list[str]]:
    """The sample topology, with the cluster id as the cell prefix (dt1-a …)."""
    rename = dict(zip(CELLS, "abcd"))
    return {f"{cluster}-{rename[c]}": [f"{cluster}-{rename[j]}" for j in nbrs] for c, nbrs in NEIGHBOURS.items()}


def register_sim_type(sdk) -> dict:
    """Registers the sim data type at DME as source domain DIGITAL_TWIN, with this rApp's health and job callback URLs, under the invoker id of the caller (the instance's when acting for one): DME takes an rApp's producer only under its own id."""
    return sdk.data.register_type(
        SIM_TYPE["namespace"], SIM_TYPE["name"], SIM_TYPE["version"], SIM_TYPE["typeName"], sdk.data.own_invoker_id() or SIM_PRODUCER_ID,
        data_production_schema={}, producer_health_callback_url=f"{SELF_URL}/sim-producer/health",
        job_callback_url=f"{SELF_URL}/sim-producer/jobs", source_domain="DIGITAL_TWIN",
        source_context={"producer": "coverage-optimization-rapp digital twin", "pattern": "injected CCO faults"})


def publish_sim(sdk, body: SimPublishRequest) -> dict:
    """Delivers the sim windows to every data job of the sim type and returns {dmeTypeId, dataJobs, recordsDelivered}."""
    type_id = next(t["dmeTypeId"] for t in sdk.data.discover_types("RAN") if t["dmeTypeIdStruct"]["name"] == SIM_TYPE["name"])
    jobs = sdk.data.list_data_jobs(dme_type_id=type_id)
    count = 0
    for cluster, spec in body.clusters.items():
        topology = sim_cluster(cluster)
        scenario = spec.get("scenario", "HEALTHY")
        fault_cell = f"{cluster}-{spec.get('faultCell', 'a')}"
        faults = {} if scenario == "HEALTHY" else {fault_cell: scenario}
        for h in range(body.hours):
            t = body.start + datetime.timedelta(hours=h)
            for m in measurements(topology, {}, faults, t, extra={
                    "cluster": cluster, "scenario": scenario, "faultCell": fault_cell}):
                payload = {"managedElementRef": body.managedElementRef, **m}
                for job in jobs:
                    sdk.data.ingest_data_record(job["dataJobId"], payload)
                    count += 1
    return {"dmeTypeId": type_id, "dataJobs": len(jobs), "recordsDelivered": count}


@router.get("/sim-producer/health")
def sim_producer_health():
    # DME's producer health callback: always healthy.
    return {"status": "healthy"}


@router.post("/sim-producer/jobs")
def sim_producer_job(body: dict):
    # DME's callback when a data job for the sim type is created: accepts and ignores it, since records are pushed by `publish`.
    return {"status": "accepted"}


@router.delete("/sim-producer/jobs/{data_job_id}", status_code=204)
def sim_producer_job_stop(data_job_id: str):
    # DME's callback when a data job is stopped: nothing to clean up (204).
    pass
