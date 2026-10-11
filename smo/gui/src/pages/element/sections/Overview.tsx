/** Element detail · overview (`element.overview`): four tiles (critical alarms of all alarms, cells and their guards, config changes in its
 * history, neighbour relations and the one-way ones) and the element's O1 endpoint card (adaptor, services, conformance, pinned host keys,
 * onboarding). Counts come from one-row pages' `total`; the cells and relations of one element are bounded lists. Cell state, PCI and load
 * from the mockup's cell table are not served by any route (README, Known limits). */
import { Link } from "react-router-dom";

import { Card, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { countLinks } from "../../topology/data/graph";
import { useAlarmCount, useElementLinks, useEntity, useHistory, useHostKeys, useOnboarding } from "../data/queries";

/** The overview of element `me`. */
export function Overview({ me }: { me: string }) {
  const t = useT();
  const entity = useEntity(me);
  const critical = useAlarmCount(me, "critical");
  const all = useAlarmCount(me);
  const history = useHistory(me, { limit: 1, offset: 0 });
  const links = useElementLinks(me);
  const e = entity.data;
  const guards = Object.values(e?.cellGuards ?? {});
  const c = links.data ? countLinks(links.data) : null;
  const special = guards.filter((g) => g.cellClass !== "NORMAL").length;
  return (
    <div className="stack" data-section="element.overview">
      <div className="grid g4">
        <Kpi label={t("element.overview.critical")} value={critical.data?.total === undefined ? null : formatCount(critical.data.total)} tone={critical.data?.total ? "hot" : undefined}
          foot={all.data?.total !== undefined ? t("element.overview.critical.foot", { count: all.data.total, total: formatCount(all.data.total) }) : undefined} to={`/alarms?me=${encodeURIComponent(me)}`} />
        <Kpi label={t("element.overview.cells")} value={e ? formatCount(guards.length) : null} foot={e ? (special ? t("element.overview.cells.guarded", { n: special }) : t("element.overview.cells.allNormal")) : undefined} />
        <Kpi label={t("element.overview.changes")} value={history.data?.total === undefined ? null : formatCount(history.data.total)} foot={t("element.overview.changes.foot")} />
        <Kpi label={t("element.overview.relations")} value={c ? formatCount(c.total) : null}
          foot={c ? (c.notReciprocal ? <span className="t-bad">{t("element.overview.relations.notReciprocal", { n: c.notReciprocal })}</span> : t("element.overview.relations.allReciprocal")) : undefined}
          to={`/topology?me=${encodeURIComponent(me)}`} />
      </div>
      <Endpoint me={me} endpointId={e?.o1AdaptorEndpointId ?? null} services={e?.supportedServices ?? null} conformance={e?.conformanceMode ?? null} />
    </div>
  );
}

/** The O1 endpoint card. */
function Endpoint({ me, endpointId, services, conformance }: { me: string; endpointId: string | null; services: string[] | null; conformance: string | null }) {
  const t = useT();
  const keys = useHostKeys(endpointId);
  const onboarding = useOnboarding(me);
  const key = keys.data?.[0];
  return (
    <Card section="element.endpoint" title={t("element.endpoint.title")} actions={<Link className="small" to="/configuration#trust">{t("element.endpoint.trust")}</Link>}>
      <KeyValue items={[
        [t("element.endpoint.adaptor"), endpointId ? <Id value={endpointId} /> : t("element.endpoint.adaptor.none")],
        [t("element.endpoint.services"), services?.length ? services.join(", ") : null],
        [t("element.endpoint.conformance"), conformance],
        [t("element.endpoint.hostKey"), key ? t("element.endpoint.hostKey.pinned", { type: key.keyType, user: key.pinnedBy }) : keys.error ? <span className="muted">{t("element.endpoint.hostKey.unreadable")}</span> : keys.data ? <span className="t-bad">{t("element.endpoint.hostKey.none")}</span> : null],
        [t("element.endpoint.onboarding"), onboarding.data ? <StateBadge state={onboarding.data.status} /> : onboarding.error?.status === 404 ? <span className="muted">{t("element.endpoint.onboarding.none")}</span> : null],
      ]} />
    </Card>
  );
}
