// @vitest-environment jsdom
/** Tests of the global scope (GUI-9.3): the URL rules (`data/scope.ts`), the provider that carries the scope over navigations
 * (`shell/ScopeProvider.tsx`), the scope reaching exactly the scopable reads (`api/hooks.ts`), the summary and attention reads and the event
 * topics, the top bar's picker (`shell/ScopePicker.tsx`) and the "network-wide" note (`kit/ScopeNote.tsx`) on the Dashboard. Uses the fake BFF
 * of `src/testing/bff.tsx`. Run: `npx vitest run src/data` from smo/gui. */
import { QueryClient } from "@tanstack/react-query";
import { act } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSmo } from "../../api/hooks";
import { AuthProvider } from "../../auth/AuthContext";
import rules from "../../auth/permissions.fixture.json";
import { ScopeNote } from "../../kit/ScopeNote";
import { Dashboard } from "../../pages/dashboard";
import { ScopePicker } from "../../shell/ScopePicker";
import { ScopeProvider } from "../../shell/ScopeProvider";
import { fakeBff, mountWith, type Call } from "../../testing/bff";
import { byText, cleanup, click, settle } from "../../testing/dom";
import { applyAttentionEvent, applySummaryEvent, topicsFor } from "../events";
import { KEYS } from "../keys";
import { NO_SCOPE, scopeFromSearch, scopeLabel, scopeQuery, useScope, useScopeState, withScopeSearch } from "../scope";
import { networkWide } from "../summary";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; });

const NORTH = { region: "north", cluster: "c1" };

describe("scope rules", () => {
  // the URL holds ?region=&cluster=; a malformed name or a cluster without a region is no scope; writing keeps the other parameters
  it("reads and writes the scope in the URL", () => {
    expect(scopeFromSearch("?region=north&cluster=c1&tab=x")).toEqual(NORTH);
    expect(scopeFromSearch("?cluster=c1")).toEqual(NO_SCOPE);
    expect(scopeFromSearch("?region=no%20spaces")).toEqual(NO_SCOPE);
    expect(scopeFromSearch("?region=north&cluster=bad%2Fname")).toEqual({ region: "north", cluster: null });
    expect(withScopeSearch("?me=du-1", NORTH)).toBe("?me=du-1&region=north&cluster=c1");
    expect(withScopeSearch("?me=du-1&region=north&cluster=c1", NO_SCOPE)).toBe("?me=du-1");
    expect([scopeLabel(NO_SCOPE), scopeLabel({ region: "north", cluster: null }), scopeLabel(NORTH)]).toEqual(["All network", "north", "north / c1"]);
  });

  // only the routes that take the filters get them; rApp instances take the region only; a page's own region wins and then gets no cluster
  it("adds the scope only to the routes that accept it", () => {
    expect(scopeQuery("/ran-nf-oam/config-jobs", { status: "HALTED" }, NORTH)).toEqual({ status: "HALTED", region: "north", site_cluster: "c1" });
    expect(scopeQuery("/rapp-mgmt/instances", undefined, NORTH)).toEqual({ region: "north" });
    expect(scopeQuery("/ran-nf-oam/safeguard-refusals", { code: "X" }, NORTH)).toEqual({ code: "X" });
    expect(scopeQuery("/ran-nf-oam/config-jobs/abc", undefined, NORTH)).toBeUndefined();
    expect(scopeQuery("/ran-nf-oam/managed-entities/health", { group_by: "site_cluster", region: "south" }, NORTH)).toEqual({ group_by: "site_cluster", region: "south" });
    expect(scopeQuery("/ran-nf-oam/managed-entities/health", { group_by: "region", region: undefined }, NORTH)).toEqual({ group_by: "region", region: "north", site_cluster: "c1" });
    expect(scopeQuery("/ran-nf-oam/alarms", { a: 1 }, NO_SCOPE)).toEqual({ a: 1 });
  });

  // every topic is scoped; the Dashboard adds the attention topic; the BFF's limit of four is kept
  it("scopes the event topics", () => {
    expect(topicsFor("alarms", NORTH)).toEqual(["summary:nav@north/c1", "summary:alarms@north/c1"]);
    expect(topicsFor("dashboard", { region: "north", cluster: null })).toEqual(["summary:nav@north", "summary:dashboard@north", "summary:attention@north"]);
    expect(topicsFor(null)).toEqual(["summary:nav"]);
  });

  // a scoped event lands in the scoped cache entry with its unscoped keys; an attention event replaces the groups of its scope
  it("writes scoped and attention events into their own cache entries", () => {
    const qc = new QueryClient();
    applySummaryEvent(qc, { page: "dashboard", counts: { "alarms.critical": 2 }, scope: { region: "north", siteCluster: "c1" }, unscoped: ["models.total"] }, { first: true });
    expect(qc.getQueryData<{ unscoped: string[] }>(KEYS.summary("dashboard", "@north/c1"))?.unscoped).toEqual(["models.total"]);
    expect(qc.getQueryData(KEYS.summary("dashboard"))).toBeUndefined();
    applyAttentionEvent(qc, { page: "attention", groups: [{ type: "approvals", total: 4, items: [] }], scope: null });
    expect(qc.getQueryData<{ groups: unknown[] }>(KEYS.attention())?.groups).toHaveLength(1);
  });

  // a box is network-wide when one of its keys (or a key under its prefix) is unscoped
  it("tells which counts stayed network-wide", () => {
    expect(networkWide({ unscoped: ["models.total", "packages.AVAILABLE"] }, ["packages"])).toBe(true);
    expect(networkWide({ unscoped: ["models.total"] }, ["alarms"])).toBe(false);
    expect(networkWide(undefined, ["alarms"])).toBe(false);
  });
});

/** Shows the scope and the URL, and offers a plain link (no scope in it), a link that sets a scope and the back button. */
function Probe() {
  const scope = useScope();
  const { setScope } = useScopeState();
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <span id="label">{scopeLabel(scope)}</span><span id="url">{loc.pathname}{loc.search}</span>
      <Link to="/approvals">plain link</Link>
      <button type="button" onClick={() => setScope({ region: "south", cluster: null })}>south</button>
      <button type="button" onClick={() => navigate(-1)}>back</button>
      <ScopeNote always />
    </div>
  );
}

describe("the scope provider", () => {
  // a link that drops the parameters keeps the scope (the URL is given them back); the back button (POP) takes the URL as it is
  it("carries the scope over navigation and lets the back button leave it", async () => {
    fakeBff({});
    const { container } = await mountWith(<ScopeProvider><Probe /></ScopeProvider>, { at: "/alarms?region=north&cluster=c1" });
    const text = (id: string) => container.querySelector(`#${id}`)!.textContent;
    expect(text("label")).toBe("north / c1");
    expect(container.querySelector(".scope-note")?.textContent).toBe("network-wide");
    await click(byText(container, "a", "plain link")!);
    await settle();
    expect(text("url")).toBe("/approvals?region=north&cluster=c1");
    expect(text("label")).toBe("north / c1");
    await click(byText(container, "button", "south")!);
    await settle();
    expect(text("url")).toBe("/approvals?region=south");
    await click(byText(container, "button", "back")!);
    await settle();
    expect(text("label")).toBe("north / c1");
  });

  // without a scope nothing is added and no note is drawn
  it("adds nothing without a scope", async () => {
    fakeBff({});
    const { container } = await mountWith(<ScopeProvider><Probe /></ScopeProvider>, { at: "/alarms" });
    await click(byText(container, "a", "plain link")!);
    await settle();
    expect(container.querySelector("#url")!.textContent).toBe("/approvals");
    expect(container.querySelector(".scope-note")).toBeNull();
  });
});

/** Reads three routes: one scopable, one not, rApp instances. */
function Reads() {
  useSmo("/ran-nf-oam/config-jobs", { status: "HALTED" });
  useSmo("/focom/alarms");
  useSmo("/rapp-mgmt/instances");
  return null;
}

describe("scoped reads", () => {
  // the scope reaches the routes that take it and no other
  it("sends region and site_cluster to scopable routes only", async () => {
    const calls = fakeBff({ "GET /smo/*": { items: [], limit: 50, offset: 0, total: 0 } });
    await mountWith(<ScopeProvider><Reads /></ScopeProvider>, { at: "/?region=north&cluster=c1" });
    await settle();
    const of = (p: string) => calls.find((c) => c.path === p)!.query;
    expect(of("/smo/ran-nf-oam/config-jobs").get("region")).toBe("north");
    expect(of("/smo/ran-nf-oam/config-jobs").get("site_cluster")).toBe("c1");
    expect(of("/smo/focom/alarms").has("region")).toBe(false);
    expect(of("/smo/rapp-mgmt/instances").get("region")).toBe("north");
    expect(of("/smo/rapp-mgmt/instances").has("site_cluster")).toBe(false);
  });

  // the picker lists the regions and clusters of /managed-entities/scopes (a null or unusable name disabled) and writes the choice into the URL
  it("picks a region and a site cluster in the top bar", async () => {
    fakeBff({ "GET /smo/ran-nf-oam/managed-entities/scopes": { regions: [
      { region: "north", elements: 12, siteClusters: [{ siteCluster: "c1", elements: 10 }, { siteCluster: null, elements: 2 }] },
      { region: null, elements: 3, siteClusters: [{ siteCluster: null, elements: 3 }] },
    ] } });
    const { container } = await mountWith(<ScopeProvider><ScopePicker /><Probe /></ScopeProvider>, { at: "/alarms" });
    await click(container.querySelector<HTMLElement>(".scope-picker .chip")!);
    await settle();
    const region = container.querySelector<HTMLSelectElement>("select[aria-label='Region']")!;
    expect(Array.from(region.options).map((o) => [o.textContent, o.disabled])).toEqual([["All network", false], ["north · 12", false], ["no region · 3", true]]);
    await act(async () => { region.value = "north"; region.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    const cluster = container.querySelector<HTMLSelectElement>("select[aria-label='Site cluster']")!;
    await act(async () => { cluster.value = "c1"; cluster.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    expect(container.querySelector("#url")!.textContent).toBe("/alarms?region=north&cluster=c1");
    expect(container.querySelector(".scope-picker .chip")!.textContent).toContain("north / c1");
    await click(byText(container, "button", "All network")!);
    await settle();
    expect(container.querySelector("#url")!.textContent).toBe("/alarms");
  });

  // GUI-5: a user whose account is limited sees the limit beside the chip; an unscoped user sees nothing extra
  it("shows the account's own scope beside the picker", async () => {
    fakeBff({ "GET /me": { username: "ana", role: "viewer", csrfToken: "c", local: true, scope: { regions: ["north"] } }, "GET /permissions": { role: "viewer", rules } });
    const { container } = await mountWith(<AuthProvider><ScopeProvider><ScopePicker /></ScopeProvider></AuthProvider>, { at: "/alarms" });
    await settle();
    expect(container.querySelector("[data-testid='account-scope']")!.textContent).toBe("Your access: regions north");
    cleanup();
    fakeBff({ "GET /me": { username: "bo", role: "viewer", csrfToken: "c", local: true, scope: null }, "GET /permissions": { role: "viewer", rules } });
    const plain = await mountWith(<AuthProvider><ScopeProvider><ScopePicker /></ScopeProvider></AuthProvider>, { at: "/alarms" });
    await settle();
    expect(plain.container.querySelector("[data-testid='account-scope']")).toBeNull();
  });

  // under a scope the Dashboard's summary, attention and fleet reads carry it, and the boxes the scope does not narrow say "network-wide"
  it("scopes the Dashboard and marks what stays network-wide", async () => {
    const calls = fakeBff({
      "GET /me": { username: "ana", role: "operator", csrfToken: "c", local: true },
      "GET /permissions": { role: "operator", rules },
      "GET /summary/dashboard": (c: Call) => ({ page: "dashboard", computedAt: "", partial: [], counts: { "mlmfBreaches.total": 3, "models.total": 9, "alarms.critical": 1 },
        scope: { region: c.query.get("region"), siteCluster: c.query.get("site_cluster") }, unscoped: ["mlmfBreaches.total", "models.total", "packages.total"] }),
      "GET /summary/attention": { page: "attention", partial: [], unscoped: ["mlmf-breaches", "escalations"], groups: [
        { type: "critical-alarms", total: 1, items: [] }, { type: "approvals", total: 0, items: [] }, { type: "mlmf-breaches", total: 3, items: [] }, { type: "escalations", total: 0, items: [] }] },
      "GET /modules/status": { checkedAt: "", modules: [] },
      "GET /smo/ran-nf-oam/managed-entities/health": { groupBy: "region", healthScore: 99, groups: [] },
      "GET /smo/ran-nf-oam/alarms/counts": { groupBy: "hour", groups: [] },
      "GET /smo/ran-nf-oam/managed-entities/worst": [],
      "GET /smo/*": { items: [], limit: 6, offset: 0, total: 0 },
    });
    const { container } = await mountWith(<AuthProvider><ScopeProvider><Dashboard /></ScopeProvider></AuthProvider>, { at: "/?region=north&cluster=c1" });
    await settle(6);
    for (const p of ["/summary/dashboard", "/summary/attention", "/smo/ran-nf-oam/managed-entities/health", "/smo/ran-nf-oam/managed-entities/worst",
      "/smo/ran-nf-oam/alarms/counts", "/smo/ran-nf-oam/decision-records"]) {
      const c = calls.find((x) => x.path === p);
      expect(c?.query.get("region"), p).toBe("north");
      expect(c?.query.get("site_cluster"), p).toBe("c1");
    }
    const attention = container.querySelector("[data-section='dashboard.attention']")!;
    expect(attention.querySelector("[data-group='mlmf-breaches'] .scope-note")).not.toBeNull();
    expect(attention.querySelector("[data-group='critical-alarms'] .scope-note")).toBeNull();
    expect(container.querySelector("[data-section='dashboard.tiles'] .scope-note")).not.toBeNull();
  });
});
