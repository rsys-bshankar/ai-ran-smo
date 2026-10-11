// @vitest-environment jsdom
/** Tests of the Approvals page (pages/approvals): the waiting queue (cards with the lapse countdown), the detail panel (impact tiles, the config
 * diff, why the rApp asks, the decision box and its result), RBAC, the Decided tab with its drawer, and the opt-in two-person approval (the
 * progress on a queue card, the approvals so far, no second approval by the same person, both approvers in the decided list), the model gates (GUI-7.3)
 * and the CM jobs held for a change window (GUI-7.1). Run: `npx vitest run src/pages/approvals`. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import rules from "../../../auth/permissions.fixture.json";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, settle, type } from "../../../testing/dom";
import { Approvals } from "..";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = ""; });

const AID = "6f1b8e0a-1111-4a2b-9c3d-aaaaaaaaaaaa";
const JOB = "7a2c9f1b-2222-4b3c-8d4e-bbbbbbbbbbbb";
const soon = () => new Date(Date.now() + 25 * 60_000).toISOString();

const approval = (extra: Record<string, unknown> = {}) => ({
  approvalId: AID, invokerId: "api-invoker-0a1b2c3d-4e5f", requestedBy: "energy-saving", status: "PENDING", managedElements: ["ME-1", "ME-2"], changeCount: 2,
  createdAt: new Date().toISOString(), expiresAt: soon(), onTimeout: "EXPIRE", decidedBy: null, decidedAt: null, decisionReason: null, jobId: null, refusalCode: null,
  correlationId: null, decision: { rationale: "PRB use under 5 percent for an hour", modelVersion: "energy-saving 1.4.2", inputsRef: "dme://data-jobs/42", actionId: "act-1" }, ...extra,
});
const detail = (extra: Record<string, unknown> = {}) => ({
  ...approval(extra), accessScope: "cell",
  changes: [{ managedElementRef: "ME-1", managedFunctionRef: "NRCellDU=101", operation: "merge", attributeChanges: { txPower: 20 } }, { managedElementRef: "ME-2", attributeChanges: { txPower: 20 } }],
});

/**
 * Starts the fake BFF for a user of `role` (the permission table is the real one) with one waiting request, one rejected request and the request's detail; `overrides` adds or replaces routes.
 */
function bff(role: "viewer" | "operator" | "admin", overrides: Record<string, unknown> = {}) {
  return fakeBff({
    "GET /me": { username: "ana", role, csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role, rules },
    "GET /smo/ran-nf-oam/rapp-approvals": (c: Call) => ({ items: c.query.get("status") === "PENDING" ? [approval()] : [approval(), approval({ approvalId: "done-1", status: "REJECTED", decidedBy: "smo-gui:bob", decisionReason: "not in this window" })], limit: 100, offset: 0, hasMore: false }),
    [`GET /smo/ran-nf-oam/rapp-approvals/${AID}`]: { body: detail() },
    "GET /smo/ran-nf-oam/decision-records": { items: [], limit: 1, offset: 0 },
    "GET /summary/approvals": { page: "approvals", computedAt: "", counts: { "approvals.PENDING": 1 }, partial: [] },
    ...overrides,
  });
}

const open = () => mountWith(<AuthProvider><Approvals /></AuthProvider>, { at: "/approvals" });
const detail$ = () => document.querySelector("[data-section='approvals.detail']") as HTMLElement;

describe("the approval inbox", () => {
  // The waiting list shows the rApp, the changes, the rationale and when each request lapses.
  it("lists what is waiting with the rApp, the changes, why, and when it lapses", async () => {
    const calls = bff("operator");
    const { container } = await open();
    await settle();
    const row = container.querySelector("[data-approval]")!;
    expect(row.textContent).toContain("energy-saving");
    expect(row.textContent).toContain("2 on ME-1, ME-2");
    expect(row.textContent).toContain("PRB use under 5 percent for an hour");
    expect(row.textContent).toMatch(/in 2\d min/);
    expect(row.textContent).toContain("expires");
    const asked = calls.find((c) => c.path === "/smo/ran-nf-oam/rapp-approvals")!;
    expect(asked.query.get("status")).toBe("PENDING");
    expect(asked.query.get("total")).toBe("false");                      // the inbox does not ask for a count it does not show
    expect(container.querySelector("[role=tab] .n")?.textContent?.trim()).toBe("1");          // the pending count comes from the summary
    expect(container.textContent).toContain("A CM job appears under Change windows");     // the note names the three kinds of request now in the inbox
  });

  // An empty queue says nothing is waiting.
  it("says when nothing waits", async () => {
    bff("operator", { "GET /smo/ran-nf-oam/rapp-approvals": { items: [], limit: 100, offset: 0, hasMore: false } });
    const { container } = await open();
    await settle();
    expect(container.textContent).toContain("Nothing is waiting for a decision.");
  });

  // Before anyone decides, the drawer shows what the request would write and why, and approving is recorded under the user's name.
  it("shows what a request would write and why before anyone decides, and approves under the user's name", async () => {
    const calls = bff("operator", { [`POST /smo/ran-nf-oam/rapp-approvals/${AID}/approve`]: { body: { ...detail({ status: "APPROVED", jobId: JOB, decidedBy: "smo-gui:ana" }), jobStatus: "COMPLETED" } } });
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    const drawer = detail$();
    expect(drawer.textContent).toContain("energy-saving 1.4.2");
    expect(drawer.textContent).toContain("dme://data-jobs/42");
    const lines = Array.from(drawer.querySelectorAll("[aria-label=Changes] > div")).map((d) => d.textContent);
    expect(lines).toEqual(["ME-1 / NRCellDU=101 · merge", "+ txPower: 20", "ME-2 · merge", "+ txPower: 20"]);
    await type(drawer.querySelector("input") as HTMLInputElement, "checked with the night plan");
    await click(byText(drawer, "button", "Approve & write")!);
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/smo/ran-nf-oam/rapp-approvals/${AID}/approve`);
    expect(post.body).toEqual({ reason: "checked with the night plan" });         // who decided is not sent: the BFF pins it to the signed-in user
    expect(detail$().querySelector("[data-section='approvals.decide']")?.textContent).toContain("APPROVED");    // the result is shown
    expect(detail$().querySelector("[data-section='approvals.decide']")?.textContent).toContain("COMPLETED");
  });

  // A rejection sends the reason and nothing is written.
  it("rejects with a reason and writes nothing", async () => {
    const calls = bff("operator", { [`POST /smo/ran-nf-oam/rapp-approvals/${AID}/reject`]: { body: detail({ status: "REJECTED" }) } });
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    await click(byText(detail$(), "button", "Reject")!);
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/smo/ran-nf-oam/rapp-approvals/${AID}/reject`);
    expect(post.body).toEqual({ reason: null });
  });

  // A viewer can read a request but is offered no way to decide it.
  it("shows a viewer the request and no way to decide it", async () => {
    bff("viewer");
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    const drawer = detail$();
    expect(drawer.textContent).toContain("Why the rApp asks");
    expect(byText(drawer, "button", "Approve & write")).toBeNull();
    expect(byText(drawer as HTMLElement, "button", "Reject")).toBeNull();
  });

  // A decided request shows who decided it, the job it became and a link to its decision record.
  it("shows a decided request with who decided, the job it became and a link to its decision record", async () => {
    bff("operator", {
      "GET /smo/ran-nf-oam/rapp-approvals": { items: [approval({ approvalId: "pending-1" }), approval({ status: "APPROVED", jobId: JOB, decidedBy: "smo-gui:bob", decisionReason: "not in this window" })], limit: 100, offset: 0, hasMore: false },
      [`GET /smo/ran-nf-oam/rapp-approvals/${AID}`]: { body: detail({ status: "APPROVED", jobId: JOB, decidedBy: "smo-gui:bob", decidedAt: new Date().toISOString(), decisionReason: "looks right" }) },
      "GET /smo/ran-nf-oam/decision-records": { items: [{ decisionId: "d-1", disposition: "APPROVED" }], limit: 1, offset: 0 },
      [`GET /smo/ran-nf-oam/config-jobs/${JOB}`]: { body: { jobId: JOB, status: "COMPLETED", subChanges: [] } },
    });
    const { container } = await open();
    await settle();
    await click(byText(container, "[role=tab]", "Decided")!);
    await settle();
    const rows = Array.from(container.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(1);                                        // the pending one is not in this list
    expect(rows[0].textContent).toContain("APPROVED");
    expect(rows[0].textContent).toContain("smo-gui:bob");
    expect(rows[0].textContent).toContain("not in this window");
    await click(rows[0] as HTMLElement);
    await settle();
    const drawer = document.querySelector("[role=dialog]")!;
    expect(drawer.querySelector("a")?.getAttribute("href")).toBe("/decisions/d-1");
    expect(drawer.textContent).toContain("Approved: the config job was made from the request");
    expect(byText(drawer as HTMLElement, "button", "Approve & write")).toBeNull();      // decided once
  });

  // A request that was approved but then refused shows the refusal code it was closed with.
  it("names the code a refused request was closed with", async () => {
    bff("operator", {
      "GET /smo/ran-nf-oam/rapp-approvals": { items: [approval({ status: "REFUSED", refusalCode: "RAPP_KILLED", decidedBy: "smo-gui:bob" })], limit: 100, offset: 0, hasMore: false },
    });
    const { container } = await open();
    await settle();
    await click(byText(container, "[role=tab]", "Decided")!);
    await settle();
    expect(container.querySelector("tbody tr")!.textContent).toContain("RAPP_KILLED");
  });

  // Pins down: draws a lapse countdown bar on each queued request and a running clock in the detail.
  it("draws a lapse countdown bar on each queued request and a running clock in the detail", async () => {
    bff("operator");
    const { container } = await open();
    await settle();
    const bar = container.querySelector("[data-approval] .meter[role=img]");
    expect(bar?.getAttribute("aria-label")).toMatch(/% of its waiting time left/);
    expect(detail$().querySelector("[aria-label='Time left']")?.textContent).toMatch(/^2\d:\d\d$/);   // selected by default: the first request
    expect(detail$().textContent).toContain("Expected KPI impact is not shown");                       // no invented prediction
  });

  // Pins down: shows a failure to read the queue instead of an empty inbox.
  // A failed read of the queue is shown as an error, not as an empty inbox.
  it("shows a failure to read the queue instead of an empty inbox", async () => {
    bff("operator", { "GET /smo/ran-nf-oam/rapp-approvals": { status: 503, body: { title: "ENDPOINT_UNREACHABLE", detail: "RAN NF OAM is down" } } });
    const { container } = await open();
    await settle();
    expect(container.textContent).toContain("RAN NF OAM is down");
    expect(container.textContent).not.toContain("Nothing is waiting");
  });
});


describe("two-person approval (opt-in; a request that needs one approval looks as before)", () => {
  const vote = (by: string, reason: string | null = null) => ({ by, at: new Date().toISOString(), reason });

  // The queue card of a two-approval request shows its progress, and a request needing one, or from a RAN NF OAM that sends neither field, shows "one needed".
  it("shows how many approvals a waiting request has and needs, and nothing extra for a single approval", async () => {
    bff("operator", {
      "GET /smo/ran-nf-oam/rapp-approvals": { items: [approval({ approvalId: "two-1", requiredApprovals: 2, approvals: [vote("smo-gui:bob")] }), approval({ approvalId: "one-1", requiredApprovals: 1, approvals: [] }), approval({ approvalId: "old-1" })], limit: 100, offset: 0, hasMore: false },
    });
    const { container } = await open();
    await settle();
    const rows = Array.from(container.querySelectorAll("[data-approval]")).map((r) => r.textContent ?? "");
    expect(rows[0]).toContain("1 of 2 approvals");
    expect(rows[1]).toContain("one needed");
    expect(rows[2]).toContain("one needed");                              // a RAN NF OAM that predates the field sends neither key
  });

  // The detail panel lists the approvals so far; the first of two approvals is sent with a null reason and the page says another person must approve, not that the change is written.
  it("lists the approvals so far in the detail and sends the first of two approvals without claiming the change is written", async () => {
    const calls = bff("operator", {
      [`GET /smo/ran-nf-oam/rapp-approvals/${AID}`]: { body: detail({ requiredApprovals: 2, approvals: [] }) },
      [`POST /smo/ran-nf-oam/rapp-approvals/${AID}/approve`]: { body: { ...detail({ requiredApprovals: 2, approvals: [vote("smo-gui:ana")] }), jobStatus: null } },
    });
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    const drawer = detail$();
    expect(drawer.textContent).toContain("Approvals so far (0 of 2)");
    expect(drawer.textContent).toContain("first of two approvals");
    await click(byText(drawer, "button", "Approve & write")!);
    await settle();
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ reason: null });
    expect(document.body.textContent).toContain("Your approval is recorded: another person must approve before anything is written");
    expect(document.body.textContent).not.toContain("Approved: the change is being written");
  });

  // Someone who already approved sees the approval and its reason, has the Approve button disabled and a notice, and can still press Reject.
  it("shows who has approved and does not offer the same person a second approval (they may still reject)", async () => {
    bff("operator", { [`GET /smo/ran-nf-oam/rapp-approvals/${AID}`]: { body: detail({ requiredApprovals: 2, approvals: [vote("smo-gui:Ana", "fine by me")] }) } });   // the signed-in user is ana
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    const drawer = detail$();
    const lines = Array.from(drawer.querySelectorAll("ul[aria-label='Approvals so far'] li")).map((li) => li.textContent ?? "");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("smo-gui:Ana");
    expect(lines[0]).toContain("fine by me");
    expect((byText(drawer, "button", "Approve & write") as HTMLButtonElement).disabled).toBe(true);
    expect(drawer.textContent).toContain("You have approved this request");
    expect((byText(drawer, "button", "Reject") as HTMLButtonElement).disabled).toBe(false);
  });

  // The detail panel tells the second approver theirs is the last one needed, and after they approve the page says the change is being written.
  it("tells a second person that theirs is the last approval and says the change is written when they send it", async () => {
    const calls = bff("operator", {
      [`GET /smo/ran-nf-oam/rapp-approvals/${AID}`]: { body: detail({ requiredApprovals: 2, approvals: [vote("smo-gui:bob")] }) },
      [`POST /smo/ran-nf-oam/rapp-approvals/${AID}/approve`]: { body: { ...detail({ status: "APPROVED", jobId: JOB, requiredApprovals: 2, approvals: [vote("smo-gui:bob"), vote("smo-gui:ana")] }), jobStatus: "COMPLETED" } },
    });
    await open();
    await settle();
    await click(byText(document.body, "button", "Review…")!);
    await settle();
    const drawer = detail$();
    expect(drawer.textContent).toContain("Your approval is the last one needed");
    await click(byText(drawer, "button", "Approve & write")!);
    await settle();
    expect(calls.find((c) => c.method === "POST")!.path).toBe(`/smo/ran-nf-oam/rapp-approvals/${AID}/approve`);
    expect(document.body.textContent).toContain("Approved: the change is being written");
  });

  // The Decided tab shows both approvers of a two-approval request in its row.
  it("names both approvers in the decided list", async () => {
    bff("operator", {
      "GET /smo/ran-nf-oam/rapp-approvals": { items: [approval({ status: "APPROVED", decidedBy: "smo-gui:ana", requiredApprovals: 2, approvals: [vote("smo-gui:bob"), vote("smo-gui:ana")] })], limit: 100, offset: 0, hasMore: false },
    });
    const { container } = await open();
    await settle();
    await click(byText(container, "button", "Decided")!);
    await settle();
    expect(container.querySelector("tbody tr")!.textContent).toContain("smo-gui:bob, smo-gui:ana");
  });

  // GUI-7.3: the Model gates tab lists AIMgF's lifecycles waiting for a decision (asked with awaiting_decision=true), counts them from the
  // summary, says what is decided, and offers each role only the decisions the BFF lets it make (approve training: operator; approve: admin).
  it("lists the model gates with their decision, and the buttons each role may use", async () => {
    const gates = {
      "GET /summary/approvals": { page: "approvals", computedAt: "", counts: { "approvals.PENDING": 0, "modelGates.waiting": 2 }, partial: [] },
      "GET /smo/aimgf/model-lifecycles": { items: [
        { modelId: "m-1", modelLifecycleState: "TRAINED", runtimeLifecycleState: "NOT_DEPLOYED", trainingApproved: false, validationApproved: false, trainingJobId: null, clearedNodeGroups: [], nfDeploymentDescriptorId: null, nfDeploymentId: null },
        { modelId: "m-2", modelLifecycleState: "PENDING_APPROVAL", runtimeLifecycleState: "NOT_DEPLOYED", trainingApproved: true, validationApproved: true, trainingJobId: null, clearedNodeGroups: [], nfDeploymentDescriptorId: null, nfDeploymentId: null },
      ], total: 2, limit: 25, offset: 0 },
      "GET /smo/mlmr/models": { items: [{ modelId: "m-1", modelType: "coverage-model", version: "1.2" }, { modelId: "m-2", modelType: "es-model", version: "3.0" }], total: 2, limit: 200, offset: 0 },
    };
    const calls = bff("operator", gates);
    window.location.hash = "#models";
    const { container } = await open();
    await settle();
    expect(byText(container, "button", /Model gates/)!.textContent).toContain("2");
    const section = container.querySelector("[data-section='approvals.models']") as HTMLElement;
    expect(calls.find((c) => c.path === "/smo/aimgf/model-lifecycles")!.query.get("awaiting_decision")).toBe("true");
    const rows = Array.from(section.querySelectorAll("tbody tr"));
    expect(rows[0].textContent).toContain("coverage-model 1.2");
    expect(rows[0].textContent).toContain("Training finished: approve it before validation can start");
    expect(byText(rows[0] as HTMLElement, "button", "Approve training")).toBeTruthy();
    expect(rows[1].textContent).toContain("Submitted: approve or reject it");
    expect(byText(rows[1] as HTMLElement, "button", "Approve")).toBeFalsy();         // admin-only
    cleanup();
    bff("admin", gates);
    window.location.hash = "#models";
    const admin = await open();
    await settle();
    const adminRows = admin.container.querySelectorAll("[data-section='approvals.models'] tbody tr");
    expect(byText(adminRows[1] as HTMLElement, "button", "Approve")).toBeTruthy();
    expect(byText(adminRows[1] as HTMLElement, "button", "Reject")).toBeTruthy();
  });
});

describe("change windows (GUI-7.1)", () => {
  const job = (id: string, status: string, extra: Record<string, unknown> = {}) => ({
    jobId: id, requestedBy: "smo-gui:alice", scope: "cell", accessScope: "cell", status, msacRole: null, createdAt: "2026-10-11T08:00:00Z",
    scheduledAt: "2026-10-11T22:00:00Z", windowEnd: "2026-10-12T02:00:00Z", decidedBy: null, decidedAt: null, decisionReason: null, ...extra });
  const windows = {
    "GET /summary/approvals": { page: "approvals", computedAt: "", counts: { "approvals.PENDING": 0, "configJobs.PENDING_APPROVAL": 1 }, partial: [] },
    "GET /smo/ran-nf-oam/config-jobs": (c: Call) => ({ items: c.query.get("status") === "PENDING_APPROVAL" ? [job("job-wait", "PENDING_APPROVAL")]
      : [job("job-sched", "SCHEDULED", { decidedBy: "smo-gui:bob", decidedAt: "2026-10-11T09:00:00Z" })], total: 1, limit: 25, offset: 0 }),
    "POST /smo/ran-nf-oam/config-jobs/job-wait/approve": { status: 200, body: { jobId: "job-wait", status: "SCHEDULED" } },
    "POST /smo/ran-nf-oam/config-jobs/job-sched/continue": { status: 202, body: { jobId: "job-sched", status: "COMPLETED" } },
  };

  // The tab counts the held jobs from the summary, lists them with their window, and an operator approves one (the BFF names the decider).
  it("lists the jobs waiting with their window and approves one", async () => {
    const calls = bff("operator", windows);
    window.location.hash = "#windows";
    const { container } = await open();
    await settle();
    expect(byText(container, "button", /Change windows/)!.textContent).toContain("1");
    const waiting = container.querySelector("[data-section='approvals.windows']") as HTMLElement;
    expect(waiting.textContent).toContain("smo-gui:alice");
    expect(calls.some((c) => c.path === "/smo/ran-nf-oam/config-jobs" && c.query.get("status") === "PENDING_APPROVAL")).toBe(true);
    await click(byText(waiting, "button", "Approve")!);
    await settle();
    expect(calls.some((c) => c.method === "POST" && c.path === "/smo/ran-nf-oam/config-jobs/job-wait/approve")).toBe(true);
  });

  // An approved job waiting for its window shows who approved it and can be started; a viewer sees both lists and no button.
  it("starts a scheduled job, and shows a viewer no decision", async () => {
    const calls = bff("operator", windows);
    window.location.hash = "#windows";
    const { container } = await open();
    await settle();
    const scheduled = container.querySelector("[data-section='approvals.scheduled']") as HTMLElement;
    expect(scheduled.textContent).toContain("smo-gui:bob");
    await click(byText(scheduled, "button", "Start")!);
    await settle();
    expect(calls.some((c) => c.method === "POST" && c.path === "/smo/ran-nf-oam/config-jobs/job-sched/continue")).toBe(true);
    cleanup();
    bff("viewer", windows);
    window.location.hash = "#windows";
    const viewer = await open();
    await settle();
    expect(viewer.container.querySelector("[data-section='approvals.windows']")!.textContent).toContain("smo-gui:alice");
    expect(byText(viewer.container, "button", "Approve")).toBeFalsy();
    expect(byText(viewer.container, "button", "Start")).toBeFalsy();
  });
});
