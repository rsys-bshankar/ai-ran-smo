/** Section `intents.formulas` (Utility formulas tab, feature 10): the TS 28.312 IntentUtilityFormula resources of Intent Service, server-paged:
 * the utility function, its weighted parameters, scale and offset. Read-only: the BFF exposes no write on formulas (README, Known limits). An
 * intent names its formula in `attributes.intentUtilityFormulaRef`; how many intents use each is not served. */
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { ServerTable } from "../../../kit/ServerTable";
import { FORMULAS } from "../data/queries";

/** One formula as Intent Service answers it. */
export interface UtilityFormula { id: string; attributes: { utilityFunctionId: string; utilityParameterList: { parameterName?: string | null; parameterWeight?: number | null }[]; utilityScale: number; utilityOffset: number } }

/** The tab. */
export function UtilityFormulas() {
  const t = useT();
  return (
    <div className="stack">
      <Callout tone="volt">{t("intents.formulas.callout")}</Callout>
      <Card section="intents.formulas" title={t("intents.formulas.title")} sub={t("intents.formulas.sub")}>
        <ServerTable<UtilityFormula> path={FORMULAS} rowKey={(f) => f.id} empty={t("intents.formulas.empty")} columns={[
          { header: t("intents.formulas.col.formula"), render: (f) => <Id value={f.id} /> },
          { header: t("intents.formulas.col.function"), render: (f) => <code className="small">{f.attributes.utilityFunctionId}</code> },
          { header: t("intents.formulas.col.parameters"), render: (f) => f.attributes.utilityParameterList.map((p) => `${p.parameterName ?? "?"}${p.parameterWeight != null ? ` × ${p.parameterWeight}` : ""}`).join(", ") || <span className="muted">—</span> },
          { header: t("intents.formulas.col.scale"), render: (f) => <span className="mono">{f.attributes.utilityScale}</span> },
          { header: t("intents.formulas.col.offset"), render: (f) => <span className="mono">{f.attributes.utilityOffset}</span> },
        ]} />
      </Card>
    </div>
  );
}
