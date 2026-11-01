#!/usr/bin/env python3
"""The TE&IV spike's measurements (plan task S0.13, ADR 0013): load time, query latency, change-event latency and runtime cost of smo-teiv 0.3.0,
directly on its API and through the TS 28.532 facade sketch (facade.py). Spike code: no module imports it.

    python spikes/teiv/measure.py --teiv http://127.0.0.1:31074 \
        --produce "docker compose -f spikes/teiv/docker-compose.yml exec -T kafka /opt/kafka/bin/kafka-console-producer.sh ..." \
        --load b=/tmp/b.events:/tmp/b.gaps.json [--load a=...] --label "laptop" --out results.json --markdown results.md

`--produce` is a command that reads CloudEvent lines (load.py's output) on its standard input and puts them on smo-teiv's ingestion topic; it is
started once and kept open, so the timings do not include a producer's start-up. Give kafka-console-producer `--timeout 0`: its default holds a message
up to a second to fill a batch, which would be counted as smo-teiv's latency. Each `--load NAME=EVENTS:GAPS` is loaded in turn; the load time runs
from the first line written to the moment smo-teiv's API shows every entity of the gap report's mappable classes and every relationship the events carry.

Queries (each `--runs` times after five warm-up calls, on random elements of the synthetic tree; p50, p95, p99 in milliseconds):
  Q1 one cell by id  Q2 a cell's neighbours (the extension's NEIGHBOR_CELL, one request)  Q2r a cell's NRCellRelation objects (the TS 28.532 form of
  a neighbour read)  Q3 all cells under a GNBDUFunction  Q4 two-hop neighbours (1 + 8 requests, the 8 in parallel; Q4or: 2 requests, the
  second with the 8 ids joined with `or`)  Q5 the cells of a SubNetwork with an attribute filter.
Target: p95 at most 150 ms at 1,000 cells (D7 "1,000-node queries", the ProvMnS latency target of S8.9). A query smo-teiv cannot express is reported
as such, and fails the target. Q2's and Q4's answers are checked against the tree (every 97th cell's adjacentNRCellRef targets, and theirs).

Change events: one NRCellDU's nRTAC is changed with a merge event; the time until smo-teiv's API shows the new value, and whether smo-teiv emits a
change notification (it has no producer: none is ever seen), and the time until the facade can raise notifyMOIAttributeValueChanges.
"""

import argparse
import datetime
import importlib.util
import json
import os
import platform
import random
import shlex
import statistics
import subprocess  # noqa: S404 — runs the producer and docker commands the operator passes, for a spike
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"spike_{name}", Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load = _module("load")
facade = _module("facade")
API = "/topology-inventory/v1"
TARGET_P95_MS = 150.0


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))]


def _stats(ms: list[float]) -> dict:
    return {"n": len(ms), "p50": round(statistics.median(ms), 1), "p95": round(_pct(ms, 95), 1), "p99": round(_pct(ms, 99), 1)}


DOMAIN = {facade.TYPES[c]: d for c, d in facade.DOMAIN_OF.items()}     # smo-teiv's domain of each entity type the spike writes


def _count(http: httpx.Client, teiv_type: str) -> int:
    r = http.get(f"/domains/{DOMAIN.get(teiv_type, 'RAN')}/entity-types/{teiv_type}/entities", params={"limit": 1})
    return r.json().get("totalCount", 0) if r.status_code == 200 else 0


def _count_relationships(http: httpx.Client, kind: str) -> int:
    """How many relationships of `kind` (module:TYPE) smo-teiv's API shows, read in the domain of the relationship's module."""
    module, name = kind.split(":", 1)
    r = http.get(f"/domains/{load.DOMAINS[module]}/relationship-types/{name}/relationships", params={"limit": 1})
    return r.json().get("totalCount", 0) if r.status_code == 200 else 0


def machine() -> dict:
    """What the numbers were taken on: platform, CPUs, memory."""
    mem = ""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = next((line.split(":", 1)[1].strip() for line in f if line.startswith("MemTotal")), "")
    except OSError:
        pass
    return {"platform": platform.platform(), "cpus": os.cpu_count(), "memory": mem, "python": platform.python_version(),
            "runner": os.environ.get("RUNNER_NAME", ""), "runnerOs": os.environ.get("ImageOS", "")}


class Producer:
    """The `--produce` command, started once; `send` writes CloudEvent lines and flushes."""

    def __init__(self, command: str):
        self.proc = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, text=True)  # noqa: S603 — the operator's own command
        time.sleep(6)                                                   # the console producer is a JVM: let it connect before anything is timed

    def send(self, lines: list[str]) -> None:
        """Writes the lines to the producer's standard input and flushes, so they leave now."""
        for line in lines:
            self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        """Closes the producer's input and waits for it to exit (it sends what it still holds first)."""
        self.proc.stdin.close()
        self.proc.wait(timeout=60)


def measure_load(http: httpx.Client, producer: Producer, name: str, events_file: str, gaps_file: str, timeout: float = 600) -> dict:
    """Load one source and time it: from the first line written to every mappable entity and every relationship visible (the counts per smo-teiv
    type grow by the report's)."""
    report = json.loads(Path(gaps_file).read_text())
    expect = {facade.TYPES[c]: n for c, n in report["byClass"].items() if c in facade.TYPES}
    expect_rel = report.get("relationships", {})
    before = {t: _count(http, t) for t in expect}
    before_rel = {k: _count_relationships(http, k) for k in expect_rel}
    lines = Path(events_file).read_text().splitlines()
    t0 = time.perf_counter()
    producer.send(lines)
    entities_at = None
    while time.perf_counter() - t0 < timeout:
        if entities_at is None and all(_count(http, t) >= before[t] + n for t, n in expect.items()):
            entities_at = time.perf_counter() - t0
        if entities_at is not None and all(_count_relationships(http, k) >= before_rel[k] + n for k, n in expect_rel.items()):
            return {"source": name, "mois": report["mois"], "entitiesMapped": sum(expect.values()), "relationships": sum(expect_rel.values()),
                    "events": len(lines), "seconds": round(time.perf_counter() - t0, 2), "entitiesSeconds": round(entities_at, 2), "byType": expect}
        time.sleep(0.2)
    return {"source": name, "mois": report["mois"], "entitiesMapped": sum(expect.values()), "relationships": sum(expect_rel.values()), "events": len(lines),
            "seconds": None, "error": f"not all visible after {timeout} s", "seen": {t: _count(http, t) - before[t] for t in expect},
            "seenRelationships": {k: _count_relationships(http, k) - before_rel[k] for k in expect_rel}}


def _time(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


def measure_queries(http: httpx.Client, fac, cells: int, runs: int, per_gnb: int = 3) -> list[dict]:
    """Q1-Q5, each `runs` times on smo-teiv's API (raw) and through the facade; random cells and gNBs of the synthetic tree. Q2 and Q4 read the
    NEIGHBOR_CELL relationship of the extension (plan task S3.20): Q2 is one request; smo-teiv's scope filter follows one association, so Q4 is
    Q2 and then Q2 again for each first-hop neighbour, sent together, and Q4or the same with the second hop as one request (the neighbours' ids
    joined with `or`), the form that is slow (ADR 0013 section 11). Q2r is the TS 28.532 form of a
    neighbour read: the cell's NRCellRelation objects with their attributes."""
    gnbs = (cells + per_gnb - 1) // per_gnb
    rnd = random.Random(11)  # noqa: S311 — choosing test inputs

    def cell(side: str = "GNBDUFunction=1,NRCellDU"):
        g = rnd.randrange(gnbs)
        return f"ManagedElement=gNB-{g:04d},{side}={rnd.randrange(1, per_gnb + 1) if g * per_gnb + per_gnb <= cells else 1}"

    def cu():
        return cell("GNBCUCPFunction=1,NRCellCU")

    def du():
        return f"ManagedElement=gNB-{rnd.randrange(gnbs):04d},GNBDUFunction=1"

    def ok(r):
        r.raise_for_status()
        return r

    def neighbours_raw(dn: str, target: str = "/attributes") -> httpx.Response:
        return ok(http.get("/domains/EXT_RAN/entity-types/NRCellCU/entities",
                           params={"targetFilter": target, "scopeFilter": f"/{facade.NEIGHBOUR_SCOPE}[@id='{load.TEIV_ID + dn}']", "limit": 500}))

    def two_hop_raw(fan_out: bool = True) -> list:
        dn = cu()
        first = [e["id"] for _, e in facade._entities(neighbours_raw(dn, "/id").json())]
        if fan_out:
            with ThreadPoolExecutor(max_workers=len(first)) as pool:
                return list(pool.map(lambda i: neighbours_raw(i.removeprefix(load.TEIV_ID)), first))
        ids = " or ".join(f"@id='{i}'" for i in first)
        return [ok(http.get("/domains/EXT_RAN/entity-types/NRCellCU/entities",
                            params={"targetFilter": "/attributes", "scopeFilter": f"/{facade.NEIGHBOUR_SCOPE}[{ids}]", "limit": 500}))]

    raw = {
        "Q1": lambda: ok(http.get(f"/domains/RAN/entity-types/NRCellDU/entities/{quote(load.TEIV_ID + cell(), safe=':=,')}")),
        "Q2": lambda: neighbours_raw(cu()),
        "Q2r": lambda: ok(http.get("/domains/EXT_RAN/entity-types/NRCellRelation/entities", params={
            "targetFilter": "/attributes", "scopeFilter": f"/contained-by-nrCellCu[@id='{load.TEIV_ID + cu()}']", "limit": 500})),
        "Q3": lambda: ok(http.get("/domains/RAN/entity-types/NRCellDU/entities", params={"scopeFilter": f"/provided-by-oduFunction[@id='{load.TEIV_ID + du()}']"})),
        "Q4": two_hop_raw,
        "Q4or": lambda: two_hop_raw(fan_out=False),
        "Q5": lambda: ok(http.get("/domains/RAN/entity-types/NRCellDU/entities", params={"targetFilter": "/attributes(nRTAC,nRPCI)",
                                  "scopeFilter": f"/sourceIds[contains(@item,'SubNetwork=Spike')];/attributes[@nRTAC={100 + rnd.randrange(50)}]", "limit": 500})),
    }
    via = {
        "Q1": lambda: fac.get_moi_attributes(cell()),
        "Q2": lambda: fac.neighbours(cu(), 1),
        "Q2r": lambda: fac.get_moi_attributes(cu(), "BASE_ALL", filter={"class": "NRCellRelation"}),
        "Q3": lambda: fac.get_moi_attributes(du(), "BASE_ALL", filter={"class": "NRCellDU"}),
        "Q4": lambda: fac.neighbours(cu(), 2),
        "Q4or": lambda: fac.neighbours(cu(), 2, fan_out=False),
        "Q5": lambda: fac.get_moi_attributes("SubNetwork=Spike", "BASE_ALL", filter={"class": "NRCellDU", "attributes": {"nRTAC": 100 + rnd.randrange(50)}}),
    }
    titles = {"Q1": "one cell by id", "Q2": "a cell's neighbours (NEIGHBOR_CELL)", "Q2r": "a cell's NRCellRelation objects (TS 28.532 form)",
              "Q3": "all cells under a GNBDUFunction", "Q4": "two-hop neighbours (1 + 8 requests, the 8 together)",
              "Q4or": "two-hop neighbours (2 requests, the second with 8 ids joined with or)", "Q5": "all cells in a SubNetwork with an attribute filter"}
    out = []
    for q, title in titles.items():
        if q not in raw:
            out.append({"query": q, "title": title, "raw": None, "facade": None, "result": "FAIL", "why": "not expressible"})
            continue
        n = min(runs, 20) if q == "Q4or" else runs                       # seconds a call: fewer runs, and the result says how many
        for fn in (raw[q], via[q]):
            for _ in range(2 if q == "Q4or" else 5):
                fn()
        answer = via[q]()
        r_ms = [_time(raw[q]) for _ in range(n)]
        f_ms = [_time(via[q]) for _ in range(n)]
        rs, fs = _stats(r_ms), _stats(f_ms)
        out.append({"query": q, "title": title, "raw": rs, "facade": fs, "facadeAddedP50": round(fs["p50"] - rs["p50"], 1),
                    "answerSize": len(answer), "result": "PASS" if fs["p95"] <= TARGET_P95_MS else "FAIL"})
    return out


def check_neighbours(fac, mois: list) -> dict:
    """Q2 and Q4 answer what the tree says: for a few cells, the facade's neighbours are the adjacentNRCellRef targets of the cell's
    NRCellRelations, and its two-hop set is those targets' targets (without the cell)."""
    adj: dict[str, set[str]] = {}
    for m in mois:
        if m.cls == "NRCellRelation":
            adj.setdefault(m.dn.rsplit(",", 1)[0], set()).add(m.attributes["adjacentNRCellRef"])
    checked, wrong = 0, []
    for dn in sorted(adj)[::97]:
        one = {m["objectInstance"] for m in fac.neighbours(dn, 1)}
        two = {m["objectInstance"] for m in fac.neighbours(dn, 2)}
        want_two = (adj[dn] | {t for n in adj[dn] for t in adj.get(n, ())}) - {dn}
        checked += 1
        if one != adj[dn] or two != want_two:
            wrong.append({"cell": dn, "q2": len(one), "q2Expected": len(adj[dn]), "q4": len(two), "q4Expected": len(want_two)})
    return {"cellsChecked": checked, "wrong": wrong}


def measure_change(http: httpx.Client, producer: Producer, runs: int = 10) -> dict:
    """Change one NRCellDU's nRTAC `runs` times: the time until smo-teiv's API shows the new value (polled every 10 ms), whether a change event
    reaches the topic smo-teiv would publish on (it publishes none), and the facade's notification after visibility."""
    rnd = random.Random(5)  # noqa: S311 — choosing test inputs
    visible, notified = [], []
    for i in range(runs):
        dn = f"ManagedElement=gNB-{rnd.randrange(300):04d},GNBDUFunction=1,NRCellDU=1"
        path = f"/domains/RAN/entity-types/NRCellDU/entities/{quote(load.TEIV_ID + dn, safe=':=,')}"
        before = facade._entities(http.get(path).json())[0][1]
        new = 900 + i
        entity = {"id": load.TEIV_ID + dn, "attributes": {**before.get("attributes", {}), "nRTAC": new}, "sourceIds": [load.TEIV_ID + "SubNetwork=Spike," + dn]}
        line = load.cloud_event_line({"entities": [{"o-ran-smo-teiv-ran:NRCellDU": [entity]}], "relationships": []})
        t0 = time.perf_counter()
        producer.send([line])
        while time.perf_counter() - t0 < 60:
            after = facade._entities(http.get(path).json())[0][1]
            if after.get("attributes", {}).get("nRTAC") == new:
                t1 = time.perf_counter()
                notes = facade.notifications_for("ran-logical-topology.merge", "NRCellDU", after, before)
                t2 = time.perf_counter()
                visible.append((t1 - t0) * 1000)
                notified.append((t2 - t0) * 1000 if notes and notes[0]["notificationType"] == "notifyMOIAttributeValueChanges" else float("nan"))
                break
            time.sleep(0.01)
    return {"runs": runs, "visible": _stats(visible) if visible else None,
            "smoTeivNotification": "none: smo-teiv 0.3.0 has no change-event producer and no subscription API",
            "facadeNotification": _stats([n for n in notified if n == n]) if notified else None}


def runtime(compose_file: str | None) -> dict:
    """Image sizes and the memory and CPU of each container of the spike (docker image inspect, docker compose stats), when docker is at hand."""
    out: dict = {}
    if not compose_file:
        return out
    for image in ("local/teiv-spike:0.3.0-ext", "local/teiv-spike-db:0.3.0-ext"):
        r = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Size}}"], capture_output=True, text=True)  # noqa: S603,S607
        out.setdefault("imageBytes", {})[image] = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    r = subprocess.run(["docker", "compose", "-f", compose_file, "stats", "--no-stream", "--format", "json"], capture_output=True, text=True)  # noqa: S603,S607
    out["containers"] = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
    return out


def markdown(result: dict) -> str:
    """The result as the tables ADR 0013 quotes."""
    lines = [f"**Machine:** {result['label']} — {result['machine']['platform']}, {result['machine']['cpus']} CPUs, {result['machine']['memory']}", "",
             "| Load | MOIs | entities in smo-teiv | relationships | events | entities visible, s | all visible, s |", "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [f"| {x['source']} | {x['mois']} | {x['entitiesMapped']} | {x.get('relationships', '')} | {x['events']} | {x.get('entitiesSeconds', '')} | "
              f"{x['seconds'] if x['seconds'] is not None else x.get('error')} |" for x in result["load"]]
    lines += ["", "| Query | raw p50 / p95 / p99 ms | facade p50 / p95 / p99 ms | facade adds (p50) | answer (MOIs) | target p95 ≤ 150 ms |",
              "|---|---|---|---:|---:|---|"]
    for q in result["queries"]:
        if q["raw"] is None:
            lines.append(f"| {q['query']} {q['title']} | – | – | – | – | **FAIL** ({q['why']}) |")
        else:
            r, f = q["raw"], q["facade"]
            lines.append(f"| {q['query']} {q['title']} | {r['p50']} / {r['p95']} / {r['p99']} | {f['p50']} / {f['p95']} / {f['p99']} | "
                         f"{q['facadeAddedP50']} | {q['answerSize']} | **{q['result']}** |")
    if result.get("neighbourCheck"):
        nc = result["neighbourCheck"]
        lines += ["", f"Q2 / Q4 answers checked against the tree's adjacentNRCellRef on {nc['cellsChecked']} cells: "
                  + ("all correct" if not nc["wrong"] else f"{len(nc['wrong'])} wrong: {nc['wrong']}")]
    c = result["change"]
    v, n = c.get("visible") or {}, c.get("facadeNotification") or {}
    lines += ["", "| Change of one NRCellDU | p50 ms | p95 ms |", "|---|---:|---:|",
              f"| visible in smo-teiv's API | {v.get('p50')} | {v.get('p95')} |", f"| smo-teiv change notification | – | – ({c['smoTeivNotification']}) |",
              f"| facade notifyMOIAttributeValueChanges | {n.get('p50')} | {n.get('p95')} |"]
    if result.get("runtime", {}).get("containers"):
        lines += ["", "| Container | memory | CPU |", "|---|---|---|"]
        lines += [f"| {x.get('Name')} | {x.get('MemUsage')} | {x.get('CPUPerc')} |" for x in result["runtime"]["containers"]]
        lines += [f"", "Images: " + ", ".join(f"{k} {round(v / 1e6)} MB" for k, v in result["runtime"]["imageBytes"].items() if v)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring; writes the JSON result and the Markdown tables and returns 0 (a FAIL is a result, not an error)."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--teiv", default="http://127.0.0.1:31074")
    p.add_argument("--produce", required=True)
    p.add_argument("--load", action="append", default=[], help="NAME=EVENTS_FILE:GAPS_FILE")
    p.add_argument("--cells", type=int, default=1000, help="the synthetic tree's cell count (the queries pick from it)")
    p.add_argument("--runs", type=int, default=50)
    p.add_argument("--label", default=platform.node())
    p.add_argument("--compose-file")
    p.add_argument("--out", required=True)
    p.add_argument("--markdown")
    a = p.parse_args(argv)
    http = httpx.Client(base_url=a.teiv.rstrip("/") + API, timeout=30)
    producer = Producer(a.produce)
    result = {"label": a.label, "machine": machine(), "takenAt": datetime.datetime.now(datetime.UTC).isoformat(), "smoTeiv": "0.3.0",
              "model": "smo-teiv 0.3.0 with TE&IV's extension (spikes/teiv/model/)", "load": []}
    for spec in a.load:
        name, files = spec.split("=", 1)
        events_file, gaps_file = files.split(":", 1)
        result["load"].append(measure_load(http, producer, name, events_file, gaps_file))
        print(json.dumps(result["load"][-1]))
    fac = facade.TeivFacade(a.teiv)
    result["queries"] = measure_queries(http, fac, a.cells, a.runs)
    result["neighbourCheck"] = check_neighbours(fac, load.synthetic(a.cells))
    result["change"] = measure_change(http, producer)
    producer.close()
    result["runtime"] = runtime(a.compose_file)
    Path(a.out).write_text(json.dumps(result, indent=1))
    text = markdown(result)
    if a.markdown:
        Path(a.markdown).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
