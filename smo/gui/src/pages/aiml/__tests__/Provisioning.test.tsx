// @vitest-environment jsdom
/** Tests of the AI/ML page's TS 28.105 tab (GUI-11.2, call flow 27): the seven provisioning resources list for every role, a row opens its attributes,
 * the write buttons follow the BFF's rules (requests and training processes: operator; inference functions and loading policies: admin; nothing for a
 * viewer), and the forms and controls send the request AIMgF expects. `fetch` is stubbed by `testing/bff.tsx` `fakeBff` with the permission table of
 * `auth/permissions.fixture.json`; no BFF runs. Run: `npx vitest run src/pages/aiml`. */
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import rules from "../../../auth/permissions.fixture.json";
import { fakeBff, mountWith } from "../../../testing/bff";
import { byText, cleanup, click, field, pick, settle } from "../../../testing/dom";
import { Aiml } from "../index";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = "#provisioning"; });

const M = "aaaaaaaa-0000-4000-8000-000000000001";
const F = "ffffffff-0000-4000-8000-00000000000f";
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 500, offset: 0 });
const nrm = (id: string, attributes: Record<string, unknown>) => ({ id, attributes });

/** The fake BFF for one role, with one object of each resource. */
function bff(role: "viewer" | "operator" | "admin") {
  return fakeBff({
    "GET /me": { username: "ana", role, csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role, rules },
    "GET /summary/aiml": { page: "aiml", computedAt: "now", counts: {}, partial: [] },
    "GET /smo/aimgf/models": page([{ modelId: M, modelType: "coverage-model", version: "1.0", owner: "es-client", targetEnvironments: [] }]),
    "GET /smo/aimgf/ml-training-requests": page([nrm("tr-1", { mLModelRef: M, trainingRequestSource: "es-client", mLTrainingType: "RE_TRAINING", requestStatus: "IN_PROGRESS" })]),
    "GET /smo/aimgf/ml-training-processes": page([nrm("tp-1", { trainingRequestRef: ["tr-1"], mLModelRef: M, progressStatus: { status: "RUNNING", progressPercentage: 40 } })]),
    "GET /smo/aimgf/aiml-inference-functions": page([nrm(F, { aIMLInferenceName: "coverage-fn", userLabel: "lab", activationStatus: "DEACTIVATED", mLModelRefList: [] })]),
    "GET /smo/aimgf/ml-model-loading-policies": page([nrm("lp-1", { aIMLInferenceFunctionRef: F, aIMLInferenceName: "nightly", mLModelRef: [M] })]),
    "POST /smo/aimgf/ml-training-requests": { status: 201, body: nrm("tr-2", { requestStatus: "IN_PROGRESS" }) },
    "POST /smo/aimgf/ml-model-loading-requests": { status: 201, body: nrm("lr-1", { requestStatus: "FINISHED" }) },
    "PATCH /smo/aimgf/ml-training-requests/tr-1": nrm("tr-1", { requestStatus: "SUSPENDED" }),
    "PATCH /smo/aimgf/ml-training-processes/tp-1": nrm("tp-1", {}),
    [`PATCH /smo/aimgf/aiml-inference-functions/${F}`]: nrm(F, { activationStatus: "ACTIVATED" }),
    "POST /smo/aimgf/ml-model-loading-policies/lp-1/trigger": { status: 201, body: nrm("lpr-1", {}) },
  });
}

const open = async () => { const m = await mountWith(<AuthProvider><Aiml /></AuthProvider>); await settle(8); return m; };
const section = (root: HTMLElement, kind: string) => root.querySelector(`[data-section="aiml.nrm.${kind}"]`) as HTMLElement;
const show = async (root: HTMLElement, label: string) => { await click(byText(root, "[role=radio]", label)!); await settle(6); };

describe("the TS 28.105 tab, read-only for a viewer", () => {
  // A viewer lists the training requests and the inference functions and opens a row's attributes, and is offered no write at all.
  it("lists the resources and opens a row without any write button", async () => {
    bff("viewer");
    const { container } = await open();
    const requests = section(container, "trainingRequests");
    expect(requests.textContent).toContain("coverage-model 1.0");
    expect(requests.textContent).toContain("IN_PROGRESS");
    expect(byText(container, "button", "New training request")).toBeNull();
    expect(byText(requests, "button", "Suspend")).toBeNull();
    await click(byText(requests, "tr", /RE_TRAINING/)!);
    const drawer = document.querySelector("[role=dialog]") as HTMLElement;
    expect(drawer.textContent).toContain("trainingRequestSource");
    expect(drawer.textContent).toContain("es-client");
    await show(container, "Inference functions");
    expect(section(container, "inferenceFunctions").textContent).toContain("coverage-fn");
    expect(byText(container, "button", "Activate")).toBeNull();
    expect(byText(container, "button", "New inference function")).toBeNull();
  });
});

describe("an operator's writes", () => {
  // The operator creates a training request for a model: the console's source and the chosen model go to AIMgF's NRM route.
  it("creates a training request", async () => {
    const calls = bff("operator");
    const { container } = await open();
    await click(byText(container, "button", "New training request")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    const create = byText<HTMLButtonElement>(dialog, "button", "Create")!;
    expect(create.disabled).toBe(true);
    await pick(field<HTMLSelectElement>(dialog, "Model"), M);
    await click(create);
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/smo/aimgf/ml-training-requests");
    expect(post.body).toEqual({ mLModelRef: M, trainingRequestSource: "smo-gui", mLTrainingType: null });
  });

  // Suspend on a running request and on a running process sends the spec's PATCH flag of each (suspendRequest, suspendProcess).
  it("suspends a request and a process through their PATCH flags", async () => {
    const calls = bff("operator");
    const { container } = await open();
    await click(byText(section(container, "trainingRequests"), "button", "Suspend")!);
    await show(container, "Training processes");
    const processes = section(container, "trainingProcesses");
    expect(processes.textContent).toContain("40 %");
    await click(byText(processes, "button", "Suspend")!);
    await settle();
    expect(calls.filter((c) => c.method === "PATCH").map((c) => [c.path, c.body])).toEqual([
      ["/smo/aimgf/ml-training-requests/tr-1", { suspendRequest: true }],
      ["/smo/aimgf/ml-training-processes/tp-1", { suspendProcess: true }],
    ]);
  });

  // A loading request names an inference function and one or more models; the operator sees no admin-only write on the functions and policies.
  it("creates a loading request and is offered no admin write", async () => {
    const calls = bff("operator");
    const { container } = await open();
    await show(container, "Loading requests");
    await click(byText(container, "button", "New loading request")!);
    await settle(4);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    await pick(field<HTMLSelectElement>(dialog, "Inference function"), F);
    const models = field<HTMLSelectElement>(dialog, "Models");
    await act(async () => { models.options[0].selected = true; models.dispatchEvent(new Event("change", { bubbles: true })); });
    await click(byText(dialog, "button", "Create")!);
    await settle();
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ aIMLInferenceFunctionRef: F, mLModelToLoadRef: [M] });
    expect(document.querySelector("[role=dialog]")).toBeNull();          // closed on success
    await show(container, "Inference functions");
    expect(section(container, "inferenceFunctions").textContent).toContain("coverage-fn");
    expect(byText(container, "button", "Activate")).toBeNull();
    await show(container, "Loading policies");
    expect(byText(container, "button", "Trigger")).toBeNull();
    expect(byText(container, "button", "New loading policy")).toBeNull();
  });
});

describe("an admin's writes", () => {
  // An admin activates an inference function and triggers a loading policy, each through its own route.
  it("activates a function and triggers a policy", async () => {
    vi.stubGlobal("confirm", () => true);
    const calls = bff("admin");
    const { container } = await open();
    await show(container, "Inference functions");
    expect(byText(container, "button", "New inference function")).not.toBeNull();
    await click(byText(section(container, "inferenceFunctions"), "button", "Activate")!);
    await show(container, "Loading policies");
    await click(byText(section(container, "loadingPolicies"), "button", "Trigger")!);
    await settle();
    const writes = calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.path, c.body]);
    expect(writes).toEqual([
      ["PATCH", `/smo/aimgf/aiml-inference-functions/${F}`, { activationStatus: "ACTIVATED" }],
      ["POST", "/smo/aimgf/ml-model-loading-policies/lp-1/trigger", undefined],
    ]);
  });
});
