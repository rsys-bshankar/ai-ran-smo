#!/usr/bin/env python3
"""TE&IV's memory budget at 1,000 cells (plan task S3.23, ADR 0013 sections 8 and 11.6): smo-teiv's two processes, its Postgres and the shared Kafka broker
of the compose profile `teiv` hold 3 GiB resident while the topology of 1,000 cells is loaded and read.

    ./scripts/init_secrets.sh && docker compose --profile teiv up -d --build --wait     # from smo/
    python teiv/loadtest/budget.py --out /tmp/teiv-budget.json --markdown /tmp/teiv-budget.md

What it does, against the running profile (`docker compose`, so the compose files it uses are the ones COMPOSE_FILE names):

1. Writes TE&IV's 1,000-cell tree (`tree.py`: 1,000 NRCellDU, 8 NRCellRelation each, 18,004 objects and 44,003 relationships, ADR 0013 section
   11.4's tree in TE&IV's forms) with TE&IV's own encoder (`app/ingest.py`): smo-teiv ingestion events, and the updateDecorator calls of the attributes
   TE&IV holds as decorators (S3.21), after loading TE&IV's decorator schema (`app/attrs.py`) as TE&IV does at start-up.
2. Produces the events to TE&IV's topic (smo.teiv.ingestion.v1) with the broker's own console producer (`docker compose exec kafka`), waits until every
   entity of the tree is visible in smo-teiv's API, then sends the decorator calls, and runs the maintenance after a bulk load (REINDEX, ANALYZE).
3. Reads the API for a while (one cell, a cell's neighbours, the cells of a GNBDUFunction), so the exposure process is measured serving, not idle.
4. Samples `docker stats` of the four containers every two seconds from before the load to the end of the reads, and reports each one's peak and the peak
   of their sum against the budget (3 GiB, `--budget-mib`). Exit 1 when the sum went over it, or when the tree did not become visible.

smo-teiv's API is read from inside the `teiv-exposure` container (`docker compose exec teiv-exposure curl`): it publishes no port and is on the internal
network `teiv` only (D19: no module but TE&IV reaches it), and the test does not open one. The tree, the encoder and the type names are TE&IV's (`teiv/`
on the path); standard library otherwise.
"""

import argparse
import datetime
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
TEIV = SMO / "teiv"
sys.path[:0] = [str(TEIV), str(TEIV / "loadtest"), str(SMO / "shared")]
os.environ.setdefault("SMO_DATABASE_URL", "sqlite://")      # app.* imports smo_shared, which wants one; nothing here touches a database
TOPIC = "smo.teiv.ingestion.v1"
API = "http://localhost:8080/topology-inventory/v1"
SERVICES = ("teiv-ingestion", "teiv-exposure", "teiv-db", "kafka")
BUDGET_MIB = 3072
UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "kb": 1000 / 2**20, "mib": 1, "mb": 10**6 / 2**20, "gib": 1024, "gb": 10**9 / 2**20}


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
    """Loads TE&IV's decorator schema, writes the tree's events, produces them to TE&IV's topic, waits until every entity is visible, then sends the
    decorator calls; what happened, with the seconds."""
    from app import attrs, ingest, model
    import tree
    mois = tree.synthetic(cells)
    events, decorator_calls = ingest.encode(mois)
    compose("exec", "-T", "teiv-exposure", "curl", "-s", "-o", "/dev/null", "-F", f"file=@-;filename={attrs.MODULE}.yang;type=application/yang",
            "http://localhost:8080/topology-inventory/v1/user-defined-schemas", stdin=attrs.YANG.read_text())
    expect: dict[tuple[str, str], int] = {}
    for m in mois:
        cls = model.CLASSES[m.cls]
        expect[(cls.domain, cls.entity_type)] = expect.get((cls.domain, cls.entity_type), 0) + 1
    before = {k: count(*k) for k in expect}
    lines = "\n".join(ingest.cloud_event_line(e) for e in events) + "\n"
    t0 = time.perf_counter()
    compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-console-producer.sh", "--bootstrap-server", "localhost:9092", "--topic", TOPIC,
            "--property", "parse.headers=true", "--property", "headers.key.separator=:::", "--property", "headers.delimiter=,,,",
            "--property", "parse.key=false", stdin=lines, timeout=timeout)
    seen = dict.fromkeys(expect, 0)
    while time.perf_counter() - t0 < timeout:
        seen = {k: count(*k) - before[k] for k in expect}
        if all(seen[k] >= n for k, n in expect.items()):
            break
        time.sleep(2)
    else:
        return {"cells": cells, "mois": len(mois), "entities": sum(expect.values()), "events": len(events), "seconds": None,
                "error": f"not every entity visible after {timeout:.0f} s", "seen": sum(seen.values())}
    visible = round(time.perf_counter() - t0, 1)
    script = ('while IFS= read -r body; do curl -s -o /dev/null -w "%{http_code}\\n" -H "Content-Type: application/json" -d "$body" '
              '"http://localhost:8080/topology-inventory/v1/manage-decorators"; done')
    codes = compose("exec", "-T", "teiv-exposure", "sh", "-c", script, stdin="\n".join(json.dumps(c) for c in decorator_calls) + "\n",
                    timeout=timeout).split()
    decorated = time.perf_counter()
    # after a bulk load, the maintenance an operator runs (teiv/README.md, "Operating"): the trigram indexes smo-teiv reads a subtree through are
    # rebuilt (an insert-then-decorate load leaves them several times their size, and their reads slower) and the planner's statistics refreshed
    compose("exec", "-T", "teiv-db", "sh", "-c", 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "REINDEX SCHEMA teiv_data" -c "ANALYZE"',
            timeout=timeout)
    relationships = sum(len(v) for e in events for block in e["relationships"] for v in block.values())
    return {"cells": cells, "mois": len(mois), "entities": sum(expect.values()), "relationships": relationships, "events": len(events),
            "decoratorCalls": len(decorator_calls), "decoratorFailures": sum(1 for c in codes if not c.startswith("2")), "seconds": visible,
            "decoratorSeconds": round(decorated - t0 - visible, 1), "maintenanceSeconds": round(time.perf_counter() - decorated, 1)}


def read(seconds: float, cells: int) -> int:
    """Reads smo-teiv's API for `seconds`: one cell, a cell's neighbours and the cells of a GNBDUFunction on cells spread over the tree, from one shell loop in teiv-exposure; how many reads."""
    import tree
    paths = []
    for i in range(0, cells, max(1, cells // 50)):
        gnb, c = i // tree.PER_GNB, i % tree.PER_GNB + 1
        du_cell = quote("urn:3gpp:dn:" + tree.du_cell(gnb, c), safe="")
        paths += [f"/domains/RAN/entity-types/NRCellDU/entities/{du_cell}",
                  "/domains/EXT_RAN/entity-types/NRCellCU/entities?limit=500&targetFilter=/attributes&scopeFilter="
                  + quote(f"/neighbor-of-nrCellCu[@id='urn:3gpp:dn:{tree.cu_cell(gnb, c)}']", safe=""),
                  "/domains/RAN/entity-types/NRCellDU/entities?scopeFilter="
                  + quote(f"/sourceIds[contains(@item,'urn:3gpp:dn:{tree.me_dn(gnb)},GNBDUFunction=1,')]", safe="")]
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
