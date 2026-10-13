#!/usr/bin/env python3
"""The Wave 10.4 demo script (DEMO_RUNBOOK.md §27, HISTORY.md
W10.4-10): Demo 00–11 for the Traffic Steering rApp against a running stack.

    python3 demo.py 00        # one step
    python3 demo.py all       # every step in order

Run it inside the compose network (e.g. from the r1-termination
container). It calls each service by hostname, the way the runbook's other
sections do. Ids are kept between steps in $DEMO_STATE (default
/tmp/traffic-steering-demo.json).

Live PM is produced from each cell's current steering (CIO and reselection
priority, read back over O1 through RAN NF OAM) by the package's own load
model. So the rApp's steps show up in the next hour's PM. Timestamps are
simulation time: the history is 2026-09-01..03 and live PM starts at noon on
the 4th. The demo-runbook integration test
(tests_integration/test_demo_runbook.py) runs these same steps through the
in-process service mesh.
"""

import datetime
import importlib
import importlib.util
import json
import os
import sys

import httpx

ME = os.environ.get("DEMO_ME", "gnb-mlb-demo-01")
HOTSPOT = {"401": "HOTSPOT"}          # a stadium event next to cell 401
CSAR_URL = os.environ.get("DEMO_CSAR_URL", "http://r1-termination:8899/traffic-steering-rapp.csar")
STATE_FILE = os.environ.get("DEMO_STATE", "/tmp/traffic-steering-demo.json")
OPERATOR = "noc-operator"
HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
LIVE_START = HISTORY_START + datetime.timedelta(days=3, hours=12)


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


def _producer():
    """The package's own load model (app/producer.py), loaded under a private
    name so it can't clash with another `app` package."""
    if "mlb_demo_app.producer" not in sys.modules:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
        spec = importlib.util.spec_from_file_location("mlb_demo_app", os.path.join(root, "__init__.py"),
                                                      submodule_search_locations=[root])
        sys.modules["mlb_demo_app"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["mlb_demo_app"])
    return importlib.import_module("mlb_demo_app.producer")


def _attrs(mfr: str) -> dict:
    return call("get", "ran-nf-oam", f"/managed-entities/{ME}/config", params={"managed_function_ref": mfr})["attributes"]


def _settings() -> dict:
    """Each cell's live steering: CIO towards each neighbour, priority towards the other layer."""
    gen = _producer()
    return {c: {"cio": {t: json.loads(_attrs(f"NRCellRelation={c}-{t}")["cellIndividualOffset"])[0] for t in nbrs},
                "prio": {gen.LAYERS[t]: int(_attrs(f"NRFreqRelation={c}-{gen.LAYERS[t]}")["cellReselectionPriority"])
                         for t in nbrs if gen.LAYERS[t] != gen.LAYERS[c]}}
            for c, nbrs in gen.NEIGHBOURS.items()}


def _report(measurements: list[dict]) -> dict:
    return call("post", "ran-nf-oam", "/pm-reports", json={"managedElementRef": ME, "counterType": "LOAD_PERFORMANCE",
                                                           "measurements": measurements})


def _live_hour(state: dict, faults: dict) -> None:
    gen = _producer()
    t = LIVE_START + datetime.timedelta(hours=state.get("liveHours", 0))
    _report(gen.measurements(gen.NEIGHBOURS, gen.LAYERS, _settings(), faults, t))
    state["liveHours"] = state.get("liveHours", 0) + 1


def _governance(model_id: str, *events: str) -> None:
    for event in events:
        call("post", "aimgf", f"/models/{model_id}/advance",
             params={"event": event, "decided_by": OPERATOR, "rationale": f"demo: {event}"})


def _decision(result: dict, cell: str) -> dict:
    return next(d for d in result["decisions"] if d["cellId"] == cell)


# ---------------------------------------------------------------- the steps

def demo_00(state: dict) -> None:
    """Prepare the RAN. Register one gNB behind the mock O1 adaptor, with load
    PM on it, the Digital Twin dataset, and the generic O1-CM intent
    handler."""
    endpoint = call("post", "ran-nf-oam", "/o1-adaptor-endpoints", json={
        "managedElementRef": ME, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"})
    call("post", "ran-nf-oam", f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat")
    call("post", "ran-nf-oam", "/pm-subscriptions", params={"managed_element_ref": ME, "counter_type": "LOAD_PERFORMANCE",
                                                           "delivery_method": "pull", "granularity_period": 3600})
    call("post", "traffic-steering-rapp", "/sim-producer/register")
    call("post", "sa-smos", "/o1-cm-handler/registration", json={})
    gen = _producer()
    show("managed element", ME)
    show("layers", gen.LAYERS)
    show("neighbours", gen.NEIGHBOURS)


def demo_01(state: dict) -> None:
    """Onboard the rApp. Onboarding → AVAILABLE, then deploy an AUTONOMOUS
    instance over the two-layer cluster and start it. Its region scope names
    every relation and frequency relation it may write."""
    gen = _producer()
    package = call("post", "onboarding", "/packages", json={"location": CSAR_URL})
    status = call("get", "onboarding", f"/packages/{package['packageId']}/onboarding-status")
    region = sorted({f"{c}-{t}" for c, n in gen.NEIGHBOURS.items() for t in n} |
                    {f"{c}-{gen.LAYERS[t]}" for c, n in gen.NEIGHBOURS.items() for t in n if gen.LAYERS[t] != gen.LAYERS[c]})
    instance = call("post", "rapp-mgmt", "/instances", json={
        "operatorApiBase": _url("traffic-steering-rapp", ""),      # where the gateway's /rapps/<instance>/operator/... reaches this rApp: its page in the GUI needs it
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS",
        "config": {"managedElementRef": ME, "cells": [{"cellId": c, "layer": layer} for c, layer in gen.LAYERS.items()]},
        "regionScope": {"objectInstance": ME, "cells": region}})
    state.update(packageId=package["packageId"], instanceId=instance["instanceId"])
    started = call("post", "traffic-steering-rapp", f"/instances/{instance['instanceId']}/start")
    state["datasets"] = started["datasets"]
    state["bootstrap"] = started.get("bootstrap")      # call flow 01d: the rApp reported the instance up, as the instance
    show("instance bootstrap", state["bootstrap"])
    show("package state", status["state"])
    show("execution modes", status["aiCapabilities"]["executionModes"])
    show("instance", instance["instanceId"])


def demo_02(state: dict) -> None:
    """Show the dataset. Three days of load PM in which each cell's CIO or
    priority was stepped in turn (TS 28.552 PRB, connected UEs, UE throughput,
    plus the CM snapshot) go RAN NF OAM → DME as LOAD_PERFORMANCE."""
    gen = _producer()
    delivered = _report(gen.history(gen.NEIGHBOURS, gen.LAYERS, HISTORY_START, 72))
    job = state["datasets"]["TRAINING"]["dataJobId"]
    records = call("get", "dme", f"/data-jobs/{job}/records", params={"limit": 1})
    show("PM measurements reported", delivered["measurements"])
    show("datasets", {stage: d["dataset"] for stage, d in state["datasets"].items()})
    show("load records in the training job", records["total"])
    state["historyRecords"] = records["total"]


def demo_03(state: dict) -> None:
    """Train the model. The forecast weights, the hour-of-day profile and the
    transfer per CIO dB / per priority step, on the MLTF runtime;
    TRAINING → TRAINED."""
    trained = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/lifecycle/train")
    state["modelId"] = trained["modelId"]
    show("training job", trained["trainingJobId"])
    show("transfer per step", trained["metrics"]["transfer"])
    show("rmse", trained["metrics"]["rmse"])
    show("model lifecycle", trained["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_TRAINING")


def demo_04(state: dict) -> None:
    """Validate the model. VALIDATING → VALIDATED."""
    validated = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/lifecycle/validate")
    show("validation", validated["metrics"])
    show("model lifecycle", validated["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_VALIDATION")


def demo_05(state: dict) -> None:
    """Emulate the model. The Digital Twin injects hotspots; the planner must
    steer out of each one without overloading a neighbour."""
    call("post", "traffic-steering-rapp", "/sim-producer/publish", json={
        "managedElementRef": "digital-twin-mlb", "start": HISTORY_START.isoformat(), "hours": 24,
        "clusters": {"dt1": {"scenario": "HOTSPOT", "hotCell": "a"}, "dt2": {"scenario": "HOTSPOT", "hotCell": "c"},
                     "dt3": {"scenario": "HEALTHY"}}})
    emulated = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/lifecycle/emulate")
    show("emulation", emulated["metrics"])
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
    """Deploy the runtime. MLLF checks the model; AIMgF and NFO deploy MLIF."""
    deployed = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/lifecycle/deploy")
    show("runtime lifecycle", deployed["lifecycle"]["runtimeLifecycleState"])
    state["runtime"] = deployed["lifecycle"]["runtimeLifecycleState"]


def demo_08(state: dict) -> None:
    """Live inference. 401 is a hotspot this afternoon. It is forecast
    congested, and one step moves load to its least-loaded neighbour."""
    _live_hour(state, HOTSPOT)
    result = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/evaluate")
    d = _decision(result, "401")
    state.update(executionId=result["executionId"], decision=d)
    show("scores → forecasts", {x["cellId"]: f"{x['score']} → {x['forecast']}" for x in result["decisions"]})
    show("401", f"{d['decision']} {d['managedRef']} {d['fromValue']} → {d['toValue']} ({d['reason']})")
    show("target after the transfer", d["prediction"]["plan"]["targetForecastAfter"])


def demo_09(state: dict) -> None:
    """Show the DME action and the O1 update. The action record and its
    correlation chain, then the steered attribute read back over NETCONF
    get-config."""
    d = state["decision"]
    action = call("get", "dme", f"/actions/{d['action']['actionId']}")
    show("action record", {k: action[k] for k in ("actionId", "status", "forwardedJobId")})
    show("correlation", {"executionId": state["executionId"], "dispatchId": d["intent"]["dispatchId"],
                         "intentId": d["intent"]["intentId"], "actionId": action["actionId"]})
    attrs = _attrs(d["managedRef"])
    show(d["managedRef"], attrs)
    show("verification", d["verification"]["result"])
    state.update(action=action, o1=attrs)


def demo_10(state: dict) -> None:
    """Check the KPI. An hour later 401's measured score is below its
    no-steering forecast and the target is not congested: CONFIRMED. Had the
    target congested, or handover failures risen, the step would have been
    reverted."""
    _live_hour(state, HOTSPOT)
    result = call("post", "traffic-steering-rapp", f"/instances/{state['instanceId']}/evaluate")
    d = _decision(result, "401")
    show("KPI", d["kpi"])
    show("401 outcome", d["outcome"])
    state["kpi"] = d["kpi"]


def demo_11(state: dict) -> None:
    """Show the Traffic Steering dashboard. For each cell: layer, score,
    steering in force, decision and outcome (GUI: Traffic Steering page)."""
    dash = call("get", "traffic-steering-rapp", f"/instances/{state['instanceId']}/dashboard")
    for c in dash["cells"]:
        d = c["latestDecision"] or {}
        print(f"  {c['cellId']} ({c['layer']}): {c['state']:9} score {d.get('score', '-'):>6}  steering {c['steering']}  "
              f"{d.get('decision', '-'):16} {d.get('outcome', '-')}")
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
