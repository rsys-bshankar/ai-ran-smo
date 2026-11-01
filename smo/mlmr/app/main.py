"""MLMR's placeholder: a FastAPI app with only the health probes and no route, code or table of its own. The model registry (its routes, its seven tables and their rows) is AIMgF's `registry` package since S2.1
(`docs/plan/CONVERGED_REWRITE_PLAN.md`, ADR 0010).

Why it still runs: its compose service, Helm module, CI suite and coverage floor stay until plan task S2.8 removes them, which follows S2.4 (the last
callers of `/mlmr/...` moved to `/aimgf/...`). Until then R1 Termination sends `/mlmr/...` to AIMgF's process (`MLMR_URL` / `MLLF_URL`), so nothing calls
this container; it answers `/live`, `/ready` and `/health` so the stack starts as before.

Before editing: add nothing here. The routes this module served are listed, with where they are served now, in `aimgf/README.md`.
"""

from fastapi import FastAPI

from smo_shared.health import database_check, install_health
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics

app = FastAPI(title="MLMR")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
# /live, /ready and the /health alias (PR-ST-7). /ready checks the database: compose and the chart still give this service the role smo_mlmr until S2.8, which owns
# no table since revision 0064, and CI's role check expects a module with a role to connect as it (and is refused every table it was not given).
install_health(app, checks=[database_check])
