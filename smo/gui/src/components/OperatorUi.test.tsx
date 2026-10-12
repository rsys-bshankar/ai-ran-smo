// @vitest-environment jsdom
/**
 * Component tests of the generic renderer of a rApp's declared operator page (components/OperatorUi.tsx): the panels and how values are formatted, unknown or garbled declarations,
 * the change buttons and their role gating, the row drawer, KPI tiles and charts, and polling. The backend is `fakeBff` (testing/bff.tsx) answering under /api/rapps/<id>/operator and
 * /api/smo; the DOM is jsdom, mounted with testing/dom.tsx, no server. `ES` is a declaration modelled on the Energy Saving sample rApp. Run: `cd gui && npx vitest run src/components/OperatorUi.test.tsx`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeclaredPage } from "./OperatorUi";
import type { Declaration } from "../api/rapps";
import { fakeBff, mountWith, type Call } from "../testing/bff";
import { byText, cleanup, click, settle, submit, type } from "../testing/dom";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
beforeEach(() => { document.body.innerHTML = ""; });

const IID = "0b9f3f1e-4b0e-4a0c-9d6f-111111111111";
const OP = `/rapps/${IID}/operator`;

const DASHBOARD = {
  cells: [
    { cellId: "C1", state: "SLEEP", overrideBy: null, o1Value: 0.5, active: true, tags: ["a", "b"], prbTrend: [{ t: "2026-10-08T10:00:00Z", v: 4 }, { t: "2026-10-08T10:05:00Z", v: 9 }],
      latestDecision: { decision: "SLEEP", prediction: { model: { futurePrb: 12.34 } }, reason: "low <b>load</b>" } },
    { cellId: "C2", state: "SERVING", overrideBy: "alice", o1Value: 80, active: false, tags: [], prbTrend: [], latestDecision: null },
  ],
};

const ES: Declaration = {
  version: 1,
  panels: [
    { id: "instance", title: "Instance", kind: "keyValues", source: { path: "/instances/{instanceId}", refreshSeconds: 30 },
      items: [{ label: "Managed element", path: "managedElementRef" }, { label: "Mode", path: "autonomyMode", format: "badge" }, { label: "Model", path: "modelId", format: "id" }, { label: "Gone", path: "nothing.here" }] },
    { id: "controls", title: "Closed loop", kind: "actions", actions: [
      { id: "evaluate", label: "Evaluate now", tone: "primary", method: "POST", path: "/instances/{instanceId}/evaluate", success: "Pass complete" },
      { id: "tune", label: "Tune", method: "PUT", path: "/instances/{instanceId}/tune", success: "Tuned", confirm: "Really tune?",
        inputs: [{ name: "reason", label: "Reason", type: "string", required: true }, { name: "level", label: "Level", type: "integer", min: 1, max: 5 }] }] },
    { id: "cells", title: "Cells", kind: "table", source: { path: "/instances/{instanceId}/dashboard", query: { points: 48 }, refreshSeconds: 15 }, rows: "cells", rowKey: "cellId", empty: "No managed cells.",
      columns: [
        { path: "cellId", label: "Cell" }, { path: "state", label: "State", format: "badge" }, { path: "o1Value", label: "O1", format: "number", unit: "dB" },
        { path: "active", label: "Active", format: "boolean" }, { path: "tags", label: "Tags", format: "list" },
        { path: "prbTrend", label: "PRB trend", format: "sparkline", y: "v" }, { path: "latestDecision.prediction.model.futurePrb", label: "Predicted", format: "percent" },
        { path: "latestDecision.reason", label: "Reason" }],
      rowActions: [
        { id: "unlock-cell", label: "Override: unlock", tone: "danger", method: "POST", path: "/instances/{instanceId}/cells/{row.cellId}/override", confirm: "Unlock this cell?", success: "Unlocked", when: { path: "overrideBy", exists: false } },
        { id: "clear-override", label: "Clear override", method: "DELETE", path: "/instances/{instanceId}/cells/{row.cellId}/override", success: "Cleared", when: { path: "overrideBy", exists: true } }],
      rowDetail: { title: "Cell {row.cellId}", blocks: [
        { kind: "chart", title: "PRB utilisation", type: "line", points: "prbTrend", x: "t", y: "v" },
        { kind: "json", title: "Latest execution", path: "latestDecision", empty: "No decision yet." },
        { kind: "keyValues", title: "Fields", items: [{ label: "State", path: "state", format: "badge" }] },
        { kind: "table", title: "History", source: { path: "/instances/{instanceId}/decisions", query: { cell_id: "{row.cellId}", limit: 20 } }, rows: "items", empty: "No decisions.",
          columns: [{ path: "reason", label: "Reason" }, { path: "decision", label: "Decision", format: "badge" }] },
        { kind: "gauge", title: "From the future" }] } },
  ],
};

/**
 * Starts the fake BFF with the answers `ES` needs (instance, dashboard, decisions, the change routes, the performance report); `extra` adds or replaces routes. Returns the recorded calls.
 */
function bff(extra: Record<string, unknown> = {}) {
  return fakeBff({
    [`GET ${OP}/instances/${IID}`]: { managedElementRef: "ne-1", autonomyMode: "ASSIST", modelId: "11111111-2222-3333-4444-555555555555" },
    [`GET ${OP}/instances/${IID}/dashboard`]: DASHBOARD,
    [`GET ${OP}/instances/${IID}/decisions`]: (c: Call) => ({ items: [{ reason: `for ${c.query.get("cell_id")}`, decision: "SLEEP" }] }),
    [`POST ${OP}/instances/${IID}/evaluate`]: { ok: true },
    [`PUT ${OP}/instances/${IID}/tune`]: { ok: true },
    [`POST ${OP}/instances/${IID}/cells/C1/override`]: { ok: true },
    [`DELETE ${OP}/instances/${IID}/cells/C2/override`]: { status: 204 },
    [`GET /smo/rapp-mgmt/instances/${IID}/performance`]: { items: [{ metrics: { throughputMbps: 123.4, latencyMs: 8 }, reportedAt: "2026-10-08T10:00:00Z" }] },
    ...extra,
  });
}

const page = (declaration: Declaration, props: { canChange?: boolean; registered?: boolean } = {}) =>
  mountWith(<DeclaredPage instanceId={IID} declaration={declaration} canChange={props.canChange ?? true} operatorApiRegistered={props.registered ?? true} />);

const buttons = (c: HTMLElement) => Array.from(c.querySelectorAll("button")).map((b) => b.textContent);

describe("DeclaredPage: admin-only actions", () => {
  // An action declared `role: admin` (a demo or simulation button) is not drawn for a user who is not an admin; a line names it instead, and the other buttons stay.
  it("leaves an admin-only action out for a user who is not an admin and names it", async () => {
    bff();
    const demo: Declaration = { version: 1, panels: [{ id: "demo", title: "Demo", kind: "actions", actions: [
      { id: "evaluate", label: "Evaluate now", method: "POST", path: "/instances/{instanceId}/evaluate", success: "ok" },
      { id: "live", label: "Send live PM", role: "admin", method: "POST", path: "/instances/{instanceId}/demo/live", success: "ok" }] }] };
    const { container } = await page(demo);
    await settle();
    expect(buttons(container)).toEqual(["Evaluate now"]);
    expect(container.textContent).toContain("Only an admin can use: Send live PM.");
  });
});

describe("DeclaredPage: panels and values", () => {
  // Each declared panel reads its own source with the route filled in and the declared query, and the cards keep the declared order.
  it("draws each panel from its source with the route and the declared query", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    expect(calls.some((c) => c.path === `${OP}/instances/${IID}` && c.method === "GET")).toBe(true);
    const dash = calls.find((c) => c.path === `${OP}/instances/${IID}/dashboard`)!;
    expect(dash.query.get("points")).toBe("48");
    expect(Array.from(container.querySelectorAll("h2")).map((h) => h.textContent)).toEqual(["Instance", "Closed loop", "Cells"]);
    expect(container.textContent).toContain("ne-1");
    expect(container.querySelector(".badge")?.textContent).toBe("ASSIST");
  });

  // A value is shown in its declared format, and markup in a value stays text; a missing value is a dash, never "undefined".
  it("formats table cells: badge, number with unit, boolean, list, percent, sparkline, and a dash for a missing value", async () => {
    bff();
    const { container } = await page(ES);
    await settle();
    const rows = Array.from(container.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    const c1 = Array.from(rows[0].querySelectorAll("td")).map((td) => td.textContent);
    expect(c1.slice(0, 4)).toEqual(["C1", "SLEEP", "0.5 dB", "yes"]);
    expect(c1[4]).toBe("a, b");
    expect(rows[0].querySelector("svg[aria-label='PRB trend']")).not.toBeNull();
    expect(c1[6]).toBe("12.3 %");
    expect(c1[7]).toBe("low <b>load</b>");                       // markup is text
    expect(rows[0].querySelector("b")).toBeNull();
    const c2 = Array.from(rows[1].querySelectorAll("td")).map((td) => td.textContent);
    expect(c2[6]).toBe("—");
    expect(c2[7]).toBe("—");
    expect(rows[1].textContent).toContain("no");
  });

  // An empty list shows the declared empty text, and a key-values item whose field is absent shows a dash.
  it("shows the empty text when the list is empty and a dash for a missing field in a key-values panel", async () => {
    bff({ [`GET ${OP}/instances/${IID}/dashboard`]: { cells: [] } });
    const { container } = await page(ES);
    await settle();
    expect(container.textContent).toContain("No managed cells.");
    const dt = Array.from(container.querySelectorAll("dt")).find((d) => d.textContent === "Gone")!;
    expect(dt.nextElementSibling?.textContent).toBe("—");
  });

  // A failing read shows the error, and the "operator API not registered" answer gets its own explanation rather than a raw error.
  it("an error from the rApp is shown, and 'not registered' is explained", async () => {
    bff({ [`GET ${OP}/instances/${IID}/dashboard`]: { status: 502, body: { title: "UPSTREAM_UNAVAILABLE", detail: "the rApp's operator API could not be reached" } },
          [`GET ${OP}/instances/${IID}`]: { status: 404, body: { title: "OPERATOR_API_NOT_REGISTERED" } } });
    const { container } = await page(ES);
    await settle();
    expect(container.textContent).toContain("UPSTREAM_UNAVAILABLE");
    expect(container.textContent).toContain("operator API is not registered");
  });

  // Before the rApp registers its operator API no operator route is called at all, and the page says why instead of showing errors.
  it("without a registered operator API the panels that read are not requested and the page says why", async () => {
    const calls = bff();
    const { container } = await page(ES, { registered: false });
    await settle();
    expect(calls.filter((c) => c.path.startsWith(OP))).toEqual([]);
    expect(container.textContent).toContain("operator API is not registered");
    expect(container.textContent).toContain("Available once the rApp registers its operator API");
  });

  // A hostile declaration or answer (script, image, bold tags) must produce no element, and a badge colour comes from the GUI's state words, never from the value.
  it("draws everything the declaration says as text, never as markup", async () => {
    bff({ [`GET ${OP}/instances/${IID}`]: { managedElementRef: "<img src=x onerror=alert(1)>", autonomyMode: "<script>x</script>" } });
    const decl: Declaration = { version: 1, panels: [{ id: "x", title: "<script>alert(1)</script>", kind: "keyValues", source: { path: "/instances/{instanceId}" },
      items: [{ label: "<b>bold</b>", path: "managedElementRef" }, { label: "Mode", path: "autonomyMode", format: "badge" }] }] };
    const { container } = await page(decl);
    await settle();
    expect(container.querySelector("script, img, b")).toBeNull();
    expect(container.querySelector("h2")?.textContent).toBe("<script>alert(1)</script>");
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(container.querySelector(".badge")?.className).toBe("badge b-mute tone-muted");        // the colour is from the GUI's table of state words, not from the value
  });
});

describe("DeclaredPage: unknown things", () => {
  // A panel kind from a newer GUI is a card saying "unsupported panel" and does not stop the other panels from drawing.
  it("an unknown panel kind is a card saying so and the other panels are drawn", async () => {
    bff();
    const decl: Declaration = { version: 1, panels: [{ id: "a", title: "From the future", kind: "heatmap", source: { path: "/x" } }, ES.panels[0]] };
    const { container } = await page(decl);
    await settle();
    expect(Array.from(container.querySelectorAll("h2")).map((h) => h.textContent)).toEqual(["From the future", "Instance"]);
    expect(container.textContent).toContain("unsupported panel");
    expect(container.textContent).toContain("ne-1");
  });

  // A table whose columns lack a label is unsupported as a whole, while an unknown format name only falls back to plain text.
  it("an unknown column shape makes the table unsupported, an unknown format is text", async () => {
    bff();
    const bad: Declaration = { version: 1, panels: [
      { id: "t1", title: "Odd columns", kind: "table", source: { path: "/instances/{instanceId}/dashboard" }, rows: "cells", rowKey: "cellId", columns: [{ path: "cellId" }] },
      { id: "t2", title: "Odd format", kind: "table", source: { path: "/instances/{instanceId}/dashboard" }, rows: "cells", rowKey: "cellId", columns: [{ path: "cellId", label: "Cell", format: "hologram" }] }] };
    const { container } = await page(bad);
    await settle();
    expect(container.textContent).toContain("unsupported panel");
    expect(Array.from(container.querySelectorAll("tbody tr")).map((r) => r.textContent)).toEqual(["C1", "C2"]);
  });

  // Missing, mistyped or null parts of a stored declaration must degrade to "unsupported" cards and never crash the page.
  it("garbage in a declaration never throws", async () => {
    bff();
    const garbage = { version: 1, panels: [{}, { kind: 5 }, { kind: "table" }, { kind: "kpis", title: "k" }, { kind: "chart", title: "c" }, { kind: "actions", title: "a", actions: [5, null] },
      { kind: "keyValues", title: "kv", items: "no" }, { kind: "table", title: "t", columns: "no" }, { id: "z", title: { a: 1 }, kind: "actions", actions: [{ id: "x" }] }] } as unknown as Declaration;
    const { container } = await page(garbage);
    await settle();
    expect(container.textContent).toContain("unsupported panel");
  });
});

describe("DeclaredPage: buttons", () => {
  // Without the right to change, no button is drawn anywhere (not even in rows), the page says which role is needed, and there is no empty actions column.
  it("a viewer (canChange false) sees no change button anywhere", async () => {
    bff();
    const { container } = await page(ES, { canChange: false });
    await settle();
    expect(buttons(container).filter((b) => b && b !== "↻")).toEqual([]);
    expect(container.textContent).toContain("Changing needs the operator role");
    expect(container.querySelectorAll("th").length).toBe(8);                  // no actions column either
  });

  // A user who may change sees the panel buttons, and a row button only on rows whose `when` condition matches.
  it("an operator sees the buttons, and a row action only for the rows its `when` allows", async () => {
    bff();
    const { container } = await page(ES);
    await settle();
    const rows = Array.from(container.querySelectorAll("tbody tr"));
    expect(buttons(rows[0] as HTMLElement)).toEqual(["Override: unlock"]);
    expect(buttons(rows[1] as HTMLElement)).toEqual(["Clear override"]);
    expect(buttons(container)).toContain("Evaluate now");
  });

  // A button sends its declared method to the declared route with its action id in X-Action-Id and an empty JSON body, and the declared success text is shown.
  it("a button posts to the declared route with its action id and no extra fields, and tells the user", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    await click(byText(container, "button", "Evaluate now")!);
    await settle();
    const sent = calls.find((c) => c.method === "POST" && c.path === `${OP}/instances/${IID}/evaluate`)!;
    expect(sent.headers["x-action-id"]).toBe("evaluate");
    expect(sent.body).toEqual({});
    expect(document.body.textContent).toContain("Pass complete");
  });

  // A button with a confirm text asks the user first: refusing sends nothing, accepting sends the call.
  it("a confirm text asks first: no is no call, yes is a call", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    const ask = vi.spyOn(window, "confirm").mockReturnValue(false);
    await click(byText(container, "button", "Override: unlock")!);
    await settle();
    expect(ask).toHaveBeenCalledWith("Unlock this cell?");
    expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/override"))).toEqual([]);
    ask.mockReturnValue(true);
    await click(byText(container, "button", "Override: unlock")!);
    await settle();
    const sent = calls.find((c) => c.path === `${OP}/instances/${IID}/cells/C1/override`)!;
    expect(sent.method).toBe("POST");
    expect(sent.headers["x-action-id"]).toBe("unlock-cell");
    ask.mockRestore();
  });

  // A DELETE action carries no body.
  it("a DELETE sends no body", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    await click(byText(container, "button", "Clear override")!);
    await settle();
    const sent = calls.find((c) => c.method === "DELETE")!;
    expect(sent.path).toBe(`${OP}/instances/${IID}/cells/C2/override`);
    expect(sent.body).toBeUndefined();
  });

  // An action with inputs asks for them in a dialog, reports each field error without sending, and sends the typed values (after the confirm) once they are valid.
  it("an action with inputs asks for them, checks them, then confirms and sends them", async () => {
    const calls = bff();
    const ask = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { container } = await page(ES);
    await settle();
    await click(byText(container, "button", "Tune")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    expect(dialog.querySelectorAll("input")).toHaveLength(2);
    await submit(dialog.querySelector("form") as HTMLFormElement);
    expect(dialog.textContent).toContain("is required");
    expect(calls.filter((c) => c.method === "PUT")).toEqual([]);
    const [reason, level] = Array.from(dialog.querySelectorAll("input"));
    await type(reason, "because");
    await type(level, "9");
    await submit(dialog.querySelector("form") as HTMLFormElement);
    expect(dialog.textContent).toContain("must be at most 5");
    await type(level, "3");
    await submit(dialog.querySelector("form") as HTMLFormElement);
    await settle();
    expect(ask).toHaveBeenCalledWith("Really tune?");
    const sent = calls.find((c) => c.method === "PUT")!;
    expect(sent.body).toEqual({ reason: "because", level: 3 });
    expect(sent.headers["x-action-id"]).toBe("tune");
    expect(document.querySelector("[role=dialog]")).toBeNull();
    ask.mockRestore();
  });

  // A refused change is shown as a toast with the action id and the server's reason, and the page stays.
  it("a failed change tells the user and keeps the page", async () => {
    bff({ [`POST ${OP}/instances/${IID}/evaluate`]: { status: 409, body: { title: "BUSY", detail: "a pass is running" } } });
    const { container } = await page(ES);
    await settle();
    await click(byText(container, "button", "Evaluate now")!);
    await settle();
    expect(document.body.textContent).toContain("evaluate failed");
    expect(document.body.textContent).toContain("BUSY");
  });

  // A row value that is not one safe path segment ("../x") must not be put into a route: the row button is not drawn, so nothing can be sent.
  it("a row value that is not one safe segment sends nothing (the button is not drawn)", async () => {
    bff({ [`GET ${OP}/instances/${IID}/dashboard`]: { cells: [{ cellId: "../x", state: "A" }] } });
    const { container } = await page(ES);
    await settle();
    expect(buttons(container.querySelector("tbody tr") as HTMLElement)).toEqual([]);
  });
});

describe("DeclaredPage: the drawer of a row", () => {
  // A row click opens the drawer with the title filled from the row and every block drawn; a block that fetches does so for the clicked row only, and an unknown block kind says so.
  it("opens on a click with the title filled from the row and every block drawn", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    await click(container.querySelector("tbody tr") as HTMLElement);
    await settle();
    const drawer = document.querySelector("[role=dialog]") as HTMLElement;
    expect(drawer.querySelector("h2")?.textContent).toBe("Cell C1");
    expect(Array.from(drawer.querySelectorAll("h3")).map((h) => h.textContent)).toEqual(["PRB utilisation", "Latest execution", "Fields", "History", "From the future"]);
    expect(drawer.querySelector("svg[role=img]")).not.toBeNull();                        // the chart block, from the row's own series
    expect(drawer.querySelector("pre")?.textContent).toContain("futurePrb");              // the json block
    expect(drawer.textContent).toContain("for C1");                                        // the fetched table, bound to the clicked row
    expect(calls.filter((c) => c.path === `${OP}/instances/${IID}/decisions`).map((c) => [c.query.get("cell_id"), c.query.get("limit")])).toEqual([["C1", "20"]]);
    expect(drawer.textContent).toContain("unsupported block");                              // the block kind this build does not know
  });

  // A json block whose field is missing shows its declared empty text, and nothing is fetched for a row until its drawer is opened.
  it("shows the empty text of a json block whose field is missing, and fetches only for the open row", async () => {
    const calls = bff();
    const { container } = await page(ES);
    await settle();
    expect(calls.filter((c) => c.path.endsWith("/decisions"))).toEqual([]);                // nothing fetched before a row is opened
    await click(container.querySelectorAll("tbody tr")[1] as HTMLElement);
    await settle();
    const drawer = document.querySelector("[role=dialog]") as HTMLElement;
    expect(drawer.textContent).toContain("No decision yet.");
    expect(calls.filter((c) => c.path.endsWith("/decisions")).map((c) => c.query.get("cell_id"))).toEqual(["C2"]);
  });

  // The drawer closes with its close button.
  it("closes with the close button", async () => {
    bff();
    const { container } = await page(ES);
    await settle();
    await click(container.querySelector("tbody tr") as HTMLElement);
    await click(document.querySelector("[aria-label=Close]") as HTMLElement);
    expect(document.querySelector("[role=dialog]")).toBeNull();
  });

  // A table that declares no row detail has no clickable rows.
  it("a table without rowDetail is not clickable", async () => {
    bff();
    const decl: Declaration = { version: 1, panels: [{ ...ES.panels[2], rowDetail: undefined }] };
    const { container } = await page(decl);
    await settle();
    expect(container.querySelector("tr.clickable")).toBeNull();
  });
});

describe("DeclaredPage: KPI tiles and charts", () => {
  const KPI: Declaration = { version: 1, panels: [
    { id: "tiles", title: "Tiles", kind: "kpis", source: { path: "/instances/{instanceId}/kpis" }, tiles: [{ label: "Cells", path: "cellCount", format: "number" }, { label: "Saved", path: "saved.pct", format: "percent" }, { label: "Missing", path: "nope" }] },
    { id: "platform", title: "Reported", kind: "kpis", tiles: [{ label: "Throughput", kpi: "throughputMbps", unit: "Mbps" }, { label: "Latency", kpi: "latencyMs" }, { label: "Unknown", kpi: "nothing" }] }] };

  // A tile bound to a path reads the panel's source, a tile bound to a KPI name reads the latest performance report (fetched once), and a missing number is a dash.
  it("path tiles read the source, kpi tiles read the latest report, a missing number is a dash", async () => {
    const calls = bff({ [`GET ${OP}/instances/${IID}/kpis`]: { cellCount: 12, saved: { pct: 37.25 } } });
    const { container } = await page(KPI);
    await settle();
    const tiles = Array.from(container.querySelectorAll(".stat")).map((s) => [s.querySelector(".stat-label")?.textContent, s.querySelector(".stat-value")?.textContent]);
    expect(tiles).toEqual([["Cells", "12"], ["Saved", "37.3 %"], ["Missing", "—"], ["Throughput", "123 Mbps"], ["Latency", "8"], ["Unknown", "—"]]);
    expect(calls.filter((c) => c.path.includes("/performance"))).toHaveLength(1);
  });

  // A panel whose tiles all name KPIs works without an operator API, because those values come from the rApp's performance reports.
  it("a panel bound only to KPI names needs no operator API", async () => {
    const calls = bff();
    const only: Declaration = { version: 1, panels: [KPI.panels[1]] };
    const { container } = await page(only, { registered: false });
    await settle();
    expect(calls.filter((c) => c.path.startsWith(OP))).toEqual([]);
    expect(container.querySelectorAll(".stat")).toHaveLength(3);
  });

  const SERIES = { points: [
    { t: "2026-10-08T10:00:00Z", v: 1, kind: "A" }, { t: "2026-10-08T10:05:00Z", v: 3, kind: "A" }, { t: "2026-10-08T10:00:00Z", v: 2, kind: "B" }, { t: "2026-10-08T10:05:00Z", v: 5, kind: "B" }] };

  // A line chart draws one line per value of `seriesBy` and a legend naming them.
  it("a line chart splits the points into one series per seriesBy value, with a legend", async () => {
    bff({ [`GET ${OP}/instances/${IID}/trend`]: SERIES });
    const decl: Declaration = { version: 1, panels: [{ id: "c", title: "Trend", kind: "chart", source: { path: "/instances/{instanceId}/trend" }, type: "line", points: "points", x: "t", y: "v", seriesBy: "kind", unit: "%" }] };
    const { container } = await page(decl);
    await settle();
    expect(container.querySelectorAll("path.chart-line")).toHaveLength(2);
    expect(Array.from(container.querySelectorAll(".chart-key")).map((k) => k.textContent?.trim())).toEqual(["A", "B"]);
  });

  // A bar chart draws one bar per point, and an empty series is reported as "no data".
  it("a bar chart draws a bar per point, and no data is said so", async () => {
    bff({ [`GET ${OP}/instances/${IID}/trend`]: SERIES, [`GET ${OP}/instances/${IID}/none`]: { points: [] } });
    const bar: Declaration = { version: 1, panels: [{ id: "c", title: "Bars", kind: "chart", source: { path: "/instances/{instanceId}/trend" }, type: "bar", points: "points", x: "t", y: "v" },
      { id: "d", title: "Nothing", kind: "chart", source: { path: "/instances/{instanceId}/none" }, type: "line", points: "points", x: "t", y: "v" }] };
    const { container } = await page(bar);
    await settle();
    expect(container.querySelectorAll("rect.chart-bar")).toHaveLength(4);
    expect(container.textContent).toContain("no data");
  });

  // A chart type this build does not know (radar) makes the panel unsupported.
  it("a chart with an unknown type is an unsupported panel", async () => {
    bff();
    const decl: Declaration = { version: 1, panels: [{ id: "c", title: "Radar", kind: "chart", source: { path: "/x" }, type: "radar", points: "p", x: "t", y: "v" }] };
    const { container } = await page(decl);
    await settle();
    expect(container.textContent).toContain("unsupported panel");
  });
});

describe("DeclaredPage: refreshSeconds", () => {
  // A source with `refreshSeconds` is read again after that interval and not before.
  it("polls a source at its interval and not before", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = bff();
    const decl: Declaration = { version: 1, panels: [{ ...ES.panels[0], source: { path: "/instances/{instanceId}", refreshSeconds: 5 } }] };
    await page(decl);
    await settle();
    const reads = () => calls.filter((c) => c.path === `${OP}/instances/${IID}`).length;
    expect(reads()).toBe(1);
    const { act } = await import("react");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reads()).toBeGreaterThanOrEqual(2);
  });

  // A source without `refreshSeconds` is read once and then only by the panel's refresh button.
  it("does not poll a source without refreshSeconds, and the refresh button reads again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = bff();
    const decl: Declaration = { version: 1, panels: [{ ...ES.panels[0], source: { path: "/instances/{instanceId}" } }] };
    const { container } = await page(decl);
    await settle();
    const { act } = await import("react");
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    const reads = () => calls.filter((c) => c.path === `${OP}/instances/${IID}`).length;
    expect(reads()).toBe(1);
    await click(container.querySelector("[aria-label='Refresh this panel']") as HTMLElement);
    await settle();
    expect(reads()).toBe(2);
  });
});
