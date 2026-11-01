#!/usr/bin/env python3
"""The Wave 10.1 demo script (DEMO_RUNBOOK.md §24, HISTORY.md
W10-27): Demo 00–11 for the EnergySaving rApp against a running stack.

    python3 demo.py 00        # one step
    python3 demo.py all       # every step in order

Run it inside the compose network (e.g. from the r1-termination
container). It calls each service by hostname, the way the runbook's other
sections do. Ids are kept between steps in $DEMO_STATE (default
/tmp/energy-saving-demo.json).

The timestamps are simulation time, not the wall clock. The history is
2026-09-01..03 and live PM starts at midnight on the 4th, so "midnight
behaviour" is reproducible on any day. The demo-runbook integration test
(tests_integration/test_demo_runbook.py) runs these same steps through the
in-process service mesh.
"""

import datetime
import json
import os
import sys

import httpx

ME = os.environ.get("DEMO_ME", "gnb-du-demo-01")
CELLS = ["101", "102", "103", "104"]
CSAR_URL = os.environ.get("DEMO_CSAR_URL", "http://r1-termination:8899/energy-saving-rapp.csar")
STATE_FILE = os.environ.get("DEMO_STATE", "/tmp/energy-saving-demo.json")
OPERATOR = "noc-operator"
HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
LIVE_START = HISTORY_START + datetime.timedelta(days=3)
RAPP = "http://energy-saving-rapp:8000"


def _mtls() -> bool:
    return os.environ.get("SMO_MTLS", "off").lower() in ("on", "true", "1")


def _url(service: str, path: str) -> str:
    return f"{'https' if _mtls() else 'http'}://{service}:8000{path}"


def _tls() -> dict:
    """With SMO_MTLS=on the services are TLS-only and want a client certificate: present the runbook identity."""
    if not _mtls():
        return {}
    import ssl
    context = ssl.create_default_context(cafile=os.environ["SMO_MTLS_CA_FILE"])
    context.load_cert_chain(os.environ["SMO_MTLS_CERT_FILE"], os.environ["SMO_MTLS_KEY_FILE"])
    return {"verify": context}


def call(verb: str, service: str, path: str, expect=(200, 201, 202, 204), **kw):
    """Calls a service on port 8000 (https with SMO_MTLS=on) with httpx and returns its JSON (None for an empty body); exits with the status and
    body when the status is not in `expect`.
    """
    resp = getattr(httpx, verb)(_url(service, path), timeout=120.0, **_tls(), **kw)
    if resp.status_code not in expect:
        raise SystemExit(f"{verb.upper()} {service}{path} → {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else None


def show(label: str, value) -> None:
    print(f"  {label}: {json.dumps(value, default=str) if not isinstance(value, str) else value}")


def _diurnal(cell: str, t: datetime.datetime) -> float:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
    from producer import diurnal_prb  # the package's own synthetic daily profile
    return diurnal_prb(cell, t)


def _pm(values: dict[str, list[float]], start: datetime.datetime, step_minutes: int) -> dict:
    measurements = [{"cellId": c, "value": v, "timestamp": (start + datetime.timedelta(minutes=i * step_minutes)).isoformat()}
                    for c, series in values.items() for i, v in enumerate(series)]
    return call("post", "ran-nf-oam", "/pm-reports", json={"managedElementRef": ME, "counterType": "PRB_UTILIZATION",
                                                           "measurements": measurements})


def _governance(model_id: str, *events: str) -> None:
    for event in events:
        call("post", "aimgf", f"/models/{model_id}/advance",
             params={"event": event, "decided_by": OPERATOR, "rationale": f"demo: {event}"})


# ---------------------------------------------------------------- the steps

def demo_00(state: dict) -> None:
    """Prepare the RAN. Register one gNB-DU behind the mock O1 adaptor, with
    PRB PM on it, the Digital Twin dataset, and the generic O1-CM intent
    handler."""
    endpoint = call("post", "ran-nf-oam", "/o1-adaptor-endpoints", json={
        "managedElementRef": ME, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"})
    call("post", "ran-nf-oam", f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat")
    call("post", "ran-nf-oam", "/pm-subscriptions", params={"managed_element_ref": ME, "counter_type": "PRB_UTILIZATION",
                                                           "delivery_method": "pull", "granularity_period": 300})
    call("put", "ran-nf-oam", f"/managed-entities/{ME}/cells/103/guards", json={"cellClass": "EMERGENCY"})
    call("post", "energy-saving-rapp", "/sim-producer/register")
    call("post", "sa-smos", "/o1-cm-handler/registration", json={})
    show("managed element", ME)
    show("cell 103", "EMERGENCY — never sleeps")


def demo_01(state: dict) -> None:
    """Onboard the rApp. Onboarding → AVAILABLE, then deploy an AUTONOMOUS
    instance and start it."""
    package = call("post", "onboarding", "/packages", json={"location": CSAR_URL})
    status = call("get", "onboarding", f"/packages/{package['packageId']}/onboarding-status")
    if status["state"] != "AVAILABLE":
        # Onboarding rejects a byte-identical package (DEMO_RUNBOOK.md §2), so when the
        # runbook's lifecycle sections already onboarded this CSAR, reuse that package.
        existing = [p for p in call("get", "onboarding", "/packages", params={"limit": 500})["items"]
                    if p["name"] == "EnergySaving_rApp" and p["state"] in ("AVAILABLE", "PRIMED")]
        if not existing:
            raise SystemExit(f"EnergySaving_rApp package is {status['state']} and none is AVAILABLE")
        package = {"packageId": existing[0]["packageId"]}
        status = call("get", "onboarding", f"/packages/{package['packageId']}/onboarding-status")
    instance = call("post", "rapp-mgmt", "/instances", json={
        "operatorApiBase": _url("energy-saving-rapp", ""),      # where the gateway's /rapps/<instance>/operator/... reaches this rApp: its page in the GUI needs it
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS",
        "config": {"managedElementRef": ME, "cells": CELLS, "actuator": "ADMINISTRATIVE_STATE"},
        "regionScope": {"objectInstance": ME, "cells": CELLS}})
    state.update(packageId=package["packageId"], instanceId=instance["instanceId"])
    started = call("post", "energy-saving-rapp", f"/instances/{instance['instanceId']}/start")
    state["datasets"] = started["datasets"]
    state["bootstrap"] = started.get("bootstrap")      # call flow 01d: the rApp reported the instance up, as the instance
    show("instance bootstrap", state["bootstrap"])
    show("package state", status["state"])
    show("execution modes", status["aiCapabilities"]["executionModes"])
    show("autonomy modes", status["aiCapabilities"]["autonomyModes"])
    show("instance", instance["instanceId"])


def demo_02(state: dict) -> None:
    """Show the dataset. O1 PM goes RAN NF OAM → DME, and the rApp finds
    PRB_UTILIZATION there."""
    values = {c: [_diurnal(c, HISTORY_START + datetime.timedelta(hours=h)) for h in range(72)] for c in CELLS}
    delivered = _pm(values, HISTORY_START, 60)
    job = state["datasets"]["TRAINING"]["dataJobId"]
    records = call("get", "dme", f"/data-jobs/{job}/records", params={"limit": 1})
    show("PM measurements reported", delivered["measurements"])
    show("datasets", {stage: d["dataset"] for stage, d in state["datasets"].items()})
    show("PRB records in the training job", records["total"])
    state["historyRecords"] = records["total"]


def demo_03(state: dict) -> None:
    """Train the model. An AIMgF TrainingJob runs on the MLTF runtime;
    TRAINING → TRAINED."""
    trained = call("post", "energy-saving-rapp", f"/instances/{state['instanceId']}/lifecycle/train")
    state["modelId"] = trained["modelId"]
    show("training job", trained["trainingJobId"])
    show("metrics", {k: trained["metrics"][k] for k in ("rmse", "confidence", "trainingRows")})
    show("model lifecycle", trained["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_TRAINING")


def demo_04(state: dict) -> None:
    """Validate the model. VALIDATING → VALIDATED."""
    validated = call("post", "energy-saving-rapp", f"/instances/{state['instanceId']}/lifecycle/validate")
    show("validation score", validated["metrics"]["score"])
    show("model lifecycle", validated["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_VALIDATION")


def demo_05(state: dict) -> None:
    """Emulate the model. The Digital Twin's PRB trend goes in, and a LOCK
    recommendation for midnight comes out."""
    call("post", "energy-saving-rapp", "/sim-producer/publish", json={
        "managedElementRef": "digital-twin-01", "cells": ["dt-1", "dt-2"], "start": HISTORY_START.isoformat(), "hours": 48})
    emulated = call("post", "energy-saving-rapp", f"/instances/{state['instanceId']}/lifecycle/emulate")
    show("midnight recommendation", emulated["metrics"]["midnightRecommendation"])
    show("estimated saving (kWh)", emulated["metrics"]["energySavingsKwh"])
    show("model lifecycle", emulated["lifecycle"]["modelLifecycleState"])
    state["emulation"] = emulated["metrics"]


def demo_06(state: dict) -> None:
    """Promote the model. CERTIFIED → PROMOTED, each step an operator
    governance decision."""
    _governance(state["modelId"], "SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY")
    certified = call("get", "aimgf", f"/models/{state['modelId']}/lifecycle")["modelLifecycleState"]
    _governance(state["modelId"], "PROMOTE")
    promoted = call("get", "aimgf", f"/models/{state['modelId']}/lifecycle")["modelLifecycleState"]
    show("model lifecycle", f"{certified} → {promoted}")
    state["promoted"] = promoted


def demo_07(state: dict) -> None:
    """Deploy the runtime. AIMgF's deploy gate checks CERTIFIED/PROMOTED, then AIMgF and NFO
    deploy MLIF; RuntimeLifecycle becomes ACTIVE."""
    deployed = call("post", "energy-saving-rapp", f"/instances/{state['instanceId']}/lifecycle/deploy")
    show("runtime lifecycle", deployed["lifecycle"]["runtimeLifecycleState"])
    state["runtime"] = deployed["lifecycle"]["runtimeLifecycleState"]


def demo_08(state: dict) -> None:
    """Live inference. Cell 101 reports PRB = 2 % for an hour after midnight,
    and the decision is LOCK."""
    _pm({"101": [2.0] * 14, "102": [40.0] * 14, "103": [2.0] * 14, "104": [40.0] * 14}, LIVE_START, 5)
    result = call("post", "energy-saving-rapp", f"/instances/{state['instanceId']}/evaluate")
    d101 = next(d for d in result["decisions"] if d["cellId"] == "101")
    d103 = next(d for d in result["decisions"] if d["cellId"] == "103")
    state.update(executionId=result["executionId"], decision=d101)
    show("cell 101 prediction", d101["prediction"]["model"])
    show("cell 101 action", d101["decision"])
    show("cell 103 (emergency)", d103["reason"])


def demo_09(state: dict) -> None:
    """Show the DME action: the action record, its source, and the
    correlation id."""
    d = state["decision"]
    action = call("get", "dme", f"/actions/{d['action']['actionId']}")
    show("action record", {k: action[k] for k in ("actionId", "status", "forwardedJobId")})
    show("action source", {"requestedBy": action["requestedBy"], **action["sourceContext"]})
    show("correlation", {"executionId": state["executionId"], "dispatchId": d["intent"]["dispatchId"],
                         "intentId": d["intent"]["intentId"], "actionId": action["actionId"]})
    state["action"] = action


def demo_10(state: dict) -> None:
    """Show the O1 update. administrativeState is read back over NETCONF
    get-config: LOCKED."""
    config = call("get", "ran-nf-oam", f"/managed-entities/{ME}/config", params={"managed_function_ref": "NRCellDU=101"})
    show("NRCellDU=101", config["attributes"])
    show("verification", state["decision"]["verification"]["result"])
    state["o1"] = config["attributes"]


def demo_11(state: dict) -> None:
    """Show the Energy Saving dashboard. For each cell: PRB trend,
    prediction, decision, action and result (GUI: Energy Saving page)."""
    dash = call("get", "energy-saving-rapp", f"/instances/{state['instanceId']}/dashboard")
    for c in dash["cells"]:
        d = c["latestDecision"] or {}
        model = (d.get("prediction") or {}).get("model") or {}
        print(f"  cell {c['cellId']}: {c['state']:9} PRB {c['prbTrend'][-1]['v'] if c['prbTrend'] else '-':>5} → "
              f"{model.get('futurePrb', '-'):>5}  {d.get('decision', '-'):9} {d.get('outcome', '-')}")
    state["dashboard"] = dash


STEPS = {f"{i:02d}": globals()[f"demo_{i:02d}"] for i in range(12)}


def run(step: str, state: dict) -> dict:
    """Runs one demo step by number, printing the first line of its docstring as the heading, and returns the shared state."""
    fn = STEPS[step]
    print(f"Demo {step} — {fn.__doc__.splitlines()[0]}")
    fn(state)
    return state


def main() -> None:
    """Runs the steps named on the command line (all of them by default), loading the state the previous run saved and saving it after each step so
    a later run can continue.
    """
    steps = list(STEPS) if sys.argv[1:] in ([], ["all"]) else sys.argv[1:]
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    for step in steps:
        run(step, state)
        with open(STATE_FILE, "w") as f:
            json.dump({k: v for k, v in state.items() if k != "dashboard"}, f, default=str)


if __name__ == "__main__":
    main()
