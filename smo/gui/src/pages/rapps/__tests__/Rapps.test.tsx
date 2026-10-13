// @vitest-environment jsdom
/** Tests of the redesigned rApps page (`pages/rapps`): the Directory default tab, the four tabs switching by hash, the summary tiles reading
 * true counts, the instance table's server paging, state filter and flow 07 lifecycle column, the rollouts list, the pinned & attention
 * strip and its pure helpers, and the first-load call budget. Run: `npx vitest run src/pages/rapps`. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import { fakeBff, mountWith, type Call } from "../../../testing/bff";
import { byText, cleanup, click, field, settle, submit, type } from "../../../testing/dom";
import { Rapps } from "..";
import { signatureText } from "../sections/PackageDrawer";
import { instanceLifecycle } from "../data/lifecycle";
import { pickAttention } from "../sections/PinnedAttention";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.history.replaceState(null, "", "/"); });

const IID = "0b9f3f1e-4b0e-4a0c-9d6f-111111111111";
const ALL = [{ method: "GET", pattern: ".*", role: "viewer", queryMatch: {} }, { method: "POST", pattern: ".*", role: "operator", queryMatch: {} },
  { method: "PUT", pattern: ".*", role: "operator", queryMatch: {} }, { method: "DELETE", pattern: ".*", role: "operator", queryMatch: {} }];
const summary = (counts: Record<string, number>) => ({ page: "rapps", computedAt: "now", partial: [], counts: {
  "instances.DEPLOYING": 0, "instances.RUNNING": 0, "instances.UPGRADING": 0, "instances.FAULTED": 0, "instances.UNDEPLOYED": 0, "instances.total": 0,
  "packages.ONBOARDING": 0, "packages.AVAILABLE": 0, "packages.PRIMED": 0, "packages.DEPRECATED": 0, "packages.FAILED": 0, "packages.total": 0, ...counts } });
const dirRow = (state: string, n = 1) => ({ instanceId: `${IID.slice(0, -1)}${n}`, packageId: "p1", name: `rApp ${n}`, version: "1.0", vendor: "Acme", state, autonomyMode: "SHADOW", hasPage: false, operatorApiRegistered: false, pinned: false });

/** A fake BFF for the rApps page with the summary `counts`, one package, a FAULTED instance (UPGRADING under that filter) and a faulted directory row. */
function bff(counts: Record<string, number> = {}, extra: Record<string, unknown> = {}) {
  return fakeBff({
    "GET /me": { username: "ana", role: "operator", csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role: "operator", rules: ALL },
    "GET /summary/rapps": summary(counts),
    "GET /me/pins": { max: 5, items: [] },
    "GET /rapps": (c: Call) => ({ items: c.query.get("state") === "FAULTED" ? [dirRow("FAULTED", 7)] : [], total: 0, limit: 25, offset: 0, owners: [], states: [] }),
    "GET /smo/onboarding/packages": { items: [{ packageId: "p1", name: "Energy", version: "1.2.0", state: "AVAILABLE" }], total: 1, limit: 500, offset: 0 },
    "GET /smo/rapp-mgmt/instances": (c: Call) => ({ items: c.query.get("state") === "UPGRADING" ? [{ instanceId: IID, packageId: "p1", state: "UPGRADING", autonomyMode: "ASSIST" }]
      : [{ instanceId: IID, packageId: "p1", state: "FAULTED", autonomyMode: "SHADOW" }], total: 1, limit: 25, offset: 0 }),
    ...extra,
  });
}
const open = (hash = "") => { if (hash) window.location.hash = hash; return mountWith(<AuthProvider><Rapps /></AuthProvider>, { at: "/rapps" }); };

describe("rApps page", () => {
  // the CI browser check needs the directory on plain /rapps (no hash)
  it("opens on the Directory tab with no hash", async () => {
    bff();
    const { container } = await open();
    await settle(6);
    expect(container.textContent).toContain("rApp directory");
    expect(Array.from(container.querySelectorAll("[role=tab]")).map((t) => t.textContent?.replace(/\d+$/, ""))).toEqual(["Directory", "Instances", "Packages", "Rollouts"]);
  });

  // tiles read the summary's true counts, never a list's length
  it("shows the summary counts in the tiles and the tab pills", async () => {
    bff({ "instances.RUNNING": 471, "instances.total": 487, "instances.UPGRADING": 10, "instances.FAULTED": 6, "packages.total": 162, "packages.AVAILABLE": 140 });
    const { container } = await open();
    await settle(6);
    const tiles = container.querySelector("[data-section='rapps.tiles']")!.textContent!;
    expect(tiles).toContain("471");
    expect(tiles).toContain("of 487 instances");
    expect(tiles).toContain("162");
    expect(byText(container, "[role=tab]", /Instances/)?.textContent).toContain("487");
  });

  // the Instances tab is a server table: paged, state filter as a query parameter, and a flow 07 lifecycle column from the row's state
  it("pages instances on the server, filters by state and draws the lifecycle column", async () => {
    const calls = bff();
    const { container } = await open("instances");
    await settle(6);
    const first = calls.filter((c) => c.path === "/smo/rapp-mgmt/instances").at(-1)!;
    expect(first.query.get("limit")).not.toBeNull();
    expect(first.query.get("offset")).toBe("0");
    const steps = container.querySelector("[data-section='rapps.instances'] .ministeps")!;
    expect(steps.getAttribute("aria-label")).toContain("Faulted · recover");
    expect(steps.querySelectorAll("i.fail")).toHaveLength(1);
    expect(container.querySelector("[data-section='rapps.instances']")!.textContent).toContain("Energy 1.2.0");
    expect(byText(container, "button", "Recover")).not.toBeNull();
    const select = container.querySelector("[data-section='rapps.instances'] select[aria-label='Filter by state']") as HTMLSelectElement;
    select.value = "RUNNING";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    expect(calls.filter((c) => c.path === "/smo/rapp-mgmt/instances").at(-1)!.query.get("state")).toBe("RUNNING");
  });

  // the headline KPI column reads the page's newest metrics in one batched call (ids of the rows shown), first metric shown
  it("shows each instance's headline KPI from one batched read", async () => {
    const calls = bff({}, { "GET /smo/rapp-mgmt/instances/performance/latest": { items: [{ instanceId: IID, at: "2026-10-09T10:00:00Z", metrics: { prbUsage: 41.25, energy: 3 } }] } });
    const { container } = await open("instances");
    await settle(8);
    const batch = calls.filter((c) => c.path === "/smo/rapp-mgmt/instances/performance/latest");
    expect(batch).toHaveLength(1);
    expect(batch[0].query.get("ids")).toBe(IID);
    const table = container.querySelector("[data-section='rapps.instances']")!.textContent!;
    expect(table).toContain("prbUsage 41.3");
    expect(table).toContain("+1");
  });

  // a deleted package (DELETING is final) is left out of "All states" and shown only when DELETING is picked in the filter
  it("leaves deleted packages out of the package list unless DELETING is picked", async () => {
    const calls = bff();
    const { container } = await open("packages");
    await settle(6);
    const packages = () => calls.filter((c) => c.path === "/smo/onboarding/packages" && c.query.get("offset") === "0").at(-1)!;
    expect(packages().query.get("include_deleted")).toBe("false");
    expect(packages().query.get("state")).toBeNull();
    const select = container.querySelector("[data-section='rapps.packages'] select") as HTMLSelectElement;
    select.value = "DELETING";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await settle();
    expect(packages().query.get("state")).toBe("DELETING");
    expect(packages().query.get("include_deleted")).toBeNull();
  });

  // Deploy with an operator API base the rApp is not reached at (credentialsDelivered false) warns at once, instead of leaving it to the rApp's page
  it("warns at Deploy when the rApp could not be reached at the operator API base URL", async () => {
    for (const delivered of [false, true]) {
      bff({}, { "POST /smo/rapp-mgmt/instances": { instanceId: IID, oauthClientId: "inv", credentialsDelivered: delivered } });
      await open("packages");
      await settle(6);
      await click(byText(document.body, "[data-section='rapps.packages'] button", "Deploy")!);
      await settle();
      const base = field(document.body, "Operator API base URL");
      await type(base, "http://energy-saving:8000");
      await submit(base.closest("form")!);
      await settle(6);
      expect(document.body.textContent?.includes("could not be reached at http://energy-saving:8000")).toBe(!delivered);
      cleanup();
    }
  });

  // Rollouts lists only UPGRADING instances, with the resolve actions
  it("lists the rollouts in progress", async () => {
    const calls = bff({ "instances.UPGRADING": 1 });
    const { container } = await open("rollouts");
    await settle(6);
    expect(calls.some((c) => c.path === "/smo/rapp-mgmt/instances" && c.query.get("state") === "UPGRADING")).toBe(true);
    expect(byText(container, "button", "Upgrade succeeded")).not.toBeNull();
    expect(container.textContent).toContain("1 instance being upgraded");
  });

  // tabs switch on click and keep the id in the hash
  it("switches tabs and writes the hash", async () => {
    bff();
    const { container } = await open();
    await settle(4);
    await click(byText(container, "[role=tab]", /Packages/)!);
    await settle(4);
    expect(window.location.hash).toBe("#packages");
    expect(container.querySelector("[data-section='rapps.packages']")).not.toBeNull();
    expect(container.textContent).toContain("Onboard a package");
  });

  // call flow 01a: a chosen .csar is uploaded as multipart to /onboarding/packages/upload, and the location field is not required then
  it("uploads a chosen .csar file instead of posting a location", async () => {
    const calls = bff({}, { "POST /smo/onboarding/packages/upload": { status: 202, body: { packageId: "p9", trackingId: "p9" } } });
    const { container } = await open("#packages");
    await settle(6);
    const form = container.querySelector<HTMLFormElement>("[data-section='rapps.onboard'] form")!;
    const input = form.querySelector<HTMLInputElement>("input[type=file]")!;
    const csar = new File([new Uint8Array([80, 75, 3, 4])], "my-rapp.csar", { type: "application/zip" });
    Object.defineProperty(input, "files", { value: [csar] });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await settle(2);
    expect(form.querySelector<HTMLInputElement>("input[pattern]")!.required).toBe(false);
    await submit(form);
    await settle(4);
    const upload = calls.find((c) => c.method === "POST" && c.path === "/smo/onboarding/packages/upload")!;
    expect((upload.body as FormData).get("file")).toBeInstanceOf(File);
    expect(((upload.body as FormData).get("file") as File).name).toBe("my-rapp.csar");
    expect(calls.some((c) => c.method === "POST" && c.path === "/smo/onboarding/packages")).toBe(false);
  });

  // faulted rApps are asked from the directory only when the summary counts some
  it("asks for faulted rApps only when there are some, and shows them in the strip", async () => {
    const quiet = bff();
    await open();
    await settle(6);
    expect(quiet.some((c) => c.path === "/rapps" && c.query.get("state") === "FAULTED")).toBe(false);
    cleanup();
    const calls = bff({ "instances.FAULTED": 1 });
    const { container } = await open();
    await settle(6);
    expect(calls.some((c) => c.path === "/rapps" && c.query.get("state") === "FAULTED")).toBe(true);
    expect(container.querySelector("[data-section='rapps.pinned']")!.textContent).toContain("rApp 7");
  });

  // first load on plain /rapps stays within the budget: summary, pins, directory (+ session and permissions)
  it("keeps the first-load call budget", async () => {
    const calls = bff();
    await open();
    await settle(6);
    const data = calls.filter((c) => !["/me", "/permissions"].includes(c.path));
    expect(new Set(data.map((c) => c.path)).size).toBeLessThanOrEqual(3);
  });
});

describe("rApps helpers", () => {
  // each instance state maps onto flow 07's five segments
  it("maps instance states to the lifecycle column", () => {
    expect(instanceLifecycle("RUNNING").states).toEqual(["done", "now", "todo", "todo", "todo"]);
    expect(instanceLifecycle("UPGRADING").stage).toBe("Upgrading");
    expect(instanceLifecycle("UNDEPLOYED").states.every((s) => s === "done")).toBe(true);
    expect(instanceLifecycle(null).states.every((s) => s === "todo")).toBe(true);
  });
  // pins come first, duplicates drop, at most six
  it("orders and caps the attention strip", () => {
    const pins = [dirRow("RUNNING", 1)];
    const faulted = [dirRow("FAULTED", 1), ...[2, 3, 4, 5, 6, 7].map((n) => dirRow("FAULTED", n))];
    const out = pickAttention(pins, faulted, [dirRow("UPGRADING", 8)]);
    expect(out).toHaveLength(6);
    expect(out[0].state).toBe("RUNNING");
    expect(new Set(out.map((r) => r.instanceId)).size).toBe(6);
  });
});

describe("the package signature", () => {
  // GUI-10.2: the drawer says what Onboarding reports (verified against its trust store, or accepted unsigned) and never claims a "dev cert"
  it("says what Onboarding reports and nothing more", () => {
    expect(signatureText(true)).toBe("verified against Onboarding's trust store");
    expect(signatureText(false)).toContain("not verified");
    expect(signatureText(undefined)).toBe("not reported");
    for (const v of [true, false, undefined]) expect(signatureText(v)).not.toMatch(/dev/i);
  });
});
