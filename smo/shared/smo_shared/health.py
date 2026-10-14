"""Liveness and readiness probes (PR-ST-7).

Two questions, answered separately because an orchestrator acts differently on each:

  /live    "is this process running?" Always 200 while the event loop answers. A failing
           liveness probe restarts the container, so it must never depend on another
           service: a database outage would otherwise restart every module in turn.
  /ready   "should this replica receive traffic?" 200 when every registered check passes,
           else 503 with the failing checks named. A failing readiness probe only takes the
           replica out of rotation; it is the right place to look at the database and SME.
  /health  the original liveness route, kept as an alias of /live: the GUI BFF's module
           grid, DME's producer health supervision and the demo runbook already call it.
  /version "which build is this?" `{module, version, buildSha, builtAt}` from the environment the
           image sets (`SMO_VERSION`, `SMO_BUILD_SHA`, `SMO_BUILT_AT`, Docker build arguments; each
           `unknown` when the image was built without them). Unauthenticated like the probes (PR-OBS-8.1).

    install_health(app, checks=[database_check, sme_token_check])

A check is a plain function that raises when the dependency is unusable; its name is the
function's `__name__`. Checks run in parallel and are bounded by `READY_CHECK_TIMEOUT_SECONDS`
(default 3): a hung dependency is reported as `timeout` instead of hanging the probe.
The probe body names the failing check and its error class, never a connection string.
"""

import concurrent.futures
import logging
import os
from collections.abc import Callable, Sequence

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .identity_secrets import check_configuration

log = logging.getLogger(__name__)

Check = Callable[[], None]


def _check_timeout() -> float:
    return float(os.environ.get("READY_CHECK_TIMEOUT_SECONDS", "3"))


def database_check() -> None:
    """The shared database answers `SELECT 1`."""
    from sqlalchemy import text
    from .db import engine  # resolved at call time, so importing this module opens nothing
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def sme_token_check() -> None:
    """This module can obtain an SMO access token from SME (the cached one counts)."""
    from .r1_client import R1_GATEWAY_URL, _module_token
    if _module_token(R1_GATEWAY_URL) is None:
        raise RuntimeError("no SMO access token from SME")


def run_checks(checks: Sequence[Check], timeout: float | None = None) -> dict[str, str]:
    """Runs every check in parallel; returns {name: "ok" | "<ErrorClass>" | "timeout"}."""
    if not checks:
        return {}
    limit = _check_timeout() if timeout is None else timeout
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="ready-check")
    futures = {check.__name__: pool.submit(check) for check in checks}
    results: dict[str, str] = {}
    try:
        done, _ = concurrent.futures.wait(futures.values(), timeout=limit)
        for name, future in futures.items():
            if future not in done:
                results[name] = "timeout"
                log.warning("readiness check %s did not answer within %.1f s", name, limit)
            elif future.exception() is not None:
                results[name] = type(future.exception()).__name__
                log.warning("readiness check %s failed: %r", name, future.exception())
            else:
                results[name] = "ok"
    finally:
        pool.shutdown(wait=False)  # a hung check must not hold the probe; its thread ends with the call
    return results


# Response model of `GET /version` (published in the OpenAPI documents, so no docstring). Every field is a string; `unknown` stands for a value the
# image was built without.
class VersionReport(BaseModel):
    module: str
    version: str
    buildSha: str
    builtAt: str


def version_report(default_module: str = "unknown") -> dict[str, str]:
    """The build this process runs, read from the environment at call time. `module` is the `MODULE` build
    argument (a sample rApp's `samples/` prefix dropped), else `default_module`."""
    module = os.environ.get("MODULE", "").removeprefix("samples/") or default_module
    return {"module": module,
            "version": os.environ.get("SMO_VERSION") or "unknown",
            "buildSha": os.environ.get("SMO_BUILD_SHA") or "unknown",
            "builtAt": os.environ.get("SMO_BUILT_AT") or "unknown"}


# Body model of the 503 answer of `GET /ready` (published in the OpenAPI documents, so no docstring): `status` is `ready` or `not-ready`, `checks`
# maps each check's function name to `ok`, an exception class name or `timeout`.
class ReadinessReport(BaseModel):
    status: str
    checks: dict[str, str]


def install_health(app: FastAPI, checks: Sequence[Check] = ()) -> None:
    """Adds `/live`, `/ready`, `/version` and the `/health` alias to `app`. Raises IdentityKeyError, so the process does not start, when the identity key
    variables are set wrongly (SEC-15.18: both `SMO_IDENTITY_KEY` and `_FILE`, an unreadable file, a key shorter than 32 characters)."""
    check_configuration()
    checks = tuple(checks)
    default_module = app.title.lower().replace(" ", "-")

    @app.get("/version", tags=["health"], response_model=VersionReport)
    def version():
        """Build identity: module, release version, git SHA and build time, as set in the image (PR-OBS-8.1)."""
        return version_report(default_module)

    @app.get("/live", tags=["health"])
    def live():
        """Liveness probe: the process is up. Depends on nothing else."""
        return {"status": "live"}

    @app.get("/health", tags=["health"])
    def health_check():
        """Liveness probe, kept as an alias of `/live`. The GUI BFF's `GET /modules/status` fans out to
        `/<module>/health` through R1 Termination for every module in parallel, and DME supervises
        producers on it.
        """
        return {"status": "healthy"}

    @app.get("/ready", tags=["health"], responses={503: {"model": ReadinessReport, "description": "A check failed: this replica should not receive traffic"}})
    def ready():
        """Readiness probe: 200 when the database and SME (where this module uses them) answer, else 503
        naming the failing check. Take the replica out of rotation on 503; do not restart it.
        """
        results = run_checks(checks)
        failing = {name: result for name, result in results.items() if result != "ok"}
        body = {"status": "not-ready" if failing else "ready", "checks": results}
        return JSONResponse(status_code=503 if failing else 200, content=body)
