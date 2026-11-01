"""AIMgF (AI Management Function): the one AI/ML management service of the SMO, served at `/aimgf` behind R1 Termination. This file builds the FastAPI app
and includes the routers of its packages; it holds no route of its own.

The code is organised by TS 28.105 capability (S2.3 of `docs/plan/CONVERGED_REWRITE_PLAN.md`, ADR 0010 section 3.1); `aimgf/README.md` has the code map:

  registry/     MLModel, MLModelRepository, MLModelCoordinationGroup, artifacts, TS 29.482 storages and profiles (was the MLMR service, S2.1)
  training/     MLTrainingFunction / Request / Process / Report, the training runs, feature groups
  testing/      MLTestingFunction / Request / Report, the validation runs
  emulation/    AIMLInferenceEmulationFunction, the emulation runs
  loading/      MLModelLoadingPolicy / Request / Process, the serving runtime and the deploy gate (was the MLLF service, S2.2)
  inference/    AIMLInferenceFunction, AIMLInferenceReport, the inference jobs and performance monitoring (MLMF)
  update/       MLUpdateFunction / Request / Process / Report
  governance/   the 14-state model lifecycle, the runtime lifecycle and the operator's decisions
  provmns/      the twenty TS 28.105 classes and MLModelGovernance over TS 28.532 ProvMnS, from `aimgf.aimgf_moi_store` (S2.7, S2.10)
  common.py, execution.py, models.py, ts28105.py: what the packages share (model lookup and the owner rule, the NFO execution runtimes, the tables, the datatypes)

There is no `mltf`, `mtef`, `mlvf`, `mlef` or `mlif` package: training, validation, emulation and inference run as NFO workloads AIMgF asks for
(`execution.py`, golden rules 3 and 4 of `docs/ARCHITECTURE.md`), and MTEF is a retired name.

Where it sits: rApps (through the SDK), the GUI BFF, MDAF, SA SMOS and SO SMOS call it over R1 at `/aimgf/...`, the one prefix every route answers at (the
registry and the deploy gate too: S2.4 moved their callers and deleted MLMR's and MLLF's gateway prefixes). It calls NFO,
DME and Onboarding through `common._r1`, one `R1Client` for the module, and caller-supplied notification URLs only through the transactional outbox.

Before editing: (1) `import httpx` and `R1Client` stay imported here although this file does not call them: the tests patch `app.main.httpx.post` and
`app.main.R1Client.get/post/delete/patch` (both are module and class attributes, so the patch reaches every package; see `ruff.toml`). (2) The route
docstrings of every package are published in `docs/openapi/aimgf.json`; rerun `scripts/generate_openapi_specs.py` after changing one.
"""

# Kept for the tests that patch `app.main.httpx.post` to capture webhook sends (ruff.toml explains why this import is not an unused-import finding).
import httpx  # noqa: F401
from contextlib import asynccontextmanager

from fastapi import FastAPI

from smo_shared.correlation import apply_correlation_id
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.mns.mns_registry import install_self_registration
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.r1_client import R1Client  # noqa: F401  (the tests patch `app.main.R1Client`)
from smo_shared.versioning import install_concurrency_handler

from . import execution
from .emulation import jobs as emulation_jobs, nrm as emulation_nrm
from .governance import routes as governance_routes
from .provmns import service as provmns
from .inference import jobs as inference_jobs, monitoring as inference_monitoring, nrm as inference_nrm
from .loading import gate as loading_gate, nrm as loading_nrm, runtime as loading_runtime
from .registry import artifacts as registry_artifacts, mlr as registry_mlr, routes as registry_routes
from .testing import jobs as testing_jobs, nrm as testing_nrm
from .training import feature_groups as training_feature_groups, jobs as training_jobs, nrm as training_nrm
from .update import nrm as update_nrm



@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start-up: SMO_AIMGF_ARTIFACT_FORMATS is checked (a value it does not know, pickle among them, stops the start: registry/artifacts.py), then the
    first projection (provmns/service.py `start`); the MnS registry registration wraps this (install_self_registration)."""
    registry_artifacts.accepted_formats()
    provmns.start()
    yield


app = FastAPI(title="AIMgF", lifespan=_lifespan)
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
apply_r1_gateway_security(app)  # the bearer-token security scheme of the R1 contract on every route, and 4xx answers for integrity and out-of-range errors
apply_correlation_id(app)  # one correlation id per request, passed on by `common._r1` to every downstream call
install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)

# One router per file that serves routes, by capability. The paths do not overlap, so the order only decides the order of the published document.
for _module in (registry_routes, registry_mlr, training_jobs, training_nrm, training_feature_groups, testing_jobs, testing_nrm, emulation_jobs, emulation_nrm,
                loading_runtime, loading_gate, loading_nrm, inference_jobs, inference_nrm, inference_monitoring, update_nrm, governance_routes, execution):
    app.include_router(_module.router)

# TS 28.105 over ProvMnS (S2.7, S2.10): the router at /ProvMnS/{version}/..., the middleware that gives its hooks the caller, and the MnS registry
# registration below.
app.include_router(provmns.router)
app.add_middleware(provmns.RequestContext)
# In SME's MnS registry at start-up (PR-CONV-S1.12), as the producer of its classes under ManagedElement=AIMgF, so R1 routes them here (S1.8).
REGISTRATION = install_self_registration(app, "aimgf", provmns.CLASSES, scope=provmns.REGISTRATION_SCOPE, capabilities=provmns.CAPABILITIES)
