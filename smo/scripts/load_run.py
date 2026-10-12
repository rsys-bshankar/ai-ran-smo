#!/usr/bin/env python3
"""Load runner for the SMO stack (PR-V-8): a weighted mix of the main routes through the R1 gateway, at a fixed concurrency for a fixed time.

    scripts/load_run.py [--gateway http://r1-termination:8000] [--sme http://sme:8000] [--duration 30] [--concurrency 20] [--warmup 5] [--out load-results]
                        [--rate 20] [--stop-file /tmp/stop]
                        [--sme-metrics http://sme:8000/metrics] [--gateway-metrics http://r1-termination:8000/metrics]

Runs on the compose network (the workflow runs it in a one-shot container, so the load does not share a container's CPU with the stack): it registers one invoker with the
enrollment secret (`/run/secrets/enrollment_secret`, an SMO module), takes a token, and each worker then picks a route by weight and calls it as fast as the answer comes
back (a closed loop: the concurrency is the number of calls in flight). Per route it reports requests, requests/s, p50 / p95 / p99 / max latency in ms and the error count
(status 5xx, or not the one expected); overall too. Writes `<out>/load-results.json` and `<out>/load-results.md` (a table for the job summary and docs/PERFORMANCE.md).

`--rate N` paces the callers to about N calls a second in all (default 0: as fast as the answers come back); `--stop-file PATH` ends the run, after the call in flight, as soon as that file exists (the
upgrade lane starts the load before `helm upgrade` and stops it afterwards: PR-V-10). The result also says WHEN the errors happened: the calls and errors in each 10 s since the end of the warm-up.

It also counts the token checks the gateway made of SME during the measured part (PR-SEC-5.4): the difference of `smo_http_requests_total{route="/oauth2/introspect"}` at SME
and of `smo_introspection_cache_total` at the gateway, read before and after the measured part, as `introspection` in the results and in the table. With
`R1_INTROSPECTION_CACHE_SECONDS` unset or 0 every call through the gateway costs one introspection; with it above 0 the cache answers most of them (scripts/introspection_compare.py
sets a run with the cache against a run without). A metrics page that cannot be read leaves that part out; it never fails the load.

The token is renewed every `--token-refresh` seconds (default 30 minutes), because SME's last an hour and a soak runs for longer.

Exit status: 1 if any route answered a 5xx or the unexpected status more than `--max-error-rate` of the time (default 0), else 0. The numbers are the stack's on that runner:
GitHub's runners vary, so they are compared run against run on the same kind of runner, not against an absolute.
"""

import argparse
import asyncio
import datetime
import json
import os
import random
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# (name, method, path, weight, expected statuses). All through the gateway, as an SMO module (the internal scope), the way the modules and the GUI backend call it.
ROUTES = [
    ("service discovery", "GET", "/sme/service-apis/v1/allServiceAPIs?api_invoker_id={invoker}", 10, (200,)),
    ("alarm list", "GET", "/ran-nf-oam/alarms?limit=50", 15, (200,)),
    ("kpi definitions", "GET", "/ran-nf-oam/kpi-definitions", 10, (200,)),
    ("config job list", "GET", "/ran-nf-oam/config-jobs?limit=20", 10, (200,)),
    ("rApp instances", "GET", "/rapp-mgmt/instances", 10, (200,)),
    ("package list", "GET", "/onboarding/packages", 10, (200,)),
    ("data types", "GET", "/dme/dme-types", 10, (200,)),
    ("config job, dry run", "POST", "/ran-nf-oam/config-jobs", 10, (200,)),
    ("token", "TOKEN", "/sme/oauth2/token", 5, (200,)),
]
DRY_RUN = {"requestedBy": "load", "scope": "cell", "dryRun": True, "changes": []}


@dataclass
class Series:
    """What a run recorded for one route: every latency in ms, the number of errors and a count per status code (0 is a transport error)."""
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    statuses: dict[int, int] = field(default_factory=dict)


def percentile(values: list[float], q: float) -> float:
    """The q-quantile (0..1) of `values` taken at the sorted list index `round(q * (n - 1))`, with no interpolation; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


async def register(client: httpx.AsyncClient, sme: str) -> dict:
    """Registers one API invoker with SME using the enrollment secret (`SMO_ENROLLMENT_SECRET_FILE`, default `/run/secrets/enrollment_secret`) and returns SME's answer.

        Raises `httpx.HTTPStatusError` on a non-2xx answer. The invoker is not removed afterwards.
    """
    secret = Path(os.environ.get("SMO_ENROLLMENT_SECRET_FILE", "/run/secrets/enrollment_secret")).read_text(encoding="utf-8").strip()
    resp = await client.post(f"{sme}/invoker-registrations", json={"apiInvokerPublicKey": "load"}, headers={"X-SMO-Enrollment": secret})
    resp.raise_for_status()
    return resp.json()


async def get_token(client: httpx.AsyncClient, sme: str, reg: dict) -> httpx.Response:
    return await client.post(f"{sme}/oauth2/token", json={"grant_type": "client_credentials", "client_id": reg["apiInvokerId"],
                                                          "client_secret": reg["onboardingSecret"], "scope": "smo-internal"})


class Bearer:
    """The access token the workers send, in a holder so a refresh reaches them: SME's tokens last an hour (`sme/app/main.py` ACCESS_TOKEN_TTL_SECONDS) and a soak runs longer."""

    def __init__(self, value: str):
        self.value = value


class Outcome(int):
    """A status code that also carries the start of the body of a refusal, for the list of failed calls."""
    detail = ""


def outcome(resp: httpx.Response) -> Outcome:
    """The status code of a response as an `Outcome`, with the first 200 characters of the body attached when the status is 400 or above."""
    result = Outcome(resp.status_code)
    if resp.status_code >= 400:
        result.detail = resp.text[:200]
    return result


async def one_call(client: httpx.AsyncClient, gateway: str, sme: str, reg: dict, access: str, route) -> int:
    """Makes one call of `route` and returns its status as an `Outcome`.

        A `TOKEN` route asks SME for a new token; a `POST` is the dry-run config job with a fresh `Idempotency-Key`; anything else is a GET through the gateway with the
        bearer token. `{invoker}` in the path is replaced by the registered invoker id. Transport errors propagate (the worker turns them into status 0).
    """
    name, method, path, _, _ = route
    if method == "TOKEN":
        return outcome(await get_token(client, sme, reg))
    headers = {"Authorization": f"Bearer {getattr(access, 'value', access)}"}      # a Bearer (refreshed during a long run) or a plain token string
    path = path.replace("{invoker}", reg["apiInvokerId"])
    if method == "POST":
        headers["Idempotency-Key"] = str(uuid.uuid4())
        return outcome(await client.post(f"{gateway}{path}", json=DRY_RUN, headers=headers))
    return outcome(await client.get(f"{gateway}{path}", headers=headers))


# one `name="value"` pair of a Prometheus sample's label set; a backslash escape inside the value is allowed
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


NOT_PROXIED = ("/metrics", "/live", "/ready", "/health", "/bootstrap")      # the gateway's own routes: a call to one of them is not a token check


def metric_sum(text: str, name: str, not_routes: tuple[str, ...] = (), **labels: str) -> float:
    """The sum of the samples of the Prometheus counter `name` whose labels include all of `labels` and whose `route` is none of `not_routes` (0.0 when there are none)."""
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(name):
            continue
        head, _, value = line.rpartition(" ")
        base, _, label_text = head.partition("{")
        if base != name:
            continue
        found = dict(LABEL.findall(label_text))
        if found.get("route") in not_routes:
            continue
        if all(found.get(key) == wanted for key, wanted in labels.items()):
            total += float(value)
    return total


async def scrape(client: httpx.AsyncClient, sme_url: str | None, gateway_url: str | None) -> dict | None:
    """The counters the introspection comparison needs, or None when a page cannot be read (the load goes on without that part)."""
    try:
        sme = (await client.get(sme_url)).text if sme_url else ""
        gateway = (await client.get(gateway_url)).text if gateway_url else ""
    except httpx.HTTPError:
        return None
    return {"sme_introspect": metric_sum(sme, "smo_http_requests_total", route="/oauth2/introspect"),
            "gateway_requests": metric_sum(gateway, "smo_http_requests_total", not_routes=NOT_PROXIED),
            "cache_hit": metric_sum(gateway, "smo_introspection_cache_total", result="hit"),
            "cache_miss": metric_sum(gateway, "smo_introspection_cache_total", result="miss")}


def introspection_summary(before: dict | None, after: dict | None, gateway_calls: int) -> dict | None:
    """What the gateway asked SME while calls went through it: the counters' differences and the share of calls that cost an introspection.
    The calls are counted by the gateway itself over the same two readings (`gateway_requests`), so the callers still in flight at either reading do not skew the ratio;
    `gateway_calls` (the ones this runner saw finish) is the fallback when the gateway's page has no such series."""
    if not before or not after:
        return None
    asked = after["sme_introspect"] - before["sme_introspect"]
    counted = int(after["gateway_requests"] - before["gateway_requests"])
    gateway_calls = counted if counted > 0 else gateway_calls
    hits, misses = after["cache_hit"] - before["cache_hit"], after["cache_miss"] - before["cache_miss"]
    return {"sme_introspections": int(asked), "gateway_calls": gateway_calls, "introspections_per_100_calls": round(100 * asked / gateway_calls, 1) if gateway_calls else 0.0,
            "cache_hits": int(hits), "cache_misses": int(misses), "cache_hit_ratio": round(hits / (hits + misses), 3) if hits + misses else None}


BUCKET_SECONDS = 10
MAX_FAILURES = 400


def stopped(stop_file: str | None) -> bool:
    return bool(stop_file) and os.path.exists(stop_file)


async def worker(client, gateway, sme, reg, access, deadline: float, record_from: float, series: dict[str, Series], weights, rng: random.Random,
                 timeline: dict[int, list[int]] | None = None, interval: float = 0.0, stop_file: str | None = None, failures: list[dict] | None = None):
    """Calls routes until the deadline (or the stop file); `interval` is the least time one call takes when the load is paced; `timeline` gets [calls, errors] per BUCKET_SECONDS;
    `failures` gets one entry per failed call (when it started, in seconds after the warm-up and in UTC, the route, the status, the time it took, the start of the answer), up to MAX_FAILURES."""
    while time.monotonic() < deadline and not stopped(stop_file):
        route = rng.choices(ROUTES, weights=weights)[0]
        started = time.monotonic()
        detail = ""
        try:
            status = await one_call(client, gateway, sme, reg, access, route)
            detail = getattr(status, "detail", "")
        except httpx.HTTPError as exc:
            status = 0
            detail = f"{type(exc).__name__}: {exc}"[:200]
        elapsed = (time.monotonic() - started) * 1000
        if started >= record_from:                            # before that: warm-up (connections, caches, the pool)
            s = series[route[0]]
            s.latencies_ms.append(elapsed)
            s.statuses[status] = s.statuses.get(status, 0) + 1
            failed = status not in route[4]
            if failed:
                s.errors += 1
                if failures is not None and len(failures) < MAX_FAILURES:
                    failures.append({"t_s": round(started - record_from, 1), "at": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                                     "route": route[0], "status": int(status), "ms": round(elapsed), "detail": detail})
            if timeline is not None:
                cell = timeline.setdefault(int((started - record_from) // BUCKET_SECONDS), [0, 0])
                cell[0] += 1
                cell[1] += failed
        if interval and elapsed / 1000 < interval:
            await asyncio.sleep(interval - elapsed / 1000)


def timeline_rows(timeline: dict[int, list[int]]) -> list[dict]:
    return [{"from_s": bucket * BUCKET_SECONDS, "to_s": (bucket + 1) * BUCKET_SECONDS, "calls": calls, "errors": errors}
            for bucket, (calls, errors) in sorted(timeline.items())]


def summarise(series: dict[str, Series], seconds: float) -> dict:
    """Per-route and overall request count, requests a second (over `seconds`), p50/p95/p99/max latency in ms, error count and status counts, plus the mean latency."""
    rows, all_latencies, all_errors = [], [], 0
    for route in ROUTES:
        s = series[route[0]]
        n = len(s.latencies_ms)
        rows.append({"route": route[0], "requests": n, "rps": round(n / seconds, 1), "p50": round(percentile(s.latencies_ms, 0.50), 1),
                     "p95": round(percentile(s.latencies_ms, 0.95), 1), "p99": round(percentile(s.latencies_ms, 0.99), 1),
                     "max": round(max(s.latencies_ms), 1) if s.latencies_ms else 0.0, "errors": s.errors, "statuses": s.statuses})
        all_latencies += s.latencies_ms
        all_errors += s.errors
    total = {"route": "all", "requests": len(all_latencies), "rps": round(len(all_latencies) / seconds, 1), "p50": round(percentile(all_latencies, 0.50), 1),
             "p95": round(percentile(all_latencies, 0.95), 1), "p99": round(percentile(all_latencies, 0.99), 1),
             "max": round(max(all_latencies), 1) if all_latencies else 0.0, "errors": all_errors, "statuses": {}}
    return {"routes": rows, "total": total, "mean_ms": round(statistics.fmean(all_latencies), 1) if all_latencies else 0.0}


def markdown(result: dict, args) -> str:
    """The report as markdown: the header line, the per-route table, the token-check line when the introspection counters were read, and the error timeline (or "No errors")."""
    paced = f", paced to about {args.rate:g} calls a second" if getattr(args, "rate", 0) else ""
    measured = result.get("seconds", args.duration)
    lines = [f"Load: {args.concurrency} callers in flight for {measured:g} s after a {args.warmup:g} s warm-up{paced}, through the gateway as an SMO module.", "",
             "| route | requests | req/s | p50 ms | p95 ms | p99 ms | max ms | errors |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in result["routes"] + [result["total"]]:
        lines.append(f"| {row['route']} | {row['requests']} | {row['rps']} | {row['p50']} | {row['p95']} | {row['p99']} | {row['max']} | {row['errors']} |")
    intro = result.get("introspection")
    if intro:
        ratio = f", the cache answered {intro['cache_hit_ratio']:.1%} of the token checks" if intro["cache_hit_ratio"] is not None else ", the cache was off"
        lines += ["", f"Token checks: SME answered {intro['sme_introspections']} introspections for {intro['gateway_calls']} calls through the gateway "
                      f"({intro['introspections_per_100_calls']} per 100 calls{ratio})."]
    bad = [row for row in result.get("timeline", []) if row["errors"]]
    lines += ["", ("Errors by time since the end of the warm-up: " + "; ".join(f"{b['from_s']}-{b['to_s']} s: {b['errors']} of {b['calls']}" for b in bad)) if bad
              else "No errors in any 10 s of the run."]
    return "\n".join(lines) + "\n"


async def main_async(args) -> int:
    """Runs the load and writes `load-results.json` and `load-results.md` into `--out`. Returns 1 when the error share of all measured calls is over `--max-error-rate`, else 0.

        Registers an invoker and takes a token (a failure there raises), starts `--concurrency` workers, and reads the metrics counters once at the end of the warm-up and once
        after the last worker; the measured time is `min(--duration, actual)` so a run ended by `--stop-file` is not credited with the full duration.
    """
    limits = httpx.Limits(max_connections=args.concurrency + 5, max_keepalive_connections=args.concurrency + 5)
    async with httpx.AsyncClient(limits=limits, timeout=args.timeout) as client:
        reg = await register(client, args.sme)
        token = await get_token(client, args.sme, reg)
        token.raise_for_status()
        access = Bearer(token.json()["access_token"])
        series = {route[0]: Series() for route in ROUTES}
        weights = [route[3] for route in ROUTES]
        timeline: dict[int, list[int]] = {}
        failures: list[dict] = []
        interval = args.concurrency / args.rate if args.rate else 0.0
        start = time.monotonic()
        deadline = start + args.warmup + args.duration
        rng = random.Random(args.seed)  # noqa: S311 — picks a route for the load, not a secret
        counters: dict[str, dict | None] = {}

        async def counters_at_start_of_measurement():
            await asyncio.sleep(args.warmup)
            counters["before"] = await scrape(client, args.sme_metrics, args.gateway_metrics)

        async def refresh_token():
            """Takes a new token every `--token-refresh` seconds for the whole run: the first one expires after an hour, and a longer run would otherwise end in 401s on every route
            (the first scheduled soak, 120 minutes). A refresh that fails keeps the old token and tries again at the next interval."""
            while True:
                await asyncio.sleep(args.token_refresh)
                try:
                    fresh = await get_token(client, args.sme, reg)
                    fresh.raise_for_status()
                    access.value = fresh.json()["access_token"]
                except (httpx.HTTPError, KeyError, ValueError):
                    pass

        sampler = asyncio.create_task(counters_at_start_of_measurement())
        refresher = asyncio.create_task(refresh_token()) if args.token_refresh > 0 else None
        await asyncio.gather(*[worker(client, args.gateway, args.sme, reg, access, deadline, start + args.warmup, series, weights, random.Random(rng.random()),  # noqa: S311
                                      timeline, interval, args.stop_file, failures) for _ in range(args.concurrency)])
        ended = time.monotonic()
        if refresher:
            refresher.cancel()
        await sampler
        counters["after"] = await scrape(client, args.sme_metrics, args.gateway_metrics)
    seconds = max(1.0, min(args.duration, ended - start - args.warmup))     # a run ended by the stop file measured less than --duration
    result = summarise(series, seconds)
    result["timeline"] = timeline_rows(timeline)
    result["failures"] = sorted(failures, key=lambda f: f["t_s"])
    result["seconds"] = round(seconds, 1)
    through_gateway = sum(len(series[route[0]].latencies_ms) for route in ROUTES if route[1] != "TOKEN")
    result["introspection"] = introspection_summary(counters.get("before"), counters.get("after"), through_gateway)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "load-results.json").write_text(json.dumps({"concurrency": args.concurrency, "duration": seconds, **result}, indent=1))
    table = markdown(result, args)
    (out / "load-results.md").write_text(table)
    print(table)
    rate = result["total"]["errors"] / max(1, result["total"]["requests"])
    if rate > args.max_error_rate:
        print(f"FAIL: {result['total']['errors']} of {result['total']['requests']} calls were errors ({rate:.2%} > {args.max_error_rate:.2%}); statuses per route are in load-results.json", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    """Command-line entry; `--sme-metrics` and `--gateway-metrics` default to `<url>/metrics`. Returns `main_async`'s exit code."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="http://r1-termination:8000")
    ap.add_argument("--sme", default="http://sme:8000")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--warmup", type=float, default=5)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--max-error-rate", type=float, default=0.0)
    ap.add_argument("--token-refresh", type=float, default=1800, help="take a new token this often, in seconds (0: never); SME's tokens last an hour, so a run over an hour needs it")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rate", type=float, default=0.0, help="about this many calls a second in all (0: as fast as the answers come back)")
    ap.add_argument("--stop-file", default=None, help="end the run when this file exists")
    ap.add_argument("--sme-metrics", default=None, help="SME's metrics page (default: <--sme>/metrics)")
    ap.add_argument("--gateway-metrics", default=None, help="the gateway's metrics page (default: <--gateway>/metrics)")
    ap.add_argument("--out", default=os.environ.get("LOAD_OUT", "load-results"))
    args = ap.parse_args()
    args.sme_metrics = args.sme_metrics or f"{args.sme}/metrics"
    args.gateway_metrics = args.gateway_metrics or f"{args.gateway}/metrics"
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
