#!/usr/bin/env python3
"""The Wave 10.2 demo script (DEMO_RUNBOOK.md §25, HISTORY.md
W10.2-10): Demo 00–11 for the Mobility Optimization rApp against a running
stack.

    python3 demo.py 00        # one step
    python3 demo.py all       # every step in order

Run it inside the compose network (e.g. from the r1-termination
container). It calls each service by hostname, the way the runbook's other
sections do. Ids are kept between steps in $DEMO_STATE (default
/tmp/mobility-optimization-demo.json).

The timestamps are simulation time, not the wall clock: the handover
history is 2026-09-01..03 and live PM starts at midnight on the 4th. The
demo-runbook integration test (tests_integration/test_demo_runbook.py) runs
these same steps through the in-process service mesh.
"""

import datetime
import importlib.util
import json
import os
import sys

import httpx

ME = os.environ.get("DEMO_ME", "gnb-du-mro-demo-01")
RELATIONS = [{"relation": "201-202", "source": "201", "target": "202"},
             {"relation": "201-203", "source": "201", "target": "203"},
             {"relation": "202-203", "source": "202", "target": "203"},
             {"relation": "203-204", "source": "203", "target": "204"}]
# the problem each relation has: 201→203 hands over too late, 202→203 too early
HISTORY = {"201-202": "HEALTHY", "201-203": "TOO_LATE", "202-203": "TOO_EARLY", "203-204": "HEALTHY"}
CSAR_URL = os.environ.get("DEMO_CSAR_URL", "http://r1-termination:8899/mobility-optimization-rapp.csar")
STATE_FILE = os.environ.get("DEMO_STATE", "/tmp/mobility-optimization-demo.json")
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
    """The package's own synthetic handover counters (app/producer.py), loaded
    under a private name so it can't clash with another `app` package."""
    if "mro_demo_app.producer" not in sys.modules:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
        spec = importlib.util.spec_from_file_location("mro_demo_app", os.path.join(root, "__init__.py"),
                                                      submodule_search_locations=[root])
        sys.modules["mro_demo_app"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["mro_demo_app"])
    return importlib.import_module("mro_demo_app.producer")


def _pm(scenarios: dict[str, str], start: datetime.datetime, hours: int) -> dict:
    """Posts hourly HO_PERFORMANCE windows for the given relations and scenarios to RAN NF OAM as a PM report and returns its answer."""
    gen = _producer()
    measurements = [{"cellId": rel.split("-")[0], "relation": rel, "timestamp": t.isoformat(),
                     "values": gen.relation_counters(rel, t, scenario)}
                    for rel, scenario in scenarios.items()
                    for t in (start + datetime.timedelta(hours=h) for h in range(hours))]
    return call("post", "ran-nf-oam", "/pm-reports", json={"managedElementRef": ME, "counterType": "HO_PERFORMANCE",
                                                           "measurements": measurements})


def _governance(model_id: str, *events: str) -> None:
    for event in events:
        call("post", "aimgf", f"/models/{model_id}/advance",
             params={"event": event, "decided_by": OPERATOR, "rationale": f"demo: {event}"})


def _decision(result: dict, relation: str) -> dict:
    return next(d for d in result["decisions"] if d["relation"] == relation)


# ---------------------------------------------------------------- the steps

def demo_00(state: dict) -> None:
    """Prepare the RAN. Register one gNB-DU behind the mock O1 adaptor, with
    handover PM on it, the Digital Twin dataset, and the generic O1-CM
    intent handler."""
    endpoint = call("post", "ran-nf-oam", "/o1-adaptor-endpoints", json={
        "managedElementRef": ME, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"})
    call("post", "ran-nf-oam", f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat")
    call("post", "ran-nf-oam", "/pm-subscriptions", params={"managed_element_ref": ME, "counter_type": "HO_PERFORMANCE",
                                                           "delivery_method": "pull", "granularity_period": 3600})
    call("put", "ran-nf-oam", f"/managed-entities/{ME}/cells/204/guards", json={"cellClass": "EMERGENCY"})
    call("post", "mobility-optimization-rapp", "/sim-producer/register")
    call("post", "sa-smos", "/o1-cm-handler/registration", json={})
    show("managed element", ME)
    show("relations", [f"{r['source']}→{r['target']}" for r in RELATIONS])
    show("cell 204", "EMERGENCY — relations towards it are never tuned")


def demo_01(state: dict) -> None:
    """Onboard the rApp. Onboarding → AVAILABLE, then deploy an AUTONOMOUS
    instance and start it."""
    package = call("post", "onboarding", "/packages", json={"location": CSAR_URL})
    status = call("get", "onboarding", f"/packages/{package['packageId']}/onboarding-status")
    instance = call("post", "rapp-mgmt", "/instances", json={
        "operatorApiBase": _url("mobility-optimization-rapp", ""),      # where the gateway's /rapps/<instance>/operator/... reaches this rApp: its page in the GUI needs it
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS",
        "config": {"managedElementRef": ME, "relations": RELATIONS},
        "regionScope": {"objectInstance": ME, "cells": [r["relation"] for r in RELATIONS]}})
    state.update(packageId=package["packageId"], instanceId=instance["instanceId"])
    started = call("post", "mobility-optimization-rapp", f"/instances/{instance['instanceId']}/start")
    state["datasets"] = started["datasets"]
    state["bootstrap"] = started.get("bootstrap")      # call flow 01d: the rApp reported the instance up, as the instance
    show("instance bootstrap", state["bootstrap"])
    show("package state", status["state"])
    show("execution modes", status["aiCapabilities"]["executionModes"])
    show("instance", instance["instanceId"])


def demo_02(state: dict) -> None:
    """Show the dataset. Hourly handover counters per relation (attempts,
    too-late, too-early, wrong-cell, ping-pong) go RAN NF OAM → DME as
    HO_PERFORMANCE."""
    delivered = _pm(HISTORY, HISTORY_START, 72)
    job = state["datasets"]["TRAINING"]["dataJobId"]
    records = call("get", "dme", f"/data-jobs/{job}/records", params={"limit": 1})
    show("PM measurements reported", delivered["measurements"])
    show("datasets", {stage: d["dataset"] for stage, d in state["datasets"].items()})
    show("handover records in the training job", records["total"])
    state["historyRecords"] = records["total"]


def demo_03(state: dict) -> None:
    """Train the model. An AIMgF TrainingJob runs on the MLTF runtime;
    TRAINING → TRAINED."""
    trained = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/train")
    state["modelId"] = trained["modelId"]
    show("training job", trained["trainingJobId"])
    show("metrics", trained["metrics"])
    show("model lifecycle", trained["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_TRAINING")


def demo_04(state: dict) -> None:
    """Validate the model. VALIDATING → VALIDATED."""
    validated = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/validate")
    show("validation", validated["metrics"])
    show("model lifecycle", validated["lifecycle"]["modelLifecycleState"])
    _governance(state["modelId"], "APPROVE_VALIDATION")


def demo_05(state: dict) -> None:
    """Emulate the model. The Digital Twin injects one fault per relation;
    the controller must pick the right CIO direction for each."""
    call("post", "mobility-optimization-rapp", "/sim-producer/publish", json={
        "managedElementRef": "digital-twin-mro", "start": HISTORY_START.isoformat(), "hours": 48,
        "relations": {"a-b": "TOO_LATE", "b-c": "TOO_EARLY", "c-d": "HEALTHY", "d-e": "WRONG_CELL", "e-f": "PING_PONG"}})
    emulated = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/emulate")
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
    """Deploy the runtime and the DMRO bounds. MLLF checks the model, AIMgF and
    NFO deploy MLIF, and the gNB's DMROFunction is set to ±6 dB and read
    back."""
    deployed = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/lifecycle/deploy")
    show("runtime lifecycle", deployed["lifecycle"]["runtimeLifecycleState"])
    show("DMROFunction", {k: deployed["dmro"]["observed"].get(k) for k in deployed["dmro"]["bounds"]})
    show("DMRO verification", deployed["dmro"]["verification"])
    state.update(runtime=deployed["lifecycle"]["runtimeLifecycleState"], dmro=deployed["dmro"]["verification"])


def demo_08(state: dict) -> None:
    """Live inference. Two hours of live PM: 201→203 still hands over too
    late, 202→203 too early. The rApp raises one CIO and lowers the other by
    2 dB each."""
    _pm(HISTORY, LIVE_START, 2)
    result = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/evaluate")
    late, early = _decision(result, "201-203"), _decision(result, "202-203")
    state.update(executionId=result["executionId"], decision=late, early=early)
    show("201→203 prediction", late["prediction"]["model"])
    show("201→203 decision", f"{late['decision']} {late['fromCio']} → {late['toCio']} dB ({late['reason']})")
    show("202→203 decision", f"{early['decision']} {early['fromCio']} → {early['toCio']} dB ({early['reason']})")
    show("203→204 (emergency target)", _decision(result, "203-204")["reason"])


def demo_09(state: dict) -> None:
    """Show the DME action and the O1 update. The action record and its
    correlation chain, then NRCellRelation.cellIndividualOffset read back
    over NETCONF get-config."""
    d = state["decision"]
    action = call("get", "dme", f"/actions/{d['action']['actionId']}")
    show("action record", {k: action[k] for k in ("actionId", "status", "forwardedJobId")})
    show("correlation", {"executionId": state["executionId"], "dispatchId": d["intent"]["dispatchId"],
                         "intentId": d["intent"]["intentId"], "actionId": action["actionId"]})
    config = call("get", "ran-nf-oam", f"/managed-entities/{ME}/config", params={"managed_function_ref": "NRCellRelation=201-203"})
    show("NRCellRelation=201-203", config["attributes"])
    show("verification", d["verification"]["result"])
    state.update(action=action, o1=config["attributes"])


def demo_10(state: dict) -> None:
    """Check the KPI. An hour after the change, too-late failures on 201→203
    have dropped: the change is CONFIRMED (had they risen, it would have been
    reverted)."""
    _pm({**HISTORY, "201-203": "HEALTHY", "202-203": "HEALTHY"}, LIVE_START + datetime.timedelta(hours=2), 1)
    result = call("post", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/evaluate")
    d = _decision(result, "201-203")
    show("201→203 KPI", d["kpi"])
    show("201→203 outcome", d["outcome"])
    state["kpi"] = d


def demo_11(state: dict) -> None:
    """Show the Mobility dashboard. For each relation: failure-rate trend,
    prediction, decision, action and the CIO in force (GUI: Mobility page)."""
    dash = call("get", "mobility-optimization-rapp", f"/instances/{state['instanceId']}/dashboard")
    for r in dash["relations"]:
        d = r["latestDecision"] or {}
        model = (d.get("prediction") or {}).get("model") or {}
        print(f"  {r['source']}→{r['target']}: {r['state']:9} CIO {r['cio']:>3} dB  rate "
              f"{r['rateTrend'][-1]['v'] if r['rateTrend'] else '-':>6} → {model.get('futureRate', '-'):>6}  "
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
