/** Pushed summary counts (SCALE.md P7): the BFF's Server-Sent Events stream `GET /api/events?topics=summary:<page>,…` (gui-bff/app/events.py).
 *
 * One stream per tab carries the "nav" counts (sidebar badges, notifications) and the visible page's counts. Every `event: summary` holds the
 * page's full counts and the keys that changed; `applySummaryEvent` writes the counts into the React Query cache under the same key
 * `useSummary` reads (`["bff","summary",page]`), so every tile updates without a request, and refetches only the lists whose counts moved
 * (`LIST_PATHS`). While the stream is open the summary polls fall back to once a minute (`LIVE_SUMMARY_POLL`), the top bar says
 * "Live · pushed", and the alarm list stops polling every 5 s. The stream itself is opened by `shell/LiveEvents.tsx`; this file holds the rules
 * it follows, kept free of React so they can be tested on their own. jsdom has no `EventSource`: the provider takes a factory, and a browser
 * without one just keeps polling.
 *
 * GUI-9.3: under a scope every topic is scoped (`summary:<page>@<region>[/<cluster>]`) and each event is written into the scoped cache entry.
 * GUI-9.8b: the Dashboard also subscribes `summary:attention`, whose `event: attention` carries the "Needs your attention" groups
 * (`applyAttentionEvent`). */
import { createContext, useContext } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { KEYS } from "./keys";
import { NO_SCOPE, scopeKey, topicSuffix, type Scope } from "./scope";
import type { Attention, AttentionGroup, Summary, SummaryPage, SummaryScope } from "./summary";

/** The summary pages the BFF can stream (gui-bff/app/summary.py `PAGES`). */
export const STREAM_PAGES: readonly SummaryPage[] = ["nav", "dashboard", "alarms", "rapps", "approvals", "decisions", "infrastructure",
  "configuration", "software", "intents", "aiml"];

/** How often a summary is still re-read while the stream is open: a safety net for a missed event, not the update path. */
export const LIVE_SUMMARY_POLL = 60_000;

/** The first reconnect waits this long; each failure doubles it up to `MAX_BACKOFF_MS`. */
export const FIRST_BACKOFF_MS = 1_000;
export const MAX_BACKOFF_MS = 60_000;

/** The summary page of a route: the counts the visible page shows, or null for a page with none (Preferences, Topology …). */
export function summaryPageOf(pathname: string): SummaryPage | null {
  if (pathname === "/" || pathname === "") return "dashboard";
  const first = pathname.split("/")[1] ?? "";
  const byRoute: Record<string, SummaryPage> = {
    alarms: "alarms", rapps: "rapps", safeguards: "rapps", approvals: "approvals", decisions: "decisions", infrastructure: "infrastructure",
    configuration: "configuration", software: "software", policy: "intents", aiml: "aiml",
  };
  return byRoute[first] ?? null;
}

/** The topics of one stream: always "summary:nav", plus the visible page's summary, plus "summary:attention" on the Dashboard, each scoped to
 * `scope` (at most 4 are allowed; this never asks more than 3). */
export function topicsFor(page: SummaryPage | null, scope: Scope = NO_SCOPE): string[] {
  const at = topicSuffix(scope);
  const out = [`summary:nav${at}`];
  if (page && page !== "nav") out.push(`summary:${page}${at}`);
  if (page === "dashboard") out.push(`summary:attention${at}`);
  return out;
}

/** The stream's URL for `topics` (same origin: the CSP's `connect-src 'self'` covers it, and the session cookie rides along). */
export function streamUrl(topics: string[]): string {
  return `/api/events?topics=${topics.join(",")}`;
}

/** The SMO list paths whose reads a changed count makes stale, by count prefix (`alarms.critical` → the RAN alarm list and its counts). */
export const LIST_PATHS: Record<string, string[]> = {
  alarms: ["/ran-nf-oam/alarms", "/ran-nf-oam/managed-entities/health", "/ran-nf-oam/managed-entities/worst"],
  ocloudAlarms: ["/focom/alarms"],
  approvals: ["/ran-nf-oam/rapp-approvals"],
  instances: ["/rapp-mgmt/instances"],
  rappsStopped: ["/rapp-mgmt/instances", "/rapp-mgmt/kill-all"],
  packages: ["/onboarding/packages"],
  deployments: ["/nfo/deployments"],
  intents: ["/intent-service/intents"],
  configJobs: ["/ran-nf-oam/config-jobs"],
  campaigns: ["/ran-nf-oam/software-campaigns"],
  decisions24h: ["/ran-nf-oam/decision-records"],
  escalations: ["/sa-smos/remedial-actions"],
  mlmfBreaches: ["/aimgf/mlmf/reports"],
  modelGates: ["/aimgf/model-lifecycles"],
  models: ["/aimgf/models"],
  trainingJobs: ["/aimgf/training-jobs"],
  elements: ["/ran-nf-oam/managed-entities"],
};

/** The list paths to refetch for a set of changed count keys (deduplicated). */
export function listPathsFor(changed: string[]): string[] {
  const out = new Set<string>();
  for (const key of changed) for (const p of LIST_PATHS[key.split(".")[0] ?? ""] ?? []) out.add(p);
  return [...out];
}

/** The body of one `event: summary` (a scoped topic's adds `topic`, `scope` and `unscoped`). */
export interface SummaryEvent {
  /** The page's panels, when one of them changed (GUI-9.11). */
  panels?: Record<string, unknown>;
  page: string; counts: Record<string, number | null>; changed?: string[]; computedAt?: string; partial?: string[];
  topic?: string; scope?: SummaryScope | null; unscoped?: string[];
}

/** The body of one `event: attention` (`changed` names the group types whose total or rows moved). */
export interface AttentionEvent { page: "attention"; groups: AttentionGroup[]; changed?: string[]; computedAt?: string; partial?: string[]; scope?: SummaryScope | null; unscoped?: string[] }

/** The cache key part of an event's scope ("" for none). */
function eventScopeKey(scope: SummaryScope | null | undefined): string {
  return scope?.region ? scopeKey({ region: scope.region, cluster: scope.siteCluster ?? null }) : "";
}

/** Write one event's counts into the cache and, unless it is the stream's first event for that page (which only says what the page already
 * read), refetch the lists whose counts changed. Returns the list paths it invalidated. */
export function applySummaryEvent(qc: QueryClient, ev: SummaryEvent, opts: { first: boolean }): string[] {
  const key = KEYS.summary(ev.page, eventScopeKey(ev.scope));
  const before = qc.getQueryData<Summary>(key);
  const body: Summary = { page: ev.page, computedAt: ev.computedAt ?? new Date().toISOString(), counts: ev.counts ?? {}, partial: ev.partial ?? [],
    scope: ev.scope ?? null, unscoped: ev.unscoped ?? [] };
  // a page's panels (GUI-9.11) come with every event of a page that has them; an event without any (an older BFF) keeps the ones already read
  const panels = ev.panels ?? before?.panels;
  if (panels) body.panels = panels;
  qc.setQueryData(key, body);
  if (opts.first) return [];
  const paths = listPathsFor(ev.changed ?? []);
  if (paths.length > 0) {
    void qc.invalidateQueries({
      predicate: (q) => q.queryKey[0] === "smo" && typeof q.queryKey[1] === "string" && paths.some((p) => (q.queryKey[1] as string).startsWith(p)),
    });
  }
  return paths;
}

/** Write one attention event into the cache entry `useAttention` reads (the groups replace the old ones; the rows are refreshed by the event
 * itself, so no list is refetched). */
export function applyAttentionEvent(qc: QueryClient, ev: AttentionEvent): void {
  const body: Attention = { page: "attention", computedAt: ev.computedAt, groups: ev.groups ?? [], partial: ev.partial ?? [], scope: ev.scope ?? null,
    unscoped: ev.unscoped ?? [] };
  qc.setQueryData(KEYS.attention(eventScopeKey(ev.scope)), body);
}

/** True when the number of critical alarms went up between two readings (a first reading is never a rise). */
export function criticalRose(before: number | null | undefined, after: number | null | undefined): boolean {
  return typeof before === "number" && typeof after === "number" && after > before;
}

/** A short two-tone beep through WebAudio (no audio file, nothing to fetch). Silently does nothing where audio is unavailable or blocked. */
export function beep(): void {
  try {
    const w = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext };
    const Ctx = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const gain = ctx.createGain();
    gain.gain.value = 0.06;
    gain.connect(ctx.destination);
    [880, 660].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(ctx.currentTime + i * 0.16);
      osc.stop(ctx.currentTime + i * 0.16 + 0.14);
      if (i === 1) osc.onended = () => { void ctx.close(); };
    });
  } catch { /* audio not allowed before a user gesture, or no device: the visual badge still shows */ }
}

/** The part of `EventSource` the provider uses (a test passes a fake). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((ev: Event) => unknown) | null;
  onerror: ((ev: Event) => unknown) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

/** Opens a stream, or returns null where the browser has no `EventSource` (the console then keeps polling). */
export type SourceFactory = (url: string) => EventSourceLike | null;

/** The browser's `EventSource`, when it has one. */
export const browserSource: SourceFactory = (url) =>
  (typeof EventSource === "function" ? new EventSource(url, { withCredentials: true }) as unknown as EventSourceLike : null);

/** The next reconnect delay after `previous` (doubling, capped). */
export function nextBackoff(previous: number): number {
  return Math.min(MAX_BACKOFF_MS, previous <= 0 ? FIRST_BACKOFF_MS : previous * 2);
}

/** What the live-events provider tells the console. */
export interface LiveState { connected: boolean }

export const LiveContext = createContext<LiveState>({ connected: false });

/** Whether the summary stream is open now (false outside the shell, in tests, and while reconnecting). */
export function useLive(): LiveState {
  return useContext(LiveContext);
}
