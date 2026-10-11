"""The permission table on its own (rbac.py), independent of HTTP.
Run with: cd smo/gui-bff && PYTHONPATH=. python -m pytest tests -q
"""

import json
import sys
from pathlib import Path

import pytest

from app.rbac import MODULES, RULES, Role, decide

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from export_permissions import FIXTURE, export  # noqa: E402


def allowed(method, path, role, **query):
    return decide(method, path, {k: [v] for k, v in query.items()}, Role(role)).allowed


# One case per proxied module: a viewer can read the module's health route, so the catch-all read rule covers every module in `MODULES`.
@pytest.mark.parametrize("module", MODULES)
def test_every_module_is_readable_by_a_viewer(module):
    assert allowed("GET", f"/{module}/health", "viewer")


# Each row is a route and the lowest role that may call it. The test checks every role against the row, so a viewer is refused below the minimum and every role at or above it is allowed. Add a row for each rule that is added to the table.
@pytest.mark.parametrize("method,path,minimum", [
    ("POST", "/onboarding/packages", "operator"),
    ("POST", "/onboarding/packages/p/prime", "operator"),
    ("DELETE", "/onboarding/packages/p", "admin"),
    ("POST", "/rapp-mgmt/instances", "operator"),
    ("POST", "/rapp-mgmt/instances/i/upgrade", "operator"),
    ("POST", "/rapp-mgmt/instances/i/recover", "operator"),
    ("POST", "/rapp-mgmt/instances/i/terminate", "admin"),
    ("DELETE", "/rapp-mgmt/instances/i", "admin"),
    ("POST", "/aimgf/training-jobs", "operator"),
    ("POST", "/aimgf/models/m/advance", "operator"),
    ("POST", "/aimgf/training-jobs/j/complete", "operator"),
    ("DELETE", "/mlmr/models/m", "admin"),
    ("PATCH", "/ran-nf-oam/alarms/a/ack", "operator"),
    ("PATCH", "/ran-nf-oam/alarms/a/clear", "operator"),
    ("POST", "/ran-nf-oam/alarms/ingest", "admin"),
    ("POST", "/sa-smos/monitors/m/evaluate", "operator"),
    ("DELETE", "/nfo/deployments/d", "admin"),
    ("GET", "/aimgf/feature-groups", "operator"),
    # GUI pass 2
    ("POST", "/dme/data-jobs", "operator"),
    ("DELETE", "/dme/data-jobs/j", "operator"),
    ("POST", "/dme/offers", "admin"),
    ("POST", "/dme/offers/o/notify", "admin"),
    ("POST", "/sme/provider-registrations", "admin"),
    ("POST", "/sme/published-apis/v1/apf/service-apis", "admin"),
    ("DELETE", "/sme/published-apis/v1/apf/service-apis/s", "admin"),
    ("POST", "/sme/invoker-registrations", "admin"),
    ("PUT", "/sme/trusted-invokers/t", "admin"),
    ("POST", "/sme/trusted-invokers/t/delete", "admin"),
    ("POST", "/sme/capif-events/v1/sub/subscriptions", "operator"),
    ("POST", "/onboarding/packages/p/usage/start", "admin"),
    ("POST", "/onboarding/packages/p/usage/r/stop", "admin"),
    ("POST", "/ran-nf-oam/o1-adaptor-endpoints/e/heartbeat", "admin"),
    ("POST", "/intent-service/intent-handling-functions", "admin"),
    ("POST", "/intent-service/intent-reports", "admin"),
    ("POST", "/ran-analytics/producers", "admin"),
    ("POST", "/mdaf/reports", "admin"),
    ("POST", "/focom/inventory/subscriptions", "operator"),
    ("POST", "/dme/production-capabilities", "admin"),
    # Wave 3 (docs/ARCHITECTURE.md (DME))
    ("POST", "/dme/data-jobs/j/records", "admin"),
    ("POST", "/dme/actions", "operator"),
    ("POST", "/aimgf/training-jobs/j/suspend", "operator"),
    ("POST", "/aimgf/training-jobs/j/resume", "operator"),
    # Wave 8 (W8-08): ASSIST dispatches are scoped or rejected by an operator
    ("POST", "/intent-service/autonomy-dispatches", "operator"),
    ("POST", "/intent-service/autonomy-dispatches/d/resolve", "operator"),
    ("POST", "/intent-service/autonomy-dispatches/d/reject", "operator"),
    # Wave 9 (W9-01..06): vendor registry, CM schemas and cell guards are admin
    ("POST", "/ran-nf-oam/cm-schemas", "admin"),
    ("POST", "/ran-nf-oam/vendor-onboarding", "admin"),
    ("PUT", "/ran-nf-oam/vendor-capabilities/acme", "admin"),
    ("DELETE", "/ran-nf-oam/vendor-capabilities/acme", "admin"),
    ("PUT", "/ran-nf-oam/managed-entities/me-1/cells/1/guards", "admin"),
    ("GET", "/ran-nf-oam/cell-guards", "viewer"),
    # AI-10.x: the safeguards of an rApp
    ("GET", "/rapp-mgmt/instances/i/safeguards", "viewer"),
    ("GET", "/ran-nf-oam/safeguard-refusals", "viewer"),
    ("GET", "/ran-nf-oam/rapp-kill", "viewer"),
    ("PUT", "/rapp-mgmt/instances/i/kill", "operator"),
    ("DELETE", "/rapp-mgmt/instances/i/kill", "admin"),
    ("PUT", "/ran-nf-oam/rapp-limits/i", "admin"),
    ("PUT", "/ran-nf-oam/managed-entities/e/scope", "admin"),           # SEC-10
    # MGT-14 / MGT-15: templates are configuration (admin); applying one and driving a campaign are operator actions; reads are viewer
    ("PUT", "/ran-nf-oam/onboarding-templates/t", "admin"),
    ("DELETE", "/ran-nf-oam/onboarding-templates/t", "admin"),
    ("POST", "/ran-nf-oam/element-onboarding/e/select", "operator"),
    ("POST", "/ran-nf-oam/element-onboarding/e/apply", "operator"),
    ("POST", "/ran-nf-oam/software-campaigns", "operator"),
    ("POST", "/ran-nf-oam/software-campaigns/c/continue", "operator"),
    ("POST", "/ran-nf-oam/software-campaigns/c/halt", "operator"),
    ("POST", "/ran-nf-oam/software-campaigns/c/abort", "operator"),
    ("POST", "/ran-nf-oam/software-campaigns/c/rollback", "operator"),
    ("GET", "/ran-nf-oam/software-campaigns/c/report", "viewer"),
    ("GET", "/ran-nf-oam/element-onboarding", "viewer"),
    ("GET", "/ran-nf-oam/software-campaigns", "viewer"),
    ("GET", "/ran-nf-oam/onboarding-templates", "viewer"),
    ("POST", "/ran-nf-oam/lifecycle-subscriptions", "admin"),            # MGT-14.7 / MGT-15.6: where a failure or a halt is announced
    ("DELETE", "/ran-nf-oam/lifecycle-subscriptions/s", "admin"),
    ("GET", "/ran-nf-oam/lifecycle-subscriptions", "viewer"),
    ("PUT", "/sme/invoker-registrations/i/authz-scope", "admin"),
    # AI-11: a person decides an rApp's action (operator); who waits for a decision, and who is told, is administrative
    ("POST", "/ran-nf-oam/rapp-approvals/a/approve", "operator"),
    ("POST", "/ran-nf-oam/rapp-approvals/a/reject", "operator"),
    # MGT-4.3: a CM job held for its change window is decided by an operator, like an rApp's action
    ("POST", "/ran-nf-oam/config-jobs/j/approve", "operator"),
    ("POST", "/ran-nf-oam/config-jobs/j/reject", "operator"),
    ("PUT", "/ran-nf-oam/rapp-approval-policy/i", "admin"),
    ("DELETE", "/ran-nf-oam/rapp-approval-policy/i", "admin"),
    ("POST", "/ran-nf-oam/approval-subscriptions", "admin"),
    ("DELETE", "/ran-nf-oam/approval-subscriptions/s", "admin"),
    # AI-11 / AI-13: the queue, a request and the decision records are reads
    ("GET", "/ran-nf-oam/rapp-approvals", "viewer"),
    ("GET", "/ran-nf-oam/rapp-approvals/a", "viewer"),
    ("GET", "/ran-nf-oam/decision-records", "viewer"),
    ("GET", "/ran-nf-oam/decision-records/d", "viewer"),
    ("DELETE", "/ran-nf-oam/rapp-limits/i", "admin"),
    ("POST", "/ran-nf-oam/safeguard-subscriptions", "admin"),
    ("DELETE", "/ran-nf-oam/safeguard-subscriptions/s", "admin"),
    ("POST", "/ran-nf-oam/safeguard-refusals/purge", "admin"),
    # change management: rollback, staged jobs, KPIs
    ("POST", "/ran-nf-oam/config-jobs/j/rollback", "operator"),
    ("POST", "/ran-nf-oam/config-jobs/j/continue", "operator"),
    ("POST", "/ran-nf-oam/config-jobs/j/halt", "operator"),
    ("POST", "/ran-nf-oam/config-jobs/j/abort", "operator"),
    ("GET", "/ran-nf-oam/config-jobs/j", "viewer"),
    ("GET", "/ran-nf-oam/kpi-definitions", "viewer"),
    ("GET", "/ran-nf-oam/kpi-schedules", "viewer"),
    ("PUT", "/ran-nf-oam/kpi-definitions/k", "admin"),
    ("DELETE", "/ran-nf-oam/kpi-definitions/k", "admin"),
    ("POST", "/ran-nf-oam/kpi-definitions/standard", "admin"),
    ("PUT", "/ran-nf-oam/kpi-schedules/s", "admin"),
    ("DELETE", "/ran-nf-oam/kpi-schedules/s", "admin"),
    # GUI-9.7: the routes the redesigned pages showed read-only, and the global stop and site cluster of GUI-9.6 / GUI-9.8
    ("POST", "/intent-service/intents/i/negotiation-feedback", "operator"),
    ("POST", "/mdaf/mda-requests", "operator"),
    ("DELETE", "/mdaf/mda-requests/r", "operator"),
    ("POST", "/ran-nf-oam/config-jobs/j/kpi-check", "operator"),
    ("PUT", "/ran-nf-oam/o1-adaptor-endpoints/e/host-keys", "admin"),
    ("DELETE", "/ran-nf-oam/o1-adaptor-endpoints/e/host-keys", "admin"),
    ("DELETE", "/ran-nf-oam/o1-adaptor-endpoints/e/host-keys/ssh-ed25519", "admin"),
    ("POST", "/ran-nf-oam/managed-entities/me-1/managed-objects/refresh", "operator"),
    ("POST", "/ran-nf-oam/msac/roles", "admin"),
    ("PUT", "/ran-nf-oam/msac/roles/r", "admin"),
    ("DELETE", "/ran-nf-oam/msac/roles/r", "admin"),
    ("POST", "/ran-nf-oam/msac/identities", "admin"),
    ("PUT", "/ran-nf-oam/msac/identities/i", "admin"),
    ("DELETE", "/ran-nf-oam/msac/identities/i", "admin"),
    ("POST", "/ran-nf-oam/msac/access-rules", "admin"),
    ("DELETE", "/ran-nf-oam/msac/access-rules/a", "admin"),
    ("GET", "/ran-nf-oam/msac/roles", "viewer"),
    ("PUT", "/rapp-mgmt/kill-all", "operator"),
    ("DELETE", "/rapp-mgmt/kill-all", "admin"),
    ("GET", "/rapp-mgmt/kill-all", "viewer"),
    ("PUT", "/ran-nf-oam/managed-entities/me-1/site-cluster", "admin"),
    # GUI-10.1: the FM subscription form and the FM / PM Unsubscribe buttons are operator actions; listing them stays a read
    ("POST", "/ran-nf-oam/fm-subscriptions", "operator"),
    ("DELETE", "/ran-nf-oam/fm-subscriptions/s-1", "operator"),
    ("DELETE", "/ran-nf-oam/pm-subscriptions/s-1", "operator"),
    ("POST", "/ran-nf-oam/pm-subscriptions", "operator"),
    ("GET", "/ran-nf-oam/fm-subscriptions", "viewer"),
])
def test_minimum_role_per_route(method, path, minimum):
    order = ["viewer", "operator", "admin"]
    for role in order:
        assert allowed(method, path, role) == (order.index(role) >= order.index(minimum)), (method, path, role)


def test_deprecate_is_admin_only_via_query_match():
    """Lifecycle events that are governance decisions are admin only, selected by the `event` query value, while an ordinary event stays operator level.
    """
    assert allowed("POST", "/aimgf/models/m/advance", "operator", event="APPROVE_TRAINING")
    assert not allowed("POST", "/aimgf/models/m/advance", "operator", event="DEPRECATE")
    assert allowed("POST", "/aimgf/models/m/advance", "admin", event="DEPRECATE")


def test_any_duplicated_value_triggers_the_query_match():
    """A repeated `event` parameter cannot slip an admin-only event past the rule: any one value that matches makes the rule apply.
    """
    decision = decide("POST", "/aimgf/models/m/advance", {"event": ["CERTIFY", "DEPRECATE"]}, Role.OPERATOR)
    assert not decision.allowed and decision.required_role == Role.ADMIN


# Each row is a route that is machine-to-machine, unknown or has no rule for that method; even an admin gets no decision (`required_role` None), so the GUI cannot reach it.
@pytest.mark.parametrize("method,path", [
    ("POST", "/sme/oauth2/token"),
    ("POST", "/sme/oauth2/introspect"),
    ("POST", "/ran-nf-oam/dme-jobs"),
    ("POST", "/nfo/deployments"),
    ("PATCH", "/dme/data-jobs/j"),
    ("POST", "/sme/trusted-invokers/t/revoke"),
    ("GET", "/dme-push/anything"),
    ("GET", "/unknown/thing"),
    ("PATCH", "/rapp-mgmt/instances/i"),
])
def test_unlisted_routes_are_not_exposed_to_anyone(method, path):
    decision = decide(method, path, {}, Role.ADMIN)
    assert not decision.allowed and decision.required_role is None


def test_path_ids_cannot_span_segments():
    """An id placeholder matches one path segment, so extra segments cannot be used to reach a rule meant for a shorter path."""
    assert not allowed("POST", "/rapp-mgmt/instances/a/b/terminate", "admin")


def test_every_mutating_rule_requires_at_least_operator():
    """No rule that changes something is open to a viewer."""
    assert all(r.role != Role.VIEWER for r in RULES if r.method != "GET")


def test_spa_permissions_fixture_matches_the_live_table():
    """smo/gui's Vitest suite checks the SPA's evaluator against this
    snapshot of the table; it must be the real one."""
    assert json.loads(FIXTURE.read_text()) == export(), "stale fixture: run scripts/export_permissions.py"


def test_the_gui_cannot_stop_an_rapp_as_somebody_else():
    """The kill request is forced to carry the signed-in user as `requestedBy`, so an operator cannot stop an rApp in someone else's name.
    """
    decision = decide("PUT", "/rapp-mgmt/instances/i/kill", {}, Role.OPERATOR)
    assert decision.allowed and decision.rule.json_overrides(type("U", (), {"username": "alice", "role": Role.OPERATOR})()) == {"requestedBy": "smo-gui:alice"}


# Each row is a change-job action; every one is attributed to `smo-gui:<user>`, and a rollback carries the MSAC admin tier only for an admin.
@pytest.mark.parametrize("path", ["/ran-nf-oam/config-jobs/j/rollback", "/ran-nf-oam/config-jobs/j/continue", "/ran-nf-oam/config-jobs/j/halt", "/ran-nf-oam/config-jobs/j/abort"])
def test_a_job_action_is_always_attributed_to_the_gui_user_and_an_admin_holds_the_msac_tier(path):
    rule = decide("POST", path, {}, Role.OPERATOR).rule
    user = lambda role: type("U", (), {"username": "alice", "role": role})()  # noqa: E731
    assert rule.json_overrides(user(Role.OPERATOR))["requestedBy"] == "smo-gui:alice"
    if path.endswith("rollback"):
        assert rule.json_overrides(user(Role.OPERATOR))["msacRole"] is None and rule.json_overrides(user(Role.ADMIN))["msacRole"] == "admin"


def test_kpi_publish_and_the_sweep_are_not_exposed_to_the_gui():
    """Publishing a KPI and the staged-job sweep are the worker's; only the KPI check of one job is a GUI action (GUI-9.7)."""
    for path in ("/ran-nf-oam/kpis/k/publish", "/ran-nf-oam/config-jobs/advance-due"):
        assert not decide("POST", path, {}, Role.ADMIN).allowed


def test_the_global_stop_is_attributed_to_the_gui_user():
    """GUI-9.6: `PUT /rapp-mgmt/kill-all` records the signed-in user as `requestedBy`, whatever the browser sent, like the per-instance kill."""
    decision = decide("PUT", "/rapp-mgmt/kill-all", {}, Role.OPERATOR)
    assert decision.allowed and decision.rule.json_overrides(type("U", (), {"username": "alice", "role": Role.OPERATOR})()) == {"requestedBy": "smo-gui:alice"}


def test_an_alarm_comment_is_an_operators_and_attributed_to_the_gui_user():
    """MGT-8.3 / GUI-2.3: commenting on an alarm needs an operator, and its `author` is the signed-in user (as an ack is), never the browser's."""
    assert not decide("POST", "/ran-nf-oam/alarms/a-1/comments", {}, Role.VIEWER).allowed
    decision = decide("POST", "/ran-nf-oam/alarms/a-1/comments", {}, Role.OPERATOR)
    assert decision.allowed and decision.rule.json_overrides(type("U", (), {"username": "ana", "role": Role.OPERATOR})()) == {"author": "ana"}
    assert decide("GET", "/ran-nf-oam/alarms/a-1/comments", {}, Role.VIEWER).allowed and decide("GET", "/ran-nf-oam/alarms/a-1/history", {}, Role.VIEWER).allowed


def test_a_host_key_pin_is_attributed_to_the_gui_user():
    """GUI-9.7 / STD-4.6: who re-pinned an SSH host key is the signed-in admin, never a `pinnedBy` the browser chose."""
    rule = decide("PUT", "/ran-nf-oam/o1-adaptor-endpoints/e/host-keys", {}, Role.ADMIN).rule
    assert rule.json_overrides(type("U", (), {"username": "root", "role": Role.ADMIN})()) == {"pinnedBy": "smo-gui:root"}


# MGT-4.3: the decider of a change window is the signed-in user, whatever the browser sent (RAN NF OAM also takes it from X-R1-Acting-User).
def test_a_change_window_decision_carries_the_signed_in_user():
    """`POST /ran-nf-oam/config-jobs/{id}/approve` forces `decidedBy` to `smo-gui:<user>`."""
    decision = decide("POST", "/ran-nf-oam/config-jobs/j/approve", {}, Role.OPERATOR)
    assert decision.allowed and decision.rule.json_overrides(type("U", (), {"username": "alice", "role": Role.OPERATOR})()) == {"decidedBy": "smo-gui:alice"}
