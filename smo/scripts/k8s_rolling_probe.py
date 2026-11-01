"""PR-HA-2: keep asking every replicated module for /health while the deployments are restarted one at a time.

Runs in a throwaway pod inside the cluster (the CI job `helm`), so it reaches each module through its Service, as another module does:

    kubectl -n smo run probe --image=local/smo-sme:ci --image-pull-policy=Never --restart=Never -- python3 -c "$(cat scripts/k8s_rolling_probe.py)"
    ... restart the deployments ...
    kubectl -n smo exec probe -- touch /tmp/stop

It stops when /tmp/stop appears, prints what it saw and exits 1 if a call failed twice in a row (one retry is allowed: a connection that
was already open to a pod being stopped may be reset, and a caller retries it). Not probed: onboarding and the GUI backend (they roll
with Recreate, so a restart is a gap by design) and the mock services.
"""
import os
import sys
import time

import httpx

HOSTS = [
    "sme", "dme", "rapp-mgmt", "ran-nf-oam", "nfo", "focom", "aimgf", "ran-analytics",
    "mdaf", "intent-service", "so-smos", "sa-smos", "energy-saving-rapp", "mobility-optimization-rapp",
    "coverage-optimization-rapp", "traffic-steering-rapp",
]
URLS = [f"http://{h}:8000/health" for h in HOSTS] + ["http://r1-termination:8000/bootstrap"]
DEADLINE = time.monotonic() + 1800  # a probe that is never stopped does not run for ever


def ok(client: httpx.Client, url: str) -> bool:
    """True when a GET of `url` answers 200; a connection or timeout error is False (not raised)."""
    try:
        return client.get(url).status_code == 200
    except httpx.HTTPError:
        return False


calls = retried = 0
failed: list[str] = []
with httpx.Client(timeout=3.0) as client:
    while not os.path.exists("/tmp/stop") and time.monotonic() < DEADLINE:
        for url in URLS:
            calls += 1
            if ok(client, url):
                continue
            retried += 1
            time.sleep(0.5)
            if not ok(client, url):
                failed.append(url)
        time.sleep(0.2)

print(f"{calls} calls, {retried} needed a retry, {len(failed)} failed twice")
for url in sorted(set(failed)):
    print(f"  FAILED {url} x{failed.count(url)}")
sys.exit(1 if failed or calls == 0 else 0)
