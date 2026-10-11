/** The "network-wide" note (GUI-9.3): shown on a box while a scope is picked (`data/scope.ts`) and the box's data was not narrowed by it, so a
 * reader does not take a network total for the region's. Either the box reads summary counts and the BFF named some of them in `unscoped`
 * (`summary` + `keys`, see `data/summary.ts` `networkWide`), or its route cannot be scoped at all (`always`, e.g. the safeguard refusals,
 * which record no element). Nothing is drawn without a scope. */
import { isScoped, useScope } from "../data/scope";
import { networkWide, type Summary } from "../data/summary";
import { useT } from "../i18n";

/** Props of {@link ScopeNote}: `always` for a route the scope cannot narrow, or the summary and the count keys (or prefixes) the box shows. */
export interface ScopeNoteProps { always?: boolean; summary?: Pick<Summary, "unscoped">; keys?: string[] }

/** The note, or nothing. */
export function ScopeNote({ always = false, summary, keys = [] }: ScopeNoteProps) {
  const t = useT();
  const scope = useScope();
  if (!isScoped(scope)) return null;
  if (!always && !networkWide(summary, keys)) return null;
  return <span className="scope-note" role="note" title={t("kit.scopeNote.title")}>{t("kit.scopeNote")}</span>;
}
