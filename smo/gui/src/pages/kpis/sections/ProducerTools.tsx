/** Section `kpis.producerTools` (RAN Analytics tab, admin-only, gated by the caller): act as an MDAF analytics producer (call flow 08):
 * register a producer (`POST /ran-analytics/producers`, which also registers `mdaf.<type>.<producer>` with SME) and publish a report (`POST /mdaf/reports`),
 * pushed best-effort to subscribers with a notification destination. */
import { useState } from "react";

import { ActionButton, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { MDAF_REPORTS, PRODUCERS, useDmeTypes } from "../data/queries";
import { useAnalyticsTypes } from "./AnalyticsLegacy";

/** The two admin forms side by side. */
export function ProducerTools() {
  const t = useT();
  const types = useAnalyticsTypes();
  const dmeTypes = useDmeTypes();
  const [producer, setProducer] = useState("rapp-mdaf-1");
  const [type, setType] = useState("coverage-issue-analysis");
  const [inputs, setInputs] = useState<string[]>([]);
  const [reportType, setReportType] = useState("");
  const [output, setOutput] = useState('{"coverageHoles": 2, "worstCell": "cell-7"}');
  const parsed = parseJsonObject(output, t);
  return (
    <Card section="kpis.producerTools" title={t("kpis.producer.title")} actions={<span className="muted small">{t("kpis.producer.adminNote")}</span>}>
      <div className="grid cols-2">
        <div>
          <h3>{t("kpis.producer.register.heading")}</h3>
          <p className="muted small">{t("kpis.producer.register.before")}<code>{t("kpis.producer.register.type")}</code>{t("kpis.producer.register.after")}</p>
          <div className="form">
            <Field label={t("kpis.producer.register.id")}><input value={producer} onChange={(e) => setProducer(e.target.value)} /></Field>
            <Field label={t("kpis.common.analyticsType")}><input value={type} onChange={(e) => setType(e.target.value)} /></Field>
            <Field label={t("kpis.producer.register.inputs")} hint={t("kpis.producer.register.inputs.hint")}>
              <select multiple size={Math.min(4, Math.max(2, dmeTypes.data?.length ?? 2))} value={inputs} onChange={(e) => setInputs([...e.target.selectedOptions].map((o) => o.value))}>
                {dmeTypes.data?.map((d) => <option key={d.dmeTypeId} value={d.dmeTypeId}>{d.typeName}</option>)}
              </select>
            </Field>
          </div>
          <ActionButton label={t("kpis.producer.register.submit")} tone="primary" disabled={!producer || !type} action={{
            method: "POST", path: PRODUCERS, query: { producer_id: producer, analytics_type: type },
            json: { dme_input_types: inputs, output_schema: { type: "object" } }, success: t("kpis.producer.register.success"),
          }} />
        </div>
        <div>
          <h3>{t("kpis.producer.publish.heading")}</h3>
          <p className="muted small">{t("kpis.producer.publish.intro")}</p>
          <div className="form">
            <Field label={t("kpis.common.analyticsType")}><input list="an-types-pub" value={reportType} onChange={(e) => setReportType(e.target.value)} /><datalist id="an-types-pub">{types.map((ty) => <option key={ty} value={ty} />)}</datalist></Field>
            <Field label={t("kpis.producer.publish.output")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><textarea rows={3} value={output} onChange={(e) => setOutput(e.target.value)} spellCheck={false} /></Field>
          </div>
          <ActionButton label={t("kpis.producer.publish.submit")} tone="primary" disabled={!reportType || !parsed.ok} action={{
            method: "POST", path: MDAF_REPORTS, query: { analytics_type: reportType },
            json: { output: parsed.ok ? parsed.value : {}, input_sources: inputs }, success: t("kpis.producer.publish.success"),
          }} />
        </div>
      </div>
    </Card>
  );
}
