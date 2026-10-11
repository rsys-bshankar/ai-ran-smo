/** The console's global scope (GUI-9.3): a region and, inside it, a site cluster (RAN NF OAM's `managed_entity.region` / `.site_cluster`,
 * ADR 0005), picked in the top bar (`shell/ScopePicker.tsx`) and carried in the URL as `?region=&cluster=` so a link keeps it.
 *
 * Every SMO read goes through `api/hooks.ts`, which adds the scope to the routes in `SCOPED_ROUTES` (the list routes whose OpenAPI `GET` takes
 * `region` / `site_cluster`), unless the page set that parameter itself (a drill-down to another region keeps its own). The summary counts
 * (`data/summary.ts`) and the event topics (`data/events.ts`) carry it too; the BFF names the counts it could not narrow in `unscoped`, and a box
 * built on them shows a "network-wide" note (`kit/ScopeNote.tsx`). The URL is read by `shell/ScopeProvider.tsx`, which also carries the scope
 * over navigations that drop it (a link inside a page); this file is kept free of the router so a test can use it on its own. */
import { createContext, useContext } from "react";

import type { Query } from "../api/client";
import type { Translator } from "../i18n";
import { ENGLISH } from "../lib/domain";

/** A scope: no region means the whole network; a cluster needs a region. */
export interface Scope { region: string | null; cluster: string | null }

/** The whole network. */
export const NO_SCOPE: Scope = { region: null, cluster: null };

/** A name the BFF accepts as a scope (gui-bff/app/summary.py `SCOPE_RE`); any other name is not offered and is ignored in the URL. */
export const SCOPE_NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;

/** The URL parameters of the scope. */
export const SCOPE_PARAMS = { region: "region", cluster: "cluster" } as const;

/** Which scope parameters each module list route accepts (docs/openapi/ran-nf-oam.json and rapp-mgmt.json, GUI-9.3). rApp Management's
 * instances take only the region (an instance's authorised regions, plus the instances with no region: `include_unscoped`, default true).
 * `/ran-nf-oam/safeguard-refusals` cannot be scoped (a refusal records no element), nor can any route not listed here. */
export const SCOPED_ROUTES: Record<string, readonly ("region" | "site_cluster")[]> = {
  "/ran-nf-oam/alarms": ["region", "site_cluster"],
  "/ran-nf-oam/alarms/counts": ["region", "site_cluster"],
  "/ran-nf-oam/alarms/stats": ["region", "site_cluster"],
  "/ran-nf-oam/decision-records": ["region", "site_cluster"],
  "/ran-nf-oam/config-jobs": ["region", "site_cluster"],
  "/ran-nf-oam/cell-guards": ["region", "site_cluster"],
  "/ran-nf-oam/topology/links": ["region", "site_cluster"],
  "/ran-nf-oam/topology/links/counts": ["region", "site_cluster"],
  "/ran-nf-oam/topology/graph": ["region", "site_cluster"],
  "/ran-nf-oam/software-campaigns": ["region", "site_cluster"],
  "/ran-nf-oam/o1-adaptor-endpoints": ["region", "site_cluster"],
  "/ran-nf-oam/element-onboarding": ["region", "site_cluster"],
  "/ran-nf-oam/rapp-approvals": ["region", "site_cluster"],
  "/ran-nf-oam/managed-entities": ["region", "site_cluster"],
  "/ran-nf-oam/managed-entities/health": ["region", "site_cluster"],
  "/ran-nf-oam/managed-entities/worst": ["region", "site_cluster"],
  "/rapp-mgmt/instances": ["region"],
};

/** True when a scope is set. */
export function isScoped(s: Scope): boolean {
  return s.region !== null;
}

/** The scope a URL search string holds: a malformed name is ignored, and a cluster without a region is no scope. */
export function scopeFromSearch(search: string | URLSearchParams): Scope {
  const p = typeof search === "string" ? new URLSearchParams(search) : search;
  const region = p.get(SCOPE_PARAMS.region);
  const cluster = p.get(SCOPE_PARAMS.cluster);
  if (!region || !SCOPE_NAME_RE.test(region)) return NO_SCOPE;
  return { region, cluster: cluster && SCOPE_NAME_RE.test(cluster) ? cluster : null };
}

/** `search` (a URL search string, with or without "?") with the scope parameters replaced by `scope`'s; "" or "?…" as `URLSearchParams` writes it. */
export function withScopeSearch(search: string, scope: Scope): string {
  const p = new URLSearchParams(search);
  p.delete(SCOPE_PARAMS.region);
  p.delete(SCOPE_PARAMS.cluster);
  if (scope.region) p.set(SCOPE_PARAMS.region, scope.region);
  if (scope.region && scope.cluster) p.set(SCOPE_PARAMS.cluster, scope.cluster);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** "eu-west / metro-a", "eu-west", or "All network". */
export function scopeLabel(s: Scope, t: Translator = ENGLISH): string {
  if (!s.region) return t("domain.scope.all");
  return s.cluster ? `${s.region} / ${s.cluster}` : s.region;
}

/** The scope as the BFF's summary and attention routes take it (`region`, `site_cluster`). */
export function summaryScopeQuery(s: Scope): Query {
  return { region: s.region ?? undefined, site_cluster: s.region ? s.cluster ?? undefined : undefined };
}

/** The suffix of an event topic for the scope: "" or "@eu-west" or "@eu-west/metro-a" (gui-bff/app/events.py `parse_topic`). */
export function topicSuffix(s: Scope): string {
  if (!s.region) return "";
  return s.cluster ? `@${s.region}/${s.cluster}` : `@${s.region}`;
}

/** A stable key part for a scope ("" for none), used in the React Query keys of the summary and attention reads. */
export function scopeKey(s: Scope): string {
  return topicSuffix(s);
}

/** Whether `path` is a list route the scope narrows. */
export function scopableRoute(path: string | null): boolean {
  return path !== null && path in SCOPED_ROUTES;
}

/** `query` with the scope parameters `path` accepts added; a parameter the page set itself (a drill into a region) is kept as the page set it. */
export function scopeQuery(path: string | null, query: Query | undefined, s: Scope): Query | undefined {
  if (!s.region || path === null) return query;
  const accepted = SCOPED_ROUTES[path];
  if (!accepted) return query;
  const out: Query = { ...(query ?? {}) };
  const blank = (v: unknown) => v === undefined || v === null || v === "";
  if (accepted.includes("region") && blank(out.region)) out.region = s.region;
  // a cluster belongs to the scope's region: a page that asked for another region does not get the scope's cluster
  if (accepted.includes("site_cluster") && s.cluster && blank(out.site_cluster) && out.region === s.region) out.site_cluster = s.cluster;
  return out;
}

/** What the scope context holds: the scope and the way to change it (the picker; the provider writes it into the URL). */
export interface ScopeState { scope: Scope; setScope: (next: Scope) => void }

export const ScopeContext = createContext<ScopeState>({ scope: NO_SCOPE, setScope: () => {} });

/** The scope the user picked (no scope outside the shell, so a page under test reads the whole network unless the test provides one). */
export function useScope(): Scope {
  return useContext(ScopeContext).scope;
}

/** The scope and its setter (the top bar's picker). */
export function useScopeState(): ScopeState {
  return useContext(ScopeContext);
}
