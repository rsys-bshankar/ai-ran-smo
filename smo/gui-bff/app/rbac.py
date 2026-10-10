"""The GUI's permission table: METHOD + /<module>/... -> minimum role.

The BFF is the authority. The SPA hides actions a role can't take, but every
proxied call is matched here before it's forwarded to R1 Termination.

Allowlist semantics: the first matching rule wins, and a request matching no
rule is refused. Every read under a known module prefix is Viewer-level
(apart from the one sensitive read listed first); every mutation must be
listed explicitly. Machine-to-machine routes that only an rApp, NF or
another SMO module should call (SME token/registration APIs, DME producer
registration, NFO Instantiate, usage registrations, heartbeats, ...) are
deliberately absent, so the GUI can't reach them at all.

Some rules also pin request parameters to the caller's GUI identity rather
than trusting what the browser sent: who acknowledged or cleared an alarm,
SA SMOS's requester_is_admin flag, the RMIO identity on Intent Service
intents, and who rejected an ASSIST autonomy dispatch, and who approved or rejected an rApp action.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable


class Role(StrEnum):
    """The three GUI roles. The string values are what is stored in `gui_user.role`, sent in the session answer and written in the audit log, so they are never renamed.
    Order of power is `RANK`, not the declaration order.
    """
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

# The R1 Termination route prefixes the GUI may reach (R1's own ROUTES table,
# minus DME's push/pull aliases, which are rApp data-plane paths).
MODULES = [
    "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam", "nfo", "focom",
    "aimgf", "mlmr", "mllf", "ran-analytics", "mdaf", "intent-service", "so-smos", "sa-smos",
]
# A rApp's own operator API is not in this list: its page, and the routes the GUI may call on it, are declared by its package and allowed by rapps.py / operator_ui.py (PR-GUI-8).

# The RMIO identity every GUI-created intent carries. Intent Service only lets
# an intent's own creator change its admin state, so pinning this on both
# create and update means the GUI can manage exactly the intents it created.
GUI_RMIO_ID = "smo-gui"


@dataclass(frozen=True)
class User:
    """The signed-in identity as the rules need it: the user name and the role read from the user table on this request. The `smo-gui:<username>` string the rules
    write into forwarded requests is built from `username`.
    """
    username: str
    role: Role


Overrides = Callable[[User], dict]


@dataclass(frozen=True)
class Rule:
    """One line of the permission table: the HTTP method, the full-path pattern, the minimum role, and optionally an extra condition on the query (`query_match`) and values
    forced from the signed-in user into the query (`query_overrides`) or the top level of the JSON body (`json_overrides`). A forced value replaces whatever the browser sent.
    Instances are immutable and live in `RULES`; the order of that list is part of the meaning.
    """
    method: str
    pattern: re.Pattern
    role: Role
    query_match: dict = field(default_factory=dict)   # extra condition on query params
    query_overrides: Overrides | None = None          # params forced from the GUI identity
    json_overrides: Overrides | None = None           # top-level JSON body fields forced from it


@dataclass(frozen=True)
class Decision:
    """The answer of `decide`: `allowed`, the role the matching rule needs (None when no rule matches, so the call is not exposed through the GUI at all) and the rule
    itself (None then), which the proxy uses to apply the overrides.
    """
    allowed: bool
    required_role: Role | None   # None: not exposed through the GUI at all
    rule: Rule | None = None


_ID = r"[^/]+"


def _rule(method: str, path: str, role: Role, **kw) -> Rule:
    return Rule(method, re.compile("^" + path.replace("{id}", _ID) + "$"), role, **kw)


V, O, A = Role.VIEWER, Role.OPERATOR, Role.ADMIN

RULES: list[Rule] = [
    # --- the one sensitive read: feature groups carry datalake tokens
    _rule("GET", "/aimgf/feature-groups", O),
    _rule("GET", "/aimgf/feature-groups/{id}", O),
    _rule("POST", "/aimgf/feature-groups", O),
    _rule("DELETE", "/aimgf/feature-groups/{id}", O),   # also terminates its DME data job

    # --- Onboarding
    _rule("POST", "/onboarding/packages", O),
    _rule("POST", "/onboarding/packages/{id}/(prime|deprime|deprecate|cancel-delete)", O),
    _rule("DELETE", "/onboarding/packages/{id}", A),
    # usage registrations are what an rApp instance itself files (call flow 06):
    # simulating one, to exercise the cascade-delete guard, is admin-only
    _rule("POST", "/onboarding/packages/{id}/usage/start", A),
    _rule("POST", "/onboarding/packages/{id}/usage/{id}/stop", A),

    # --- rApp Management
    _rule("POST", "/rapp-mgmt/instances", O),
    _rule("PUT", "/rapp-mgmt/instances/{id}/config", O),
    _rule("POST", "/rapp-mgmt/instances/{id}/(upgrade|upgrade/resolve|rollback|recover|bootstrap-complete)", O),
    # AI-10.4: stopping an rApp is an emergency action, so an operator may do it (and it is attributed to them); letting it write again is admin
    _rule("PUT", "/rapp-mgmt/instances/{id}/kill", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    _rule("DELETE", "/rapp-mgmt/instances/{id}/kill", A),
    # GUI-9.6: the one-call global stop of every rApp's writes and its resume, the same tiers and attribution as the per-instance kill above
    _rule("PUT", "/rapp-mgmt/kill-all", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    _rule("DELETE", "/rapp-mgmt/kill-all", A),
    _rule("POST", "/rapp-mgmt/instances/{id}/terminate", A),
    _rule("DELETE", "/rapp-mgmt/instances/{id}", A),
    _rule("POST", "/rapp-mgmt/instances/{id}/(performance|fault)", A),   # test-data injection

    # --- AI Platform (Wave 1 split of the former ai-ml-workflow: MLMR owns
    # the model/artifact/coordination-group rows, AIMgF owns lifecycle
    # state/training/validation/emulation/inference/MLMF/feature-groups,
    # MLLF owns deploy). AIMgF's own internal
    # PATCH /models/{id}/runtime/node-groups is deliberately absent — a
    # machine-to-machine route only MLLF calls, same as the SME/DME/NFO
    # internal routes above.
    _rule("POST", "/mlmr/models", O),
    _rule("PUT", "/mlmr/models/{id}", O),
    _rule("DELETE", "/mlmr/models/{id}", A),
    _rule("POST", "/mlmr/models/{id}/artifact", O),
    _rule("POST", "/mlmr/coordination-groups", O),
    # Wave 2: the six governance decisions (Approval/Certification/
    # Promotion/Rollback plus the submit/reject pair framing approval) are
    # admin-only, the same elevated stakes DEPRECATE/RETIRE already get —
    # everything else `advance` can fire (the automatic TRAINING_COMPLETE/
    # VALIDATION_COMPLETE/EMULATION_COMPLETE-style transitions) is operator.
    # The first match wins, so these admin-only events must stay above the generic advance rule that follows them: that rule would match the same request at
    # operator level. `decide` looks for any value of `event` among all the values sent, and tests/test_rbac_matrix.py fails for a rule that another rule shadows.
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "DEPRECATE"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "RETIRE"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "SUBMIT_FOR_APPROVAL"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "APPROVE"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "REJECT"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "CERTIFY"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "PROMOTE"}),
    _rule("POST", "/aimgf/models/{id}/advance", A, query_match={"event": "ROLLBACK"}),
    _rule("POST", "/aimgf/models/{id}/(advance|inference-jobs)", O),
    _rule("POST", "/aimgf/models/{id}/runtime/terminate", A),  # tearing down a runtime is destructive, like the DELETEs above
    _rule("POST", "/aimgf/models/{id}/runtime/(deploy|activate|scale)", O),
    _rule("POST", "/mllf/models/{id}/deploy", O),
    _rule("POST", "/aimgf/inference-jobs/{id}/resolve", O),
    _rule("POST", "/aimgf/training-jobs", O),
    _rule("DELETE", "/aimgf/training-jobs/{id}", O),        # cancel, not a hard delete
    _rule("POST", "/aimgf/training-jobs/{id}/model-metrics", O),
    _rule("POST", "/aimgf/training-jobs/{id}/(suspend|resume)", O),  # Wave 3: same tier as cancel
    # Training completes only through its job route — AIMgF's advance refuses
    # TRAINING_COMPLETE (OI-2-governance-bypass); same tier as the validation/emulation completes.
    _rule("POST", "/aimgf/training-jobs/{id}/complete", O),
    _rule("POST", "/aimgf/training-jobs/{id}/progress", O),   # the runtime's step report (OI-5-aiml-trainingjob-steps)
    _rule("POST", "/aimgf/validation-jobs", O),
    _rule("POST", "/aimgf/validation-jobs/{id}/complete", O),
    _rule("POST", "/aimgf/emulation-jobs", O),
    _rule("POST", "/aimgf/emulation-jobs/{id}/complete", O),
    _rule("POST", "/aimgf/mlmf/subscriptions", O),    # (POST /aimgf/feature-groups is the rule at the top: the same role, and a second copy of it here was never reached)
    _rule("POST", "/aimgf/mlmf/subscriptions/{id}/reports", A),  # test-data injection

    # --- RAN NF OAM
    _rule("PATCH", "/ran-nf-oam/alarms/{id}/ack", O,
          query_overrides=lambda u: {"ack_user_id": u.username}),
    _rule("PATCH", "/ran-nf-oam/alarms/{id}/clear", O,
          query_overrides=lambda u: {"clear_user_id": u.username}),
    # MGT-8.3 / GUI-2.3: a comment on an alarm, written by the signed-in user (as the ack and clear above), never the browser's `author`
    _rule("POST", "/ran-nf-oam/alarms/{id}/comments", O,
          json_overrides=lambda u: {"author": u.username}),
    _rule("POST", "/ran-nf-oam/alarms/ingest", A),                   # test-data injection
    # CM writes: who asked, and the MSAC access tier that entire-RAN scope
    # requires, come from the GUI identity (an admin holds the tier; an
    # operator's entire-RAN write is refused by RAN NF OAM's own MSAC gate)
    _rule("POST", "/ran-nf-oam/config-jobs", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}", "msacRole": "admin" if u.role == Role.ADMIN else None}),
    # Undoing a job and driving a staged one are CM actions too: attributed to the GUI user, and an admin holds the MSAC tier (as for a new write)
    _rule("POST", "/ran-nf-oam/config-jobs/{id}/rollback", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}", "msacRole": "admin" if u.role == Role.ADMIN else None}),
    _rule("POST", "/ran-nf-oam/config-jobs/{id}/(continue|halt|abort)", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    # GUI-9.7: re-running a job's KPI check only reads PM and records the verdict on the job: operator, like driving the job
    _rule("POST", "/ran-nf-oam/config-jobs/{id}/kpi-check", O),
    # KPI definitions and their schedules are platform configuration (internal-only at R1): admin
    _rule("PUT", "/ran-nf-oam/kpi-definitions/{id}", A),
    _rule("DELETE", "/ran-nf-oam/kpi-definitions/{id}", A),
    _rule("POST", "/ran-nf-oam/kpi-definitions/standard", A),
    _rule("PUT", "/ran-nf-oam/kpi-schedules/{id}", A),
    _rule("DELETE", "/ran-nf-oam/kpi-schedules/{id}", A),
    _rule("POST", "/ran-nf-oam/(pm-subscriptions|software-management-jobs|o1-adaptor-endpoints|o1-adaptor-endpoints/discover)", O),
    # GUI-10.1: the FM subscription form and the FM / PM Unsubscribe buttons: subscribing an element's alarms or measurements to the SMO, and ending it,
    # is an operator action like creating the PM subscription above
    _rule("POST", "/ran-nf-oam/fm-subscriptions", O),
    _rule("DELETE", "/ran-nf-oam/(fm-subscriptions|pm-subscriptions)/{id}", O),
    _rule("POST", "/ran-nf-oam/software-management-jobs/{id}/advance", O),
    _rule("POST", "/ran-nf-oam/o1-adaptor-endpoints/{id}/heartbeat", A),   # what the ME's adaptor sends: simulation
    # GUI-9.7: a pinned SSH host key is the trust anchor of the O1 session, so re-pinning or unpinning one is admin; who pinned it is the
    # signed-in user, never what the browser sent (STD-4.6). DELETE is `/host-keys/{keyType}` in RAN NF OAM; the bare path is matched too.
    _rule("PUT", "/ran-nf-oam/o1-adaptor-endpoints/{id}/host-keys", A,
          json_overrides=lambda u: {"pinnedBy": f"smo-gui:{u.username}"}),
    _rule("DELETE", "/ran-nf-oam/o1-adaptor-endpoints/{id}/host-keys(/{id})?", A),
    # GUI-9.7: reading an element's managed objects again over O1 changes only the SMO's copy: operator
    _rule("POST", "/ran-nf-oam/managed-entities/{id}/managed-objects/refresh", O),
    # GUI-9.7: the MSAC roles, identities and access rules decide who may write to the RAN: administrative
    _rule("POST", "/ran-nf-oam/msac/(roles|identities|access-rules)(/{id})?", A),
    _rule("PUT", "/ran-nf-oam/msac/(roles|identities|access-rules)(/{id})?", A),
    _rule("DELETE", "/ran-nf-oam/msac/(roles|identities|access-rules)(/{id})?", A),
    # MGT-14 / MGT-15: what a new element is configured with is platform configuration (admin); applying it to an element and driving a software campaign are
    # operator actions, attributed to the signed-in user. The sweep (`advance-due`) is the worker's and is not exposed.
    _rule("PUT", "/ran-nf-oam/onboarding-templates/{id}", A),
    _rule("DELETE", "/ran-nf-oam/onboarding-templates/{id}", A),
    _rule("POST", "/ran-nf-oam/element-onboarding/{id}/select", O),
    _rule("POST", "/ran-nf-oam/element-onboarding/{id}/apply", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    _rule("POST", "/ran-nf-oam/software-campaigns", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    _rule("POST", "/ran-nf-oam/software-campaigns/{id}/(continue|halt|abort|rollback)", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    # MGT-14.7 / MGT-15.6: where the platform calls when an onboarding fails or a campaign halts is an administrative decision (as for safeguard and approval subscriptions)
    _rule("POST", "/ran-nf-oam/lifecycle-subscriptions", A),
    _rule("DELETE", "/ran-nf-oam/lifecycle-subscriptions/{id}", A),
    # Wave 9 (W9-01..06): the vendor capability registry, CM schema
    # descriptors and cell guards are inventory/onboarding data — admin.
    _rule("POST", "/ran-nf-oam/(cm-schemas|vendor-onboarding)", A),
    _rule("PUT", "/ran-nf-oam/vendor-capabilities/{id}", A),
    _rule("DELETE", "/ran-nf-oam/vendor-capabilities/{id}", A),
    _rule("PUT", "/ran-nf-oam/managed-entities/{id}/cells/{id}/guards", A),
    _rule("DELETE", "/ran-nf-oam/managed-entities/{id}/cells/{id}/guards", A),

    # SEC-10: where a managed element is and whom it belongs to, and which regions and tenants an invoker (an rApp) may touch, are administrative decisions
    _rule("PUT", "/ran-nf-oam/managed-entities/{id}/scope", A),
    _rule("PUT", "/ran-nf-oam/managed-entities/{id}/site-cluster", A),    # GUI-9.8: which site cluster an element belongs to, the same tier as its scope
    _rule("PUT", "/sme/invoker-registrations/{id}/authz-scope", A),

    # AI-10.2/10.3, AI-10.6: what an rApp may do (its limits) and who is told when it is refused are administrative decisions
    _rule("PUT", "/ran-nf-oam/rapp-limits/{id}", A),
    _rule("DELETE", "/ran-nf-oam/rapp-limits/{id}", A),
    _rule("POST", "/ran-nf-oam/safeguard-subscriptions", A),
    _rule("DELETE", "/ran-nf-oam/safeguard-subscriptions/{id}", A),
    _rule("POST", "/ran-nf-oam/safeguard-refusals/purge", A),
    # AI-11: whether an rApp's action is written is a person's decision. Approving or rejecting is an operator's, as is writing a change by hand, and who
    # decided is the signed-in user, never what the browser sent. Which rApps wait for a decision, and who is told, are administrative (admin).
    # The sweep that lapses old requests is not exposed.
    _rule("POST", "/ran-nf-oam/rapp-approvals/{id}/(approve|reject)", O,
          json_overrides=lambda u: {"decidedBy": f"smo-gui:{u.username}"}),
    _rule("PUT", "/ran-nf-oam/rapp-approval-policy/{id}", A,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),
    _rule("DELETE", "/ran-nf-oam/rapp-approval-policy/{id}", A),
    _rule("POST", "/ran-nf-oam/approval-subscriptions", A),
    _rule("DELETE", "/ran-nf-oam/approval-subscriptions/{id}", A),

    # --- DME: consumers are operator-level, producers admin
    _rule("POST", "/dme/data-jobs", O),
    _rule("PUT", "/dme/data-jobs/{id}", O),
    _rule("DELETE", "/dme/data-jobs/{id}", O),                       # terminate a consumer job
    _rule("POST", "/dme/type-subscriptions", O),
    _rule("DELETE", "/dme/type-subscriptions/{id}", O),
    _rule("POST", "/dme/production-capabilities", A),
    _rule("DELETE", "/dme/production-capabilities", A),
    _rule("POST", "/dme/offers", A),
    _rule("POST", "/dme/offers/{id}/notify", A),
    _rule("DELETE", "/dme/offers/{id}", A),
    # Wave 3 (docs/ARCHITECTURE.md (DME)): ingesting a real data
    # payload is a producer-side operation, same tier as production-
    # capabilities/offers above. Mediating an O1 action is consumer-side
    # (an rApp's AI/ML decision) — operator, mirroring ran-nf-oam's own
    # POST /config-jobs identity-pinning above, since this route forwards
    # to exactly that one.
    _rule("POST", "/dme/data-jobs/{id}/records", A),
    _rule("POST", "/dme/actions", O,
          json_overrides=lambda u: {"requestedBy": f"smo-gui:{u.username}"}),

    # --- SME: registry administration is admin; event subscriptions operator.
    # Invoker onboarding returns a one-time secret, so it's admin-only and
    # audited; token issuance and introspection stay unexposed.
    _rule("POST", "/sme/provider-registrations", A),
    _rule("DELETE", "/sme/provider-registrations/{id}", A),
    _rule("POST", "/sme/published-apis/v1/{id}/service-apis", A),
    _rule("DELETE", "/sme/published-apis/v1/{id}/service-apis/{id}", A),
    _rule("POST", "/sme/invoker-registrations", A),
    _rule("PUT", "/sme/invoker-registrations/{id}", A),      # key rotation (SA-SME-1-public-key)
    _rule("DELETE", "/sme/invoker-registrations/{id}", A),   # offboarding
    _rule("PUT", "/sme/trusted-invokers/{id}", A),
    _rule("POST", "/sme/trusted-invokers/{id}/(update|delete)", A),
    _rule("DELETE", "/sme/trusted-invokers/{id}", A),
    _rule("POST", "/sme/capif-events/v1/{id}/subscriptions", O),
    _rule("DELETE", "/sme/capif-events/v1/{id}/subscriptions/{id}", O),

    # --- NFO
    _rule("POST", "/nfo/deployments/{id}/(heal|scale)", O),
    _rule("DELETE", "/nfo/deployments/{id}", A),
    _rule("POST", "/nfo/deployments/{id}/dms-notifications", A),   # the DMS's report; no real DMS runs (OI-3-nfo-abnormal)

    # --- FOCOM
    _rule("POST", "/focom/resources/provision", A),
    _rule("DELETE", "/focom/resources/{id}", A),
    _rule("POST", "/focom/alarms/ingest", A),                        # test-data injection
    _rule("POST", "/focom/inventory/subscriptions", O),
    _rule("DELETE", "/focom/inventory/subscriptions/{id}", O),

    # --- Intent Service (formerly Policy Mgmt — renamed in Wave 1 of the
    # AI Platform Service Decomposition; see docs/ARCHITECTURE.md (Intent Service))
    _rule("POST", "/intent-service/intents", O, json_overrides=lambda u: {"rmioId": GUI_RMIO_ID}),
    _rule("PATCH", "/intent-service/intents/{id}/admin-state", O, json_overrides=lambda u: {"requesterId": GUI_RMIO_ID}),
    _rule("DELETE", "/intent-service/intents/{id}", A),
    # GUI-9.7: the consumer's answer to a negotiation report (TS 28.312 IntentFulfilmentNegotiationFeedback) is an operator's, like changing the admin state
    _rule("POST", "/intent-service/intents/{id}/negotiation-feedback", O),
    # RMIH registration is framework-internal only (D-SEC-POLICY-1: SO/SA SMOS
    # identities) and fulfilment reports come from an RMIH, so both are admin
    # acting on the framework's behalf (call flow 09)
    _rule("POST", "/intent-service/intent-handling-functions", A),
    _rule("DELETE", "/intent-service/intent-handling-functions/{id}", A),
    _rule("POST", "/intent-service/intent-reports", A),
    # rApp autonomy modes (HISTORY.md OI-6.3; Wave 8 W8-08): an operator
    # requests a dispatch, and scopes (resolve) or rejects an ASSIST one
    # left AWAITING_SCOPE — who rejected is pinned to the GUI identity.
    _rule("POST", "/intent-service/autonomy-dispatches", O),
    _rule("POST", "/intent-service/autonomy-dispatches/{id}/resolve", O),
    _rule("POST", "/intent-service/autonomy-dispatches/{id}/reject", O,
          json_overrides=lambda u: {"rejectedBy": f"smo-gui:{u.username}"}),

    # --- RAN Analytics / MDAF (Wave 1 split: reports/subscriptions moved
    # to mdaf/, producer registration stays in ran-analytics/)
    _rule("POST", "/mdaf/subscriptions", O),
    _rule("DELETE", "/mdaf/subscriptions/{id}", O),
    _rule("POST", "/mdaf/mda-requests", O),                          # GUI-9.7: asking MDAF for an analysis, and withdrawing the request
    _rule("DELETE", "/mdaf/mda-requests/{id}", O),
    _rule("POST", "/ran-analytics/producers", A),                    # producer side of call flow 08
    _rule("POST", "/mdaf/reports", A),

    # --- SO / SA SMOS
    _rule("POST", "/so-smos/orders", O),
    _rule("POST", "/so-smos/orders/{id}/cancel", O),
    _rule("POST", "/sa-smos/monitors", O),
    _rule("POST", "/sa-smos/monitors/{id}/(evaluate|escalate)", O),
    _rule("POST", "/sa-smos/monitors/{id}/remedial-actions", O,
          query_overrides=lambda u: {"requester_is_admin": "true" if u.role == Role.ADMIN else "false"}),

    # --- every other read under a known module prefix
    _rule("GET", "/(" + "|".join(re.escape(m) for m in MODULES) + ")(/.*)?", V),
]


def decide(method: str, path: str, query: dict[str, list[str]], role: Role) -> Decision:
    """`query` maps each param to ALL its values: a query_match rule
    matches if any value does, so `?event=CERTIFY&event=DEPRECATE` can't
    slip a deprecation past the admin-only rule whichever value the
    backend ends up reading.
    """
    method = method.upper()
    for rule in RULES:
        if rule.method != method or not rule.pattern.match(path):
            continue
        if any(v not in query.get(k, []) for k, v in rule.query_match.items()):
            continue
        return Decision(allowed=RANK[role] >= RANK[rule.role], required_role=rule.role, rule=rule)
    return Decision(allowed=False, required_role=None)
