/** The Decisions page (route /decisions, PR-AI-13.4, BRIEF §4 "Decisions", handoff `Decisions.dc.html`): why rApps acted — one record per
 * config job an rApp made, and per request that ended without one. Filter bar, four 24 h tiles, the record table and, for the selected row, the
 * decision chain and its integrity. `DecisionDetail` is the one-record route `/decisions/:decisionId`. Sections and their calls: README.md.
 * Every word comes from the catalog (`i18n/en/decisions.ts`, GUI-6.4). */
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import type { DecisionRecord } from "../../api/types";
import { ErrorBox, Id, PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { decisionFilterQuery, sinceOf, useDecisionRecord, type DecisionFilter, type Range } from "./data/queries";
import { DecisionChain } from "./sections/DecisionChain";
import { DecisionTable } from "./sections/DecisionTable";
import { FilterBar } from "./sections/FilterBar";
import { Integrity, IntegrityOf } from "./sections/Integrity";
import { Outcome, Why } from "./sections/RecordDetail";
import { SummaryTiles } from "./sections/SummaryTiles";

/** The list page. `?job=` and `?approval=` narrow it to one job or one approval request; `?invoker=` starts it filtered to one rApp (the rApp detail page links here). */
export function Decisions() {
  const t = useT();
  const [params] = useSearchParams();
  const job = params.get("job") ?? "";
  const approval = params.get("approval") ?? "";
  const invoker = params.get("invoker") ?? "";
  const [range, setRange] = useState<Range>(job || approval ? "all" : "24h");
  const [filter, setFilter] = useState<DecisionFilter>(() => ({ invoker, disposition: "", model: "", until: "", job, approval, since: sinceOf(job || approval ? "all" : "24h") }));
  const [selected, setSelected] = useState<DecisionRecord | null>(null);
  const query = useMemo(() => decisionFilterQuery({ ...filter, job, approval }), [filter, job, approval]);
  const onChange = (patch: Partial<DecisionFilter>) => setFilter((f) => ({ ...f, ...patch }));
  const onRange = (r: Range) => { setRange(r); onChange({ since: sinceOf(r) }); };
  return (
    <>
      <PageHeader eyebrow={t("decisions.eyebrow")} title={t("decisions.title")} subtitle={t("decisions.subtitle")} />
      <SectionBoundary id="decisions.filters"><FilterBar filter={{ ...filter, job, approval }} range={range} onChange={onChange} onRange={onRange} query={query} /></SectionBoundary>
      <SectionBoundary id="decisions.tiles"><SummaryTiles /></SectionBoundary>
      <div className="grid g-main-side">
        <SectionBoundary id="decisions.table"><DecisionTable query={query} selected={selected} onSelect={setSelected} /></SectionBoundary>
        <div className="stack">
          <SectionBoundary id="decisions.chain"><DecisionChain record={selected} /></SectionBoundary>
          <SectionBoundary id="decisions.integrity"><IntegrityOf decisionId={selected?.decisionId ?? null} /></SectionBoundary>
        </div>
      </div>
    </>
  );
}

/** One decision record, with the result of checking it against the audit chain, and the job and approval request it belongs to. */
export function DecisionDetail() {
  const t = useT();
  const { decisionId } = useParams();
  const record = useDecisionRecord(decisionId);
  const r = record.data;
  return (
    <>
      <PageHeader title={t("decisions.detail.title")} subtitle={<><Id value={decisionId} /> · <Link to="/decisions">{t("decisions.detail.back")}</Link></>} />
      <ErrorBox error={record.error} />
      {r && <>
        <SectionBoundary id="decisions.integrity"><Integrity record={r} /></SectionBoundary>
        <div className="grid g2">
          <SectionBoundary id="decisions.outcome"><Outcome record={r} /></SectionBoundary>
          <SectionBoundary id="decisions.why"><Why record={r} /></SectionBoundary>
        </div>
        <SectionBoundary id="decisions.chain"><DecisionChain record={r} /></SectionBoundary>
      </>}
    </>
  );
}
