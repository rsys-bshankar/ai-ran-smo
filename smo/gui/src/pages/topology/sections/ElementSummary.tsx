/** RAN topology · element summary (`topology.element`): the focused managed element at a glance (vendor, O1 protocol, region and tenant,
 * its cells and their guard classes, sector groups and incident zones), from `GET /managed-entities/{me}`, with links to its Element
 * detail page, its alarms and its cell guards. */
import { Link } from "react-router-dom";

import { Card, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Empty, QueryState } from "../../../kit/states";
import { elementHref } from "../../element/data/types";
import { useEntity, useTopologyParams } from "../data/queries";

/** The summary card. */
export function ElementSummary() {
  const t = useT();
  const { me } = useTopologyParams();
  const entity = useEntity(me);
  if (!me) return <Card section="topology.element" title={t("topology.element.title")}><Empty title={t("topology.element.none")} /></Card>;
  const e = entity.data;
  const guards = Object.entries(e?.cellGuards ?? {});
  const distinct = (k: "sectorGroup" | "incidentZone") => [...new Set(guards.map(([, g]) => g[k]).filter(Boolean))].join(", ") || null;
  return (
    <Card section="topology.element" title={me} actions={<Link className="small" to={elementHref(me)}>{t("topology.element.detail")}</Link>}>
      <QueryState q={entity} isEmpty={() => false}>
        {e && <>
          <KeyValue items={[
            [t("topology.element.vendor"), e.vendorName ? `${e.vendorName}${e.o1Protocol ? ` · O1 ${e.o1Protocol}` : ""}` : null],
            [t("topology.element.type"), e.entityType],
            [t("topology.element.regionTenant"), e.region || e.tenant ? `${e.region ?? "—"} / ${e.tenant ?? "—"}` : null],
            [t("topology.element.cells"), guards.length ? guards.map(([id]) => id).join(", ") : null],
            [t("topology.element.sectorGroup"), distinct("sectorGroup")],
            [t("topology.element.incidentZone"), distinct("incidentZone")],
            [t("topology.element.guards"), guards.filter(([, g]) => g.cellClass !== "NORMAL").length
              ? <span className="row wrap">{guards.filter(([, g]) => g.cellClass !== "NORMAL").map(([id, g]) => <span key={id} className="small">{id} <StateBadge state={g.cellClass} /></span>)}</span>
              : t("topology.element.allNormal")],
          ]} />
          <div className="row wrap">
            <Link className="btn small" to={`/alarms?me=${encodeURIComponent(me)}`}>{t("topology.element.alarms")}</Link>
            <Link className="btn small" to={elementHref(me, "guards")}>{t("topology.element.cellGuards")}</Link>
          </div>
        </>}
      </QueryState>
    </Card>
  );
}
