#!/usr/bin/env python3
"""The latency of TE&IV's reads at 1,000 cells, in TS 28.532 form (plan tasks S3.19, S3.20; ADR 0013 sections 5 and 11.4's Q1 to Q5): p50, p95 and p99 of
each query through a ProvMnS root, TE&IV's own or R1 Termination's (by class name, S1.8), with the neighbour answers checked against the tree.

    python teiv/loadtest/queries.py --root http://teiv:8000/ProvMnS/19.2.0 --out /tmp/q.json --markdown /tmp/q.md
    python teiv/loadtest/queries.py --root https://r1:8443/ProvMnS/19.2.0 --token-file /run/token ...      # through R1

The queries, on random cells of `tree.py` (the tree loaded by budget.py), target p95 at most 150 ms each:

  Q1  one cell                       getMOIAttributes of an NRCellDU (BASE_ONLY)
  Q2  a cell's neighbours            its NRCellRelation objects' adjacentNRCellRef: the NRCellCU, scope BASE_NTH_LEVEL 1, filter its class, attributes adjacentNRCellRef
  Q3  the cells of a GNBDUFunction   the GNBDUFunction, scope BASE_NTH_LEVEL 1, filter NRCellDU
  Q4  two hops                       Q2, then Q2 of each adjacent cell, all at once (the fan-out of ADR 0013 section 11.5 b6): the time to the last answer
  Q5  a SubNetwork's cells by value  the SubNetwork, scope BASE_ALL, filter NRCellDU and nRTAC equal to a value (smo-teiv filters on it)

Every Q2 and Q4 answer is compared with the neighbours the tree holds, and Q1, Q3 and Q5 answers with what the tree must give; a wrong one counts as a failure, and any failure, or a p95 over the target,
exits 1. Standard library and httpx.
"""

import argparse
import json
import os
import platform
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx

SMO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SMO / "teiv"), str(SMO / "teiv" / "loadtest"), str(SMO / "shared")]      # tree.py imports TE&IV's app.ingest, as budget.py does
os.environ.setdefault("SMO_DATABASE_URL", "sqlite://")      # app.* imports smo_shared, which wants one; nothing here touches a database
import tree  # noqa: E402

TARGET_MS = 150.0
FLAT = {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
NTH1 = json.dumps({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1})


def path(dn: str) -> str:
    """The URI path of a DN below the ProvMnS root (each RDN a segment, its value encoded)."""
    return "/".join(f"{rdn.split('=', 1)[0]}={quote(rdn.split('=', 1)[1], safe='')}" for rdn in dn.split(","))


def neighbours_in_tree(cells: int) -> dict[str, set[str]]:
    """NRCellCU DN -> the DNs its NRCellRelations name as adjacent, from the tree."""
    out: dict[str, set[str]] = {}
    for m in tree.synthetic(cells):
        if m.cls == "NRCellRelation":
            out.setdefault(m.dn.rsplit(",", 1)[0], set()).add(m.attributes["adjacentNRCellRef"])
    return out


def pct(values: list[float], p: float) -> float:
    """The p-th percentile (nearest rank)."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))]


class Runner:
    """Q1 to Q5 against one ProvMnS root."""

    def __init__(self, root: str, cells: int, token: str | None = None, verify: str | bool = True):
        """A client of `root` (with `token` as Bearer, `verify` the CA bundle), the tree's truth for `cells` cells, a seeded choice of cells."""
        headers = {**FLAT, **({"Authorization": f"Bearer {token}"} if token else {})}
        self.http = httpx.Client(base_url=root.rstrip("/"), headers=headers, timeout=10, verify=verify,
                                 limits=httpx.Limits(max_connections=32, max_keepalive_connections=32))
        self.cells = cells
        self.gnbs = (cells + tree.PER_GNB - 1) // tree.PER_GNB
        self.truth = neighbours_in_tree(cells)
        self.by_tac: dict[int, int] = {}
        for m in tree.synthetic(cells):
            if m.cls == "NRCellDU":
                self.by_tac[m.attributes["nRTAC"]] = self.by_tac.get(m.attributes["nRTAC"], 0) + 1
        self.rnd = random.Random(11)  # noqa: S311 (test data)
        self.pool = ThreadPoolExecutor(max_workers=16)
        self.wrong = 0

    def _get(self, dn: str, **params) -> list[dict]:
        r = self.http.get("/" + path(dn), params=params)
        r.raise_for_status()
        return r.json()

    def _cell(self) -> tuple[int, int]:
        n = self.rnd.randrange(self.cells)
        return n // tree.PER_GNB, n % tree.PER_GNB + 1

    def _expect(self, ok: bool) -> None:
        if not ok:
            self.wrong += 1

    def relations(self, cu: str) -> set[str]:
        """Q2's answer: the adjacent cells of `cu`'s relations."""
        return {r["attributes"].get("adjacentNRCellRef") for r in self._get(cu, scope=NTH1, filter="@.objectClass=='NRCellRelation'",
                                                                       attributes="adjacentNRCellRef")}

    def q1(self) -> None:
        """Q1: one random NRCellDU, BASE_ONLY."""
        gnb, c = self._cell()
        self._expect(len(self._get(tree.du_cell(gnb, c))) == 1)

    def q2(self) -> None:
        """Q2: the neighbours of one random NRCellCU, checked against the tree."""
        cu = tree.cu_cell(*self._cell())
        if self.relations(cu) != self.truth[cu]:
            self.wrong += 1

    def q3(self) -> None:
        """Q3: the NRCellDU of one random GNBDUFunction, counted."""
        gnb = self.rnd.randrange(self.gnbs)
        self._expect(len(self._get(f"{tree.me_dn(gnb)},GNBDUFunction=1", scope=NTH1, filter="@.objectClass=='NRCellDU'")) ==
                     min(tree.PER_GNB, self.cells - gnb * tree.PER_GNB))

    def q4(self) -> None:
        """Q4: Q2 of a random cell, then Q2 of each neighbour at once; the union checked against the tree."""
        cu = tree.cu_cell(*self._cell())
        first = self.relations(cu)
        second = set().union(*self.pool.map(self.relations, sorted(first)))
        expected = set().union(*(self.truth[n] for n in self.truth[cu])) | self.truth[cu]
        if first | second != expected:
            self.wrong += 1

    def q5(self) -> None:
        """Q5: the SubNetwork's NRCellDU with one random nRTAC, checked against the tree's count."""
        tac = 100 + self.rnd.randrange(50)
        found = self._get(tree.SUBNETWORK, scope=json.dumps({"scopeType": "BASE_ALL"}),
                         filter=f"@.objectClass=='NRCellDU' && @.attributes.nRTAC=={tac}")
        self._expect(len(found) == self.by_tac.get(tac, 0) and all(r["attributes"]["nRTAC"] == tac for r in found))

    def measure(self, runs: int, warmup: int = 5) -> dict:
        """Each query `runs` times after `warmup`, one after another; {name: {n, p50, p95, p99, pass}}."""
        out = {}
        for name in ("q1", "q2", "q3", "q4", "q5"):
            fn = getattr(self, name)
            for _ in range(warmup):
                fn()
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                fn()
                times.append((time.perf_counter() - t0) * 1000)
            p95 = pct(times, 95)
            out[name.upper()] = {"n": runs, "p50": round(statistics.median(times), 1), "p95": round(p95, 1), "p99": round(pct(times, 99), 1),
                                 "pass": p95 <= TARGET_MS}
        return out


LABELS = {"Q1": "one cell", "Q2": "a cell's neighbours (NRCellRelation, adjacentNRCellRef)", "Q3": "the cells of a GNBDUFunction",
          "Q4": "two hops (Q2, then Q2 of each neighbour, at once)", "Q5": "a SubNetwork's cells with nRTAC equal to a value"}


def markdown(result: dict) -> str:
    """The table the job summary shows."""
    rows = "\n".join(f"| {q} {LABELS[q]} | {r['p50']} | {r['p95']} | {r['p99']} | {'pass' if r['pass'] else 'FAIL'} |" for q, r in result["queries"].items())
    return (f"### TE&IV reads at {result['cells']} cells, through {result['through']}\n\n{result['machine']}; {result['runs']} runs each; "
            f"answers wrong: {result['wrong']}\n\n| Query | p50 ms | p95 ms | p99 ms | p95 ≤ {TARGET_MS:.0f} ms |\n|---|---:|---:|---:|---|\n{rows}\n")


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring; 0 when every query passes and every neighbour answer is right."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", required=True, help="a ProvMnS root, http(s)://host:port/ProvMnS/19.2.0")
    p.add_argument("--through", default="TE&IV", help="what the root is, for the report (TE&IV, R1 Termination)")
    p.add_argument("--token-file", help="a file holding an access token R1 Termination accepts")
    p.add_argument("--ca", help="the CA bundle the root's certificate is checked against")
    p.add_argument("--cells", type=int, default=1000)
    p.add_argument("--runs", type=int, default=100)
    p.add_argument("--out", required=True)
    p.add_argument("--markdown")
    a = p.parse_args(argv)
    token = Path(a.token_file).read_text().strip() if a.token_file else None
    runner = Runner(a.root, a.cells, token, a.ca or True)
    queries = runner.measure(a.runs)
    result = {"cells": a.cells, "through": a.through, "runs": a.runs, "machine": f"{platform.platform()}, {__import__('os').cpu_count()} CPUs",
              "queries": queries, "wrong": runner.wrong}
    Path(a.out).write_text(json.dumps(result, indent=1) + "\n")
    if a.markdown:
        Path(a.markdown).write_text(markdown(result))
    print(markdown(result))
    return 0 if runner.wrong == 0 and all(q["pass"] for q in queries.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
