"""Sample load data for the Traffic Steering rApp (Wave 10.4, W10.4-03).

  * `loads` is a small, deterministic stand-in for the network. It takes a
    cluster's steering settings (each cell's CIO towards each neighbour and
    its reselection priority towards each other layer) and any injected
    hotspots, and returns each cell's carried load. The integration tests
    and demo.py feed it the **live** O1 settings read back from the mock
    adaptor. So every step the rApp takes shows up in the next hour's PM,
    and the loop closes.
  * `cell_counters` / `measurements` shape that as the PM reports an NF
    sends RAN NF OAM (`POST /ran-nf-oam/pm-reports`, counter type
    LOAD_PERFORMANCE). RAN NF OAM delivers them to DME.
  * `history` is training history in which the biases were stepped, one
    cell at a time, so the transfer per step can be learned.
  * The Digital Twin producer registers `LOAD_PERFORMANCE_SIM`
    (sourceDomain DIGITAL_TWIN) and publishes clusters with a known
    hotspot, so emulation can score the planner.

The load model works on offered load as a fraction of capacity. Each cell
offers BASE_LOAD × the daily load shape; a HOTSPOT multiplies that by 1.8.
From cell S to neighbour T it moves:
  * 3 % of S's load per dB of CIO on S → T above the baseline (0 dB);
  * 6 % per reselection-priority step above the baseline (5) towards T's
    layer, spread over S's neighbours on that layer.

At most 60 % of a cell's load moves out. The congestion score of a cell
carrying load L is then ≈ 98·L. The handover failure rate on S → T is
1 % + 1.5 % per dB of CIO above 2 dB.
"""

import datetime
import hashlib
from collections.abc import Callable

from fastapi import APIRouter
from pydantic import BaseModel
from smo_shared import mtls

from .model.series import CIO, HO_ATT, HO_FAIL, PRB, PRIO, SAMPLES, THP, UE_CAPACITY, UES

router = APIRouter()

SIM_PRODUCER_ID = "steering-dt-producer"   # only when the caller's own invoker id cannot be told: DME takes an rApp's registration under that id alone (SEC-15.10b)
SIM_TYPE = {"namespace": "RAN", "name": "LOAD_PERFORMANCE_SIM", "version": "1.0.0", "typeName": "RAN.LOAD_PERFORMANCE_SIM"}
SELF_URL = mtls.http_url("http://traffic-steering-rapp:8000")   # https:// with SMO_MTLS=on (PR-SEC-2): the address DME calls back

BASELINE_CIO, BASELINE_PRIORITY = 0, 5
BASE_LOAD, HOTSPOT_FACTOR = 0.5, 1.8
CIO_TRANSFER, IDLE_TRANSFER, MAX_OUT = 0.03, 0.06, 0.6

# the sample cluster: two capacity-layer and two coverage-layer cells of one gNB
CELLS = ["401", "402", "411", "412"]
LAYERS = {"401": "F3500", "402": "F3500", "411": "F2100", "412": "F2100"}
NEIGHBOURS = {"401": ["402", "411"], "402": ["401", "412"], "411": ["401", "412"], "412": ["402", "411"]}

_LOAD = [(0, 0.05), (5, 0.05), (8, 0.8), (12, 0.9), (18, 1.0), (22, 0.4), (24, 0.05)]


def load(hour: float) -> float:
    """The sample traffic load (0.05 to 1.0) at an hour of the day, interpolated between the points of the daily curve."""
    for (h0, v0), (h1, v1) in zip(_LOAD, _LOAD[1:]):
        if h0 <= hour <= h1:
            return v0 + (v1 - v0) * (hour - h0) / (h1 - h0)
    return 0.05


def _jitter(key: str, t: datetime.datetime, spread: float = 0.06) -> float:
    return (hashlib.sha256(f"{key}{t.isoformat()}".encode()).digest()[0] / 255 - 0.5) * spread


def baseline_settings(neighbours: dict[str, list[str]], layers: dict[str, str]) -> dict[str, dict]:
    return {c: {"cio": {t: BASELINE_CIO for t in nbrs},
                "prio": {layers[t]: BASELINE_PRIORITY for t in nbrs if layers[t] != layers[c]}}
            for c, nbrs in neighbours.items()}


def loads(neighbours: dict[str, list[str]], layers: dict[str, str], settings: dict[str, dict],
          faults: dict[str, str] | None, t: datetime.datetime) -> dict[str, float]:
    """{cell: carried load (fraction of capacity)} at time `t`."""
    faults = faults or {}
    offered = {c: BASE_LOAD * load(t.hour + t.minute / 60) * (HOTSPOT_FACTOR if faults.get(c) == "HOTSPOT" else 1.0)
               for c in neighbours}
    carried = dict(offered)
    for s, nbrs in neighbours.items():
        cfg = settings.get(s) or {}
        out = {}
        for tgt in nbrs:
            f = max(0.0, CIO_TRANSFER * (float((cfg.get("cio") or {}).get(tgt, BASELINE_CIO)) - BASELINE_CIO))
            if layers[tgt] != layers[s]:
                same_layer = [n for n in nbrs if layers[n] == layers[tgt]]
                steps = float((cfg.get("prio") or {}).get(layers[tgt], BASELINE_PRIORITY)) - BASELINE_PRIORITY
                f += max(0.0, IDLE_TRANSFER * steps) / len(same_layer)
            out[tgt] = f
        total = sum(out.values())
        scale = MAX_OUT / total if total > MAX_OUT else 1.0
        for tgt, f in out.items():
            moved = offered[s] * f * scale
            carried[s] -= moved
            carried[tgt] += moved
    return carried


def cell_counters(cell: str, t: datetime.datetime, carried: float, setting: dict, nbrs: list[str]) -> dict:
    """One cell's PM window, plus its CM snapshot."""
    j = lambda k: 1 + _jitter(cell + k, t)  # noqa: E731
    lv = min(1.0, carried)
    counts = {PRB: round(min(100.0, 100.0 * carried * j(PRB)), 2), UES: round(UE_CAPACITY * carried * j(UES), 1),
              THP: round(50.0 * (1 - 0.9 * lv) * j(THP), 2), SAMPLES: 12}
    for tgt in nbrs:
        cio = float((setting.get("cio") or {}).get(tgt, BASELINE_CIO))
        att = max(1, round(300 * carried * j(HO_ATT + tgt)))
        counts[f"{HO_ATT}{tgt}"] = att
        counts[f"{HO_FAIL}{tgt}"] = round(att * (0.01 + 0.015 * max(0.0, cio - 2)))
        counts[f"{CIO}{tgt}"] = cio
    for layer, prio in (setting.get("prio") or {}).items():
        counts[f"{PRIO}{layer}"] = float(prio)
    return counts


def measurements(neighbours: dict[str, list[str]], layers: dict[str, str], settings: dict[str, dict] | None,
                 faults: dict[str, str] | None, t: datetime.datetime, overrides: dict[str, dict] | None = None,
                 extra: dict | Callable[..., dict] | None = None) -> list[dict]:
    """PM measurements of every cell for the window starting at `t`."""
    settings = settings or baseline_settings(neighbours, layers)
    carried = loads(neighbours, layers, settings, faults, t)
    out = []
    for c in neighbours:
        values = (overrides or {}).get(c) or cell_counters(c, t, carried[c], settings.get(c, {}), neighbours[c])
        out.append({"cellId": c, "values": values, "timestamp": t.isoformat(), **(extra(c) if callable(extra) else extra or {})})
    return out


def exploration(neighbours: dict[str, list[str]], layers: dict[str, str], hours: int) -> list[dict[str, dict]]:
    """Per hour, the settings of a history in which, every 3 hours, one cell
    (in turn) had one bias stepped off its baseline (a CIO of 2 or 4 dB
    towards one neighbour, or 1 or 2 priority steps towards the other layer),
    and the previously stepped cell went back to baseline."""
    cells = list(neighbours)
    out = []
    for h in range(hours):
        settings = baseline_settings(neighbours, layers)
        block = h // 3
        if block:
            cell = cells[block % len(cells)]
            pick = hashlib.sha256(f"{cell}{block}".encode()).digest()
            if pick[0] % 2 and settings[cell]["prio"]:
                layer = sorted(settings[cell]["prio"])[0]
                settings[cell]["prio"][layer] = BASELINE_PRIORITY + 1 + pick[1] % 2
            else:
                tgt = neighbours[cell][pick[1] % len(neighbours[cell])]
                settings[cell]["cio"][tgt] = BASELINE_CIO + 2 * (1 + pick[2] % 2)
        out.append(settings)
    return out


def history(neighbours: dict[str, list[str]], layers: dict[str, str], start: datetime.datetime, hours: int,
            faults: dict[str, str] | None = None) -> list[dict]:
    return [m for h, settings in enumerate(exploration(neighbours, layers, hours))
            for m in measurements(neighbours, layers, settings, faults, start + datetime.timedelta(hours=h))]


# ---------------------------------------------------------------- Digital Twin producer (DME callbacks)

# Request body of POST /sim-producer/publish: the managed element, the clusters to publish ({cluster: {scenario, hotCell}}), the first window
# and the number of hourly windows.
class SimPublishRequest(BaseModel):
    managedElementRef: str
    clusters: dict[str, dict]  # cluster id -> {"scenario": "HOTSPOT" | "HEALTHY", "hotCell": "a" | "b" | "c" | "d"}
    start: datetime.datetime
    hours: int = 24


def sim_cluster(cluster: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """The sample topology and layers, with the cluster id as the cell prefix (dt1-a …)."""
    rename = {c: f"{cluster}-{x}" for c, x in zip(CELLS, "abcd")}
    return ({rename[c]: [rename[j] for j in nbrs] for c, nbrs in NEIGHBOURS.items()},
            {rename[c]: layer for c, layer in LAYERS.items()})


def register_sim_type(sdk) -> dict:
    """Registers the sim data type at DME as source domain DIGITAL_TWIN, with this rApp's health and job callback URLs, under the invoker id of the caller (the instance's when acting for one): DME takes an rApp's producer only under its own id."""
    return sdk.data.register_type(
        SIM_TYPE["namespace"], SIM_TYPE["name"], SIM_TYPE["version"], SIM_TYPE["typeName"], sdk.data.own_invoker_id() or SIM_PRODUCER_ID,
        data_production_schema={}, producer_health_callback_url=f"{SELF_URL}/sim-producer/health",
        job_callback_url=f"{SELF_URL}/sim-producer/jobs", source_domain="DIGITAL_TWIN",
        source_context={"producer": "traffic-steering-rapp digital twin", "pattern": "injected hotspots"})


def publish_sim(sdk, body: SimPublishRequest) -> dict:
    """Delivers the sim windows to every data job of the sim type and returns {dmeTypeId, dataJobs, recordsDelivered}."""
    type_id = next(t["dmeTypeId"] for t in sdk.data.discover_types("RAN") if t["dmeTypeIdStruct"]["name"] == SIM_TYPE["name"])
    jobs = sdk.data.list_data_jobs(dme_type_id=type_id)
    count = 0
    for cluster, spec in body.clusters.items():
        topology, layers = sim_cluster(cluster)
        scenario = spec.get("scenario", "HEALTHY")
        hot = f"{cluster}-{spec.get('hotCell', 'a')}"
        faults = {hot: "HOTSPOT"} if scenario == "HOTSPOT" else {}
        for h in range(body.hours):
            t = body.start + datetime.timedelta(hours=h)
            for m in measurements(topology, layers, None, faults, t, extra=lambda c, cluster=cluster, scenario=scenario, hot=hot, layers=layers: {
                    "cluster": cluster, "scenario": scenario, "hotCell": hot, "layer": layers[c]}):
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
