"""MLLF's placeholder: a FastAPI app with only the health probes and no route, code or table of its own. The deploy gate is AIMgF's `loading/gate.py` since S2.2, an in-process check instead of an R1 round trip
(`docs/plan/CONVERGED_REWRITE_PLAN.md`, ADR 0010).

Why it still runs: its compose service, Helm module, CI suite and coverage floor stay until plan task S2.8 removes them, which follows S2.4 (the last
callers of `/mllf/...` moved to `/aimgf/...`). Until then R1 Termination sends `/mllf/...` to AIMgF's process (`MLMR_URL` / `MLLF_URL`), so nothing calls
this container; it answers `/live`, `/ready` and `/health` so the stack starts as before.

Before editing: add nothing here. The routes this module served are listed, with where they are served now, in `aimgf/README.md`.
"""

from fastapi import FastAPI

from smo_shared.health import install_health
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics

app = FastAPI(title="MLLF")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_health(app)  # /live, /ready and the /health alias (PR-ST-7); no check: the placeholder depends on nothing
