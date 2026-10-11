/** Section `decisions.table`: the decision records matching the filter bar and the global scope, newest first, paged by keyset (GUI-9.5b,
 * `kit/KeysetTable`: the route's `after` cursor and `nextCursor`, so a deep page of a large range costs the same as the first; no count). A row
 * click selects it for the chain panel; the first row is selected when nothing is. The time links to the record's own page. */
import { useCallback } from "react";
import { Link } from "react-router-dom";

import type { Query } from "../../../api/client";
import type { DecisionRecord } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { KeysetTable } from "../../../kit/KeysetTable";
import { DISPOSITION_MEANING, formatTime } from "../../../lib/domain";
import { DECISION_RECORDS } from "../data/queries";

/** Props of {@link DecisionTable}. */
export interface DecisionTableProps { query: Query; selected: DecisionRecord | null; onSelect: (r: DecisionRecord) => void }

/** The table. */
export function DecisionTable({ query, selected, onSelect }: DecisionTableProps) {
  const t = useT();
  const onRows = useCallback((rows: DecisionRecord[]) => {
    const fresh = selected ? rows.find((r) => r.decisionId === selected.decisionId) : undefined;
    if (fresh) onSelect(fresh);
    else if (!selected && rows[0]) onSelect(rows[0]);
  }, [selected?.decisionId]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Card section="decisions.table" title={t("decisions.table.title")} sub={t("decisions.table.sub")}>
      <KeysetTable<DecisionRecord> path={DECISION_RECORDS} query={query} rowKey={(r) => r.decisionId}
        onRowClick={onSelect} selectedKey={selected?.decisionId ?? null} onRows={onRows}
        empty={t("decisions.table.empty")} columns={[
          { header: t("decisions.table.when"), render: (r) => <Link to={`/decisions/${r.decisionId}`} onClick={(e) => e.stopPropagation()}>{formatTime(r.occurredAt)}</Link> },
          { header: t("decisions.table.rapp"), render: (r) => <Id value={r.invokerId} /> },
          { header: t("decisions.table.outcome"), render: (r) => <span title={DISPOSITION_MEANING[r.disposition] && t(DISPOSITION_MEANING[r.disposition])}><StateBadge state={r.disposition} /></span> },
          { header: t("decisions.table.model"), render: (r) => r.modelVersion ?? <span className="muted">—</span> },
          { header: t("decisions.table.rationale"), render: (r) => <span className="small">{r.rationale ?? <span className="muted">{t("decisions.table.noRationale")}</span>}</span> },
          { header: t("decisions.table.changes"), render: (r) => r.changeCount },
          { header: t("decisions.table.approvedBy"), render: (r) => (r.approvers?.length ? r.approvers.join(", ") : r.approvedBy ?? <span className="muted">—</span>) },
        ]} />
    </Card>
  );
}
