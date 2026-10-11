"""Summary counts for the console's tiles, badges and meters (the GUI redesign, SCALE.md P2): `GET /api/summary/{page}`.

Before this, every tile counted rows of the first page of a list in the browser, so past 100 rows (the SMO's default page size,
smo_shared/pagination.py) every count was wrong. Here the BFF asks each module for a one-row page with the filter that defines the count
(`?severity=critical&limit=1`) and reads the envelope's `total`, which the module computes with `COUNT(*)` in SQL. The calls of one page run in
parallel, and the answer is kept `CACHE_SECONDS` and shared by every signed-in user: the counts are module-wide reads that every role may make
(app/rbac.py lets a viewer read every module), so no user's view differs from another's.

Installed by app/main.py. Reads only: nothing here writes. A count whose module did not answer is `null` and named in `partial`, so a tile shows
"—" and the box says which module is missing (SCALE.md P10) instead of the whole summary failing. Which counts a page gets is the `PAGES` table
below; the GUI's `src/data/summary.ts` reads the same keys. A few values are not a `total` but one field of a module's statistics answer (the mean
time to acknowledge, `alarms.mtta`, from RAN NF OAM's `/alarms/stats`; the number of stopped rApps from rApp Management's `GET /kill-all`): a
`Count` with `field` set. Such a field may be null in a good answer (no alarm acknowledged in the window), which is not a missing module.
A count that needs data no module serves today (a network health score) is deliberately not here: the GUI shows "—" for it (BRIEF §5).

The same cached computation feeds the event stream (app/events.py): `install` keeps it on `app.state.summary_page`, so a page pushed to fifty
open streams and asked by fifty tiles is still one fan-out per `CACHE_SECONDS`.

GUI-9.3, the scope picker: `?region=` and `?site_cluster=` narrow every count whose module list accepts them (`SCOPED_PATHS`, the paths and which of
the two parameters each takes); the other counts stay network-wide and are named in the answer's `unscoped`. The cache key is the page and the scope,
so each scope is its own shared entry (at most `MAX_CACHE_ENTRIES` entries, the oldest dropped first). A scope value is checked against `SCOPE_RE`
before it reaches a module or a cache key.

GUI-9.8b, "Needs your attention": `GET /api/summary/attention` is the Dashboard's four short lists in one call (`ATTENTION`): each group is one
call to its module for the newest `limit` rows with the filter of the matching count, which answers the rows and their `total` together. The rows are
trimmed to the fields the GUI shows. Cached and shared the same way (`app.state.summary_attention`, also fed to the event stream as the topic
`summary:attention`). This route is declared before `/api/summary/{page}`, so "attention" is never read as a page name.

GUI-9.11, the Dashboard in three calls: a page can also carry `panels` (`PANELS`), small module answers kept whole (the latest decisions, the fleet
health by region, the worst elements, the 24 hourly alarm buckets), asked in the same parallel fan-out as its counts, narrowed by the same scope and
cached and pushed with them. The Dashboard's first load is then this summary, the attention call and the shell's module status.
"""

import asyncio
import datetime
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from fastapi import Depends, FastAPI, Query

from .rbac import decide
from . import scoping
from .smo_client import SmoAuthError

log = logging.getLogger("smo-gui-bff")

CACHE_SECONDS = 5.0
COUNT_TIMEOUT_SECONDS = 5.0
# One entry per (page or "attention", scope): bounded, because the scope is chosen by the caller. Past this the oldest entries are dropped.
MAX_CACHE_ENTRIES = 256
# A region or site-cluster name as RAN NF OAM stores it (ADR 0005). Checked before it is sent to a module or used in a cache key or an event topic.
SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ATTENTION_DEFAULT_LIMIT = 3
ATTENTION_MAX_LIMIT = 10


@dataclass(frozen=True)
class Count:
    """One count: the `total` of `path` filtered by `params`. `since_hours` adds a `since` parameter that many hours before now (decisions in 24 h).
    With `field`, the value is that top-level field of the answer (a number or null) instead of `total`, and no one-row page is asked for."""
    path: str
    params: tuple[tuple[str, str], ...] = ()
    since_hours: int | None = None
    field: str | None = None


def _states(path: str, param: str, states: tuple[str, ...], prefix: str) -> dict[str, Count]:
    """`{prefix.STATE: Count}` for each state, plus `prefix.total` unfiltered."""
    out = {f"{prefix}.{s}": Count(path, ((param, s),)) for s in states}
    out[f"{prefix}.total"] = Count(path)
    return out


SEVERITIES = ("critical", "major", "minor", "warning", "cleared")
ALARMS = {**{f"alarms.{s}": Count("/ran-nf-oam/alarms", (("severity", s),)) for s in SEVERITIES},
          "alarms.total": Count("/ran-nf-oam/alarms"), "ocloudAlarms.total": Count("/focom/alarms"),
          # GUI-9.8: the alarms nobody acknowledged yet, and the mean time to acknowledge (seconds, over the alarms acknowledged in the last 24 h)
          "alarms.unacked": Count("/ran-nf-oam/alarms", (("ack_state", "UNACKNOWLEDGED"),)),
          "alarms.mtta": Count("/ran-nf-oam/alarms/stats", (("window_hours", "24"),), field="mttaSeconds")}
APPROVALS = {"approvals.PENDING": Count("/ran-nf-oam/rapp-approvals", (("status", "PENDING"),))}
# GUI-7.3: the models whose next step is a governance decision (AIMgF's `awaiting_decision` filter), the Approvals inbox's second kind of request
MODEL_GATES = {"modelGates.waiting": Count("/aimgf/model-lifecycles", (("awaiting_decision", "true"),))}
INSTANCES = _states("/rapp-mgmt/instances", "state", ("DEPLOYING", "RUNNING", "UPGRADING", "UNDEPLOYED", "FAULTED"), "instances")
STOPPED = {"rappsStopped": Count("/rapp-mgmt/kill-all", field="stopped")}     # GUI-9.6: how many rApps have their writes stopped
PACKAGES = _states("/onboarding/packages", "state", ("ONBOARDING", "AVAILABLE", "PRIMED", "DEPRECATED", "FAILED"), "packages")
DEPLOYMENTS = _states("/nfo/deployments", "state", ("INSTANTIATING", "RUNNING", "UPDATING", "TERMINATING", "ABNORMAL"), "deployments")
INTENTS = _states("/intent-service/intents", "admin_state", ("ACTIVATED", "DEACTIVATED"), "intents")
CONFIG_JOBS = _states("/ran-nf-oam/config-jobs", "status", ("PENDING", "PENDING_APPROVAL", "SCHEDULED", "PROCESSING", "HALTED", "COMPLETED",
                                                           "PARTIAL_SUCCESS", "FAILED", "REJECTED"), "configJobs")
# MGT-4 / GUI-7.1: the CM jobs held for a change-window approval, the Approvals inbox's third kind of request
CHANGE_WINDOWS = {"configJobs.PENDING_APPROVAL": CONFIG_JOBS["configJobs.PENDING_APPROVAL"]}
CAMPAIGNS = _states("/ran-nf-oam/software-campaigns", "status",
                    ("PENDING", "RUNNING", "HALTED", "COMPLETED", "ABORTED", "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED"), "campaigns")
DECISIONS_24H = {f"decisions24h.{d}": Count("/ran-nf-oam/decision-records", (("disposition", d),), since_hours=24)
                 for d in ("DIRECT", "APPROVED", "REJECTED", "EXPIRED", "REFUSED")}
DECISIONS_24H["decisions24h.total"] = Count("/ran-nf-oam/decision-records", since_hours=24)
ESCALATIONS = {"escalations.total": Count("/sa-smos/remedial-actions", (("outcome", "ESCALATED"),))}
BREACHES = {"mlmfBreaches.total": Count("/aimgf/mlmf/reports", (("breached_only", "true"),))}
MODELS = {"models.total": Count("/mlmr/models"), "trainingJobs.total": Count("/aimgf/training-jobs")}
ELEMENTS = {"elements.total": Count("/ran-nf-oam/managed-entities")}

# The counts each page asks for, by the page's name in the URL. "nav" is the sidebar's badges, asked on every page, so it is kept small.
PAGES: dict[str, dict[str, Count]] = {
    "nav": {"alarms.critical": ALARMS["alarms.critical"], "alarms.major": ALARMS["alarms.major"], **APPROVALS, **MODEL_GATES, **CHANGE_WINDOWS,
            "configJobs.HALTED": CONFIG_JOBS["configJobs.HALTED"], "campaigns.HALTED": CAMPAIGNS["campaigns.HALTED"]},
    "dashboard": {**ALARMS, **APPROVALS, **INSTANCES, **PACKAGES, **DEPLOYMENTS, **INTENTS, **DECISIONS_24H, **ESCALATIONS, **BREACHES, **MODELS,
                  **ELEMENTS, **STOPPED},
    "alarms": ALARMS,
    "rapps": {**INSTANCES, **PACKAGES, **STOPPED},
    "approvals": {**APPROVALS, **MODEL_GATES, **CHANGE_WINDOWS},
    "decisions": DECISIONS_24H,
    "infrastructure": {**DEPLOYMENTS, **ELEMENTS},
    "configuration": CONFIG_JOBS,
    "software": CAMPAIGNS,
    "intents": INTENTS,
    "aiml": {**MODELS, **BREACHES, **MODEL_GATES},
}


# GUI-9.3: the module lists that accept the scope filters (RAN NF OAM's `managed_entity.region` / `.site_cluster`, ADR 0005), and which of the two
# each accepts. Every other count is network-wide whatever the scope (named in the answer's `unscoped`). rApp Management's instances have a region
# (the instance's authorised regions, plus the unscoped instances) but no site cluster. A module build without the filter ignores the parameter, so
# its count is then network-wide without saying so: these paths assume the modules of the same release.
SCOPE_BOTH = ("region", "site_cluster")
SCOPED_PATHS: dict[str, tuple[str, ...]] = {
    "/ran-nf-oam/alarms": SCOPE_BOTH,
    "/ran-nf-oam/alarms/stats": SCOPE_BOTH,
    "/ran-nf-oam/rapp-approvals": SCOPE_BOTH,
    "/ran-nf-oam/config-jobs": SCOPE_BOTH,
    "/ran-nf-oam/software-campaigns": SCOPE_BOTH,
    "/ran-nf-oam/decision-records": SCOPE_BOTH,
    "/ran-nf-oam/managed-entities": SCOPE_BOTH,
    "/ran-nf-oam/alarms/counts": SCOPE_BOTH,
    "/ran-nf-oam/managed-entities/health": SCOPE_BOTH,
    "/ran-nf-oam/managed-entities/worst": SCOPE_BOTH,
    "/rapp-mgmt/instances": ("region",),
}


@dataclass(frozen=True)
class Panel:
    """A module answer a page carries whole (GUI-9.11): `path` asked with `params` (and the scope where `SCOPED_PATHS` lists the path)."""
    path: str
    params: tuple[tuple[str, str], ...] = ()


# The panels each page carries next to its counts. Keys are what the GUI reads under `panels`.
PANELS: dict[str, dict[str, Panel]] = {
    "dashboard": {
        "decisions": Panel("/ran-nf-oam/decision-records", (("limit", "6"), ("total", "false"))),
        "health": Panel("/ran-nf-oam/managed-entities/health", (("group_by", "region"),)),
        "worst": Panel("/ran-nf-oam/managed-entities/worst", (("limit", "10"),)),
        "alarmHours": Panel("/ran-nf-oam/alarms/counts", (("group_by", "hour"),)),
    },
}


@dataclass(frozen=True)
class AttentionGroup:
    """One group of "Needs your attention": the newest rows of `path` filtered by `params`, each trimmed to `fields` (the ones the GUI shows)."""
    type: str
    path: str
    params: tuple[tuple[str, str], ...]
    fields: tuple[str, ...]


# The groups in the order the Dashboard shows them; each filter is the one of the summary count of the same name (alarms.critical,
# approvals.PENDING, mlmfBreaches.total, escalations.total), so a group's `total` equals that count.
ATTENTION = (
    AttentionGroup("critical-alarms", "/ran-nf-oam/alarms", (("severity", "critical"),),
                   ("alarmId", "managedElementRef", "managedFunctionRef", "severity", "ackState", "specificProblem", "probableCause", "alarmType",
                    "raisedAt")),
    AttentionGroup("approvals", "/ran-nf-oam/rapp-approvals", (("status", "PENDING"),),
                   ("approvalId", "invokerId", "status", "changeCount", "managedElements", "createdAt", "expiresAt")),
    AttentionGroup("mlmf-breaches", "/aimgf/mlmf/reports", (("breached_only", "true"),), ("reportId", "subscriptionId", "breachedFloor", "reportedAt")),
    AttentionGroup("escalations", "/sa-smos/remedial-actions", (("outcome", "ESCALATED"),), ("actionId", "monitorId", "actionType", "outcome", "autoExecuted")),
)


def valid_scope(value: str | None) -> bool:
    """Whether `value` is an absent scope (None) or a name `SCOPE_RE` accepts."""
    return value is None or bool(SCOPE_RE.match(value))


def scope_params(path: str, region: str | None, site_cluster: str | None) -> list[tuple[str, str]]:
    """The scope parameters `path` accepts (`SCOPED_PATHS`) with their values; empty when there is no scope or the path takes none."""
    accepted = SCOPED_PATHS.get(path, ())
    return [(k, v) for k, v in (("region", region), ("site_cluster", site_cluster)) if v is not None and k in accepted]


def scope_view(region: str | None, site_cluster: str | None) -> dict | None:
    """The `scope` member of an answer: null when unscoped, else `{region, siteCluster}`."""
    return None if region is None and site_cluster is None else {"region": region, "siteCluster": site_cluster}


def _narrowed(path: str, region: str | None, site_cluster: str | None) -> bool:
    """Whether every part of the scope asked for narrows `path` (a path that takes only the region is not narrowed by a site cluster)."""
    accepted = SCOPED_PATHS.get(path, ())
    return (region is None or "region" in accepted) and (site_cluster is None or "site_cluster" in accepted)


@dataclass
class _Cached:
    at: float
    body: dict = field(default_factory=dict)


def page_allowed(page: str, role) -> bool:
    """Whether `role` may read every count of `page` (each is a GET through the permission table), or every list of "attention". Every role may
    today; checked anyway, so a future rule that narrows a read also narrows its count, here and on the event stream."""
    paths = [g.path for g in ATTENTION] if page == "attention" else [c.path for c in PAGES[page].values()] + [s.path for s in PANELS.get(page, {}).values()]
    return all(decide("GET", p, {}, role).allowed for p in paths)


def install(app: FastAPI, *, current_session, problem) -> None:
    """Add `GET /api/summary/attention` and `GET /api/summary/{page}` to `app`, and keep the cached computations on `app.state.summary_page` (an
    async function of the page name and the optional scope) and `app.state.summary_attention` (of the scope and the group size) for the event
    stream. `problem` is main.py's RFC 7807 answer builder (an unknown page is a 404, a malformed scope a 400)."""
    cache: dict[tuple, _Cached] = {}
    locks: dict[tuple, asyncio.Lock] = {}

    async def count(name: str, spec: Count, scope: list[tuple[str, str]], claim: str | None = None) -> tuple[str, int | float | None, bool]:
        """(`name`, the value, whether the module answered). `scope` is the scope parameters this count's path accepts. The value is None when
        the module could not be asked or answered without one (then `answered` is false), or when a `field` count's module answered null (then it
        is true). Never raises."""
        params = [*spec.params, *scope] if spec.field else [*spec.params, *scope, ("limit", "1")]
        if spec.since_hours is not None:
            since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=spec.since_hours)
            params.append(("since", since.strftime("%Y-%m-%dT%H:%M:%SZ")))
        try:
            resp = await app.state.gateway.request("GET", spec.path, params=params, timeout=COUNT_TIMEOUT_SECONDS, headers=scoping.shared_headers(claim))
            body = resp.json() if resp.status_code == 200 else None
        except (SmoAuthError, httpx.HTTPError, ValueError) as exc:
            log.warning("summary count %s (%s) failed: %r", name, spec.path, exc)
            return name, None, False
        if not isinstance(body, dict):
            return name, None, False
        if spec.field:
            # A field the answer does not carry at all is an older build of the module (not a null value): counted as missing.
            value = body.get(spec.field)
            if spec.field not in body or (value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)))):
                return name, None, False
            return name, value, True
        total = body.get("total")
        return (name, total, True) if isinstance(total, int) and not isinstance(total, bool) else (name, None, False)

    async def panel(name: str, spec: Panel, region: str | None, site_cluster: str | None, claim: str | None = None) -> tuple[str, object, bool]:
        """(`name`, the module's answer as it came, whether it answered with 200 and JSON). The answer is None when it did not. Never raises."""
        try:
            resp = await app.state.gateway.request("GET", spec.path, params=[*spec.params, *scope_params(spec.path, region, site_cluster)],
                                                   timeout=COUNT_TIMEOUT_SECONDS, headers=scoping.shared_headers(claim))
            return (name, resp.json(), True) if resp.status_code == 200 else (name, None, False)
        except (SmoAuthError, httpx.HTTPError, ValueError) as exc:
            log.warning("summary panel %s (%s) failed: %r", name, spec.path, exc)
            return name, None, False

    async def compute(page: str, region: str | None, site_cluster: str | None, claim: str | None = None) -> dict:
        """Ask every count (and panel) of `page` in parallel, narrowed by the scope where its path accepts it; the body the route answers."""
        specs = PAGES[page]
        names = list(specs)
        panels = PANELS.get(page, {})
        results, panel_results = await asyncio.gather(
            asyncio.gather(*(count(n, specs[n], scope_params(specs[n].path, region, site_cluster), claim) for n in names)),
            asyncio.gather(*(panel(n, panels[n], region, site_cluster, claim) for n in panels)))
        scoped = region is not None or site_cluster is not None
        failed = {specs[n].path.split("/")[1] for n, _, answered in results if not answered}
        failed |= {panels[n].path.split("/")[1] for n, _, answered in panel_results if not answered}
        body = {"page": page, "computedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "counts": {n: v for n, v, _ in results},
                "partial": sorted(failed), "scope": scope_view(region, site_cluster),
                "unscoped": sorted(n for n in names if not _narrowed(specs[n].path, region, site_cluster)) if scoped else []}
        if panels:
            body["panels"] = {n: v for n, v, _ in panel_results}
        return body

    async def group(spec: AttentionGroup, limit: int, region: str | None, site_cluster: str | None, claim: str | None = None) -> tuple[dict, bool]:
        """One attention group and whether its module answered: `{type, total, items}`, the newest `limit` rows trimmed to `spec.fields`.
        A module that failed or answered something that is not a page gives `total` null and no items. Never raises."""
        params = [*spec.params, *scope_params(spec.path, region, site_cluster), ("limit", str(limit))]
        try:
            resp = await app.state.gateway.request("GET", spec.path, params=params, timeout=COUNT_TIMEOUT_SECONDS, headers=scoping.shared_headers(claim))
            body = resp.json() if resp.status_code == 200 else None
        except (SmoAuthError, httpx.HTTPError, ValueError) as exc:
            log.warning("attention group %s (%s) failed: %r", spec.type, spec.path, exc)
            body = None
        items = body.get("items") if isinstance(body, dict) else None
        total = body.get("total") if isinstance(body, dict) else None
        if not isinstance(items, list) or not isinstance(total, int) or isinstance(total, bool):
            return {"type": spec.type, "total": None, "items": []}, False
        trimmed = [{f: row.get(f) for f in spec.fields} for row in items[:limit] if isinstance(row, dict)]
        return {"type": spec.type, "total": total, "items": trimmed}, True

    async def compute_attention(limit: int, region: str | None, site_cluster: str | None, claim: str | None = None) -> dict:
        """Ask every attention group in parallel; the body `GET /api/summary/attention` answers."""
        results = await asyncio.gather(*(group(g, limit, region, site_cluster, claim) for g in ATTENTION))
        scoped = region is not None or site_cluster is not None
        return {"page": "attention", "computedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "groups": [g for g, _ in results],
                "partial": sorted({spec.path.split("/")[1] for spec, (_, answered) in zip(ATTENTION, results) if not answered}),
                "scope": scope_view(region, site_cluster),
                "unscoped": [g.type for g in ATTENTION if not _narrowed(g.path, region, site_cluster)] if scoped else []}

    def remember(key: tuple, body: dict) -> None:
        """Keep `body` under `key`; past `MAX_CACHE_ENTRIES` drop the expired entries, then the oldest, with their idle locks."""
        cache[key] = _Cached(time.monotonic(), body)
        if len(cache) <= MAX_CACHE_ENTRIES:
            return
        now = time.monotonic()
        for k in [k for k, v in cache.items() if now - v.at >= CACHE_SECONDS]:
            cache.pop(k, None)
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.pop(min(cache, key=lambda k: cache[k].at))
        for k in [k for k, lock in locks.items() if k not in cache and not lock.locked()]:
            locks.pop(k, None)

    async def cached(key: tuple, make) -> dict:
        """The body under `key`, at most `CACHE_SECONDS` old: from the cache, or made once (`await make()`) for every caller waiting on it."""
        hit = cache.get(key)
        if hit and time.monotonic() - hit.at < CACHE_SECONDS:
            return hit.body
        # One computation per key at a time: fifty operators opening the Dashboard together cost one fan-out, not fifty.
        lock = locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = cache.get(key)
            if hit and time.monotonic() - hit.at < CACHE_SECONDS:
                return hit.body
            body = await make()
            remember(key, body)
            return body

    async def page_counts(page: str, region: str | None = None, site_cluster: str | None = None, claim: str | None = None) -> dict:
        """The body of `page` (a key of `PAGES`) in the scope (both None: the whole network), at most `CACHE_SECONDS` old. The scope must
        already be checked (`valid_scope`). GUI-5: `claim` is the asking user's stored scope claim (None: unscoped); the counts are asked with it and
        cached under it, so users with different claims never share an answer."""
        return await cached((page, region, site_cluster, claim), lambda: compute(page, region, site_cluster, claim))

    async def attention(region: str | None = None, site_cluster: str | None = None, limit: int = ATTENTION_DEFAULT_LIMIT, claim: str | None = None) -> dict:
        """The attention groups in the scope, `limit` rows each, at most `CACHE_SECONDS` old. The scope must already be checked; `claim` as for `page_counts`."""
        return await cached(("attention", region, site_cluster, limit, claim), lambda: compute_attention(limit, region, site_cluster, claim))

    page_counts.cache = cache        # type: ignore[attr-defined]  # read by the tests (its bound), never written through
    app.state.summary_page = page_counts
    app.state.summary_attention = attention

    scope_docs = {"region": "Only what lies in this region (GUI-9.3), where the module's list accepts it.",
                  "site_cluster": "Only what lies in this site cluster, where the module's list accepts it."}

    def bad_scope(region: str | None, site_cluster: str | None):
        """A 400 `INVALID_SCOPE` answer when a scope value is malformed, else None."""
        if valid_scope(region) and valid_scope(site_cluster):
            return None
        return problem(400, "INVALID_SCOPE", "region and site_cluster are 1-64 characters of A-Z a-z 0-9 . _ -")

    @app.get("/api/summary/attention")
    async def summary_attention(limit: int = Query(ATTENTION_DEFAULT_LIMIT, ge=1, le=ATTENTION_MAX_LIMIT, description="Rows per group (1-10)."),
                                region: str | None = Query(None, description=scope_docs["region"]),
                                site_cluster: str | None = Query(None, description=scope_docs["site_cluster"]),
                                session=Depends(current_session)):
        """GUI-9.8b: the Dashboard's "Needs your attention" in one call: `{page: "attention", computedAt, groups: [{type, total, items}], partial,
        scope, unscoped}`, the groups `critical-alarms`, `approvals`, `mlmf-breaches` and `escalations` in that order, each the newest `limit`
        rows (trimmed to the fields the console shows) and the true total. A group whose module did not answer has `total` null and no items, and
        the module is in `partial`. Cached 5 s and shared by every user. 400 `INVALID_SCOPE`; 403 if a future rule narrows one of the reads."""
        refusal = bad_scope(region, site_cluster)
        if refusal is not None:
            return refusal
        if not page_allowed("attention", session.user.role):
            return problem(403, "FORBIDDEN", "a list on this page needs a read your role does not have")
        return await attention(region, site_cluster, limit, session.user.scope)

    @app.get("/api/summary/{page}")
    async def summary(page: str, region: str | None = Query(None, description=scope_docs["region"]),
                      site_cluster: str | None = Query(None, description=scope_docs["site_cluster"]), session=Depends(current_session)):
        """The counts of one console page (`PAGES`), at most `CACHE_SECONDS` old and shared by every user. `counts` maps a key such as
        `alarms.critical` to its total (null when its module did not answer); `partial` lists the modules that did not. With `region` and/or
        `site_cluster` (GUI-9.3) each count whose list accepts them is narrowed; `scope` echoes it and `unscoped` names the counts that stayed
        network-wide. 404 for an unknown page, 400 `INVALID_SCOPE` for a malformed scope."""
        if page not in PAGES:
            return problem(404, "NO_SUCH_SUMMARY", f"pages: {', '.join(sorted(PAGES))}")
        refusal = bad_scope(region, site_cluster)
        if refusal is not None:
            return refusal
        if not page_allowed(page, session.user.role):
            return problem(403, "FORBIDDEN", "a count on this page needs a read your role does not have")
        return await page_counts(page, region, site_cluster, session.user.scope)
