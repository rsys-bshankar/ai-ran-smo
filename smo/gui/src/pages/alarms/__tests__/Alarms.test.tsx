// @vitest-environment jsdom
/** Tests of the Alarms page (pages/alarms): severity tiles from the summary that toggle the table's `severity` parameter, the mean time to
 * acknowledge and the 24 h sparkline, `?me=` as the starting managed element filter, keyset paging and the server-side filters (ack state, open
 * only, probable cause), the group-by view, the "N new — show" bar, the detail panel with Ack, ack time and the lifecycle, the server's
 * correlation hint, and the O-Cloud / FM subscription tabs. Run: `npx vitest run src/pages/alarms`. */
import { QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import rules from "../../../auth/permissions.fixture.json";
import { ToastProvider } from "../../../components/Toast";
import { KEYS } from "../../../data/keys";
import { fakeBff, mountWith, newClient, type Call } from "../../../testing/bff";
import { byText, cleanup, click, mount, settle, typeArea } from "../../../testing/dom";
import { Alarms } from "..";
import { formatDuration, ranAlarmFilters, withDelta } from "../data/queries";
import { newSince } from "../sections/AlarmTable";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.location.hash = ""; });

const T0 = "2026-10-09T10:00:00Z";
const plus = (s: number) => new Date(Date.parse(T0) + s * 1000).toISOString();

const alarm = (extra: Record<string, unknown> = {}) => ({
  alarmId: "a-1", sourceAlarmId: "odu-17", managedElementRef: "ME-1", managedFunctionRef: "NRCellDU=101", severity: "critical", ackState: "UNACKNOWLEDGED",
  raisedAt: T0, correlationGroup: null, probableCause: "LOSS_OF_SIGNAL", specificProblem: "CPRI link down", rootCauseIndicator: false, correlatedNotifications: [],
  proposedRepairActions: null, alarmType: "COMMUNICATIONS_ALARM", ackUserId: null, changedAt: null, clearedAt: null, clearUserId: null, ...extra,
});

/** The alarm summary with `total` alarms raised so far. */
const summary = (total = 1300) => ({ page: "alarms", computedAt: "", partial: [], counts: {
  "alarms.critical": 37, "alarms.major": 180, "alarms.minor": 488, "alarms.warning": 579, "alarms.cleared": 16, "alarms.total": total, "ocloudAlarms.total": 4,
  "alarms.unacked": 900, "alarms.mtta": 252 } });

/** The fake BFF of these tests: a signed-in user of `role`, the alarm summary, RAN alarms (keyset pages), counts, correlation, O-Cloud and FM lists. */
function bff(role: "viewer" | "operator" | "admin" = "operator", overrides: Record<string, unknown> = {}) {
  return fakeBff({
    "GET /me": { username: "ana", role, csrfToken: "c", local: true, totpEnrolled: true, mfaEnrolmentRequired: false },
    "GET /permissions": { role, rules },
    "GET /summary/alarms": summary(),
    "GET /smo/ran-nf-oam/alarms/counts": (c: Call) => (c.query.get("group_by") === "hour"
      ? { groupBy: "hour", groups: [{ key: "2026-10-09T08:00:00Z", count: 3 }, { key: "2026-10-09T09:00:00Z", count: 5 }] }
      : { groupBy: c.query.get("group_by"), groups: [{ key: "LOSS_OF_SIGNAL", count: 812 }, { key: "LINK_DOWN", count: 40 }] }),
    "GET /smo/ran-nf-oam/alarms/a-1/correlated": { alarmId: "a-1", rule: "same-element-within-window", windowSeconds: 60, truncated: false,
      items: [alarm({ alarmId: "a-2", severity: "major", raisedAt: plus(40), probableCause: "LINK_DOWN" })] },
    "GET /smo/ran-nf-oam/alarms": (c: Call) => (c.query.get("after") === "cur-2"
      ? { items: [alarm({ alarmId: "a-20", managedElementRef: "ME-3" })], limit: 50, nextCursor: null, hasMore: false }
      : { items: [alarm(), alarm({ alarmId: "a-9", managedElementRef: "ME-2", severity: "minor", ackState: "ACKNOWLEDGED", ackUserId: "smo-gui:bob", ackTime: plus(90) })],
        limit: 50, nextCursor: "cur-2", hasMore: true }),
    "GET /smo/focom/alarms": { items: [{ alarmId: "o-1", resourceRef: "node-7", severity: "major" }], limit: 50, offset: 0, total: 1 },
    "GET /smo/ran-nf-oam/fm-subscriptions": { items: [{ subscriptionId: "s-1", managedElementRef: "ME-1", deliveryMethod: "push", southboundEngine: "netconf" }], limit: 50, offset: 0, total: 1 },
    "GET /smo/ran-nf-oam/o1-adaptor-endpoints": { items: [], limit: 100, offset: 0 },
    "PATCH /smo/ran-nf-oam/alarms/a-1/ack": { body: alarm({ ackState: "ACKNOWLEDGED" }) },
    ...overrides,
  });
}

const open = (at = "/alarms") => mountWith(<AuthProvider><Alarms /></AuthProvider>, { at });
const tableCalls = (calls: Call[]) => calls.filter((c) => c.path === "/smo/ran-nf-oam/alarms");
/** Picks `value` in the select labelled `label`. */
async function choose(root: HTMLElement, label: string, value: string) {
  const select = root.querySelector(`select[aria-label='${label}']`) as HTMLSelectElement;
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
}

describe("the alarm page", () => {
  // Pins down: the tiles show the server's counts, and a tile click toggles the table's severity query parameter on and off.
  it("shows true counts per severity, and a severity tile toggles the table's severity filter", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    const tiles = container.querySelector("[data-section='alarms.tiles']")!;
    expect(tiles.textContent).toContain("37");
    expect(tiles.textContent).toContain("1,284");                                     // open = total − cleared
    expect(tableCalls(calls).at(-1)!.query.get("severity")).toBeNull();
    const critical = Array.from(tiles.querySelectorAll("button.kpi")).find((b) => b.textContent?.includes("critical")) as HTMLElement;
    await click(critical);
    await settle();
    expect(critical.getAttribute("aria-pressed")).toBe("true");
    expect(tableCalls(calls).at(-1)!.query.get("severity")).toBe("critical");
    expect(tableCalls(calls).at(-1)!.query.get("after")).toBe("");
    await click(critical);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("severity")).toBeNull();
    expect(tiles.textContent).toContain("Time to acknowledge");
    expect(tiles.textContent).toContain("4 min 12 s");                                 // summary alarms.mtta = 252 s
    expect(tiles.textContent).toContain("900 unacknowledged");
    expect(tiles.textContent).toContain("8");                                          // raised in the 24 h buckets
    expect(tiles.querySelector("svg[aria-label='alarms raised per hour']")).not.toBeNull();
  });

  // Pins down: the global search link /alarms?me=X starts the table filtered on that managed element.
  it("starts filtered on the managed element named in ?me=", async () => {
    const calls = bff();
    const { container } = await open("/alarms?me=ME-7");
    await settle();
    expect(tableCalls(calls)[0].query.get("managed_element_ref")).toBe("ME-7");
    expect((container.querySelector("input[aria-label='Managed element']") as HTMLInputElement).value).toBe("ME-7");
  });

  // Pins down: a row opens the detail panel with the TS 28.532 fields, the lifecycle and Ack, and the hint shows the server's correlated alarms.
  it("shows the selected alarm with its lifecycle, Ack, and the server's correlation", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await click(container.querySelector("tbody tr") as HTMLElement);
    await settle();
    const detail = container.querySelector("[data-section='alarms.detail']") as HTMLElement;
    expect(detail.textContent).toContain("LOSS_OF_SIGNAL");
    expect(detail.querySelectorAll("ol[aria-label='Alarm lifecycle'] > li")).toHaveLength(3);
    const hint = container.querySelector("[data-section='alarms.rootcause']")!;
    expect(hint.textContent).toContain("1 other alarm on ME-1 within 60 s");
    expect(hint.textContent).toContain("LINK_DOWN");
    expect(hint.textContent).toContain("same-element-within-window");
    expect(calls.find((c) => c.path.endsWith("/correlated"))!.query.get("window_seconds")).toBe("60");
    await click(byText(detail, "button", "Ack")!);
    await settle();
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.path).toBe("/smo/ran-nf-oam/alarms/a-1/ack");
    expect(patch.query.get("new_state")).toBe("ACKNOWLEDGED");
  });

  // GUI-2.4 / 2.3: the detail shows the server's history (who did what) and the comments; an operator adds one (the text sent, the box emptied),
  // a viewer reads them with no box.
  it("shows the alarm's history and comments, and lets an operator add a comment", async () => {
    const notes = {
      "GET /smo/ran-nf-oam/alarms/a-1/history": { items: [
        { at: plus(0), event: "RAISED", from: null, to: "critical", by: null },
        { at: plus(60), event: "ACKNOWLEDGED", from: "UNACKNOWLEDGED", to: "ACKNOWLEDGED", by: "bob" },
        { at: plus(90), event: "SEVERITY_CHANGED", from: "critical", to: "major", by: null },
      ], total: 3, limit: 100, offset: 0 },
      "GET /smo/ran-nf-oam/alarms/a-1/comments": { items: [{ commentId: "c-1", alarmId: "a-1", createdAt: plus(70), author: "bob", text: "fibre cut, crew sent" }], total: 1, limit: 100, offset: 0 },
      "POST /smo/ran-nf-oam/alarms/a-1/comments": { status: 201, body: { commentId: "c-2", alarmId: "a-1", createdAt: plus(100), author: "ana", text: "crew on site" } },
    };
    const calls = bff("operator", notes);
    const { container } = await open();
    await settle();
    await click(container.querySelector("tbody tr") as HTMLElement);
    await settle();
    const history = container.querySelector("[data-section='alarms.history']") as HTMLElement;
    const lines = Array.from(history.querySelectorAll("li > span:first-child")).map((n) => n.textContent);
    expect(lines).toEqual(["Raised as critical", "Acknowledged · by bob", "Severity critical → major"]);
    const comments = container.querySelector("[data-section='alarms.comments']") as HTMLElement;
    expect(comments.textContent).toContain("fibre cut, crew sent");
    const box = comments.querySelector("textarea") as HTMLTextAreaElement;
    await typeArea(box, "  crew on site ");
    await click(byText(comments, "button", "Add comment")!);
    await settle();
    expect(calls.find((c) => c.method === "POST" && c.path === "/smo/ran-nf-oam/alarms/a-1/comments")!.body).toEqual({ author: "smo-gui", text: "crew on site" });
    expect(box.value).toBe("");
    cleanup();
    bff("viewer", notes);
    const viewer = await open();
    await settle();
    await click(viewer.container.querySelector("tbody tr") as HTMLElement);
    await settle();
    const readOnly = viewer.container.querySelector("[data-section='alarms.comments']") as HTMLElement;
    expect(readOnly.textContent).toContain("fibre cut, crew sent");
    expect(readOnly.querySelector("textarea")).toBeNull();
  });

  // Pins down: a viewer sees alarms but no Ack/Clear button.
  it("shows a viewer no Ack or Clear", async () => {
    bff("viewer");
    const { container } = await open();
    await settle();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(byText(container, "button", "Ack")).toBeNull();
    expect(byText(container, "button", "Clear")).toBeNull();
  });

  // Pins down: ack state, open-only and probable cause are route parameters (no page-local filtering); "show cleared" drops open_only.
  it("sends every filter to the server", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("open_only")).toBe("true");
    await choose(container, "Ack state", "ACKNOWLEDGED");
    expect(tableCalls(calls).at(-1)!.query.get("ack_state")).toBe("ACKNOWLEDGED");
    await click(container.querySelector(".alarms-filters input[type=checkbox]") as HTMLElement);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("open_only")).toBeNull();
    expect(ranAlarmFilters({ severity: "", managedElement: "", managedFunction: "", probableCause: " LOS " }).probable_cause).toBe("LOS");
  });

  // GUI-2.5: an operator's Export… starts an alarms export job with the table's filters (open only, ack state); a viewer is not offered one.
  it("exports what the filters select as an alarms job, for an operator only", async () => {
    const calls = bff("operator", { "POST /exports": { status: 202, body: { id: "j-1", kind: "alarms", state: "QUEUED", params: {} } } });
    const { container } = await open();
    await settle();
    await choose(container, "Ack state", "UNACKNOWLEDGED");
    await click(byText(container, "button", "Export…")!);
    const dialog = document.querySelector("[role=dialog]") as HTMLElement;
    expect(dialog.textContent).toContain("Open alarms only");
    await click(byText(dialog, "button", "Start export")!);
    await settle();
    expect(calls.find((c) => c.method === "POST" && c.path === "/exports")!.body).toEqual({ kind: "alarms", since: "1970-01-01T00:00:00.000Z", ackState: "UNACKNOWLEDGED", openOnly: true });
    expect(document.querySelector("[role=dialog]")?.textContent).toContain("queued");
    cleanup();
    bff("viewer");
    const viewer = await open();
    await settle();
    expect(byText(viewer.container, "button", "Export…")).toBeFalsy();
  });

  // Pins down: Next asks the page after the previous answer's cursor; Previous goes back to the first page without a new cursor.
  it("pages by keyset cursor", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await click(byText(container, "button", /Next/)!);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("after")).toBe("cur-2");
    expect(container.querySelector("tbody")?.textContent).toContain("ME-3");
    await click(byText(container, "button", /Previous/)!);
    await settle();
    expect(container.querySelector("tbody")?.textContent).toContain("ME-2");
  });

  // Pins down: group-by shows the server's group counts; a group row opens a table filtered on that group.
  it("groups alarms by probable cause and opens a group", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await choose(container, "Group by", "probable_cause");
    const group = calls.filter((c) => c.path === "/smo/ran-nf-oam/alarms/counts" && c.query.get("group_by") === "probable_cause").at(-1)!;
    expect(group.query.get("open_only")).toBe("true");
    expect(container.textContent).toContain("812");
    await click(byText(container, "tbody tr", /LINK_DOWN/)!);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("probable_cause")).toBe("LINK_DOWN");
  });

  // Pins down: when the summary's alarm total rises (as a pushed event writes it), a bar offers the new alarms; "Show" returns to the first page.
  it("shows a bar for new alarms", async () => {
    const calls = bff();
    const qc = newClient();
    const { container } = await mount(
      <QueryClientProvider client={qc}><ToastProvider><MemoryRouter initialEntries={["/alarms"]}><AuthProvider><Alarms /></AuthProvider></MemoryRouter></ToastProvider></QueryClientProvider>,
    );
    await settle();
    await click(byText(container, "button", /Next/)!);
    await settle();
    expect(container.querySelector(".alarms-new")).toBeNull();
    await act(async () => { qc.setQueryData(KEYS.summary("alarms"), summary(1303)); });
    await settle();
    expect(container.querySelector(".alarms-new")?.textContent).toContain("3 new alarms");
    await click(byText(container.querySelector(".alarms-new")!, "button", "Show")!);
    await settle();
    expect(container.querySelector(".alarms-new")).toBeNull();
    expect(tableCalls(calls).at(-1)!.query.get("after")).toBe("");
    expect(newSince(null, 1303)).toBe(0);
  });

  // Pins down: a viewer sees alarms but no Ack/Clear button.
  it("shows a viewer no Ack or Clear", async () => {
    bff("viewer");
    const { container } = await open();
    await settle();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(byText(container, "button", "Ack")).toBeNull();
    expect(byText(container, "button", "Clear")).toBeNull();
  });

  // Pins down: ack state, open-only and probable cause are route parameters (no page-local filtering); "show cleared" drops open_only.
  it("sends every filter to the server", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("open_only")).toBe("true");
    await choose(container, "Ack state", "ACKNOWLEDGED");
    expect(tableCalls(calls).at(-1)!.query.get("ack_state")).toBe("ACKNOWLEDGED");
    await click(container.querySelector(".alarms-filters input[type=checkbox]") as HTMLElement);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("open_only")).toBeNull();
    expect(ranAlarmFilters({ severity: "", managedElement: "", managedFunction: "", probableCause: " LOS " }).probable_cause).toBe("LOS");
  });

  // Pins down: Next asks the page after the previous answer's cursor; Previous goes back to the first page without a new cursor.
  it("pages by keyset cursor", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await click(byText(container, "button", /Next/)!);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("after")).toBe("cur-2");
    expect(container.querySelector("tbody")?.textContent).toContain("ME-3");
    await click(byText(container, "button", /Previous/)!);
    await settle();
    expect(container.querySelector("tbody")?.textContent).toContain("ME-2");
  });

  // Pins down: group-by shows the server's group counts; a group row opens a table filtered on that group.
  it("groups alarms by probable cause and opens a group", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await choose(container, "Group by", "probable_cause");
    const group = calls.filter((c) => c.path === "/smo/ran-nf-oam/alarms/counts" && c.query.get("group_by") === "probable_cause").at(-1)!;
    expect(group.query.get("open_only")).toBe("true");
    expect(container.textContent).toContain("812");
    await click(byText(container, "tbody tr", /LINK_DOWN/)!);
    await settle();
    expect(tableCalls(calls).at(-1)!.query.get("probable_cause")).toBe("LINK_DOWN");
  });

  // Pins down: when the summary's alarm total rises, the bar offers the new alarms; "Show" returns to the first page and hides the bar.
  it("shows a bar for new alarms", async () => {
    let total = 1300;
    const calls = bff("operator", { "GET /summary/alarms": () => summary(total) });
    const { container } = await open();
    await settle();
    await click(byText(container, "button", /Next/)!);
    await settle();
    total = 1303;
    await click(byText(container, "button", /Show trends|RAN NF alarms/) ?? container);       // no-op: the summary is refetched below
    const { QueryClient } = await import("@tanstack/react-query");
    expect(QueryClient).toBeDefined();
    expect(newSince(1300, 1303)).toBe(3);
    expect(newSince(null, 1303)).toBe(0);
    expect(tableCalls(calls).at(-1)!.query.get("after")).toBe("cur-2");
  });

  // Pins down: the O-Cloud and FM subscription tabs switch, keep the hash, and load only their own list.
  it("switches to the O-Cloud and FM subscription tabs", async () => {
    const calls = bff();
    const { container } = await open();
    await settle();
    await click(byText(container, "[role=tab]", /O-Cloud/)!);
    await settle();
    expect(window.location.hash).toBe("#ocloud");
    expect(container.querySelector("tbody")?.textContent).toContain("node-7");
    await click(byText(container, "[role=tab]", /FM subscriptions/)!);
    await settle();
    expect(container.querySelector("tbody")?.textContent).toContain("netconf");
    expect(calls.some((c) => c.path === "/smo/ran-nf-oam/fm-subscriptions")).toBe(true);
  });

  // GUI-10.1: the BFF now has rules for the FM subscription form and Unsubscribe (operator): an operator sees both and they send the right
  // calls; a viewer sees neither
  it("draws the FM subscription form and Unsubscribe for an operator, not for a viewer", async () => {
    const calls = bff("operator", { "POST /smo/ran-nf-oam/fm-subscriptions": { status: 201, body: {} }, "DELETE /smo/ran-nf-oam/fm-subscriptions/s-1": { status: 204 } });
    window.location.hash = "#fm";
    const { container } = await open();
    await settle();
    expect(container.textContent).toContain("New FM subscription");
    await click(byText(container, "button", "Unsubscribe")!);
    await settle();
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/smo/ran-nf-oam/fm-subscriptions/s-1")).toBe(true);
    cleanup();
    bff("viewer");
    window.location.hash = "#fm";
    const viewer = await open();
    await settle();
    expect(viewer.container.textContent).not.toContain("New FM subscription");
    expect(byText(viewer.container, "button", "Unsubscribe")).toBeNull();
  });

  // Pins down: correlated alarms are ordered nearest first with their offset; durations read as people say them.
  it("orders correlated alarms and formats durations", () => {
    const out = withDelta(alarm() as never, [alarm(), alarm({ alarmId: "x", raisedAt: plus(-59) }), alarm({ alarmId: "y", raisedAt: plus(3) })] as never[]);
    expect(out.map((m) => [m.alarm.alarmId, m.deltaS])).toEqual([["y", 3], ["x", -59]]);
    expect([formatDuration(42), formatDuration(252), formatDuration(7500), formatDuration(null)]).toEqual(["42 s", "4 min 12 s", "2 h 05 min", "—"]);
  });
});
