/** Section `safeguards.stopped`: everything stopped at RAN NF OAM, by invoker id, including rApps that are not instances of this platform; paged on
 * the server. Resume is per instance, in the limits table. */
import type { RappKill } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { STOPPED } from "../data/queries";

/** The stopped list. */
export function Stopped() {
  const t = useT();
  return (
    <Card section="safeguards.stopped" title={t("safeguards.stoppedList.title")}>
      <p className="muted small">{t("safeguards.stoppedList.intro")}</p>
      <ServerTable<RappKill> path={STOPPED} rowKey={(r) => r.invokerId} empty={t("safeguards.stoppedList.empty")} columns={[
        { header: t("safeguards.stoppedList.invoker"), render: (r) => <Id value={r.invokerId} /> },
        { header: t("safeguards.stoppedList.stoppedBy"), render: (r) => r.killedBy },
        { header: t("safeguards.reason"), render: (r) => r.reason ?? <span className="muted">—</span> },
        { header: t("safeguards.stoppedList.since"), render: (r) => formatTime(r.killedAt) },
      ]} />
    </Card>
  );
}
