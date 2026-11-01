/**
 * Tests of the permission evaluator in `rbac.ts` against the BFF's real permission table (the JSON snapshot `permissions.fixture.json`, exported by
 * gui-bff/scripts/export_permissions.py). Pure functions, no DOM. Run: `npx vitest run src/auth/rbac.test.ts` from `smo/gui`.
 */

import { describe, expect, it } from "vitest";

import fixture from "./permissions.fixture.json";
import { can, requiredRole, roleAtLeast, type PermissionRule, type Role } from "./rbac";

// The BFF's real table (gui-bff/scripts/export_permissions.py; the BFF's own
// test_rbac.py fails if this snapshot drifts). Evaluating it here proves the
// Python regexes — re.escape output included — behave the same in JS.
const RULES = fixture as PermissionRule[];

describe("requiredRole against the BFF's table", () => {
  // Table: method, path, expected minimum role; one row per representative route (viewer reads, operator writes, admin-only actions).
  it.each<[string, string, Role]>([
    ["GET", "/rapp-mgmt/instances", "viewer"],
    ["GET", "/ran-nf-oam/alarms", "viewer"],
    ["GET", "/aimgf/models/abc/artifact/2", "viewer"],
    ["POST", "/onboarding/packages", "operator"],
    ["POST", "/aimgf/training-jobs", "operator"],
    ["PATCH", "/ran-nf-oam/alarms/a1/ack", "operator"],
    ["PATCH", "/ran-nf-oam/alarms/a1/clear", "operator"],
    ["POST", "/sa-smos/monitors/m1/evaluate", "operator"],
    ["POST", "/rapp-mgmt/instances/i1/terminate", "admin"],
    ["DELETE", "/onboarding/packages/p1", "admin"],
    ["DELETE", "/nfo/deployments/d1", "admin"],
    ["GET", "/aimgf/feature-groups", "operator"],
  ])("%s %s needs %s", (method, path, role) => {
    expect(requiredRole(RULES, method, path)).toBe(role);
  });

  // A route the BFF does not expose to the GUI must read as "no role allows it" (null), never as viewer.
  it("returns null for routes the GUI does not expose", () => {
    expect(requiredRole(RULES, "POST", "/sme/oauth2/token")).toBeNull();
    expect(requiredRole(RULES, "POST", "/nfo/deployments")).toBeNull();
    expect(requiredRole(RULES, "GET", "/dme-push/x")).toBeNull();
    expect(requiredRole(RULES, "GET", "/not-a-module")).toBeNull();
  });

  // A path pattern's id placeholder must not match across "/": a crafted path must not borrow another route's rule.
  it("does not let an id span path segments", () => {
    expect(requiredRole(RULES, "POST", "/rapp-mgmt/instances/a/b/terminate")).toBeNull();
  });

  // A rule can depend on a query parameter (the advance event): DEPRECATE and the governance decisions need admin, and a parameter given several values matches a rule when any one of them equals the rule's.
  it("applies query matches: DEPRECATE and governance decisions are admin-only, other advances are operator", () => {
    expect(requiredRole(RULES, "POST", "/aimgf/models/m/advance", { event: "APPROVE_TRAINING" })).toBe("operator");
    expect(requiredRole(RULES, "POST", "/aimgf/training-jobs/j/complete")).toBe("operator");
    expect(requiredRole(RULES, "POST", "/aimgf/models/m/advance", { event: "DEPRECATE" })).toBe("admin");
    expect(requiredRole(RULES, "POST", "/aimgf/models/m/advance", { event: "CERTIFY" })).toBe("admin");
    expect(requiredRole(RULES, "POST", "/aimgf/models/m/advance", { event: ["APPROVE_TRAINING", "DEPRECATE"] })).toBe("admin");
  });
});

describe("can", () => {
  // A call is allowed only when the role ranks at least the rule's role.
  it("gates by role rank", () => {
    const terminate = ["POST", "/rapp-mgmt/instances/i/terminate"] as const;
    expect(can(RULES, "operator", ...terminate)).toBe(false);
    expect(can(RULES, "admin", ...terminate)).toBe(true);
    expect(can(RULES, "viewer", "POST", "/aimgf/training-jobs")).toBe(false);
    expect(can(RULES, "operator", "POST", "/aimgf/training-jobs")).toBe(true);
    expect(can(RULES, "viewer", "GET", "/nfo/deployments")).toBe(true);
  });

  // No role, or a route outside the table, denies even an admin.
  it("denies everything without a role or for unexposed routes", () => {
    expect(can(RULES, undefined, "GET", "/nfo/deployments")).toBe(false);
    expect(can(RULES, "admin", "POST", "/sme/oauth2/token")).toBe(false);
  });

  // The first matching rule decides, so a specific rule placed before a general one must win, exactly as in the BFF.
  it("is first-match-wins, like the BFF", () => {
    const rules: PermissionRule[] = [
      { method: "GET", pattern: "^/x/secret$", role: "admin", queryMatch: {} },
      { method: "GET", pattern: "^/x/.*$", role: "viewer", queryMatch: {} },
    ];
    expect(can(rules, "viewer", "GET", "/x/secret")).toBe(false);
    expect(can(rules, "viewer", "GET", "/x/other")).toBe(true);
  });

  // The role order is viewer < operator < admin.
  it("orders roles viewer < operator < admin", () => {
    expect(roleAtLeast("admin", "operator")).toBe(true);
    expect(roleAtLeast("operator", "admin")).toBe(false);
    expect(roleAtLeast("viewer", "viewer")).toBe(true);
  });
});
