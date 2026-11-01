"""OI-2-compose-e2e: smoke-test the running docker-compose stack.

Runs INSIDE the r1-termination container (the only one that publishes a host
port, and the only vantage point from which module hostnames resolve), fed on
stdin by the CI job:

    docker compose exec -T r1-termination python3 - < scripts/compose_e2e.py

It is the fast gate before the full replay: every service answers, R1
Termination's `/bootstrap` answers, and a token-gated call is rejected
without a token. The runbook itself (§2-§27: onboarding, deployment, every
module, the four reference rApps) is replayed against the same stack by
`tests_integration/test_demo_runbook.py` with `SMO_E2E_LIVE=1`
(see `tests_integration/live.py`), which needs the stack otherwise untouched,
so this script leaves no state behind.

Exit status 0 only if every check passes; every failure is listed.
"""
import sys
import time

import httpx

# Services with their own GET /health (the three without one — gui-bff and
# the two mocks — are probed for "answers HTTP at all").
HEALTH = [
    "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam", "nfo",
    "focom", "aimgf", "ran-analytics", "mdaf", "intent-service",
    "so-smos", "sa-smos", "energy-saving-rapp", "mobility-optimization-rapp",
    "coverage-optimization-rapp", "traffic-steering-rapp",
]
ANSWERS = ["mock-o1-adaptor", "gui-bff"]
CSAR_URL = "http://r1-termination:8899/energy-saving-rapp.csar"

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    """Prints one `ok` / `FAIL` line and remembers a failure; the script goes on and lists every failure at the end."""
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" -- {detail}"))
    if not ok:
        failures.append(name)


def wait_for(url: str, want_200: bool, timeout: float = 180.0) -> httpx.Response | None:
    """Polls `url` every 2 s for up to `timeout` seconds and returns the first response (any status when `want_200` is false, only a 200 otherwise).

        Returns None, after printing the last status or error, when none came in time. A service that is still starting is the normal reason for the wait.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=3.0)
            if r.status_code == 200 or not want_200:
                return r
            last = r.status_code
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(2)
    print(f"  (gave up waiting on {url}: {last})")
    return None


# --- 1. every service is up -------------------------------------------------
for host in HEALTH:
    check(f"{host} /health", wait_for(f"http://{host}:8000/health", True) is not None)
for host in ANSWERS:
    check(f"{host} answers HTTP", wait_for(f"http://{host}:8000/", False) is not None)
check("r1-termination /health", wait_for("http://localhost:8000/health", True) is not None)

# --- 2. DEMO_RUNBOOK §4: bootstrap + an R1-routed, token-gated call ---------
r = httpx.get("http://r1-termination:8000/bootstrap", timeout=10)
check("R1 /bootstrap -> 200", r.status_code == 200, r.status_code)
if r.status_code == 200:
    check("bootstrap names service-apis", "service-apis" in r.text, r.text[:200])
r = httpx.get("http://r1-termination:8000/sme/health", timeout=10)
check("R1 rejects an unauthenticated routed call", r.status_code in (401, 403), r.status_code)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all compose e2e checks passed")
