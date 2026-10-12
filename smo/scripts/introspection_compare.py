#!/usr/bin/env python3
"""Sets two load runs side by side: the gateway without its introspection cache and with it (PR-SEC-5.4, 5.5).

    scripts/introspection_compare.py --off load-out --on load-out/cache-30 [--seconds 30] [--out load-out/introspection-cache.md] [--min-saving 0.5]

Both directories hold what `scripts/load_run.py` writes (`load-results.json`). The table lists, for each run, the introspections SME answered, how many of them each call through
the gateway cost, the requests a second, the p50 / p95 / p99 of all calls and the errors; then what the cache saved. It FAILS (exit 1) when

  - either run has an error, a 5xx or an unexpected status on any route (a cache that breaks a route is not a saving);
  - the run without the cache did not cost about one introspection per call through the gateway (then the run was not what it says: the cache was on, or the calls skipped the check);
  - the run with the cache did not cut the introspections by at least `--min-saving` (default 0.5: half).

It never loosens anything: the numbers it reports are the measurement, and the choice of the default (`R1_INTROSPECTION_CACHE_SECONDS`: 30 in compose and the chart since SEC-5.5, 0 in the code) stays the owner's
(HISTORY.md, PR-SEC-5.5). That default is why the lane must set 0 itself for the run it passes as `--off` (smo-load.yml does). The latency columns are indicative: the second run follows the first on the same database, and a CI runner varies from run to run.
"""

import argparse
import json
import sys
from pathlib import Path


def load(directory: Path) -> dict:
    """The `load-results.json` that `scripts/load_run.py` wrote in `directory`."""
    return json.loads((directory / "load-results.json").read_text(encoding="utf-8"))


def verdict(off: dict, on: dict, min_saving: float) -> tuple[list[str], list[str]]:
    """(problems, findings): what makes the comparison unusable, and what it shows."""
    problems: list[str] = []
    for label, run in (("without the cache", off), ("with the cache", on)):
        if run["total"]["errors"]:
            problems.append(f"the run {label} had {run['total']['errors']} errors of {run['total']['requests']} calls")
        if not run.get("introspection"):
            problems.append(f"the run {label} has no introspection counters (SME's or the gateway's /metrics could not be read)")
    if problems:
        return problems, []
    a, b = off["introspection"], on["introspection"]
    if a["cache_hits"] + a["cache_misses"]:
        problems.append("the run without the cache counted cache lookups: the cache was on in it")
    if a["introspections_per_100_calls"] < 80:
        problems.append(f"the run without the cache cost only {a['introspections_per_100_calls']} introspections per 100 calls (expected about 100; callers still in flight at the first reading can move it by a few points)")
    if not (b["cache_hits"] + b["cache_misses"]):
        problems.append("the run with the cache counted no cache lookups: the cache was off in it (R1_INTROSPECTION_CACHE_SECONDS)")
    saving = 1 - (b["introspections_per_100_calls"] / a["introspections_per_100_calls"]) if a["introspections_per_100_calls"] else 0.0
    if saving < min_saving:
        problems.append(f"the cache cut the introspections by {saving:.0%}, under the {min_saving:.0%} asked for")
    return problems, [f"introspections per 100 calls: {a['introspections_per_100_calls']} without, {b['introspections_per_100_calls']} with the cache: {saving:.1%} fewer"]


def row(label: str, run: dict) -> str:
    """One markdown table row for a run; the cache column is "off" when the run counted no cache lookups, the hit ratio when it did, "?" when unknown."""
    i, t = run.get("introspection") or {}, run["total"]
    cache = "off" if i and not (i["cache_hits"] + i["cache_misses"]) else (f"{i['cache_hit_ratio']:.1%} hits" if i and i["cache_hit_ratio"] is not None else "?")
    return (f"| {label} | {i.get('sme_introspections', '?')} | {i.get('gateway_calls', '?')} | {i.get('introspections_per_100_calls', '?')} | {cache} | {t['rps']} | {t['p50']} | {t['p95']} | "
            f"{t['p99']} | {t['errors']} |")


def table(off: dict, on: dict, seconds: str, findings: list[str], problems: list[str]) -> str:
    """The markdown report: the two rows, the findings, the requests-a-second and p95 comparison (only when the run without the cache has both numbers), and each problem as a `PROBLEM:` line."""
    lines = ["| run | introspections at SME | calls through the gateway | per 100 calls | cache | req/s | p50 ms | p95 ms | p99 ms | errors |", "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|",
             row("cache off", off), row(f"cache {seconds} s", on), ""]
    lines += [f"- {f}" for f in findings]
    off_t, on_t = off["total"], on["total"]
    if off_t["rps"] and off_t["p95"]:
        lines.append(f"- requests a second: {off_t['rps']} without, {on_t['rps']} with; p95: {off_t['p95']} ms without, {on_t['p95']} ms with (indicative: the second run follows the first on a warm database)")
    lines += [f"- PROBLEM: {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Compares two load runs, prints the report, writes it to `--out` when given, and returns 1 when `verdict` found a problem, else 0."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--off", type=Path, required=True, help="the load_run.py output directory of the run without the cache")
    ap.add_argument("--on", type=Path, required=True, help="the output directory of the run with the cache")
    ap.add_argument("--seconds", default="on", help="the TTL the second run used, for the label")
    ap.add_argument("--min-saving", type=float, default=0.5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    off, on = load(args.off), load(args.on)
    problems, findings = verdict(off, on, args.min_saving)
    text = table(off, on, args.seconds, findings, problems)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
