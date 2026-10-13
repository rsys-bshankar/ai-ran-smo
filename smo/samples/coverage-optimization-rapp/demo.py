#!/usr/bin/env python3
"""The Wave 10.3 demo script (DEMO_RUNBOOK.md §26, HISTORY.md
W10.3-10): Demo 00–11 for the Coverage Optimization rApp against a running
stack.

    python3 demo.py 00        # one step
    python3 demo.py all       # every step in order

Run it inside the compose network (e.g. from the r1-termination
container). It calls each service by hostname, the way the runbook's other
sections do. Ids are kept between steps in $DEMO_STATE (default
/tmp/coverage-optimization-demo.json).

Live PM is produced from each cell's current tilt and power, read back over
O1 through RAN NF OAM, by the package's own propagation model. So the
rApp's changes show up in the next hour's PM. Timestamps are simulation
time: the history is 2026-09-01..03 and live PM starts at midnight on the
4th. The demo-runbook integration test (tests_integration/test_demo_runbook.py)
runs these same steps through the in-process service mesh.
"""

import datetime
import importlib
import importlib.util
import json
import os
import sys

import httpx

ME = os.environ.get("DEMO_ME", "gnb-cco-demo-01")
CELLS = ["301", "302", "303", "304"]
FAULTS = {"301": "OVERSHOOT"}          # 301 reaches too far, into 302 and 303
CSAR_URL = os.environ.get("DEMO_CSAR_URL", "http://r1-termination:8899/coverage-optimization-rapp.csar")
STATE_FILE = os.environ.get("DEMO_STATE", "/tmp/coverage-optimization-demo.json")
OPERATOR = "noc-operator"
HISTORY_START = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
LIVE_START = HISTORY_START + datetime.timedelta(days=3)


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
    """The package's own propagation model (app/producer.py), loaded under a
    private name so it can't clash with another `app` package."""
    if "cco_demo_app.producer" not in sys.modules:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
        spec = importlib.util.spec_from_file_location("cco_demo_app", os.path.join(root, "__init__.py"),
                                                      submodule_search_locations=[root])
        sys.modules["cco_demo_app"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["cco_demo_app"])
    return importlib.import_module("cco_demo_app.producer")


def _config(cell: str) -> tuple[int, int]:
    """The cell's live (digitalTilt, configuredMaxTxPower), over NETCONF get-config."""
    get = lambda mfr: call("get", "ran-nf-oam", f"/managed-entities/{ME}/config", params={"managed_function_ref": mfr})["attributes"]  # noqa: E731
    return int(get(f"CommonBeamformingFunction={cell}")["digitalTilt"]), int(get(f"NRSectorCarrier={cell}")["configuredMaxTxPower"])


def _report(measurements: list[dict]) -> dict:
    return call("post", "ran-nf-oam", "/pm-reports", json={"managedElementRef": ME, "counterType": "COVERAGE_PERFORMANCE",
                                                           "measurements": measurements})


def _live_hour(state: dict, faults: dict) -> None:
    gen = _producer()
    t = LIVE_START + datetime.timedelta(hours=state.get("liveHours", 0))
    _report(gen.measurements(gen.NEIGHBOURS, {c: _config(c) for c in CELLS}, faults, t))
    state["liveHours"] = state.get("liveHours", 0) + 1


def _governance(model_id: str, *events: str) -> None:
    for event in events:
        call("post", "aimgf", f"/models/{model_id}/advance",
             params={"event": event, "decided_by": OPERATOR, "rationale": f"demo: {event}"})


def _decision(result: dict, cell: str) -> dict:
    return next(d for d in result["decisions"] if d["cellId"] == cell)


# ---------------------------------------------------------------- the steps

def demo_00(state: dict) -> None:
    """Prepare the RAN. Register one gNB behind the mock O1 adaptor, with
    coverage PM on it, the Digital Twin dataset, and the generic O1-CM
    intent handler."""
    endpoint = call("post", "ran-nf-oam", "/o1-adaptor-endpoints", json={
        "managedElementRef": ME, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"})
    call("post", "ran-nf-oam", f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat")
    call("post", "ran-nf-oam", "/pm-subscriptions", params={"managed_element_ref": ME, "counter_type": "COVERAGE_PERFORMANCE",
                                                           "delivery_method": "pull", "granularity_period": 3600})
    call("post", "coverage-optimization-rapp", "/sim-producer/register")
    call("post", "sa-smos", "/o1-cm-handler/registration", json={})
    show("managed element", ME)
    show("cluster", {c: n for c, n in _producer().NEIGHBOURS.items()})
    show("cell settings", {c: _config(c) for c in CELLS})


def demo_01(state: dict) -> None:
    """Onboard the rApp. Onboarding → AVAILABLE, then deploy an AUTONOMOUS
    instance over the four-cell cluster and start it."""
    package = call("post", "onboarding", "/packages", json={"location": CSAR_URL})
    status = call("get", "onboarding", f"/packages/{package['packageId']}/onboarding-status")
    instance = call("post", "rapp-mgmt", "/instances", json={
        "operatorApiBase": _url("coverage-optimization-rapp", ""),      # where the gateway's /rapps/<instance>/operator/... reaches this rApp: its page in the GUI needs it
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS",
        "config": {"managedElementRef": ME, "cells": CELLS}, "regionScope": {"objectInstance": ME, "cells": CELLS}})
    state.update(packageId=package["packageId"], instanceId=instance["instanceId"])
    started = call("post", "coverage-optimization-rapp", f"/instances/{instance['instanceId']}/start")
    state["datasets"] = started["datasets"]
    state["bootstrap"] = started.get("bootstrap")      # call flow 01d: the rApp reported the instance up, as the instance
    show("instance bootstrap", state["bootstrap"])
    show("package state", status["state"])
    show("execution modes", status["aiCapabilities"]["executionModes"])
    show("instance", instance["instanceId"])


def demo_02(state: dict) -> None:
    """Show the dataset. Three days of history in which each cell's tilt or
    power was stepped in turn: measurement-report statistics plus the CM
    snapshot go RAN NF OAM → DME as COVERAGE_PERFORMANCE."""
    gen = _producer()
    delivered = _report(gen.history(gen.NEIGHBOURS, HISTORY_START, 72))
    job = state["datasets"]["TRAINING"]["dataJobId"]
    records = call("get", "dme", f"/data-jobs/{job}/records", params={"limit": 1})
    show("PM measurements reported", delivered["measurements"])
    show("datasets", {stage: d["dataset"] for stage, d in state["datasets"].items()})
    show("coverage records in the training job", records["total"])
    state["historyRecords"] = records["total"]


def demo_03(state: dict) -> None:
    """Train the model. An AIMgF TrainingJob learns the 12 sensitivities on
    the MLTF runtime; TRAINING → TRAINED."""
    trained = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/train")
    state["modelId"] = trained["modelId"]
    show("training job", trained["trainingJobId"])
    show("sensitivities [ownTilt, ownPower, nbrTilt, nbrPower]", trained["metrics"]["sensitivities"])
    show("rmse", trained["metrics"]["rmse"])
    show("model lifecycle", trained["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_TRAINING")


def demo_04(state: dict) -> None:
    """Validate the model. VALIDATING → VALIDATED."""
    validated = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/validate")
    show("validation", validated["metrics"])
    show("model lifecycle", validated["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_VALIDATION")


def demo_05(state: dict) -> None:
    """Emulate the model. The Digital Twin injects one fault per cluster; the
    joint optimiser must answer each with the right move."""
    call("post", "coverage-optimization-rapp", "/sim-producer/publish", json={
        "managedElementRef": "digital-twin-cco", "start": HISTORY_START.isoformat(), "hours": 12,
        "clusters": {"dt1": {"scenario": "OVERSHOOT", "faultCell": "a"}, "dt2": {"scenario": "WEAK_COVERAGE", "faultCell": "b"},
                     "dt3": {"scenario": "PILOT_POLLUTION", "faultCell": "c"}, "dt4": {"scenario": "HEALTHY"}}})
    emulated = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/emulate")
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
    deployed = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/deploy")
    show("runtime lifecycle", deployed["lifecycle"]["runtimeLifecycleState"])
    state["runtime"] = deployed["lifecycle"]["runtimeLifecycleState"]


def demo_08(state: dict) -> None:
    """Live inference. Cell 301 overshoots into 302 and 303. The joint plan
    downtilts 301, the cause, rather than the neighbours it pollutes."""
    _live_hour(state, FAULTS)
    result = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/evaluate")
    state.update(executionId=result["executionId"], plan=result["plan"], decision=_decision(result, "301"))
    show("shares", {d["cellId"]: d["shares"] for d in result["decisions"]})
    show("joint plan", {c: f"{m} ({result['plan']['drivers'][c]})" for c, m in result["plan"]["moves"].items()})
    show("cluster objective", f"{result['plan']['objectiveBefore']} → {result['plan']['objectiveAfter']} (predicted)")
    show("302", _decision(result, "302")["reason"])


def demo_09(state: dict) -> None:
    """Show the DME action and the O1 update. The action record and its
    correlation chain, then CommonBeamformingFunction.digitalTilt read back
    over NETCONF get-config."""
    d = state["decision"]
    action = call("get", "dme", f"/actions/{d['action']['actionId']}")
    show("action record", {k: action[k] for k in ("actionId", "status", "forwardedJobId")})
    show("correlation", {"executionId": state["executionId"], "dispatchId": d["intent"]["dispatchId"],
                         "intentId": d["intent"]["intentId"], "actionId": action["actionId"]})
    config = call("get", "ran-nf-oam", f"/managed-entities/{ME}/config", params={"managed_function_ref": "CommonBeamformingFunction=301"})
    show("CommonBeamformingFunction=301", config["attributes"])
    show("verification", d["verification"]["result"])
    state.update(action=action, o1=config["attributes"])


def demo_10(state: dict) -> None:
    """Check the KPI. An hour later the PM, produced from the new tilt, shows
    the cluster objective down: the change set is CONFIRMED. Had it risen,
    the change set would have been reverted."""
    _live_hour(state, FAULTS)
    result = call("post", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/evaluate")
    show("KPI", result["kpi"])
    show("301 outcome", _decision(result, "301")["outcome"])
    state["kpi"] = result["kpi"]


def demo_11(state: dict) -> None:
    """Show the Coverage dashboard. For each cell: tilt and power, problem
    shares, decision and outcome (GUI: Coverage page)."""
    dash = call("get", "coverage-optimization-rapp", f"/instances/{state['instanceId']}/dashboard")
    for c in dash["cells"]:
        d = c["latestDecision"] or {}
        s = d.get("shares") or {}
        print(f"  {c['cellId']}: {c['state']:9} tilt {c['digitalTilt'] / 10:4.1f}° power {c['configuredMaxTxPower']} dBm  "
              f"weak {s.get('WEAK_COVERAGE', '-'):>6} over {s.get('OVERSHOOT', '-'):>6} poll {s.get('PILOT_POLLUTION', '-'):>6}  "
              f"{d.get('decision', '-'):10} {d.get('outcome', '-')}")
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
