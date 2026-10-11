/** The SMO modules a flow crosses, as chips (from `lib/flows.ts` FlowDef.modules). Section id `flows.modules`. */
import { useT } from "../../../i18n";

/** The chip row. */
export function ModuleChips({ modules }: { modules: string[] }) {
  const t = useT();
  return <div className="row gap wrap" data-section="flows.modules" aria-label={t("flows.modules.aria")}>{modules.map((m) => <span key={m} className="chip mono">{m}</span>)}</div>;
}
