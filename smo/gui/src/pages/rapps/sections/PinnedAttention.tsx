/** "Pinned & needs attention" (SCALE.md, rApps: the instance cards become a strip): your pinned rApps, then FAULTED and UPGRADING ones, at
 * most six cards. Pins come from the BFF (`/api/me/pins`, shared with the sidebar); the faulted and upgrading rApps from the BFF directory
 * filtered by state (`/api/rapps?state=`), asked only when the summary counts any. Each card shows the rApp's headline KPI (one batched read,
 * `/rapp-mgmt/instances/performance/latest?ids=`). Section id `rapps.pinned`. */
import { Link } from "react-router-dom";

import { usePins, type RappSummary } from "../../../api/rapps";
import { Card, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Icon } from "../../../kit/icons";
import { Empty, Skeleton } from "../../../kit/states";
import { count } from "../../../data/summary";
import { useLatestKpis, useRappsInState, useRappsSummary } from "../data/queries";
import { HeadlineKpi } from "./HeadlineKpi";
import { InstanceActions } from "./InstanceActions";
import { LifecycleCell } from "./LifecycleCell";

/** At most this many cards. */
export const MAX_CARDS = 6;

/** Pins first, then faulted, then upgrading, without duplicates, cut at `max`. */
export function pickAttention(pins: RappSummary[], faulted: RappSummary[], upgrading: RappSummary[], max = MAX_CARDS): RappSummary[] {
  const seen = new Set<string>();
  const out: RappSummary[] = [];
  for (const r of [...pins, ...faulted, ...upgrading]) {
    if (seen.has(r.instanceId)) continue;
    seen.add(r.instanceId);
    out.push(r);
  }
  return out.slice(0, max);
}

/** The strip. */
export function PinnedAttention() {
  const t = useT();
  const summary = useRappsSummary();
  const pins = usePins();
  const nFaulted = count(summary.data, "instances.FAULTED") ?? 0;
  const nUpgrading = count(summary.data, "instances.UPGRADING") ?? 0;
  const faulted = useRappsInState("FAULTED", MAX_CARDS, nFaulted > 0);
  const upgrading = useRappsInState("UPGRADING", MAX_CARDS, nUpgrading > 0);
  const cards = pickAttention(pins.data?.items ?? [], nFaulted ? faulted.data?.items ?? [] : [], nUpgrading ? upgrading.data?.items ?? [] : []);
  const loading = !pins.data && pins.isLoading;
  const kpis = useLatestKpis(cards.map((c) => c.instanceId));
  return (
    <Card section="rapps.pinned" title={t("rapps.pinned.title")} sub={t("rapps.pinned.sub", { max: MAX_CARDS })}>
      {loading ? <Skeleton lines={2} /> : cards.length === 0
        ? <Empty title={t("rapps.pinned.empty")}>{t("rapps.pinned.empty.body")}</Empty>
        : (
          <div className="tiles">
            {cards.map((r) => (
              <div key={r.instanceId} className={`tile${r.state === "FAULTED" ? " bad" : r.state === "UPGRADING" ? " warn" : ""}`}>
                <div className="row gap"><Icon name={r.pinned ? "pin" : r.state === "FAULTED" ? "warn" : "rapps"} />
                  <Link to={`/rapps/${r.instanceId}`}><strong>{r.name ?? r.instanceId.slice(0, 8)}</strong></Link></div>
                <span className="small muted">{r.version ?? "—"}{r.vendor ? ` · ${r.vendor}` : ""}</span>
                <div className="row gap wrap"><StateBadge state={r.state} /><StateBadge state={r.autonomyMode} /></div>
                <HeadlineKpi kpi={kpis.get(r.instanceId)} />
                <LifecycleCell state={r.state} />
                {r.state && <InstanceActions inst={{ instanceId: r.instanceId, state: r.state }} />}
              </div>
            ))}
          </div>
        )}
    </Card>
  );
}

