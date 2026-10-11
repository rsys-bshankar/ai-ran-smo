/** Section `approvals.votes` ("Approvals so far", two-person approval): for a request whose rApp's policy needs two different people
 * (`requiredApprovals: 2`), who has approved so far, when and with what reason, in the order RAN NF OAM recorded them. Draws nothing for a
 * request that needs one approval, so such a request looks as it always did. Reads only the request already loaded by `ApprovalBody`. */
import type { ApprovalDetail } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";

/** The approvals given so far, or nothing when one approval is enough. */
export function Votes({ approval: a }: { approval: ApprovalDetail }) {
  const t = useT();
  const needed = a.requiredApprovals ?? 1;
  const given = a.approvals ?? [];
  if (needed <= 1) return null;
  return (
    <Card section="approvals.votes" title={t("approvals.votes.title", { given: given.length, needed })}>
      {given.length === 0
        ? <p className="muted small">{t("approvals.votes.none", { needed })}</p>
        : <ul aria-label={t("approvals.votes.aria")}>{given.map((v, i) => <li key={i} className="small"><b>{v.by}</b> <span className="muted">{formatTime(v.at)}{v.reason ? ` · ${v.reason}` : ""}</span></li>)}</ul>}
    </Card>
  );
}
