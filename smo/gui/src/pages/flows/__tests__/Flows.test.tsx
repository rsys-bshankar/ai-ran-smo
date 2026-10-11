// @vitest-environment jsdom
/** Tests of the Lifecycle flows page (`pages/flows`): routing (/flows, /flows/:flowId, an old #NN hash), the catalogue links, loading only the
 * selected flow's sources for the chosen subject, the subject combobox (typeahead, `?subject=`, last-used memory), the sequence lanes, the
 * step timeline with its actions, and the fleet funnel built from summary counts or hidden with a gap note. Run: `npx vitest run src/pages/flows`. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import { ENGLISH } from "../../../lib/domain";
import { FLOWS, type FlowStep } from "../../../lib/flows";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, settle, type } from "../../../testing/dom";
import { Flows, resolveFlowId } from "..";
import { filterSubjects, readRecent, rememberRecent } from "../data/subjects";
import { lanesOf, stepActors } from "../sections/SequenceLanes";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.history.replaceState(null, "", "/"); try { window.localStorage.clear(); } catch { /* none */ } });

const A = "aaaaaaaa-0000-0000-0000-000000000001";
const B = "bbbbbbbb-0000-0000-0000-000000000002";
const ALL = ["GET", "POST", "PUT", "DELETE"].map((method) => ({ method, pattern: ".*", role: method === "GET" ? "viewer" : "operator", queryMatch: {} }));
const inst = (id: string, state: string) => ({ instanceId: id, packageId: "p1", state, autonomyMode: "SHADOW" });

/** A fake BFF with two rApp instances, the rApps summary and one FAILED software job; `extra` adds or overrides routes. */
function bff(extra: Record<string, unknown> = {}) {
  return fakeBff({
    "GET /me": { username: "ana", role: "operator", csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false }, "GET /permissions": { role: "operator", rules: ALL },
    "GET /smo/rapp-mgmt/instances": { items: [inst(A, "RUNNING"), inst(B, "FAULTED")], total: 2, limit: 500, offset: 0 },
    [`GET /smo/rapp-mgmt/instances/${A}`]: { ...inst(A, "RUNNING"), workloadRef: null, configuration: {}, pendingUpgradeInstanceId: null, smeServiceIds: null, regionScope: null },
    [`GET /smo/rapp-mgmt/instances/${B}`]: { ...inst(B, "FAULTED"), workloadRef: null, configuration: {}, pendingUpgradeInstanceId: null, smeServiceIds: null, regionScope: null },
    "GET /smo/rapp-mgmt/instances/*": { items: [] },
    "GET /summary/rapps": { page: "rapps", computedAt: "t", partial: [], counts: { "instances.total": 487, "instances.RUNNING": 471, "instances.FAULTED": 6, "instances.DEPLOYING": 0, "instances.UPGRADING": 10, "instances.UNDEPLOYED": 0 } },
    "GET /smo/ran-nf-oam/software-management-jobs": { items: [{ jobId: "j1", managedElementRef: "ME-1", ruInstanceId: null, phase: "INSTALL", status: "FAILED" }], total: 1, limit: 500, offset: 0 },
    "GET /smo/onboarding/packages": { items: [], total: 0, limit: 500, offset: 0 },
    ...extra,
  });
}
const open = (at: string, route = "/flows/:flowId") => mountWith(<AuthProvider><Flows /></AuthProvider>, { at, route });
const modules = (calls: Call[]) => new Set(calls.filter((c) => c.path.startsWith("/smo/")).map((c) => c.path.split("/")[2]));

describe("Lifecycle flows page", () => {
  // /flows with no id shows the first flow, and every catalogue entry links to its own route
  it("defaults to the first flow and links every flow", async () => {
    bff();
    const { container } = await open("/flows", "/flows");
    await settle(6);
    expect(container.querySelector("[data-section='flows.board']")!.textContent).toContain(`Flow 01 · ${ENGLISH(FLOWS[0].title)}`);
    expect(Array.from(container.querySelectorAll(".flow-list a")).map((a) => a.getAttribute("href"))).toEqual(FLOWS.map((f) => `/flows/${f.id}`));
    expect(container.textContent).toContain("No package to follow yet.");
  });

  // an old /flows#07 link still opens flow 07
  it("reads an old hash link", () => {
    expect(resolveFlowId(undefined, "#07")).toBe("07");
    expect(resolveFlowId("19", "#07")).toBe("19");
    expect(resolveFlowId("99", "")).toBe("01");
  });

  // only flow 07's own module is read, for the subject in ?subject=, and its steps, lanes and actions render
  it("loads only the selected flow's sources for the chosen subject", async () => {
    const calls = bff();
    const { container } = await open(`/flows/07?subject=${B}`);
    await settle(8);
    expect([...modules(calls)]).toEqual(["rapp-mgmt"]);
    expect(calls.some((c) => c.path === `/smo/rapp-mgmt/instances/${B}`)).toBe(true);
    expect(calls.some((c) => c.path === `/smo/rapp-mgmt/instances/${A}`)).toBe(false);
    expect(container.textContent).toContain(`following ${B.slice(0, 8)} (FAULTED)`);
    expect(container.querySelectorAll(".lanes svg .lane-name").length).toBeGreaterThan(1);
    expect(container.querySelector(".lanes .lane-call.now")).not.toBeNull();
    expect(container.querySelectorAll("[data-section='flows.steps'] .tl-i").length).toBe(7);
    expect(byText(container, ".flow-list a.active", /rApp instance lifecycle/)?.textContent).toMatch(/\d+\/7/);
  });

  // typing filters the subjects; choosing one follows it and remembers it as recent
  it("follows a subject chosen in the combobox", async () => {
    const calls = bff();
    const { container } = await open("/flows/07");
    await settle(8);
    expect(calls.some((c) => c.path === `/smo/rapp-mgmt/instances/${B}`)).toBe(true);   // newest (last) by default
    const input = container.querySelector("input[role=combobox]") as HTMLInputElement;
    await type(input, "aaaa");
    const options = Array.from(container.querySelectorAll("[role=option]"));
    expect(options).toHaveLength(1);
    options[0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    await settle(8);
    expect(calls.some((c) => c.path === `/smo/rapp-mgmt/instances/${A}`)).toBe(true);
    expect(container.textContent).toContain(`following ${A.slice(0, 8)} (RUNNING)`);
    expect(readRecent("07")[0]).toBe(A);
  });

  // flow 07's funnel is the summary's instance-state counts
  it("builds flow 07's funnel from the summary", async () => {
    bff();
    const { container } = await open("/flows/07");
    await settle(8);
    const funnel = container.querySelector("[data-section='flows.funnel']")!;
    expect(funnel.textContent).toContain("Fleet funnel · 487 instances");
    expect(funnel.textContent).toContain("Faulted (recover)");
    expect(funnel.querySelectorAll(".funnel-row")).toHaveLength(5);
  });

  // flow 19 has no state counts: the funnel hides with a gap note, and a failed job offers a retry as a new job
  it("shows flow 19 with a failed phase, its retry, and the funnel gap", async () => {
    const calls = bff({ "POST /smo/ran-nf-oam/software-management-jobs": { jobId: "j2" } });
    const { container } = await open("/flows/19");
    await settle(8);
    expect(container.querySelector("[data-section='flows.funnel'] .gap-note")).not.toBeNull();
    expect(calls.some((c) => c.path.startsWith("/summary"))).toBe(false);
    expect(container.querySelector("[data-section='flows.steps'] .tl-i.fail")?.textContent).toContain("software-install");
    await click(byText(container, "button", "Retry from DOWNLOAD (new job)")!);
    await settle();
    expect(calls.find((c) => c.method === "POST")?.query.get("managed_element_ref")).toBe("ME-1");
  });
});

describe("flows helpers", () => {
  // an actor chain splits on arrows, and the lanes keep first-appearance order
  it("builds the lanes from the step actors", () => {
    expect(stepActors("Caller → NFO ← DMS")).toEqual(["Caller", "NFO", "DMS"]);
    const steps = [{ actor: "Operator → rApp Mgmt" }, { actor: "rApp Mgmt → NFO" }, { actor: "NFO" }] as FlowStep[];
    expect(lanesOf(steps)).toEqual(["Operator", "rApp Mgmt", "NFO"]);
  });
  // recent subjects rank first and the memory keeps five, newest first
  it("ranks recent subjects and keeps five", () => {
    for (const id of ["1", "2", "3", "4", "5", "6"]) rememberRecent("01", id);
    expect(readRecent("01")).toEqual(["6", "5", "4", "3", "2"]);
    const subs = ["1", "2", "6"].map((id) => ({ id, label: `pkg ${id}` }));
    expect(filterSubjects(subs, "pkg", readRecent("01")).map((s) => s.id)).toEqual(["6", "2", "1"]);
  });
});
