/**
 * The react-query hooks for the BFF's rApp routes (gui-bff/app/rapps.py, PR-GUI-8): the directory, one rApp with its declared page, the
 * declared operator reads and actions, and the user's pins (the sidebar shortcuts).
 *
 * Every query key starts with "bff"; `useSmoAction` refreshes the directory, one rApp and the pins after any SMO change (data/keys.ts). The types here
 * mirror the BFF's answers; the page declaration is typed loosely on purpose (see lib/operatorUi.ts).
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useToast } from "../components/Toast";
import { useT } from "../i18n";
import { refreshInterval, type Obj } from "../lib/operatorUi";
import { invalidateAfter } from "../data/keys";
import { api, ApiError, type Query } from "./client";

/**
 * One row of the rApp directory: the instance and its package, state and autonomy mode, whether its package declares an operator page, whether its operator API is registered, and whether the user pinned it.
 */
export interface RappSummary {
  instanceId: string; packageId: string; name: string | null; version: string | null; vendor: string | null;
  state: string | null; autonomyMode: string | null; hasPage: boolean; operatorApiRegistered: boolean; pinned: boolean;
}

export interface RappDirectory { items: RappSummary[]; total: number; limit: number; offset: number; owners: string[]; states: string[] }

/**
 * One rApp as the detail page needs it: the summary plus the declaration state (declared, none, or unreadable), the declaration itself and the two flags that decide whether the renderer draws buttons.
 */
export interface RappPage extends RappSummary {
  declarationState: "declared" | "none" | "unreadable";
  declaration: Declaration | null;
  readOnly: boolean;
  /** The user's role may change things AND the page is not read-only: the renderer draws the change buttons only then. */
  canChange: boolean;
}

// The declaration as the renderer reads it. Loose on purpose (see lib/operatorUi.ts): the stored one may be newer than this build.
export interface Declaration { version?: number; readOnly?: boolean; panels: Obj[] }

export interface DirectoryQuery { search?: string; state?: string; owner?: string; hasPage?: boolean | ""; pinned?: boolean | ""; limit?: number; offset?: number }

/**
 * Queries the directory of rApps with the page's filters (search, state, owner, has-page, pinned, limit, offset) and polls it every 15 s.
 * An empty-string has-page or pinned filter means "any" and is left out of the request. The previous page stays on screen while the next loads.
 */
export function useRappDirectory(q: DirectoryQuery) {
  return useQuery<RappDirectory, ApiError>({
    queryKey: ["bff", "rapps", q],
    queryFn: ({ signal }) => api<RappDirectory>("/rapps", { query: { ...q, hasPage: q.hasPage === "" ? undefined : q.hasPage, pinned: q.pinned === "" ? undefined : q.pinned } as Query, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });
}

/** Queries one rApp with its declared page (GET /rapps/{id}), refreshed every 15 s. */
export function useRapp(instanceId: string) {
  return useQuery<RappPage, ApiError>({
    queryKey: ["bff", "rapp", instanceId],
    queryFn: ({ signal }) => api<RappPage>(`/rapps/${instanceId}`, { signal }),
    refetchInterval: 15_000,
  });
}

// ---------------------------------------------------------------- the declared routes

/** A declared read through the BFF. `path` is the route relative to the rApp's operator API (already filled); `seconds` is the source's refreshSeconds. */
export function useOperatorRead(instanceId: string, path: string | null, query: Query | null, seconds: unknown, enabled = true) {
  return useQuery<unknown, ApiError>({
    queryKey: ["bff", "rapp-op", instanceId, path, query ?? {}],
    queryFn: ({ signal }) => api<unknown>(`/rapps/${instanceId}/operator${path}`, { query: query ?? undefined, signal }),
    enabled: enabled && path !== null && query !== null,
    refetchInterval: refreshInterval(seconds),
  });
}

export interface OperatorAction { actionId: string; method: "POST" | "PUT" | "PATCH" | "DELETE"; path: string; body?: Obj; success: string }

/**
 * Returns the mutation behind a declared action button: sends the action to the rApp's operator API through the BFF with its action id in the X-Action-Id header
 * (DELETE carries no body, the other methods send the given body or {}). Toasts the declared success text or the error, and refreshes this rApp's declared reads and every SMO read.
 */
export function useOperatorAction(instanceId: string) {
  const qc = useQueryClient();
  const toast = useToast();
  const t = useT();
  return useMutation<unknown, ApiError, OperatorAction>({
    mutationFn: (a) => api(`/rapps/${instanceId}/operator${a.path}`, { method: a.method, json: a.method === "DELETE" ? undefined : a.body ?? {}, headers: { "X-Action-Id": a.actionId } }),
    onSuccess: (_data, a) => {
      toast.push({ tone: "success", text: a.success });
      qc.invalidateQueries({ queryKey: ["bff", "rapp-op", instanceId] });
      // a declared action changes the rApp, and through its config jobs the network: those reads only (SCALE.md P8)
      void invalidateAfter(qc, "/rapp-mgmt", ["ran-nf-oam"]);
    },
    // R1 Termination's 502 UPSTREAM_UNAVAILABLE: it could not open a connection to the rApp at the instance's registered base, so say what to check
    onError: (err, a) => toast.push({ tone: "error", text: `${a.actionId} failed — ${err.message}${err.title === "UPSTREAM_UNAVAILABLE" ? `. ${t("rappDetail.operator.unreachable")}` : ""}` }),
  });
}

// ---------------------------------------------------------------- pins (the sidebar)

export const MAX_PINS = 5;

export interface Pins { max: number; items: RappSummary[] }

export function usePins(enabled = true) {
  return useQuery<Pins, ApiError>({ queryKey: ["bff", "pins"], queryFn: ({ signal }) => api<Pins>("/me/pins", { signal }), enabled, staleTime: 30_000, retry: false });
}

/**
 * Returns the mutation that pins (PUT) or unpins (DELETE) an rApp for the signed-in user. A 409 means the user is at the pin limit (`MAX_PINS`) and gets that explanation as a toast; success refreshes every BFF read, so the sidebar updates.
 */
export function usePinToggle() {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation<unknown, ApiError, { instanceId: string; pin: boolean }>({
    mutationFn: ({ instanceId, pin }) => api(`/me/pins/${instanceId}`, { method: pin ? "PUT" : "DELETE" }),
    onSuccess: (_d, { pin }) => {
      toast.push({ tone: "success", text: pin ? "Pinned to the sidebar" : "Unpinned" });
      for (const k of ["pins", "rapps", "rapp"]) qc.invalidateQueries({ queryKey: ["bff", k] });
    },
    onError: (err) => toast.push({ tone: "error", text: err.status === 409 ? `At most ${MAX_PINS} rApps can be pinned: unpin one first` : `Pinning failed — ${err.message}` }),
  });
}
