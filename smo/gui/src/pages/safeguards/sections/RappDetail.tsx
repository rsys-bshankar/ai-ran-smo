/** Section `safeguards.detail`: the selected rApp instance — whether it may write (and who stopped it, why, when), its limits as meters, its
 * approval policy, and its refusals in the last 24 h (a server count: one call with `limit=1`). */
import { useMemo } from "react";

import { Card, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { UsageMeter } from "../../../kit/Meter";
import { Empty, QueryState } from "../../../kit/states";
import { describeApprovalPolicy, describeLimits, formatTime } from "../../../lib/domain";
import { refusalSince, useInstanceSafeguards, useInvokerRefusalCount } from "../data/queries";

/** The detail card; `instanceId` null asks the operator to pick a row. */
export function RappDetail({ instanceId }: { instanceId: string | null }) {
  const t = useT();
  const sg = useInstanceSafeguards(instanceId);
  const since = useMemo(() => refusalSince("24h") ?? "", [instanceId]); // eslint-disable-line react-hooks/exhaustive-deps
  const invoker = sg.data?.invokerId ?? null;
  const refusals = useInvokerRefusalCount(since, invoker);
  if (!instanceId) return <Card section="safeguards.detail" title={t("safeguards.detail.title")}><Empty title={t("safeguards.detail.none.title")}>{t("safeguards.detail.none.body")}</Empty></Card>;
  const v = sg.data;
  const l = v?.limits;
  return (
    <Card section="safeguards.detail" title={<>{t("safeguards.detail.title")} <Id value={instanceId} /></>}>
      <QueryState q={sg} isEmpty={() => false}>
        {v && <>
          <div className="row gap wrap">
            {!v.invokerId ? <span className="muted">{t("safeguards.detail.terminated")}</span> : v.killed ? <><StateBadge state="DISABLED" /> {t("safeguards.stopped")}</> : <StateBadge state="ACTIVE" />}
            {v.invokerId && <Id value={v.invokerId} />}
          </div>
          {v.killed && v.kill && <p className="small">{t("safeguards.detail.stoppedBy", { who: v.kill.killedBy, when: formatTime(v.kill.killedAt), reason: v.kill.reason ?? t("safeguards.noReason") })}</p>}
          {l?.maxConfigJobsPerHour != null && <div className="col"><span className="small">{t("safeguards.detail.jobsThisHour", { used: l.configJobsLastHour, max: l.maxConfigJobsPerHour })}</span>
            <UsageMeter used={l.configJobsLastHour} limit={l.maxConfigJobsPerHour} /></div>}
          <KeyValue items={[
            [t("safeguards.detail.limits"), v.invokerId ? describeLimits(l, t) : "—"],
            [t("safeguards.detail.approval"), v.invokerId ? describeApprovalPolicy(v.approvalPolicy, t) : "—"],
            [t("safeguards.detail.refusals"), v.invokerId ? (refusals.data ? formatCount(refusals.data.total ?? null) : refusals.error ? t("safeguards.unknown") : "…") : "—"],
          ]} />
        </>}
      </QueryState>
    </Card>
  );
}
