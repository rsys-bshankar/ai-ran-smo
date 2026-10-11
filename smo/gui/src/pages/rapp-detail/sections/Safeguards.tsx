/** The rApp detail "Safeguards" box: whether its writes are stopped, and its limits as meters (config jobs used this hour against the hourly
 * limit; elements per job and change per write as maxima), from `/rapp-mgmt/instances/{id}/safeguards`. Stop and limits are changed in the
 * header (Stop / Resume) and under Safeguards. Section id `rapp.safeguards`. */
import { Link } from "react-router-dom";

import { Card, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { UsageMeter } from "../../../kit/Meter";
import { Empty, QueryState } from "../../../kit/states";
import { describeLimits, formatTime } from "../../../lib/domain";
import { useSafeguards } from "../data/queries";

/** The box of instance `id`. */
export function Safeguards({ id }: { id: string }) {
  const t = useT();
  const q = useSafeguards(id);
  const d = q.data;
  const l = d?.limits;
  return (
    <Card section="rapp.safeguards" title={t("rappDetail.safeguards.title")} actions={<Link className="btn small" to="/safeguards">{t("rappDetail.safeguards.edit")}</Link>}>
      <QueryState q={q} isEmpty={() => false}>
        {d && !d.invokerId ? <Empty title={t("rappDetail.safeguards.terminated")} /> : d && (
          <div className="stack">
            <KeyValue items={[
              [t("rappDetail.safeguards.status"), d.killed ? <span key="k"><StateBadge state="DISABLED" /> {t("rappDetail.safeguards.stoppedBy", { who: d.kill?.killedBy ?? "", reason: d.kill?.reason ?? t("rappDetail.safeguards.noReason"), time: formatTime(d.kill?.killedAt) })}</span> : <StateBadge key="k" state="ACTIVE" />],
              [t("rappDetail.safeguards.limits"), describeLimits(l)],
            ]} />
            {l?.maxConfigJobsPerHour != null && (
              <div><div className="row between small"><span>{t("rappDetail.safeguards.configJobs")}</span><span className="num">{l.configJobsLastHour} / {l.maxConfigJobsPerHour}</span></div>
                <UsageMeter used={l.configJobsLastHour} limit={l.maxConfigJobsPerHour} label={t("rappDetail.safeguards.configJobs")} /></div>
            )}
            {l && <div className="row gap wrap small">
              <span className="chip">{t("rappDetail.safeguards.elementsPerJob", { value: l.maxElementsPerJob ?? t("rappDetail.safeguards.noLimit") })}</span>
              <span className="chip">{t("rappDetail.safeguards.changePerWrite", { value: l.maxChangePercent != null ? t("rappDetail.safeguards.maxPercent", { value: l.maxChangePercent }) : t("rappDetail.safeguards.noLimit") })}</span>
            </div>}
          </div>
        )}
      </QueryState>
      <p className="small"><Link to="/safeguards">{t("rappDetail.safeguards.link")}</Link></p>
    </Card>
  );
}
