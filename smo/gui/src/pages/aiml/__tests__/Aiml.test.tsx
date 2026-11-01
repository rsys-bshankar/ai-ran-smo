// @vitest-environment jsdom
/** Tests of the AI/ML page (pages/aiml): the stage board (columns, the server's stage counts, the red "under floor" outline), training progress
 * (epoch bar and ETA), the first-load call budget of the
 * Models tab, the model detail with its governance history and the admin-only roll back with a rationale (feature 7), Suspend / Resume on
 * training jobs (feature 7), the feature-group form (it sends `tokenRef`, never a token; SEC-15.2b), the Registry tab (feature 8), and that the pre-redesign tab ids still open their tab. `fetch` is stubbed by
 * `testing/bff.tsx` `fakeBff` with the permission table of `auth/permissions.fixture.json`; no BFF runs. Run: `npx vitest run src/pages/aiml`. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import rules from "../../../auth/permissions.fixture.json";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, field, settle, type } from "../../../testing/dom";
import { Aiml } from "../index";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = ""; });

const A = "aaaaaaaa-0000-4000-8000-000000000001";
const B = "bbbbbbbb-0000-4000-8000-000000000002";
const C = "cccccccc-0000-4000-8000-000000000003";
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 500, offset: 0 });
const model = (id: string, type: string) => ({ modelId: id, modelType: type, version: "1.0", artifactLocation: id === A ? `model-artifact:${A}:2` : null, description: `${type} use`, author: null, owner: id === B ? null : "ops", inputDataType: null, outputDataType: null, targetEnvironments: [] });
const life = (id: string, state: string, runtime = "NOT_DEPLOYED") => ({ modelId: id, modelLifecycleState: state, runtimeLifecycleState: runtime, trainingJobId: null, clearedNodeGroups: [], nfDeploymentDescriptorId: null, nfDeploymentId: null, trainingApproved: false, validationApproved: false });
const job = (id: string, status: string) => ({ epoch: id === J1 ? 7 : null, totalEpochs: id === J1 ? 20 : null, etaSeconds: id === J1 ? 252 : null, trainingJobId: id, modelId: C, modelCoordinationGroupId: null, producerId: "smo-gui", status, runId: null, trainingDataset: null, validationDataset: null, modelMetrics: null, nfDeploymentId: null, currentStep: "TRAINING", steps: { DATA_EXTRACTION: "FINISHED", TRAINING: "RUNNING", TRAINED_MODEL: "NOT_STARTED" } });
const J1 = "11111111-1111-4111-8111-111111111111";
const J2 = "22222222-2222-4222-8222-222222222222";

/** The fake BFF for one role. */
function bff(role: "viewer" | "operator" | "admin") {
  return fakeBff({
    "GET /me": { username: "ana", role, csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role, rules },
    "GET /summary/aiml": { page: "aiml", computedAt: "now", counts: { "models.total": 3, "trainingJobs.total": 2, "mlmfBreaches.total": 1 }, partial: [] },
    "GET /smo/aimgf/models": page([model(A, "coverage-model"), model(B, "load-model"), model(C, "steer-model")]),
    "GET /smo/aimgf/model-lifecycles": page([life(A, "PROMOTED", "ACTIVE"), life(C, "TRAINING")]),
    "GET /smo/aimgf/model-lifecycles/counts": { groups: [{ state: "TRAINING", count: 40 }, { state: "PROMOTED", count: 1 }, { state: "RETIRED", count: 2 }] },
    "GET /smo/aimgf/mlmf/subscriptions": (c: Call) => page(c.query.get("model_id") && c.query.get("model_id") !== A ? [] : [{ subscriptionId: "s1", modelId: A, metricTypes: ["accuracy"], dmeTypeId: "d", guardKpiFloor: { accuracy: 0.85 }, notificationDestination: null }]),
    "GET /smo/aimgf/mlmf/reports": page([{ reportId: "r2", subscriptionId: "s1", metrics: { accuracy: 0.82 }, breachedFloor: true, reportedAt: "2026-10-09T11:00:00Z" }]),
    "GET /smo/aimgf/mlmf/subscriptions/s1/reports": page([
      { reportId: "r2", subscriptionId: "s1", metrics: { accuracy: 0.82, f1Score: 0.8 }, breachedFloor: true, reportedAt: "2026-10-09T11:00:00Z" },
      { reportId: "r1", subscriptionId: "s1", metrics: { accuracy: 0.9, f1Score: 0.85 }, breachedFloor: false, reportedAt: "2026-10-09T10:00:00Z" }]),
    "GET /smo/aimgf/training-jobs": (c: Call) => page([job(J1, "IN_PROGRESS"), job(J2, "SUSPENDED")].filter((j) => !c.query.get("status") || j.status === c.query.get("status"))),
    "GET /smo/aimgf/inference-jobs": page([]),
    [`GET /smo/aimgf/models/${A}`]: model(A, "coverage-model"),
    [`GET /smo/aimgf/models/${A}/lifecycle`]: life(A, "PROMOTED", "ACTIVE"),
    [`GET /smo/aimgf/models/${A}/governance-history`]: page([{ certificationRecordId: "g1", modelId: A, decision: "PROMOTE", decidedBy: "admin-1", rationale: "Emulation clean", decidedAt: "2026-10-09T11:02:00Z" }]),
    [`GET /smo/aimgf/models/${A}/lifecycle-history`]: page([{ fsm: "MODEL", fromState: "CERTIFIED", toState: "PROMOTED", event: "PROMOTE", occurredAt: "2026-10-09T11:02:00Z" }]),
    [`GET /smo/aimgf/models/${B}`]: model(B, "load-model"),
    [`GET /smo/aimgf/models/${B}/lifecycle`]: life(B, "REGISTERED"),
    [`PUT /smo/aimgf/models/${B}/owner`]: { modelId: B, owner: "es-client", previousOwner: null, changed: true },
    [`PUT /smo/aimgf/models/${A}/owner`]: { modelId: A, owner: "mobility-client", previousOwner: "ops", changed: true },
    [`POST /smo/aimgf/models/${A}/advance`]: { modelId: A },
    [`POST /smo/aimgf/training-jobs/${J1}/suspend`]: { trainingJobId: J1, status: "SUSPENDED" },
    [`POST /smo/aimgf/training-jobs/${J2}/resume`]: { trainingJobId: J2, status: "IN_PROGRESS" },
    "GET /smo/aimgf/ml-model-repositories": page([{ id: "r-1", attributes: { userLabel: "main repo" }, MLModel: [A, B], MLModelCoordinationGroup: [] }]),
    "GET /smo/aimgf/storages": page([{ storageId: "st-1", mlModelsAddresses: ["s3://models"], suppFeat: "0" }]),
    "GET /smo/aimgf/coordination-groups": page([]),
  });
}

const open = async () => { const m = await mountWith(<AuthProvider><Aiml /></AuthProvider>); await settle(8); return m; };
const column = (root: HTMLElement, id: string) => root.querySelector(`[data-stage="${id}"]`) as HTMLElement;

describe("the stage board", () => {
  // Every model sits in its stage's column with the column's count, and the model whose newest MLMF report breached is outlined and says so in words.
  it("groups the models by stage and outlines the one under its floor", async () => {
    bff("viewer");
    const { container } = await open();
    expect(column(container, "registered").textContent).toContain("load-model");
    expect(column(container, "training").textContent).toContain("steer-model");
    const active = column(container, "active");
    expect(active.getAttribute("aria-label")).toBe("Active runtime: 1");
    const card = active.querySelector(".kanban-card") as HTMLElement;
    expect(card.classList.contains("breach")).toBe(true);
    expect(card.textContent).toContain("under floor");
    expect(column(container, "registered").querySelector(".kanban-card")!.classList.contains("breach")).toBe(false);
    expect(byText(container, "[role=tab]", /Models/)!.textContent).toContain("3");
  });

  // Column counts are AIMgF's GROUP BY by state (not the cards read); off-board states come from it too.
  it("counts each stage on the server", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    expect(calls.some((c) => c.path === "/smo/aimgf/model-lifecycles/counts")).toBe(true);
    expect(column(container, "training").getAttribute("aria-label")).toBe("Training: 40");
    expect(column(container, "promoted").getAttribute("aria-label")).toBe("Promoted: 1");
    expect(container.querySelector('[data-section="aiml.board"]')!.textContent).toContain("2 retired");
  });

  // SCALE.md §4: the Models tab's first load stays within its call budget (summary + stage counts + four board reads + the running-training card).
  it("loads the Models tab within seven calls", async () => {
    const calls = bff("viewer");
    await open();
    const data = calls.filter((c) => c.path.startsWith("/smo/") || c.path.startsWith("/summary/"));
    expect(data.length).toBeLessThanOrEqual(7);
  });
});

describe("model detail and governance (feature 7)", () => {
  // Selecting a card opens the model's pipeline, guard KPI (with the floor and the breach) and its governance history with who and why.
  it("shows the selected model's guard KPI and governance history", async () => {
    bff("viewer");
    const { container } = await open();
    await click(column(container, "active").querySelector(".kanban-card") as HTMLElement);
    await settle(8);
    const guard = container.querySelector('[data-section="aiml.guard"]') as HTMLElement;
    expect(guard.textContent).toContain("Guard KPI · accuracy");
    expect(guard.textContent).toContain("Under floor");
    expect(guard.querySelector("svg[role=img]")).not.toBeNull();
    const gov = container.querySelector('[data-section="aiml.governance"]') as HTMLElement;
    expect(gov.textContent).toContain("PROMOTE");
    expect(gov.textContent).toContain("Emulation clean");
    await click(byText(gov, "[role=radio]", "All transitions")!);
    await settle(6);
    expect(gov.textContent).toContain("CERTIFIED → PROMOTED");
    expect(byText(container, "button", "Roll back to CERTIFIED")).toBeNull();             // a viewer is offered no end-of-life action
  });

  // An admin rolls a PROMOTED model back only with a rationale, and the rationale and decider travel with the advance call.
  it("rolls back with a rationale", async () => {
    const calls = bff("admin");
    const { container } = await open();
    await click(column(container, "active").querySelector(".kanban-card") as HTMLElement);
    await settle(8);
    const gov = container.querySelector('[data-section="aiml.governance"]') as HTMLElement;
    const rollback = byText<HTMLButtonElement>(gov, "button", "Roll back to CERTIFIED")!;
    expect(rollback.disabled).toBe(true);
    await type(gov.querySelector("input[placeholder='Why this model leaves service']") as HTMLInputElement, "accuracy under floor");
    await click(byText(gov, "button", "Roll back to CERTIFIED")!);
    await settle();
    const post = calls.find((c) => c.method === "POST" && c.path.endsWith("/advance"))!;
    expect(post.query.get("event")).toBe("ROLLBACK");
    expect(post.query.get("rationale")).toBe("accuracy under floor");
    expect(post.query.get("decided_by")).toBe("smo-gui");
  });
});

describe("model owner (SEC-15.3c)", () => {
  const selectB = async (container: HTMLElement) => {
    await click(byText(container, "[role=radio]", "Table")!);
    await settle(6);
    await click(byText(container, "tr", /load-model/)!);
    await settle(8);
    return container.querySelector('[data-section="aiml.detail"]') as HTMLElement;
  };

  // A model registered before owners were recorded is found in the list by its warning badge; one with an owner shows the owner's id.
  it("marks a model with no owner in the list", async () => {
    bff("viewer");
    const { container } = await open();
    await click(byText(container, "[role=radio]", "Table")!);
    await settle(6);
    const rows = [...container.querySelectorAll('[data-section="aiml.table"] tbody tr')] as HTMLElement[];
    const legacy = rows.find((r) => r.textContent!.includes("load-model"))!;
    const owned = rows.find((r) => r.textContent!.includes("coverage-model"))!;
    expect(legacy.querySelector(".b-warn")!.textContent).toBe("No owner (open to any rApp)");
    expect(owned.querySelector(".b-warn")).toBeNull();
    expect(owned.textContent).toContain("ops");
  });

  // A viewer and an operator see the owner, or that there is none, and are offered no way to set it: only an admin may (the console's rule table).
  it("offers Set owner to an admin only", async () => {
    for (const role of ["viewer", "operator"] as const) {
      bff(role);
      const { container } = await open();
      const detail = await selectB(container);
      expect(detail.textContent).toContain("No owner (open to any rApp)");
      expect(byText(detail, "button", /owner/i)).toBeNull();
      cleanup();
    }
  });

  // An admin sets the owner of an owner-less model: the dialog says what an owner means, and the id goes to MLMR's owner route, not to PUT /models/{id}.
  it("sets the owner of a model that has none", async () => {
    const calls = bff("admin");
    const { container } = await open();
    const detail = await selectB(container);
    await click(byText(detail, "button", "Set owner")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    expect(dialog.textContent).toContain("Owner of load-model v1.0");
    const save = byText<HTMLButtonElement>(dialog, "button", "Save owner")!;
    expect(save.disabled).toBe(true);
    await type(field(dialog, "Owner"), "es-client");
    expect(save.disabled).toBe(false);
    await click(save);
    await settle();
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.path).toBe(`/smo/aimgf/models/${B}/owner`);
    expect(put.body).toEqual({ owner: "es-client" });
  });

  // An owned model reads "Change owner", starts from the current owner and cannot be saved unchanged.
  it("changes the owner of an owned model", async () => {
    const calls = bff("admin");
    const { container } = await open();
    await click(column(container, "active").querySelector(".kanban-card") as HTMLElement);
    await settle(8);
    const detail = container.querySelector('[data-section="aiml.detail"]') as HTMLElement;
    await click(byText(detail, "button", "Change owner")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    expect(field(dialog, "Owner").value).toBe("ops");
    expect(byText<HTMLButtonElement>(dialog, "button", "Save owner")!.disabled).toBe(true);
    await type(field(dialog, "Owner"), "mobility-client");
    await click(byText(dialog, "button", "Save owner")!);
    await settle();
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ owner: "mobility-client" });
  });

  // The edit dialog no longer has an owner field and sends the stored owner back unchanged, so editing the description cannot change or clear it.
  it("keeps the owner out of the edit dialog", async () => {
    const calls = bff("operator");
    const { container } = await open();
    await click(column(container, "active").querySelector(".kanban-card") as HTMLElement);
    await settle(8);
    await click(byText(container, "button", "Edit metadata")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    expect(() => field(dialog, "Owner")).toThrow();
    await click(byText(dialog, "button", "Save")!);
    await settle();
    expect((calls.find((c) => c.method === "PUT")!.body as Record<string, unknown>).owner).toBe("ops");
  });
});

describe("training jobs (feature 7)", () => {
  // An operator suspends a running job and resumes a suspended one through AIMgF's own routes.
  it("suspends and resumes", async () => {
    window.location.hash = "#training";
    const calls = bff("operator");
    const { container } = await open();
    const table = container.querySelector('[data-section="aiml.training"]') as HTMLElement;
    await click(byText(table, "button", "Suspend")!);
    await click(byText(table, "button", "Resume")!);
    await settle();
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([`/smo/aimgf/training-jobs/${J1}/suspend`, `/smo/aimgf/training-jobs/${J2}/resume`]);
    expect(table.textContent).toContain("epoch 7/20 · about 4 min 12 s left");
    expect(table.querySelector("[aria-label='epoch 7 of 20']")).not.toBeNull();
  });

  // A viewer sees the jobs but no control over them.
  it("offers a viewer no job control", async () => {
    window.location.hash = "#training";
    bff("viewer");
    const { container } = await open();
    expect(container.textContent).toContain("IN_PROGRESS");
    expect(byText(container, "button", "Suspend")).toBeNull();
    expect(byText(container, "button", "Resume")).toBeNull();
  });
});

describe("feature groups (SEC-15.2b)", () => {
  const groups = page([
    { featureGroupId: "g1", featureGroupName: "fg_ref", featureList: "rsrp", datalakeSource: "InfluxSource", host: "h", port: "8086", bucket: "b", measurement: "m", tokenSet: true, tokenRef: "lake-ref", enableDme: false, dmeDataJobId: null },
    { featureGroupId: "g2", featureGroupName: "fg_old", featureList: "rsrp", datalakeSource: "InfluxSource", host: "h", port: "8086", bucket: "b", measurement: "m", tokenSet: true, tokenRef: null, enableDme: false, dmeDataJobId: null },
  ]);
  const operatorBff = () => fakeBff({
    "GET /me": { username: "ana", role: "operator", csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role: "operator", rules },
    "GET /summary/aiml": { page: "aiml", computedAt: "now", counts: {}, partial: [] },
    "GET /smo/aimgf/feature-groups": groups,
    "POST /smo/aimgf/feature-groups": { featureGroupId: "g3", tokenSet: true, tokenRef: "lake-ref" },
  });

  // The form asks for the NAME of a secret (no password field), says so when the value is not a name, and posts `tokenRef` with no `token` key.
  it("registers a group with a secret reference and sends no token", async () => {
    window.location.hash = "#features";
    const calls = operatorBff();
    const { container } = await open();
    const form = container.querySelector('[data-section="aiml.newFeatureGroup"]') as HTMLElement;
    expect(form.querySelector("input[type=password]")).toBeNull();
    await type(field(form, "Secret reference"), "Not A Name!");
    expect(form.textContent).toContain("Use lower-case letters");
    await type(field(form, "Secret reference"), "lake-ref");
    await type(field(form, "Name"), "fg_new");
    await type(field(form, "Features"), "rsrp");
    await type(field(form, "Host"), "influx.demo");
    await type(field(form, "Bucket"), "b");
    await type(field(form, "DB org"), "o");
    await type(field(form, "Measurement"), "m");
    await click(byText(form, "button", "Create feature group")!);
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/smo/aimgf/feature-groups");
    expect((post.body as Record<string, unknown>).tokenRef).toBe("lake-ref");
    expect(Object.keys(post.body as object)).not.toContain("token");
  });

  // The table shows the reference's name, says "clear-text token (deprecated)" for a group that has only the old token, and never a token value.
  it("lists the reference, or that an old clear-text token is set, and no token", async () => {
    window.location.hash = "#features";
    operatorBff();
    const { container } = await open();
    const table = container.querySelector('[data-section="aiml.features"]') as HTMLElement;
    expect(table.textContent).toContain("lake-ref");
    expect(table.textContent).toContain("clear-text token (deprecated)");
  });
});

describe("tabs", () => {
  // Feature 8: the Registry tab lists repositories, storages and every model's downloadable artifact versions.
  it("shows the registry", async () => {
    window.location.hash = "#registry";
    bff("viewer");
    const { container } = await open();
    expect(container.querySelector('[data-section="aiml.repositories"]')!.textContent).toContain("main repo");
    expect(container.querySelector('[data-section="aiml.storages"]')!.textContent).toContain("s3://models");
    const links = [...container.querySelectorAll('[data-section="aiml.artifactVersions"] a')].map((a) => a.getAttribute("href"));
    expect(links).toEqual([`/api/smo/aimgf/models/${A}/artifact/2`, `/api/smo/aimgf/models/${A}/artifact/1`]);
  });

  // A link saved before the redesign (#groups) still opens the coordination-groups tab, and clicking a tab switches to it.
  it("keeps the old tab ids and switches tabs", async () => {
    window.location.hash = "#groups";
    bff("viewer");
    const { container } = await open();
    expect(container.querySelector('[data-section="aiml.groups"]')).not.toBeNull();
    await click(byText(container, "[role=tab]", /Registry/)!);
    await settle(6);
    expect(container.querySelector('[data-section="aiml.repositories"]')).not.toBeNull();
    expect(window.location.hash).toBe("#registry");
  });
});
