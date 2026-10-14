"""Sample PRB data for the EnergySaving rApp (Wave 10.1, W10-04/W10-05).

  * `diurnal_prb` / `pm_report` — a realistic daily PRB profile (near-idle
    after midnight, busy in the day), shaped as the PM reports an NF sends
    RAN NF OAM (`POST /ran-nf-oam/pm-reports`), which delivers them to DME
    as the PRB_UTILIZATION dataset.
  * The Digital Twin producer (decision D-7) — registers the synthetic
    `PRB_UTILIZATION_SIM` DME type (sourceDomain DIGITAL_TWIN, so DME never
    lets it feed INFERENCE) and publishes the emulation trend to every data
    job on it. Its DME callbacks are the routes below.
"""

import datetime
import hashlib

from fastapi import APIRouter
from pydantic import BaseModel
from smo_shared import mtls

router = APIRouter()

SIM_PRODUCER_ID = "energy-saving-dt-producer"   # only when the caller's own invoker id cannot be told: DME takes an rApp's registration under that id alone (SEC-15.10b)
SIM_TYPE = {"namespace": "RAN", "name": "PRB_UTILIZATION_SIM", "version": "1.0.0", "typeName": "RAN.PRB_UTILIZATION_SIM"}
SELF_URL = mtls.http_url("http://energy-saving-rapp:8000")   # https:// with SMO_MTLS=on (PR-SEC-2): the address DME calls back

# (hour, PRB %) — linear in between: idle 00–04, busy 07–21
_PROFILE = [(0, 2.5), (4, 2.5), (5, 4.0), (6, 12.0), (7, 30.0), (12, 50.0), (18, 60.0), (21, 30.0),
            (22, 12.0), (23, 4.0), (24, 2.5)]


def _jitter(key: str, t: datetime.datetime, amplitude: float) -> float:
    h = hashlib.sha256(f"{key}{t.isoformat()}".encode()).digest()[0]
    return (h / 255 - 0.5) * 2 * amplitude


def diurnal_prb(cell: str, t: datetime.datetime, scale: float = 1.0) -> float:
    """The sample PRB (%) of a cell at time `t`: the daily profile interpolated linearly, times `scale`, plus a small deterministic jitter, kept
    within 0 to 100.
    """
    hour = t.hour + t.minute / 60
    for (h0, v0), (h1, v1) in zip(_PROFILE, _PROFILE[1:]):
        if h0 <= hour <= h1:
            base = v0 + (v1 - v0) * (hour - h0) / (h1 - h0)
            break
    jitter = _jitter(cell, t, 0.3 if base < 5 else 2.0)
    return round(max(0.0, min(100.0, base * scale + jitter)), 2)


def samples(cells: list[str], start: datetime.datetime, hours: float, step_minutes: int = 60,
            value=None) -> dict[str, list[dict]]:
    """{cellId: [{cellId, value, timestamp}, ...]} — `value(cell, t)` overrides the profile."""
    out = {}
    steps = int(hours * 60 / step_minutes)
    for cell in cells:
        out[cell] = [{"cellId": cell, "timestamp": (t := start + datetime.timedelta(minutes=i * step_minutes)).isoformat(),
                      "value": value(cell, t) if value else diurnal_prb(cell, t)} for i in range(steps)]
    return out


def pm_report(managed_element_ref: str, measurements: list[dict], counter: str = "PRB_UTILIZATION") -> dict:
    return {"managedElementRef": managed_element_ref, "counterType": counter, "measurements": measurements}


# ---------------------------------------------------------------- Digital Twin producer (DME callbacks)

# Request body of POST /sim-producer/publish: the managed element, the cells, the first sample time, the span in hours and the step in
# minutes.
class SimPublishRequest(BaseModel):
    managedElementRef: str
    cells: list[str]
    start: datetime.datetime
    hours: float = 24
    stepMinutes: int = 60


def register_sim_type(sdk) -> dict:
    """Registers the sim data type at DME as source domain DIGITAL_TWIN, with this rApp's health and job callback URLs, under the invoker id of the caller (the instance's when acting for one): DME takes an rApp's producer only under its own id."""
    return sdk.data.register_type(
        SIM_TYPE["namespace"], SIM_TYPE["name"], SIM_TYPE["version"], SIM_TYPE["typeName"], sdk.data.own_invoker_id() or SIM_PRODUCER_ID,
        data_production_schema={}, producer_health_callback_url=f"{SELF_URL}/sim-producer/health",
        job_callback_url=f"{SELF_URL}/sim-producer/jobs", source_domain="DIGITAL_TWIN",
        source_context={"producer": "energy-saving-rapp digital twin", "pattern": "diurnal PRB"})


def publish_sim(sdk, body: SimPublishRequest) -> dict:
    """Delivers the sim samples to every data job of the sim type and returns {dmeTypeId, dataJobs, recordsDelivered}."""
    type_id = next(t["dmeTypeId"] for t in sdk.data.discover_types("RAN") if t["dmeTypeIdStruct"]["name"] == SIM_TYPE["name"])
    jobs = sdk.data.list_data_jobs(dme_type_id=type_id)
    count = 0
    for series in samples(body.cells, body.start, body.hours, body.stepMinutes).values():
        for s in series:
            for job in jobs:
                sdk.data.ingest_data_record(job["dataJobId"], {"managedElementRef": body.managedElementRef, **s})
                count += 1
    return {"dmeTypeId": type_id, "dataJobs": len(jobs), "recordsDelivered": count}


@router.get("/sim-producer/health")
def sim_producer_health():
    # DME's producer health callback: always healthy.
    return {"status": "healthy"}


@router.post("/sim-producer/jobs")
def sim_producer_job(body: dict):
    """DME's job push for the Digital Twin type — data is published on
    demand (POST /sim-producer/publish), so this only acknowledges."""
    return {"status": "accepted"}


@router.delete("/sim-producer/jobs/{data_job_id}", status_code=204)
def sim_producer_job_stop(data_job_id: str):
    # DME's callback when a data job is stopped: nothing to clean up (204).
    pass
