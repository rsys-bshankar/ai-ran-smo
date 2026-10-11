/** Section `decisions.integrity`: whether a record still matches the hash written to the audit chain, as the API reports it
 * (`GET /decision-records/{id}` → `integrity`: VERIFIED, UNCHAINED or MISMATCH), with the record hash and its audit row. The API checks one
 * record against its own audit row, not the whole chain, and serves no "previous hash"; the box says how to verify the chain. */
import type { DecisionRecord } from "../../../api/types";
import { Card, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { QueryState } from "../../../kit/states";
import { INTEGRITY_MEANING } from "../../../lib/domain";
import { useDecisionRecord } from "../data/queries";

/** The command that verifies the whole audit chain (a command, not a word to translate). */
const VERIFY_COMMAND = "python -m smo_shared.audit verify";

/** The integrity card of a record that was read with its check. */
export function Integrity({ record: r }: { record: DecisionRecord }) {
  const t = useT();
  return (
    <Card section="decisions.integrity" title={t("decisions.integrity.title")}>
      <div className="row gap wrap" role="status">
        <StateBadge state={r.integrity?.status} />
        <span className="small">{r.integrity && INTEGRITY_MEANING[r.integrity.status] ? t(INTEGRITY_MEANING[r.integrity.status]) : ""}</span>
      </div>
      {r.integrity?.reason && <p className="small">{r.integrity.reason}</p>}
      <KeyValue items={[
        [t("decisions.integrity.recordHash"), <code key="h" className="id">{r.contentHash}</code>],
        [t("decisions.integrity.auditRow"), r.integrity?.auditSeq ?? r.auditSeq],
        [t("decisions.integrity.auditHash"), r.integrity?.auditHash ? <code key="a" className="id">{r.integrity.auditHash}</code> : null],
      ]} />
      <p className="muted small">{t("decisions.integrity.note")} <code>{VERIFY_COMMAND}</code>.</p>
    </Card>
  );
}

/** The integrity card for a record id: reads the record with its check (one call per selected row). */
export function IntegrityOf({ decisionId }: { decisionId: string | null }) {
  const record = useDecisionRecord(decisionId);
  if (!decisionId) return null;
  return (
    <QueryState q={record} isEmpty={() => false}>
      {record.data && <Integrity record={record.data} />}
    </QueryState>
  );
}
