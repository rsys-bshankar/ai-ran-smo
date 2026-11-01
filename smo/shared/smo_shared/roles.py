"""Who is calling, as R1 Termination vouches for it (PR-SEC-14): an SMO module (or the operator's GUI) or an rApp.

SME records the `kind` of every invoker when it registers: `internal` for one that presented the enrollment secret (a compose secret every SMO
module mounts, `secrets/enrollment_secret`), `rapp` for every other. SME's token introspection answers with that as `role`; R1 Termination
forwards it in `X-R1-Role` (dropping any value a caller sent) and refuses a caller with the `rapp` role on the routes of `INTERNAL_ONLY` before
anything reaches a backend. The invoker id is `X-R1-Invoker-Id` (invoker.py).

Enforcement (`SMO_ROLE_ENFORCEMENT`, read by SME and R1 Termination, default `enforce`):
  enforce  an rApp is refused on an internal-only route (403 `ROLE_NOT_PERMITTED`) and is not granted an internal scope
  audit    the same decision is made, counted and logged, and then allowed: for a rolling upgrade from a release that has no enrollment
Two lists decide what an rApp may call. `INTERNAL_ONLY` is a deny-list (any method, so it also covers reads): a route listed is refused. `RAPP_MAY_CHANGE` is
an allow-list for *changes* (POST, PUT, PATCH, DELETE): an rApp may change only what the SDK and the 3GPP consumer-facing routes need, and a change anywhere
else (onboarding, instance management, orchestration, the other modules' administration) is refused with the same 403 `ROLE_NOT_PERMITTED`. Reads are
open to every valid token, as before; what a caller with a scope claim may read is decided by the module that owns the data (PR-SEC-10, scope.py). Add a route to either list with the test that proves it exists.
"""

import hmac
import os
import re

ROLE_INTERNAL = "internal"
ROLE_RAPP = "rapp"
ROLE_HEADER = "X-R1-Role"
ENROLLMENT_HEADER = "X-SMO-Enrollment"
BOOTSTRAP_KEY_HEADER = "X-Bootstrap-Key"             # the optional shared key GET /bootstrap asks for (PR-SEC-9.3): R1_BOOTSTRAP_KEY[_FILE] at the gateway, SMO_BOOTSTRAP_KEY[_FILE] in a client

# the scopes only an internal invoker is granted (SME); `smo-rapp` is the scope an rApp asks for
INTERNAL_SCOPES = frozenset({"smo-internal", "smo-gui"})
RAPP_SCOPE = "smo-rapp"

# (module prefix at R1, methods, path regex) -> refused to an rApp. Paths are as the backend serves them, without the prefix.
INTERNAL_ONLY: tuple[tuple[str, frozenset[str], re.Pattern], ...] = tuple(
    (module, frozenset(methods), re.compile(pattern)) for module, methods, pattern in (
        # AI-10.2: what one rApp may do is set by the platform, never by an rApp
        ("/ran-nf-oam", ("PUT", "DELETE"), r"^/rapp-limits/[^/]+$"),
        # MGT-11: the KPIs the platform measures by
        ("/ran-nf-oam", ("PUT", "DELETE"), r"^/kpi-definitions/[^/]+$"),
        # MGT-1.8: removing CM history
        ("/ran-nf-oam", ("POST",), r"^/config-history/purge$"),
        # AI-10.4: stopping an rApp, lifting it, and who is stopped (an rApp may read whether it is stopped itself)
        ("/ran-nf-oam", ("PUT", "DELETE"), r"^/rapp-kill/[^/]+$"),
        ("/ran-nf-oam", ("GET",), r"^/rapp-kill$"),
        ("/rapp-mgmt", ("PUT", "DELETE"), r"^/instances/[^/]+/kill$"),
        # GUI-9.6: the global stop of every rApp, its lift, and how many are stopped
        ("/rapp-mgmt", ("PUT", "DELETE", "GET"), r"^/kill-all$"),
        # MGT-11.6/11.7: seeding the standard KPIs, and pushing results to DME (the reads stay open)
        ("/ran-nf-oam", ("POST",), r"^/kpi-definitions/standard$"),
        ("/ran-nf-oam", ("POST",), r"^/kpis/[^/]+/publish$"),
        # AI-10.6: who is told about refusals, and the record of them (it names other rApps)
        ("/ran-nf-oam", ("GET", "POST", "DELETE"), r"^/safeguard-subscriptions(/[^/]+)?$"),
        ("/ran-nf-oam", ("GET",), r"^/safeguard-refusals$"),
        ("/ran-nf-oam", ("POST",), r"^/safeguard-refusals/purge$"),
        ("/ran-nf-oam", ("PUT", "DELETE"), r"^/kpi-schedules/[^/]+$"),
        # AI-11: a human decides whether an rApp's action is written. An rApp never decides (the allow-list below refuses its changes here as well), never
        # sets who needs approval, and does not list what other rApps asked or why (it reads its own request, and its own record, by id)
        ("/ran-nf-oam", ("PUT", "DELETE"), r"^/rapp-approval-policy/[^/]+$"),
        ("/ran-nf-oam", ("POST",), r"^/rapp-approvals/([^/]+/(approve|reject)|expire-due)$"),
        ("/ran-nf-oam", ("GET",), r"^/rapp-approvals$"),
        ("/ran-nf-oam", ("GET", "POST", "DELETE"), r"^/approval-subscriptions(/[^/]+)?$"),
        # MGT-14.7, MGT-15.6: who is told when an onboarding fails or a campaign halts (a destination the platform will call)
        ("/ran-nf-oam", ("GET", "POST", "DELETE"), r"^/lifecycle-subscriptions(/[^/]+)?$"),
        # AI-13: the record of why rApps acted, and (GUI-9.5) its CSV export, which holds the same rows
        ("/ran-nf-oam", ("GET",), r"^/decision-records(/export\.csv)?$"),
        # SEC-10: what a caller (or a target) is scoped to is set by the platform, never by an rApp
        ("/sme", ("PUT",), r"^/invoker-registrations/[^/]+/authz-scope$"),
        # call flow 01d: a new secret for an existing invoker is rApp Management's act for a RUNNING instance, never an rApp's (it would take the identity over)
        ("/sme", ("POST",), r"^/invoker-registrations/[^/]+/secret$"),
        # call flow 18c: the trust contexts hold each invoker's authentication and authorization info (the list unredacted); an rApp reads none of them
        ("/sme", ("GET",), r"^/trusted-invokers(/[^/]+)?$"),
        ("/ran-nf-oam", ("PUT",), r"^/managed-entities/[^/]+/scope$"),
        # SEC-15.3c part 3: who owns a model is set by the operator or an SMO module, never by an rApp (`PUT /models/{id}` is on the allow-list and does not match this path)
        ("/aimgf", ("PUT",), r"^/models/[^/]+/owner$"),
        # MSG-3.2: the SMO's event feed names every module's entities; the platform and its console follow it, an rApp subscribes through DME's data jobs
        ("/dme", ("GET",), r"^/smo-events$"),
    ))


CHANGES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# module prefix at R1 -> the changes an rApp may make there, as (methods, path regex); `None` is every change (a module that is an rApp's own surface).
# Derived from what `smo_sdk` calls (tests/test_roles.py in r1-termination proves every SDK call is on it) plus the consumer-facing request routes
# of the 3GPP services that an rApp that does not use the SDK would call. What is left out is what administers a service rather than uses it:
# registering functions, repositories and storages, runtime scaling and termination, sweeps, trusted invokers, purging, and every route of the
# modules an operator drives (onboarding, instance management, orchestration, the sample rApps' own APIs).
RAPP_MAY_CHANGE: dict[str, tuple[tuple[frozenset[str], re.Pattern], ...] | None] = {
    prefix: None if rules is None else tuple((frozenset(m), re.compile(rx)) for m, rx in rules) for prefix, rules in {
        "/sme": (
            # not /oauth2/introspect: only R1 Termination introspects, and it calls SME directly (call flow 18d)
            (("POST",), r"^/(provider-registrations|invoker-registrations|oauth2/token)$"),
            (("DELETE",), r"^/provider-registrations/[^/]+$"),
            (("POST",), r"^/published-apis/v1/[^/]+/service-apis$"),
            (("DELETE",), r"^/published-apis/v1/[^/]+/service-apis/[^/]+$"),
            (("POST",), r"^/capif-events/v1/[^/]+/subscriptions$"),
            (("DELETE",), r"^/capif-events/v1/[^/]+/subscriptions/[^/]+$"),
        ),
        "/dme": (
            (("POST",), r"^/(actions|production-capabilities|data-jobs|offers|type-subscriptions)$"),
            (("DELETE",), r"^/(production-capabilities|data-jobs)$"),
            (("PUT", "DELETE"), r"^/data-jobs/[^/]+$"),
            (("POST",), r"^/data-jobs/[^/]+/records$"),
            # not /dme-types/{id}: a type is shared by its producers and consumers, and only the console or a module deletes it (call flow 11d)
            (("DELETE",), r"^/(offers|type-subscriptions)/[^/]+$"),
            (("POST",), r"^/offers/[^/]+/notify$"),
        ),
        "/aimgf": (
            (("POST",), r"^/(training-jobs|validation-jobs|emulation-jobs|feature-groups|mlmf/subscriptions|ml-(training|testing|update|model-loading)-requests|aiml-inference-reports)$"),
            (("DELETE",), r"^/(training-jobs|feature-groups|mlmf/subscriptions|ml-training-requests)/[^/]+$"),
            (("PATCH",), r"^/ml-(training|testing|update|model-loading)-requests/[^/]+$"),
            (("POST",), r"^/training-jobs/[^/]+/(suspend|resume|progress|model-metrics|complete)$"),
            (("POST",), r"^/(validation|emulation)-jobs/[^/]+/complete$"),
            (("POST",), r"^/mlmf/subscriptions/[^/]+/reports$"),
            # not `/models/{id}/advance`: the governance and end-of-life decisions are the operator's (SEC-15.1, SEC-15.1b); AIMgF refuses an rApp there too
            # call flow 17: the owner rApp drives its model's serving runtime (deploy, activate, scale, terminate); AIMgF refuses another rApp's model
            (("POST",), r"^/models/[^/]+/(inference-jobs|runtime/deploy|runtime/activate|runtime/scale|runtime/terminate)$"),
            (("POST",), r"^/inference-jobs/[^/]+/resolve$"),
            # the model registry (MLMR's until S2.1): an rApp registers, updates, deletes and uploads for its own models, groups them, writes their phase
            (("POST",), r"^/(models|coordination-groups)$"),
            (("PUT", "DELETE"), r"^/models/[^/]+$"),
            (("POST",), r"^/models/[^/]+/artifact$"),
            (("PATCH",), r"^/models/[^/]+/phase-info$"),
            # the deploy gate (MLLF's until S2.2)
            (("POST",), r"^/models/[^/]+/deploy$"),
        ),
        # GUI-8.3: a rApp registers (or withdraws) the operator API of its own instance; rApp Management checks that the instance is the caller's
        "/rapp-mgmt": (
            (("PUT", "DELETE"), r"^/instances/[^/]+/operator-api$"),
            # the container reports that it is up, how it performs and its faults (call flow 07a: a critical one before it is up is a failed bootstrap); rApp
            # Management checks that the instance is the caller's own (it is refused for another)
            (("POST",), r"^/instances/[^/]+/(bootstrap-complete|performance|fault)$"),
        ),
        "/mdaf": (
            (("POST",), r"^/(subscriptions|mda-requests|mda-reports|reports)$"),
            (("DELETE",), r"^/(subscriptions|mda-requests)/[^/]+$"),
        ),
        "/intent-service": (
            (("POST",), r"^/(intents|intent-reports|intent-handling-functions|autonomy-dispatches)$"),
            (("DELETE",), r"^/(intents|intent-handling-functions)/[^/]+$"),
            (("PATCH",), r"^/intents/[^/]+/admin-state$"),
            (("POST",), r"^/intents/[^/]+/negotiation-feedback$"),
            (("POST",), r"^/autonomy-dispatches/[^/]+/(resolve|reject)$"),
        ),
        # CM writes by an rApp are checked by its own safeguards (AI-10); undoing its job is the same path
        "/ran-nf-oam": (
            (("POST",), r"^/config-jobs$"),
            (("POST",), r"^/config-jobs/[^/]+/rollback$"),
        ),
        # an rApp's own data-producer surface, which the SDK does not wrap
        "/ran-analytics": None,
    }.items()
}


def rapp_may_change(module: str, method: str, path: str) -> bool:
    """Whether an rApp may make this *change*; a read is always `True` here. A module that is not in `RAPP_MAY_CHANGE` is not for rApps to change."""
    if method.upper() not in CHANGES:
        return True
    if module not in RAPP_MAY_CHANGE:
        return False
    rules = RAPP_MAY_CHANGE[module]
    if rules is None:
        return True
    path = "/" + path.lstrip("/")
    # `fullmatch`, not `match`: the patterns end in `$`, which also matches just before a trailing "\n", so `/config-jobs\n` would pass an allow-list
    # that lists `/config-jobs`. An allow-list must accept exactly the listed path.
    return any(method.upper() in methods and pattern.fullmatch(path) for methods, pattern in rules)


def enforcement_mode() -> str:
    """Returns `enforce` or `audit` from `SMO_ROLE_ENFORCEMENT`; unset or any other value gives `enforce`, so a typo never weakens the policy."""
    mode = os.environ.get("SMO_ROLE_ENFORCEMENT", "enforce").strip().lower()
    return mode if mode in ("enforce", "audit") else "enforce"      # a typo must not switch protection off


def internal_only(module: str, method: str, path: str) -> bool:
    """Whether an rApp is refused this call: `module` is the R1 prefix (`/ran-nf-oam`), `path` what follows it (`/rapp-limits/x`)."""
    path = "/" + path.lstrip("/")
    # `.match` on purpose: the patterns end in `$`, which also matches before a final "\n", so this deny-list errs on the side of refusing
    # (`/rapp-kill\n` is refused like `/rapp-kill`). The allow-list below must be the opposite, a strict `fullmatch`.
    return any(m == module and method.upper() in methods and pattern.match(path) for m, methods, pattern in INTERNAL_ONLY)


def role_of(request) -> str | None:
    """The role R1 Termination vouched for on this request; None when it did not come through R1 (an in-process call, a test)."""
    return request.headers.get(ROLE_HEADER) or None


def enrollment_secret_valid(presented: str | None, expected: str) -> bool:
    """True when `presented` equals the `expected` enrollment secret; False when either is empty.

    Compared with `hmac.compare_digest` (constant time), so response timing does not reveal how much of a guess was right.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())
