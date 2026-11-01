#!/usr/bin/env python3
"""TE&IV's memory budget at 1,000 cells (plan task S3.23, ADR 0013 sections 8 and 11.6): smo-teiv's two processes, its Postgres and the shared Kafka broker
of the compose profile `teiv` hold 3 GiB resident while the topology of 1,000 cells is loaded and read.

    ./scripts/init_secrets.sh && docker compose --profile teiv up -d --build --wait     # from smo/
    python teiv/loadtest/budget.py --out /tmp/teiv-budget.json --markdown /tmp/teiv-budget.md

What it does, against the running profile (`docker compose`, so the compose files it uses are the ones COMPOSE_FILE names):

1. Writes the extension spike's synthetic tree (`spikes/teiv/load.py --synthetic`, ADR 0013 section 11.4: 1,000 NRCellDU, 8 NRCellRelation each, 18,004
   MOIs and 44,003 relationships, 61 CloudEvents) as smo-teiv's ingestion events.
2. Produces them to TE&IV's topic (smo.teiv.ingestion.v1) with the broker's own console producer (`docker compose exec kafka`), and waits until every entity
   of the tree is visible in smo-teiv's API.
3. Reads the API for a while (one cell, a cell's neighbours, the cells of a GNBDUFunction), so the exposure process is measured serving, not idle.
4. Samples `docker stats` of the four containers every two seconds from before the load to the end of the reads, and reports each one's peak and the peak
   of their sum against the budget (3 GiB, `--budget-mib`). Exit 1 when the sum went over it, or when the tree did not become visible.

smo-teiv's API is read from inside the `teiv-exposure` container (`docker compose exec teiv-exposure curl`): it publishes no port and is on the internal
network `teiv` only (D19: no module but TE&IV reaches it), and the test does not open one. The synthetic tree and smo-teiv's type names come from the
spike's `load.py` and `facade.py` (loaded by path, as `spikes/teiv/measure.py` does; no module imports them), until TE&IV's own facade replaces them (S3.19).
Standard library only, besides what `load.py` imports.
"""

import argparse
import datetime
import importlib.util
import json
import os
import platform
import re
import subprocess  # noqa: S404 — runs docker compose with fixed argument vectors
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

SMO = Path(__file__).resolve().parents[2]
SPIKE = SMO / "spikes" / "teiv"
TOPIC = "smo.teiv.ingestion.v1"
API = "http://localhost:8080/topology-inventory/v1"
SERVICES = ("teiv-ingestion", "teiv-exposure", "teiv-db", "kafka")
BUDGET_MIB = 3072
UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "kb": 1000 / 2**20, "mib": 1, "mb": 10**6 / 2**20, "gib": 1024, "gb": 10**9 / 2**20}


def _spike(name: str):
    """The spike module `name` (load or facade), loaded from spikes/teiv/ by path."""
    sys.path.insert(0, str(SPIKE))
    spec = importlib.util.spec_from_file_location(f"spike_{name}", SPIKE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mib(text: str) -> float:
    """A `docker stats` size ("1.05GiB", "527.3MiB", "12kB") in MiB."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if not m or m.group(2).lower() not in UNITS:
        raise ValueError(f"not a size: {text!r}")
    return float(m.group(1)) * UNITS[m.group(2).lower()]


def compose(*args: str, stdin: str | None = None, timeout: float = 120) -> str:
    """`docker compose --profile teiv <args>`; its standard output, or RuntimeError with its standard error."""
    r = subprocess.run(["docker", "compose", "--profile", "teiv", *args], input=stdin, capture_output=True, text=True, timeout=timeout)  # noqa: S603,S607
    if r.returncode:
        raise RuntimeError(f"docker compose {' '.join(args[:3])}: {r.stderr.strip()[-500:]}")
    return r.stdout


def containers() -> dict[str, str]:
    """service -> container id of the four services the budget covers (all must be running)."""
    ids = {}
    for service in SERVICES:
        cid = compose("ps", "-q", service).strip()
        if not cid:
            raise RuntimeError(f"{service} is not running: start the profile first (docker compose --profile teiv up -d --wait)")
        ids[service] = cid
    return ids


def sample(ids: dict[str, str]) -> dict[str, float]:
    """One `docker stats --no-stream` reading: service -> resident MiB."""
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.ID}} {{.MemUsage}}", *ids.values()],  # noqa: S603,S607
                       capture_output=True, text=True, timeout=60)
    by_id = {}
    for line in r.stdout.splitlines():
        cid, usage = line.split(" ", 1)
        by_id[cid] = mib(usage.split("/")[0])
    return {service: next(v for k, v in by_id.items() if cid.startswith(k)) for service, cid in ids.items()}


class Sampler(threading.Thread):
    """Samples the four containers' memory every `interval` seconds until stopped; keeps every reading."""

    def __init__(self, ids: dict[str, str], interval: float = 2.0):
        super().__init__(daemon=True)
        self.ids, self.interval, self.readings, self.stop = ids, interval, [], threading.Event()

    def run(self) -> None:
        """Reads until `stop` is set; a failed reading is skipped (docker stats can time out under load)."""
        while not self.stop.is_set():
            try:
                self.readings.append(sample(self.ids))
            except (subprocess.SubprocessError, ValueError, StopIteration):
                pass
            self.stop.wait(self.interval)


def api(path: str) -> dict:
    """GET smo-teiv's API from inside teiv-exposure; the decoded JSON (an empty dict when the answer is not 200)."""
    out = compose("exec", "-T", "teiv-exposure", "curl", "-s", "-w", "\n%{http_code}", f"{API}{path}")
    body, _, code = out.rpartition("\n")
    return json.loads(body) if code.strip() == "200" and body else {}


def count(domain: str, teiv_type: str) -> int:
    """How many entities of `teiv_type` smo-teiv's API shows."""
    return api(f"/domains/{domain}/entity-types/{teiv_type}/entities?limit=1").get("totalCount", 0)


def load(cells: int, timeout: float) -> dict:
    """Writes the synthetic tree's events, produces them to TE&IV's topic and waits until every entity is visible; what happened, with the seconds."""
    loader, facade = _spike("load"), _spike("facade")
    with tempfile.TemporaryDirectory() as tmp:
        events, gaps = Path(tmp, "events"), Path(tmp, "gaps.json")
        loader.main(["--synthetic", str(cells), "--events", str(events), "--gaps", str(gaps)])
        report = json.loads(gaps.read_text())
        lines = events.read_text()
    expect = {(facade.DOMAIN_OF[c], facade.TYPES[c]): n for c, n in report["byClass"].items() if c in facade.TYPES}
    before = {k: count(*k) for k in expect}
    t0 = time.perf_counter()
    compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-console-producer.sh", "--bootstrap-server", "localhost:9092", "--topic", TOPIC,
            "--property", "parse.headers=true", "--property", "headers.key.separator=:::", "--property", "headers.delimiter=,,,",
            "--property", "parse.key=false", stdin=lines, timeout=timeout)
    seen = dict.fromkeys(expect, 0)
    while time.perf_counter() - t0 < timeout:
        seen = {k: count(*k) - before[k] for k in expect}
        if all(seen[k] >= n for k, n in expect.items()):
            return {"cells": cells, "mois": report["mois"], "entities": sum(expect.values()), "relationships": sum(report["relationships"].values()),
                    "events": report["events"], "seconds": round(time.perf_counter() - t0, 1)}
        time.sleep(2)
    return {"cells": cells, "mois": report["mois"], "entities": sum(expect.values()), "events": report["events"], "seconds": None,
            "error": f"not every entity visible after {timeout:.0f} s", "seen": sum(seen.values())}


def read(seconds: float, cells: int) -> int:
    """Reads smo-teiv's API for `seconds`, the spike's Q1, Q2 and Q3 on cells spread over the tree, from one shell loop in teiv-exposure; how many reads."""
    paths = []
    for i in range(0, cells, max(1, cells // 50)):
        me, c = f"ManagedElement=gNB-{i // 3:04d}", i % 3 + 1          # the tree's names: 3 cells to a gNB (load.synthetic), ids are DNs below the SubNetwork
        du_cell = quote(f"urn:3gpp:dn:{me},GNBDUFunction=1,NRCellDU={c}", safe="")
        cu_cell = f"urn:3gpp:dn:{me},GNBCUCPFunction=1,NRCellCU={c}"
        paths += [f"/domains/RAN/entity-types/NRCellDU/entities/{du_cell}",
                  "/domains/EXT_RAN/entity-types/NRCellCU/entities?limit=500&targetFilter=/attributes&scopeFilter="
                  + quote(f"/neighbor-of-nrCellCu[@id='{cu_cell}']", safe=""),
                  "/domains/RAN/entity-types/NRCellDU/entities?scopeFilter=" + quote(f"/provided-by-oduFunction[@id='urn:3gpp:dn:{me},GNBDUFunction=1']", safe="")]
    script = ("end=$(( $(date +%s) + {s} )); n=0; while [ $(date +%s) -lt $end ]; do for p in {p}; do curl -s -o /dev/null \"{api}$p\"; "
              "n=$((n+1)); done; done; echo $n").format(s=int(seconds), p=" ".join(f"'{p}'" for p in paths), api=API)
    return int(compose("exec", "-T", "teiv-exposure", "sh", "-c", script, timeout=seconds + 120).strip().splitlines()[-1])


def machine() -> dict:
    """Where the numbers were taken: platform, CPUs, memory, the runner when in CI."""
    mem = ""
    try:
        mem = next((x.split(":", 1)[1].strip() for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemTotal")), "")
    except OSError:
        pass
    return {"platform": platform.platform(), "cpus": os.cpu_count(), "memory": mem, "runner": os.environ.get("RUNNER_NAME", "")}


def summarise(readings: list[dict[str, float]], budget: float) -> dict:
    """Each service's peak, the peak of the sum and the last reading, in MiB, and whether the sum stayed within `budget`."""
    if not readings:
        return {"error": "no docker stats reading", "within": False}
    peak = {s: round(max(r[s] for r in readings), 1) for s in SERVICES}
    total = round(max(sum(r.values()) for r in readings), 1)
    return {"samples": len(readings), "peakMiB": peak, "peakTotalMiB": total, "lastMiB": {s: round(v, 1) for s, v in readings[-1].items()},
            "budgetMiB": budget, "within": total <= budget}


def markdown(result: dict) -> str:
    """The result as the Markdown table the README and the CI job summary show."""
    m, lo = result["memory"], result["load"]
    rows = "\n".join(f"| {s} | {m['peakMiB'][s]:.0f} | {m['lastMiB'][s]:.0f} |" for s in SERVICES) if "peakMiB" in m else ""
    return (f"### TE&IV memory budget at {lo['cells']} cells\n\n{result['machine']['platform']}, {result['machine']['cpus']} CPUs, "
            f"{result['machine']['memory']}; {result['taken']}\n\n"
            f"Load: {lo.get('mois')} MOIs, {lo.get('entities')} entities, {lo.get('relationships', '?')} relationships, {lo.get('events')} events, "
            f"all visible after **{lo.get('seconds')} s**; then {result['reads']} reads in {result['readSeconds']} s.\n\n"
            f"| Container | peak MiB | after the reads MiB |\n|---|---:|---:|\n{rows}\n"
            f"| **sum** (peak of the sum) | **{m.get('peakTotalMiB')}** | |\n\n"
            f"Budget {m.get('budgetMiB')} MiB: **{'within' if m.get('within') else 'OVER'}**\n")


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring; 0 within the budget, 1 over it or when the load did not settle."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cells", type=int, default=1000)
    p.add_argument("--budget-mib", type=float, default=BUDGET_MIB)
    p.add_argument("--read-seconds", type=float, default=60)
    p.add_argument("--timeout", type=float, default=900, help="seconds the load may take to become visible")
    p.add_argument("--out", required=True, help="the result as JSON")
    p.add_argument("--markdown", help="the result as Markdown")
    a = p.parse_args(argv)
    ids = containers()
    sampler = Sampler(ids)
    sampler.start()
    loaded = load(a.cells, a.timeout)
    reads = read(a.read_seconds, a.cells) if loaded.get("seconds") is not None else 0
    time.sleep(4)
    sampler.stop.set()
    sampler.join(timeout=70)
    result = {"taken": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC"), "machine": machine(), "load": loaded,
              "reads": reads, "readSeconds": a.read_seconds, "memory": summarise(sampler.readings, a.budget_mib)}
    Path(a.out).write_text(json.dumps(result, indent=1) + "\n")
    if a.markdown:
        Path(a.markdown).write_text(markdown(result))
    print(markdown(result))
    return 0 if result["memory"]["within"] and loaded.get("seconds") is not None else 1


if __name__ == "__main__":
    sys.exit(main())
