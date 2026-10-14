// @vitest-environment jsdom
/** Infrastructure redesign behaviour: Topology is the default tab and draws the TEIV export level by level (O-Cloud → pools / managers /
 * workloads → resources) with health from alarms and deployment states; the O-Cloud inventory level picker switches the FOCOM route it lists;
 * service orders draw a stepper from their steps; a resource's CPU and memory come from FOCOM (one read in the inspector, one batched read for a
 * pool's resource table, GUI-9.8b); and the pure tree builder. Run: `npx vitest run src/pages/infrastructure` from smo/gui. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Topology } from "../../../api/types";
import { AuthProvider } from "../../../auth/AuthContext";
import rules from "../../../auth/permissions.fixture.json";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, settle } from "../../../testing/dom";
import { Infrastructure } from "..";
import { buildTopology } from "../data/topology";
import { orderState, stepState } from "../sections/ServiceOrders";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = ""; });

const URN = "urn:oran:smo:teiv";
const P = "o-ran-smo-teiv-cloud";
const TOPOLOGY: Topology = {
  entities: [
    { [`${P}:ResourcePool`]: [{ id: `${URN}:ResourcePool:pool-gpu-1`, attributes: { name: "pool-gpu-1", description: "GPU", oCloudId: "edge-metro-a" } }] },
    { [`${P}:DeploymentManager`]: [{ id: `${URN}:DeploymentManager:dm-1`, attributes: { name: "dms-k8s", oCloudId: "edge-metro-a", serviceUri: "http://dms" } }] },
    { [`${P}:Resource`]: [
      { id: `${URN}:Resource:r-1`, attributes: { resourceTypeId: "gpu", resourcePoolId: "pool-gpu-1", description: "gpu-node-1" } },
      { id: `${URN}:Resource:r-2`, attributes: { resourceTypeId: "gpu", resourcePoolId: "pool-gpu-1", description: "gpu-node-2" } },
      { id: `${URN}:Resource:r-3`, attributes: { resourceTypeId: "nic", resourcePoolId: "pool-gpu-1", description: "nic-of-node-2" } },
    ] },
  ],
  relationships: [
    { [`${P}:RESOURCE_CONTAINED_IN_RESOURCEPOOL`]: ["r-1", "r-2", "r-3"].map((r) => ({ id: `c:${r}`, aSide: `${URN}:Resource:${r}`, bSide: `${URN}:ResourcePool:pool-gpu-1` })) },
    { [`${P}:RESOURCE_CHILD_OF_RESOURCE`]: [{ id: "x", aSide: `${URN}:Resource:r-3`, bSide: `${URN}:Resource:r-2` }] },
  ],
};
const DEPLOYMENT = { nfDeploymentId: "d-1", name: "es-rapp-14", state: "RUNNING", clusterId: "edge-metro-a", nfDeploymentDescriptorId: "x", workloadRef: null, requiredResourceTypeId: null, abnormalReason: null };
const ALARM = { alarmId: "a-1", resourceRef: "r-3", severity: "major", alarmClearedTime: null };

/** The fake BFF for the topology and inventory tabs; every FOCOM inventory list answers one empty page. */
function bff() {
  return fakeBff({
    "GET /me": { username: "ana", role: "admin", csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role: "admin", rules },
    "GET /summary/infrastructure": { page: "infrastructure", computedAt: "", counts: { "deployments.total": 1 }, partial: [] },
    "GET /smo/focom/topology": TOPOLOGY,
    "GET /smo/nfo/deployments": { items: [DEPLOYMENT], total: 1, limit: 500, offset: 0 },
    "GET /smo/focom/alarms": { items: [ALARM], total: 1, limit: 500, offset: 0 },
    "GET /smo/focom/resources/r-1/utilisation": { resourceId: "r-1", cpuPercent: 42.5, memoryPercent: 61, at: "2026-10-09T10:00:00Z" },
    "GET /smo/focom/utilisation": (c: Call) => ({ items: (c.query.get("resource_ids") ?? "").split(",").map((id) => (id === "r-1"
      ? { resourceId: id, cpuPercent: 42.5, memoryPercent: 61, at: "2026-10-09T10:00:00Z" } : { resourceId: id, cpuPercent: null, memoryPercent: null, at: null })) }),
    "GET /smo/focom/resource-pools": { items: [{ resourcePoolId: "pool-gpu-1", name: "pool-gpu-1", description: "GPU", oCloudId: "edge-metro-a" }], total: 1, limit: 50, offset: 0 },
    "GET /smo/focom/resource-pools/pool-gpu-1/resources": { items: [{ resourceId: "r-1", resourceTypeId: "gpu", description: "gpu-node-1", parentId: null },
      { resourceId: "r-2", resourceTypeId: "gpu", description: "gpu-node-2", parentId: null }], total: 2, limit: 50, offset: 0 },
    "GET /smo/focom/*": { items: [], total: 0, limit: 50, offset: 0 },
    "GET /smo/so-smos/orders": { items: [{ orderId: "o-1", scope: "coverage v4", rmihRegistration: "r", homingDecision: null, steps: [
      { stepType: "INFRA", targetModule: "FOCOM", status: "COMPLETED" }, { stepType: "TRAINING", targetModule: "AI_ML_WORKFLOW", status: "FAILED", error: "no model" },
      { stepType: "DEPLOY", targetModule: "NFO", status: "PENDING" }] }], total: 1, limit: 50, offset: 0 },
  });
}

const open = () => mountWith(<AuthProvider><Infrastructure /></AuthProvider>);
const tiles = (root: ParentNode) => Array.from(root.querySelectorAll(".topo-tile")).map((t) => t.querySelector(".topo-name")?.textContent);

describe("the topology tab", () => {
  // With no hash the page opens on Topology and draws the O-Clouds from the TEIV export, rolled up to the worst health under them.
  it("is the default tab and draws the O-Clouds of the TEIV export", async () => {
    const calls = bff();
    const { container } = await open();
    await settle(8);
    expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe("Topology");
    expect(calls.some((c) => c.path === "/smo/focom/topology")).toBe(true);
    expect(tiles(container)).toEqual(["edge-metro-a"]);
    expect(container.querySelector(".topo-tile")?.className).toContain("h-bad");
    expect(container.querySelector(".topo-tile")?.textContent).toContain("Faulty");
  });

  // Opening an O-Cloud shows its managers, pools and workloads; opening the pool shows its top resources, and the breadcrumb leads back.
  it("expands one level at a time with a breadcrumb", async () => {
    bff();
    const { container } = await open();
    await settle(8);
    await click(container.querySelector<HTMLElement>(".topo-tile")!);
    expect(tiles(container)).toEqual(expect.arrayContaining(["dms-k8s", "pool-gpu-1", "es-rapp-14"]));
    expect(container.querySelector('[data-section="infrastructure.inspector"]')?.textContent).toContain("es-rapp-14");
    await click(byText(container, ".topo-tile", /pool-gpu-1/)!);
    expect(tiles(container)).toEqual(["gpu-node-2", "gpu-node-1"]);
    expect(container.querySelector(".crumb")?.textContent).toContain("pool-gpu-1");
    await click(byText(container, ".crumb button", "All O-Clouds")!);
    expect(tiles(container)).toEqual(["edge-metro-a"]);
  });

  // The inspector shows the selected node's attributes; a pool says utilisation is per resource, a resource shows FOCOM's CPU and memory and
  // GPU as a gap (GUI-9.8b)
  it("inspects the selected node and shows a resource's utilisation from FOCOM", async () => {
    const calls = bff();
    const { container } = await open();
    await settle(8);
    await click(container.querySelector<HTMLElement>(".topo-tile")!);
    await click(byText(container, ".topo-tile", /pool-gpu-1/)!);
    const inspector = () => container.querySelector('[data-section="infrastructure.inspector"]')!;
    expect(inspector().textContent).toContain("Resource pool");
    expect(inspector().textContent).toContain("Measured per resource");
    expect(calls.some((c) => c.path.endsWith("/utilisation"))).toBe(false);
    await click(byText(container, ".topo-tile", /gpu-node-1/)!);
    await settle(4);
    expect(inspector().textContent).toContain("CPU42.5 %");
    expect(inspector().textContent).toContain("Memory61.0 %");
    expect(inspector().textContent).toContain("GPU—");
    expect(inspector().querySelector(".gap-note")?.textContent).toContain("GPU utilisation is measured by no module");
  });
});

describe("the O-Cloud inventory tab", () => {
  // The level picker lists one level at a time: picking "Sites" reads /focom/o-cloud-sites with a page size.
  it("switches the FOCOM route it lists when a level is picked", async () => {
    window.location.hash = "#ocloud";
    const calls = bff();
    const { container } = await open();
    await settle(6);
    expect(calls.some((c) => c.path === "/smo/focom/locations")).toBe(true);
    expect(calls.some((c) => c.path === "/smo/focom/o-cloud-sites")).toBe(false);
    await click(byText(container, "button", "Sites")!);
    await settle(4);
    const sites = calls.find((c) => c.path === "/smo/focom/o-cloud-sites");
    expect(sites?.query.get("limit")).toBeTruthy();
    expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toBe("Sites");
  });

  // A pool's resource table shows CPU and memory for the page shown, read in ONE batched call (GUI-9.8b); a resource with no record shows "—"
  it("shows the utilisation of a pool's resources from one batched read", async () => {
    window.location.hash = "#ocloud";
    const calls = bff();
    const { container } = await open();
    await settle(6);
    await click(byText(container, "button", "Resource pools")!);
    await settle(4);
    await click(byText(container, "tbody tr", /pool-gpu-1/)!);
    await settle(6);
    const batch = calls.filter((c) => c.path === "/smo/focom/utilisation");
    expect(batch).toHaveLength(1);
    expect(batch[0].query.get("resource_ids")).toBe("r-1,r-2");
    const table = container.querySelector('[data-section="infrastructure.pool-resources"]')!;
    const rows = Array.from(table.querySelectorAll("tbody tr")).map((r) => r.textContent);
    expect(rows[0]).toContain("42.5 %");
    expect(rows[0]).toContain("61.0 %");
    expect(rows[1]).toContain("—");
    expect(calls.some((c) => /\/focom\/resources\/[^/]+\/utilisation/.test(c.path))).toBe(false);
  });
});

describe("service orders", () => {
  // Each order card draws its steps as a stepper: done, failed, then not reached.
  it("draws each order's steps as a stepper", async () => {
    window.location.hash = "#orders";
    bff();
    const { container } = await open();
    await settle(6);
    const steps = Array.from(container.querySelectorAll(".order-card .step")).map((s) => s.className.trim());
    expect(steps).toEqual(["step done", "step fail", "step"]);
    expect(container.querySelector(".order-card")?.textContent).toContain("TRAINING: no model");
  });

  // An order's state and each step's stepper state follow the step statuses.
  it("maps step statuses onto stepper and order states", () => {
    expect(["COMPLETED", "FAILED", "PENDING", "CANCELLED", "RUNNING"].map(stepState)).toEqual(["done", "fail", "todo", "block", "now"]);
    expect(orderState([{ stepType: "A", targetModule: "X", status: "COMPLETED" }])).toBe("COMPLETED");
    expect(orderState([{ stepType: "A", targetModule: "X", status: "COMPLETED" }, { stepType: "B", targetModule: "X", status: "PENDING" }])).toBe("IN_PROGRESS");
    expect(orderState([{ stepType: "A", targetModule: "X", status: "COMPLETED" }, { stepType: "B", targetModule: "X", status: "CANCELLED" }])).toBe("CANCELLED");
    expect(orderState([{ stepType: "A", targetModule: "X", status: "RUNNING" }], "IN_PROGRESS")).toBe("IN_PROGRESS");
  });
});

describe("buildTopology", () => {
  // A child resource sits under its parent, the alarm on it makes it and every ancestor faulty, and cut inputs are named in a note.
  it("nests child resources, rolls health up and names a cut list", () => {
    const tree = buildTopology({ topology: TOPOLOGY, workloads: [DEPLOYMENT], alarms: [ALARM], workloadsCut: { shown: 1, total: 900 } });
    expect(tree.nodes.get("resource:r-3")?.parent).toBe("resource:r-2");
    expect(tree.nodes.get("resource:r-2")?.health).toBe("bad");
    expect(tree.nodes.get("resource:r-1")?.health).toBe("ok");
    expect(tree.nodes.get("pool:pool-gpu-1")?.health).toBe("bad");
    expect(tree.nodes.get("dm:dm-1")?.health).toBe("unknown");
    expect(tree.roots).toEqual(["ocloud:edge-metro-a"]);
    expect(tree.notes[0]).toContain("900");
  });

  // Without alarm data a resource's health is unknown, not healthy.
  it("does not call a resource healthy when alarms could not be read", () => {
    const tree = buildTopology({ topology: TOPOLOGY });
    expect(tree.nodes.get("resource:r-1")?.health).toBe("unknown");
  });
});
