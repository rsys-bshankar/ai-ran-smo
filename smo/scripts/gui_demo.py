#!/usr/bin/env python3
"""The presenter's helper for the GUI demo (docs/GUI_DEMO_GUIDE.md): the parts of a sample rApp's demo that the operator console has no button for.

    python3 /srv/scratch/gui_demo.py <rapp> <command> [instanceId]

<rapp> is energy-saving-rapp, mobility-optimization-rapp, coverage-optimization-rapp or traffic-steering-rapp. Run it inside the compose network, from
the r1-termination container, next to the rApp's demo directory copied to /srv/scratch/<rapp> (DEMO_RUNBOOK.md §24-§27 copy it there). It imports that
rApp's own demo.py and runs one of its steps, or the part of a step the console does not do, so the data, ids and timings are exactly the script's:

    prepare          step 00 without the RAN NF OAM calls (done in the GUI): registers the Digital Twin data producer and the O1-CM intent handler
    adopt <id>       binds the instance created in the GUI (rApps > Deploy) to the rApp: POST /instances/<id>/start, and keeps the id for the next commands
    history          step 02: three days of history PM into RAN NF OAM -> DME; prints the records DME holds for the training job
    train, validate  steps 03 and 04 up to, not including, the governance decision (that is the GUI's "Approve training" / "Approve validation")
    emulate          step 05: the Digital Twin publishes its data and the model is emulated against it
    deploy           step 07: MLLF checks the model, AIMgF and NFO deploy the inference runtime
    live             step 08's live PM, without the evaluation (that is the GUI's "Evaluate now")
    kpi-hour         step 10's next hour of PM, without the evaluation (not for energy-saving-rapp, whose step 10 is the O1 read-back)
    dme              step 09: the DME action record and its correlation chain of each decision that wrote (no page shows a DME action record)

The ids live in the demo's own state file ($DEMO_STATE), so `demo.py <step>` and this helper can be mixed.
"""

import importlib.util
import json
import os
import sys

RAPPS = ("energy-saving-rapp", "mobility-optimization-rapp", "coverage-optimization-rapp", "traffic-steering-rapp")
SCRATCH = os.environ.get("GUI_DEMO_DIR", "/srv/scratch")


class _Stop(Exception):
    """Raised by the patched `call` at the call the console makes instead (an evaluation or a governance decision)."""


def load(rapp: str):
    """The rApp's demo.py as a module, from <GUI_DEMO_DIR>/<rapp>/demo.py."""
    path = os.path.join(SCRATCH, rapp, "demo.py")
    spec = importlib.util.spec_from_file_location(f"demo_{rapp.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(demo) -> dict:
    """The demo's saved state, or an empty one."""
    return json.load(open(demo.STATE_FILE)) if os.path.exists(demo.STATE_FILE) else {}


def _save(demo, state: dict) -> None:
    """Keeps `state` where demo.py keeps it, so its own steps can run next."""
    with open(demo.STATE_FILE, "w") as f:
        json.dump({k: v for k, v in state.items() if k != "dashboard"}, f, default=str)


def _run_until(demo, step: str, state: dict, stop=lambda verb, service, path: False, skip=lambda verb, service, path: False) -> None:
    """Runs demo step `step` with its `call` patched: a call `skip` names answers a stand-in and is not made, and the first call `stop` names ends the
    step there (the work before it is done; the console does the rest)."""
    real = demo.call

    def patched(verb, service, path, *args, **kw):
        if stop(verb, service, path):
            raise _Stop(path)
        if skip(verb, service, path):
            return {"endpointId": "set-up-in-the-gui"}
        return real(verb, service, path, *args, **kw)

    demo.call = patched
    try:
        demo.STEPS[step](state)
    except _Stop:
        pass
    finally:
        demo.call = real


def show_dme(demo, rapp: str, state: dict) -> None:
    """Step 09 for a decision made from the console: for every cell or relation whose latest decision wrote through DME, the action record, its source and
    the correlation chain execution -> dispatch -> intent -> action (read from the rApp's dashboard, so it needs no state from `demo.py 08`)."""
    if "instanceId" not in state:
        raise SystemExit("no instance kept yet: run `adopt <instanceId>` first")
    dash = demo.call("get", rapp, f"/instances/{state['instanceId']}/dashboard")
    shown = 0
    for row in dash.get("cells") or dash.get("relations") or []:
        decision = row.get("latestDecision") or {}
        action_id = (decision.get("action") or {}).get("actionId")
        if not action_id:
            continue
        action = demo.call("get", "dme", f"/actions/{action_id}")
        intent = decision.get("intent") or {}
        print(f"{row.get('cellId') or row.get('relation')}: decision {decision.get('decision')}, outcome {decision.get('outcome')}")
        print("  action record:", json.dumps({k: action.get(k) for k in ("actionId", "status", "forwardedJobId")}))
        print("  action source:", json.dumps({"requestedBy": action.get("requestedBy"), **(action.get("sourceContext") or {})}))
        print("  correlation:  ", json.dumps({"executionId": decision.get("executionId"), "dispatchId": intent.get("dispatchId"),
                                              "intentId": intent.get("intentId"), "actionId": action_id}))
        shown += 1
    if not shown:
        print("no decision has written through DME yet: press Evaluate now on the rApp's page after `live`")


def main(argv: list[str]) -> None:
    """Parses `<rapp> <command> [instanceId]`, runs the command and prints what it did."""
    if len(argv) < 2 or argv[0] not in RAPPS:
        raise SystemExit(__doc__)
    rapp, command, rest = argv[0], argv[1], argv[2:]
    demo = load(rapp)
    state = _state(demo)
    evaluate = lambda verb, service, path: path.endswith("/evaluate")
    govern = lambda verb, service, path: path.endswith("/advance")
    if command == "prepare":
        _run_until(demo, "00", state, skip=lambda verb, service, path: service == "ran-nf-oam")
        print("Digital Twin producer and O1-CM intent handler registered")
    elif command == "adopt":
        if not rest:
            raise SystemExit("adopt needs the instance id the GUI showed after Deploy")
        state = {"instanceId": rest[0]}
        started = demo.call("post", rapp, f"/instances/{rest[0]}/start")
        state["datasets"] = started["datasets"]
        print("instance", rest[0], "started; datasets:", {stage: d["dataset"] for stage, d in started["datasets"].items()})
    elif command == "history":
        demo.run("02", state)
    elif command in ("train", "validate"):
        _run_until(demo, "03" if command == "train" else "04", state, stop=govern)
        print(f"{command} done; model {state.get('modelId')} now waits for the governance decision in the GUI (AI/ML > Models)")
    elif command == "emulate":
        demo.run("05", state)
    elif command == "deploy":
        demo.run("07", state)
    elif command == "live":
        _run_until(demo, "08", state, stop=evaluate)
        print("live PM delivered; press Evaluate now on the rApp's page")
    elif command == "kpi-hour":
        if rapp == "energy-saving-rapp":
            raise SystemExit("energy-saving-rapp has no KPI hour: its step 10 is the O1 read-back (Element page > Managed objects)")
        _run_until(demo, "10", state, stop=evaluate)
        print("the next hour of PM delivered; press Evaluate now again to see the change CONFIRMED (or reverted)")
    elif command == "dme":
        show_dme(demo, rapp, state)
    else:
        raise SystemExit(__doc__)
    _save(demo, state)


if __name__ == "__main__":
    main(sys.argv[1:])
