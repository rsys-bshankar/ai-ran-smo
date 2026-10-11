/** Section `decisions.filters`: the list's filters, each one a query parameter of the route — rApp (invoker id), outcome, model version, a time
 * range (default the last 24 h, SCALE.md: 100k records a day need a range) and an optional "until". `?job=` / `?approval=` in the address narrow
 * the list to one config job or one approval request; then the range starts at "all", so an older record still shows. "Export…" (operator)
 * starts an asynchronous export job of the records of the current range, rApp, outcome and scope (GUI-9.5b, `data/exports.ts`; no span limit,
 * so "All" exports too); the file is downloaded from the Exports page. */
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/AuthContext";
import { roleAtLeast } from "../../../auth/rbac";
import { ExportJobButton } from "../../../components/ExportJobButton";
import { Card, Field, Id } from "../../../components/ui";
import { decisionsExport } from "../../../data/exports";
import { useT } from "../../../i18n";
import { useScope } from "../../../data/scope";
import { Segmented } from "../../../kit/Segmented";
import type { Query } from "../../../api/client";
import { DISPOSITIONS, type DecisionFilter, type Range } from "../data/queries";

/** Props of {@link FilterBar}. */
export interface FilterBarProps { filter: DecisionFilter; range: Range; onChange: (patch: Partial<DecisionFilter>) => void; onRange: (r: Range) => void; query?: Query }

/** The filter bar. */
export function FilterBar({ filter, range, onChange, onRange, query }: FilterBarProps) {
  const t = useT();
  const { me } = useAuth();
  const scope = useScope();
  const canExport = !!me && roleAtLeast(me.role, "operator");
  return (
    <Card section="decisions.filters">
      <div className="row gap wrap">
        <Field label={t("decisions.filters.invoker")}><input aria-label={t("decisions.filters.invoker.aria")} value={filter.invoker} onChange={(e) => onChange({ invoker: e.target.value })} /></Field>
        <Field label={t("decisions.filters.outcome")}>
          <select aria-label={t("decisions.filters.outcome.aria")} value={filter.disposition} onChange={(e) => onChange({ disposition: e.target.value })}>
            <option value="">{t("decisions.filters.all")}</option>
            {DISPOSITIONS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </Field>
        <Field label={t("decisions.filters.model")}><input aria-label={t("decisions.filters.model.aria")} value={filter.model} onChange={(e) => onChange({ model: e.target.value })} /></Field>
        <Field label={t("decisions.filters.since")}>
          <Segmented<Range> label={t("decisions.filters.range")} value={range} onChange={onRange}
            options={[{ id: "1h", label: "1 h" }, { id: "24h", label: "24 h" }, { id: "7d", label: "7 d" }, { id: "all", label: t("decisions.filters.all") }]} />
        </Field>
        <Field label={t("decisions.filters.until")} hint={t("decisions.filters.optional")}><input type="datetime-local" aria-label={t("decisions.filters.until")} value={filter.until} onChange={(e) => onChange({ until: e.target.value })} /></Field>
        {query && canExport && (
          <ExportJobButton what={t("decisions.filters.exportWhat")} request={decisionsExport(query, scope)}
            note={filter.model || filter.job || filter.approval ? t("decisions.filters.exportNote") : null} />
        )}
      </div>
      {(filter.job || filter.approval) && (
        <p className="small">{t("decisions.filters.narrowedTo")} {filter.job ? <>{t("decisions.filters.job")} <Id value={filter.job} /></> : <>{t("decisions.filters.request")} <Id value={filter.approval} /></>}. <Link to="/decisions">{t("decisions.filters.showAll")}</Link></p>
      )}
    </Card>
  );
}
