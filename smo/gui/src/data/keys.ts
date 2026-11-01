/** Every React Query key family of the console, and which ones a change invalidates (SCALE.md P8: targeted invalidation).
 *
 * Reads of an SMO module are keyed `["smo", "/<module>/...", query]` by `api/hooks.ts`; the BFF's own reads `["bff", <name>, ...]`. A mutation
 * used to refetch every read in the tab; now `invalidationTargets` names the modules it can change (its own, plus the ones a lifecycle call in it
 * is known to touch, `CROSS_MODULE`) and only those reads, the summary counts and the rApp directory are refetched. The session (`["bff","me"]`),
 * the permission table and the preferences are never refetched by an SMO action. */
import type { QueryClient, QueryKey } from "@tanstack/react-query";

/** The BFF query keys the console reads. */
export const KEYS = {
  me: ["bff", "me"] as const,
  modulesStatus: ["bff", "modules-status"] as const,
  preferences: ["bff", "preferences"] as const,
  /** A page's summary counts under a scope (`data/scope.ts` scopeKey: "" for the whole network). */
  summary: (page: string, scope = "") => ["bff", "summary", page, scope] as const,
  /** The Dashboard's attention groups under a scope; under "summary" so an action's refetch of the counts refetches them too. */
  attention: (scope = "") => ["bff", "summary", "attention", scope] as const,
  summaryAll: ["bff", "summary"] as const,
  rapps: ["bff", "rapps"] as const,
  pins: ["bff", "pins"] as const,
};

/** A call into one module that is known to change another module's state too (a rApp instantiation creates an NFO deployment and an SME
 * registration; a service order runs NFO, FOCOM and the AI/ML pipeline). The pre-redesign GUI refetched everything for this reason. */
export const CROSS_MODULE: Record<string, string[]> = {
  "rapp-mgmt": ["onboarding", "nfo", "sme", "dme"],
  onboarding: ["rapp-mgmt"],
  "so-smos": ["nfo", "focom", "aimgf"],
  nfo: ["focom"],
  "ran-nf-oam": ["sa-smos"],
  "intent-service": ["sa-smos"],
};

/** The modules whose reads a change to `path` can make stale: its own module and the ones `CROSS_MODULE` lists. */
export function invalidationTargets(path: string): string[] {
  const module = path.split("/")[1] ?? "";
  return [module, ...(CROSS_MODULE[module] ?? [])];
}

/** True when a cached key is a read of one of `modules`, the summary counts, or the rApp directory and pins. */
export function isAffected(key: QueryKey, modules: string[]): boolean {
  if (key[0] === "smo" && typeof key[1] === "string") return modules.includes(key[1].split("/")[1] ?? "");
  return key[0] === "bff" && (key[1] === "summary" || key[1] === "rapps" || key[1] === "pins" || key[1] === "rapp");
}

/** Refetch what a change to `path` can have changed. `extra` adds module names (for an action whose effect the table does not know). */
export function invalidateAfter(qc: QueryClient, path: string, extra: string[] = []): Promise<void> {
  const modules = [...invalidationTargets(path), ...extra];
  return qc.invalidateQueries({ predicate: (q) => isAffected(q.queryKey, modules) });
}
