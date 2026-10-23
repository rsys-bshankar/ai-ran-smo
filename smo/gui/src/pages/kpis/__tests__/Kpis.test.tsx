// @vitest-environment jsdom
/** Tests of the KPIs & Assurance page (pages/kpis): Overview is the default tab, its tiles show the server-computed KPIs (and "—" with the
 * reason for one that is not defined), the tiles follow the top bar's scope, the chart draws each KPI over time with its own region filter and
 * saves and loads the user's layouts (GUI-4.1 to 4.3), the worst-10 list is ranked lowest first, the escalations callout carries the true count, the range
 * reaches the KPI calls, the MDA request form is read-only for a viewer and sends a TS 28.104 body for an operator (feature 9), a request can be cancelled,
 * MDA reports filter by kind and offer the file, an admin (only) deregisters a RAN Analytics producer (GUI-11.1), and the pre-redesign tab ids (MLMF, Definitions) still work. `fetch` is stubbed by
 * `testing/bff.tsx` `fakeBff` with `auth/permissions.fixture.json`; no BFF runs. Run: `npx vitest run src/pages/kpis` from smo/gui. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import fixture from "../../../auth/permissions.fixture.json";
import { ScopeContext } from "../../../data/scope";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, pick, settle, type } from "../../../testing/dom";
import { Kpis } from "../index";
import { Producers } from "../sections/AnalyticsLegacy";
import { PmSubscriptions } from "../sections/PmSubscriptions";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = ""; });

const page = <T,>(items: T[], total = items.length) => ({ items, total, limit: 50, offset: 0 });
const kpi = (name: string, unit: string, items: unknown[]) => ({ kpi: name, unit, from: "f", to: "t", groupBy: "all", filesScanned: 3, truncated: false, items });

/** The fake BFF; `extraRules` go first in the permission table. */
function bff(role: "viewer" | "operator" | "admin", extraRules: unknown[] = []) {
  return fakeBff({
    "GET /me": { username: "ana", role, csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role, rules: [...extraRules, ...fixture] },
    "GET /smo/ran-nf-oam/kpis/dl_ue_throughput": (c: Call) => c.query.get("group_by") === "element"
      ? kpi("dl_ue_throughput", "Mbit/s", [{ group: { managedElementRef: "du-01" }, value: 180, samples: 4, reason: null }, { group: { managedElementRef: "du-03" }, value: 0.4, samples: 4, reason: null },
        { group: { managedElementRef: "du-02" }, value: 12, samples: 4, reason: null }, { group: { managedElementRef: "du-09" }, value: null, samples: 0, reason: "NO_DATA" }])
      : kpi("dl_ue_throughput", "Mbit/s", [{ group: {}, value: 182.34, samples: 1200, reason: null }]),
    "GET /smo/ran-nf-oam/kpis/dl_prb_utilization": kpi("dl_prb_utilization", "percent", [{ group: {}, value: 57, samples: 1200, reason: null }]),
    "GET /smo/ran-nf-oam/kpis/handover_success_rate": kpi("handover_success_rate", "percent", [{ group: {}, value: null, samples: 0, reason: "NO_DATA" }]),
    "GET /smo/ran-nf-oam/kpis/handover_failure_rate": kpi("handover_failure_rate", "percent", [{ group: {}, value: 0.9, samples: 30, reason: null }]),
    "GET /smo/ran-nf-oam/kpis/rrc_connected_ues_mean": { status: 404, body: { title: "KPI_NOT_FOUND" } },
    "GET /smo/sa-smos/monitors": page([{ monitorId: "m-1", targetOrderId: null, targetCoordinationGroupId: null, targetRappInstanceId: null, analyticsSubscriptionId: null, thresholds: { rrcSetupSuccess: 98 } }]),
    "GET /smo/sa-smos/remedial-actions": (c: Call) => c.query.get("outcome") === "ESCALATED"
      ? page([{ actionId: "a-1", monitorId: "m-1", actionType: "CONFIG_CHANGE", autoExecuted: true, outcome: "ESCALATED" }], 4) : page([]),
    "GET /smo/mdaf/mda-functions": page([{ id: "fn-1", attributes: { userLabel: "mdaf-coverage-1", supportedMDACapabilities: ["COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS"], supportedMDADomain: "RAN", mLModelRefList: [], aIMLInferenceFunctionRefList: [] } }]),
    "GET /smo/mdaf/mda-requests": page([{ id: "req-9", attributes: { requestedMDAOutputs: [{ mDAType: "COVERAGE" }], analyticsScope: null, reportingMethod: "FILE", requestedBy: "smo-gui", active: true } }]),
    "DELETE /smo/mdaf/mda-requests/req-9": { status: 204 },
    "GET /smo/mdaf/mda-reports": (c: Call) => page([{ id: "rep-1", attributes: { mDAOutputs: [], mDARequestRef: null, mDAFunctionRef: "fn-1", reportKind: "DRIFT", scope: null, deliveredToRequestRefList: [], generatedAt: "2026-10-09T10:00:00Z" } }]
      .filter((r) => !c.query.get("report_kind") || r.attributes.reportKind === c.query.get("report_kind"))),
    "GET /smo/mdaf/reports": page([]), "GET /smo/mdaf/subscriptions": page([]), "GET /smo/ran-analytics/producers": page([]),
    "POST /smo/mdaf/mda-requests": { status: 201, body: { id: "req-1" } },
    "GET /smo/aimgf/mlmf/subscriptions": page([]), "GET /smo/mlmr/models": page([]),
    "GET /smo/ran-nf-oam/kpi-definitions": page([{ name: "dl_ue_throughput", formula: "thp", unit: "Mbit/s", description: null, counters: [{ counter: "DRB.UEThpDl", variable: "thp", aggregation: "avg" }] }]),
    "GET /smo/ran-nf-oam/kpi-schedules": page([]),
    "GET /smo/ran-nf-oam/kpis/dl_ue_throughput/series": (c: Call) => ({ kpi: "dl_ue_throughput", unit: "Mbit/s", from: "f", to: "t", stepSeconds: Number(c.query.get("step_seconds")),
      filesScanned: 2, truncated: false, points: [{ at: "2026-10-10T10:00:00Z", value: 100, samples: 3, reason: null }, { at: "2026-10-10T10:15:00Z", value: null, samples: 0, reason: "NO_DATA" },
        { at: "2026-10-10T10:30:00Z", value: c.query.get("region") === "east" ? 50 : 120, samples: 2, reason: null }] }),
    "GET /smo/ran-nf-oam/managed-entities/scopes": { regions: [{ region: "east", elements: 3, siteClusters: [{ siteCluster: "metro-a", elements: 2 }] }, { region: "west", elements: 1, siteClusters: [] }] },
    "GET /me/kpi-layouts": { max: 20, items: [{ name: "Night", updatedAt: "2026-10-10T00:00:00Z", kpis: ["dl_ue_throughput", "dl_prb_utilization"], range: "7d", region: "west", siteCluster: null }] },
    "PUT /me/kpi-layouts/*": (c: Call) => c.body,
    "DELETE /me/kpi-layouts/*": { status: 204 },
  });
}

const open = async () => { const m = await mountWith(<AuthProvider><Kpis /></AuthProvider>); await settle(8); return m; };
const tile = (root: HTMLElement, label: string) => byText(root, ".kpi", new RegExp(label)) as HTMLElement;

describe("the Overview tab", () => {
  // Overview is the default; each tile is the server's KPI value, a KPI without data or definition shows "—" with the reason.
  it("shows the KPI tiles from the server's computation", async () => {
    bff("viewer");
    const { container } = await open();
    expect(byText(container, "[role=tab][aria-selected=true]", "Overview")).not.toBeNull();
    expect(tile(container, "DL UE throughput").querySelector(".kpi-v")!.textContent).toBe("182.3 Mbit/s");
    expect(tile(container, "DL PRB utilisation").querySelector(".kpi-v")!.textContent).toBe("57 %");
    expect(tile(container, "Handover success").textContent).toContain("no data in window");
    expect(tile(container, "RRC-connected UEs").querySelector(".kpi-v")!.textContent).toBe("—");
    expect(tile(container, "RRC-connected UEs").textContent).toContain("not defined");
  });

  // The worst list ranks the lowest values first and leaves out a group with no value.
  it("ranks the lowest-throughput elements", async () => {
    bff("viewer");
    const { container } = await open();
    const rows = [...container.querySelectorAll('[data-section="kpis.worst"] li')].map((li) => li.querySelector(".mono")!.textContent);
    expect(rows).toEqual(["du-03", "du-02", "du-01"]);
  });

  // The escalations callout shows the newest escalated actions and the true count from the envelope.
  it("shows what is escalated, with the true count", async () => {
    bff("viewer");
    const { container } = await open();
    const box = container.querySelector('[data-section="kpis.escalations"]') as HTMLElement;
    expect(box.textContent).toContain("CONFIG_CHANGE could not recover it");
    expect(byText(box, "button", "4 →")).not.toBeNull();
  });

  // Changing the range recomputes the KPIs over a longer window (from_time moves back).
  it("passes the range to the KPI calls", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    const before = calls.filter((c) => c.path === "/smo/ran-nf-oam/kpis/dl_prb_utilization").length;
    await click(byText(container, "[role=radio]", "7 d")!);
    await settle(6);
    const all = calls.filter((c) => c.path === "/smo/ran-nf-oam/kpis/dl_prb_utilization");
    expect(all.length).toBeGreaterThan(before);
    const hours = (Date.now() - Date.parse(all[all.length - 1].query.get("from_time")!)) / 3_600_000;
    expect(Math.round(hours)).toBe(168);
  });
});

describe("the KPI chart (GUI-4.1 to 4.3)", () => {
  const chart = (root: HTMLElement) => root.querySelector('[data-section="kpis.chart"]') as HTMLElement;
  const seriesCalls = (calls: Call[], name = "dl_ue_throughput") => calls.filter((c) => c.path === `/smo/ran-nf-oam/kpis/${name}/series`);

  // The chart reads the series in the range's step and draws the steps with data; the caption counts the step without.
  it("draws a KPI over time", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    expect(chart(container).querySelector('[data-kpi="dl_ue_throughput"] svg')).not.toBeNull();
    expect(chart(container).textContent).toContain("3 steps of 15 min, 1 without data");
    expect(seriesCalls(calls).at(-1)!.query.get("step_seconds")).toBe("900");
  });

  // The chart's own region and site cluster reach the series call; the tiles keep reading the whole network.
  it("narrows the chart to a region and a site cluster", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    await pick(chart(container).querySelector('select[aria-label="Chart region"]') as HTMLSelectElement, "east");
    await settle(4);
    await pick(chart(container).querySelector('select[aria-label="Chart site cluster"]') as HTMLSelectElement, "metro-a");
    await settle(4);
    const last = seriesCalls(calls).at(-1)!;
    expect([last.query.get("region"), last.query.get("site_cluster")]).toEqual(["east", "metro-a"]);
    expect(calls.filter((c) => c.path === "/smo/ran-nf-oam/kpis/dl_prb_utilization").every((c) => !c.query.has("region"))).toBe(true);
  });

  // The top bar's scope reaches the tiles and is where the chart starts.
  it("follows the top bar's scope", async () => {
    const calls = bff("viewer");
    await mountWith(<ScopeContext.Provider value={{ scope: { region: "west", cluster: null }, setScope: () => {} }}><AuthProvider><Kpis /></AuthProvider></ScopeContext.Provider>);
    await settle(8);
    expect(calls.find((c) => c.path === "/smo/ran-nf-oam/kpis/dl_prb_utilization")!.query.get("region")).toBe("west");
    expect(seriesCalls(calls).at(-1)!.query.get("region")).toBe("west");
  });

  // Saving sends the KPIs charted, the range and the place under the name typed.
  it("saves the layout", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    await pick(chart(container).querySelector('select[aria-label="Chart region"]') as HTMLSelectElement, "east");
    await type(chart(container).querySelector('input[aria-label="Layout name"]') as HTMLInputElement, "Morning check");
    await click(byText(chart(container), "button", "Save layout")!);
    await settle(4);
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.path).toBe("/me/kpi-layouts/Morning%20check");
    expect(put.body).toEqual({ kpis: ["dl_ue_throughput"], range: "24h", region: "east", siteCluster: null });
  });

  // Loading a saved layout puts back its KPIs, its range (the Overview's, so the tiles follow) and its region; it can be deleted.
  it("loads and deletes a saved layout", async () => {
    const calls = bff("viewer");
    const { container } = await open();
    await pick(chart(container).querySelector('select[aria-label="Layout"]') as HTMLSelectElement, "Night");
    await settle(6);
    expect([...chart(container).querySelectorAll("[data-kpi]")].map((e) => e.getAttribute("data-kpi"))).toEqual(["dl_ue_throughput", "dl_prb_utilization"]);
    expect(seriesCalls(calls).at(-1)!.query.get("step_seconds")).toBe("3600");
    expect(seriesCalls(calls).at(-1)!.query.get("region")).toBe("west");
    expect(chart(container).querySelector('[data-kpi="dl_prb_utilization"]')!.textContent).toContain("not defined");
    await click(byText(chart(container), "button", "Delete layout")!);
    await settle(4);
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/me/kpi-layouts/Night")).toBe(true);
  });
});

describe("RAN Analytics (feature 9)", () => {
  // An operator cancels an analysis request with DELETE after a confirm.
  it("cancels an analysis request", async () => {
    window.location.hash = "#analytics";
    vi.stubGlobal("confirm", () => true);
    const calls = bff("operator");
    const { container } = await open();
    const box = container.querySelector('[data-section="kpis.mdaRequests"]') as HTMLElement;
    await click(byText(box, "button", "Cancel")!);
    await settle();
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/smo/mdaf/mda-requests/req-9")).toBe(true);
  });

  // The BFF opens POST /mdaf/mda-requests to operators: a viewer gets a read-only note and nothing can be sent.
  it("keeps the request form read-only for a viewer", async () => {
    window.location.hash = "#analytics";
    bff("viewer");
    const { container } = await open();
    const box = container.querySelector('[data-section="kpis.mdaRequest"]') as HTMLElement;
    expect(box.textContent).toContain("Read-only here");
    expect(byText(box, "button", "Request analysis")).toBeNull();
  });

  // An operator's request carries the function, the MDA type it supports, the entity scope and the delivery method.
  it("requests an analysis as an operator", async () => {
    window.location.hash = "#analytics";
    const calls = bff("operator");
    const { container } = await open();
    const box = container.querySelector('[data-section="kpis.mdaRequest"]') as HTMLElement;
    const [fn] = box.querySelectorAll("select") as NodeListOf<HTMLSelectElement>;
    fn.value = "fn-1";
    fn.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    const [, output, method] = box.querySelectorAll("select") as NodeListOf<HTMLSelectElement>;
    output.value = "COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS";
    output.dispatchEvent(new Event("change", { bubbles: true }));
    method.value = "STREAMING";
    method.dispatchEvent(new Event("change", { bubbles: true }));
    await type(box.querySelector("input.mono") as HTMLInputElement, "du-03, du-04");
    await click(byText(box, "button", "Request analysis")!);
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ mDAFunctionRef: "fn-1", requestedMDAOutputs: [{ mDAType: "COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS" }], reportingMethod: "STREAMING",
      reportingTarget: null, analyticsScope: { managedEntitiesScope: ["du-03", "du-04"] }, requestedBy: "smo-gui" });
  });

  // Reports filter by kind on the server and each offers its file through the BFF.
  it("lists MDA reports with a kind filter and the file download", async () => {
    window.location.hash = "#analytics";
    const calls = bff("viewer");
    const { container } = await open();
    const box = container.querySelector('[data-section="kpis.mdaReports"]') as HTMLElement;
    expect(box.querySelector("a")!.getAttribute("href")).toBe("/api/smo/mdaf/mda-reports/rep-1/file");
    const kind = box.querySelector("select") as HTMLSelectElement;
    kind.value = "PREDICTION";
    kind.dispatchEvent(new Event("change", { bubbles: true }));
    await settle(6);
    expect(calls.some((c) => c.path === "/smo/mdaf/mda-reports" && c.query.get("report_kind") === "PREDICTION")).toBe(true);
    expect(box.textContent).toContain("No MDA report published.");
  });
});

describe("kept tabs", () => {
  // The pre-redesign ids still open their tab: #mlmf the model KPIs (from the AI/ML folder), #definitions the KPI catalogue.
  it("opens #mlmf and #definitions", async () => {
    window.location.hash = "#mlmf";
    bff("viewer");
    const first = await open();
    expect(first.container.querySelector('[data-section="aiml.mlmf"]')).not.toBeNull();
    cleanup();
    window.location.hash = "#definitions";
    bff("viewer");
    const second = await open();
    expect(second.container.querySelector('[data-section="kpis.definitions"]')!.textContent).toContain("DRB.UEThpDl");
  });
});

describe("PM subscriptions", () => {
  // GUI-10.1: the BFF now has a rule for DELETE /pm-subscriptions/{id} (operator): an operator's Unsubscribe is drawn and sends it; a viewer's is not
  it("draws Unsubscribe for an operator only", async () => {
    const list = { items: [{ subscriptionId: "p-1", managedElementRef: "du-01", counterType: "DRB.UEThpDl", deliveryMethod: "pull", granularityPeriod: 900 }], total: 1, limit: 50, offset: 0 };
    const subs = (role: string) => fakeBff({
      "GET /me": { username: "ana", role, csrfToken: "c", local: true }, "GET /permissions": { role, rules: fixture },
      "GET /smo/ran-nf-oam/pm-subscriptions": list, "DELETE /smo/ran-nf-oam/pm-subscriptions/p-1": { status: 204 },
      "GET /smo/ran-nf-oam/o1-adaptor-endpoints": { items: [], total: 0, limit: 500, offset: 0 }, "GET /smo/ran-nf-oam/kpi-definitions": { items: [], total: 0, limit: 50, offset: 0 },
    });
    const calls = subs("operator");
    const op = await mountWith(<AuthProvider><PmSubscriptions /></AuthProvider>);
    await settle(6);
    await click(byText(op.container, "button", "Unsubscribe")!);
    await settle();
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/smo/ran-nf-oam/pm-subscriptions/p-1")).toBe(true);
    cleanup();
    subs("viewer");
    const viewer = await mountWith(<AuthProvider><PmSubscriptions /></AuthProvider>);
    await settle(6);
    expect(viewer.container.querySelector("tbody")!.textContent).toContain("du-01");
    expect(byText(viewer.container, "button", "Unsubscribe")).toBeNull();
  });
});

describe("RAN Analytics producers (GUI-11.1)", () => {
  const prod = (producerId: string, analyticsType: string) => ({ producerId, analyticsType, mdaType: null, dmeInputTypes: [], outputSchema: { type: "object" } });
  /** A fake BFF whose producer list loses a producer once it is deregistered. */
  const producers = (role: string) => {
    let list = [prod("rapp-mdaf-1", "coverage-issue-analysis"), prod("rapp-mdaf-2", "energy-analysis")];
    return fakeBff({
      "GET /me": { username: "ana", role, csrfToken: "c", local: true }, "GET /permissions": { role, rules: fixture },
      "GET /smo/ran-analytics/producers": () => ({ items: list, total: list.length, limit: 50, offset: 0 }),
      "DELETE /smo/ran-analytics/producers/*": (c: Call) => {
        list = list.filter((p) => !(`/smo/ran-analytics/producers/${p.producerId}` === c.path && p.analyticsType === c.query.get("analytics_type")));
        return { status: 204 };
      },
    });
  };
  const mountProducers = async () => { const m = await mountWith(<AuthProvider><Producers /></AuthProvider>); await settle(6); return m; };

  // The BFF allows DELETE /ran-analytics/producers/{id} to an admin only (call flow 08a): an operator and a viewer see the list without the button.
  it("draws Deregister for an admin only", async () => {
    for (const role of ["operator", "viewer"]) {
      producers(role);
      const m = await mountProducers();
      expect(m.container.querySelector("tbody")!.textContent).toContain("rapp-mdaf-1");
      expect(byText(m.container, "button", "Deregister")).toBeNull();
      cleanup();
    }
    producers("admin");
    const admin = await mountProducers();
    expect(admin.container.querySelectorAll("tbody button.danger")).toHaveLength(2);
  });

  // After the confirm, Deregister sends DELETE with the producer in the path and its analytics type as the query; the reloaded list no longer has it.
  it("deregisters a producer and drops it from the list", async () => {
    const asked: string[] = [];
    vi.stubGlobal("confirm", (msg: string) => { asked.push(msg); return true; });
    const calls = producers("admin");
    const { container } = await mountProducers();
    const row = [...container.querySelectorAll("tbody tr")].find((r) => r.textContent!.includes("rapp-mdaf-1")) as HTMLElement;
    await click(byText(row, "button", "Deregister")!);
    await settle(6);
    expect(asked[0]).toContain("rapp-mdaf-1");
    const del = calls.find((c) => c.method === "DELETE")!;
    expect([del.path, del.query.get("analytics_type")]).toEqual(["/smo/ran-analytics/producers/rapp-mdaf-1", "coverage-issue-analysis"]);
    expect(container.querySelector("tbody")!.textContent).not.toContain("rapp-mdaf-1");
    expect(container.querySelector("tbody")!.textContent).toContain("rapp-mdaf-2");
  });

  // Declining the confirm sends nothing.
  it("sends nothing when the confirm is declined", async () => {
    vi.stubGlobal("confirm", () => false);
    const calls = producers("admin");
    const { container } = await mountProducers();
    await click(byText(container, "button", "Deregister")!);
    await settle();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });
});
