/** The top bar's scope picker (GUI-9.3, rules in `data/scope.ts`): a chip with the scope in force ("All network", "eu-west",
 * "eu-west / metro-a") that opens a popover with a region and, once a region is picked, one of its site clusters, each with its element count,
 * from RAN NF OAM's `GET /managed-entities/scopes` (one GROUP BY). "All network" clears it. The choice goes into the URL (`?region=&cluster=`), so
 * a link or a reload keeps it. Elements with no region or no site cluster are counted but cannot be picked (no route filters on "not set"), and a
 * name the BFF would refuse as a scope (`SCOPE_NAME_RE`) is shown disabled. GUI-5: a user whose account is limited to some regions or tenants sees that limit
 * beside the chip (`GET /api/me` `scope`); the modules already narrow every answer to it, so the regions offered here are only theirs. */
import { useState } from "react";

import { useSmo } from "../api/hooks";
import { useOptionalAuth } from "../auth/AuthContext";
import { isScoped, SCOPE_NAME_RE, scopeLabel, useScopeState } from "../data/scope";
import { formatCount } from "../kit/Kpi";
import { describeUserScope } from "../lib/domain";

/** One site cluster of a region (`siteCluster` null: the elements with none). */
export interface ScopeCluster { siteCluster: string | null; elements: number }
/** One region with its element count and site clusters (`region` null: the elements with none). */
export interface ScopeRegion { region: string | null; elements: number; siteClusters: ScopeCluster[] }
/** The body of `GET /ran-nf-oam/managed-entities/scopes`. */
export interface ScopesAnswer { regions: ScopeRegion[] }

/** RAN NF OAM's scope list. */
export const SCOPES_PATH = "/ran-nf-oam/managed-entities/scopes";

/** Whether a region or cluster name can be picked (set, and a name the BFF accepts). */
export const pickable = (name: string | null): name is string => name !== null && SCOPE_NAME_RE.test(name);

/** The chip and its popover. */
export function ScopePicker() {
  const { scope, setScope } = useScopeState();
  const me = useOptionalAuth()?.me;
  const limited = me?.scope ? describeUserScope(me.scope) : null;
  const [open, setOpen] = useState(false);
  const scopes = useSmo<ScopesAnswer>(SCOPES_PATH, undefined, { refetchInterval: 300_000, staleTime: 60_000, enabled: open || isScoped(scope) });
  const regions = scopes.data?.regions ?? [];
  const region = regions.find((r) => r.region === scope.region);
  return (
    <div className="rel scope-picker">
      <button type="button" className={`chip${isScoped(scope) ? " on" : ""}`} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}
        title="Scope: the region and site cluster every list and count is narrowed to">
        <span className="muted xs">Scope</span> <strong>{scopeLabel(scope)}</strong>
      </button>
      {isScoped(scope) && (
        <button type="button" className="btn ghost small" onClick={() => setScope({ region: null, cluster: null })} aria-label="Clear the scope: all network">All network</button>
      )}
      {limited && <span className="badge info small" data-testid="account-scope" title="Your account is limited to these; an administrator sets it">Your access: {limited}</span>}
      {open && (
        <div className="popover" role="dialog" aria-label="Scope">
          <strong>Scope</strong>
          <span className="xs muted">Lists and counts that know where an element is are narrowed; the others say "network-wide".</span>
          {scopes.error && <span className="small text-bad">Could not read the regions: {scopes.error.message}</span>}
          <label className="col small">Region
            <select aria-label="Region" value={scope.region ?? ""} onChange={(e) => { setScope({ region: e.target.value || null, cluster: null }); }}>
              <option value="">All network</option>
              {regions.map((r) => (
                <option key={r.region ?? "∅"} value={r.region ?? ""} disabled={!pickable(r.region)}>
                  {r.region ?? "no region"} · {formatCount(r.elements)}
                </option>
              ))}
            </select>
          </label>
          {scope.region && (
            <label className="col small">Site cluster
              <select aria-label="Site cluster" value={scope.cluster ?? ""} onChange={(e) => setScope({ region: scope.region, cluster: e.target.value || null })}>
                <option value="">All of {scope.region}</option>
                {(region?.siteClusters ?? []).map((c) => (
                  <option key={c.siteCluster ?? "∅"} value={c.siteCluster ?? ""} disabled={!pickable(c.siteCluster)}>
                    {c.siteCluster ?? "no site cluster"} · {formatCount(c.elements)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="row end"><button type="button" className="btn small" onClick={() => setOpen(false)}>Done</button></div>
        </div>
      )}
    </div>
  );
}
