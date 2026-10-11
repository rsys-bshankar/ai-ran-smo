/** The network health map (`dashboard.map`, handoff `Main.dc.html`, BRIEF §4b): regions as tiles, a region opens its site clusters, a site
 * cluster opens its managed elements (server-paged, each linking to the element page); the crumb goes back up. Every tile is coloured by the
 * worst open alarm severity of its elements and shows how many are unhealthy (an open critical or major alarm), all computed by RAN NF OAM
 * (`GET /managed-entities/health?group_by=region|site_cluster`, ran-nf-oam/app/fleet.py). Elements with no region are counted but cannot be
 * opened (no route filters "no region"); a region's elements with no site cluster open as the region's whole element list, labelled so. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { Icon } from "../../../kit/icons";
import { ServerTable } from "../../../kit/ServerTable";
import { QueryState } from "../../../kit/states";
import { ELEMENTS_PATH, useFleetHealth } from "../data/queries";
import type { HealthGroup, ManagedEntity, WorstSeverity } from "../data/types";

/** The tile tone of a group's worst open severity: critical → bad, major → warn, anything else → ok. */
export function toneOf(sev: WorstSeverity): "bad" | "warn" | "ok" {
  return sev === "critical" ? "bad" : sev === "major" ? "warn" : "ok";
}

/** True when a health answer has no group (no element at this level). */
const noGroups = (d: unknown) => ((d as { groups?: unknown[] }).groups ?? []).length === 0;

/** Where the map is: all regions, one region's clusters, or one cluster's (or one region's) elements. */
type Level = { region: null } | { region: string; cluster?: undefined } | { region: string; cluster: string | null };

/** One tile of the map. */
function GroupTile({ g, kind, onOpen }: { g: HealthGroup; kind: "region" | "siteCluster"; onOpen?: () => void }) {
  const t = useT();
  const word = t(`dashboard.map.kind.${kind}`);
  const label = g.key ?? t(`dashboard.map.no.${kind}`);
  const body = (
    <>
      <span className="eyebrow">{word}</span>
      <b>{label}</b>
      <span className="small">{t("dashboard.map.tile.elements", { count: g.elements, total: formatCount(g.elements) })} · <span className={g.unhealthy ? "t-bad" : "muted"}>{t("dashboard.map.tile.unhealthy", { n: formatCount(g.unhealthy) })}</span></span>
      <span className="xs muted">{g.worstSeverity ? t("dashboard.map.tile.worst", { severity: g.worstSeverity }) : t("dashboard.map.tile.noAlarm")}</span>
    </>
  );
  const cls = `tile ${toneOf(g.worstSeverity)}`;
  return onOpen
    ? <button type="button" className={cls} onClick={onOpen} title={t("dashboard.map.tile.open", { kind: word, label })}>{body}</button>
    : <div className={cls} title={t("dashboard.map.tile.cannotOpen", { kind: word })}>{body}</div>;
}

/** The map card. */
export function HealthMap() {
  const t = useT();
  const [level, setLevel] = useState<Level>({ region: null });
  const regions = useFleetHealth("region");
  const inRegion = level.region !== null;
  const clusters = useFleetHealth("site_cluster", level.region, inRegion);
  const atElements = inRegion && "cluster" in level && level.cluster !== undefined;
  const cluster = atElements ? (level as { cluster: string | null }).cluster : undefined;
  const score = regions.data?.healthScore;
  const elements = regions.data?.groups.reduce((a, g) => a + g.elements, 0) ?? null;
  return (
    <Card section="dashboard.map" className="s2" title={t("dashboard.map.title")}
      sub={t("dashboard.map.sub", { count: elements ?? 0, total: formatCount(elements), score: score == null ? "—" : `${score} %` })}
      actions={<Link className="btn small" to="/topology">{t("dashboard.map.openTopology")}</Link>}>
      <nav className="crumb" aria-label={t("dashboard.map.level")}>
        {inRegion ? <button type="button" className="btn ghost small" onClick={() => setLevel({ region: null })}>{t("dashboard.map.allRegions")}</button> : <strong>{t("dashboard.map.allRegions")}</strong>}
        {inRegion && <><span aria-hidden>›</span>{atElements
          ? <button type="button" className="btn ghost small" onClick={() => setLevel({ region: level.region })}>{level.region}</button>
          : <strong>{level.region}</strong>}</>}
        {atElements && <><span aria-hidden>›</span><strong>{cluster ?? t("dashboard.map.allElements")}</strong></>}
        {!inRegion && <span>{t("dashboard.map.clickRegion")}</span>}
      </nav>
      {atElements ? (
        <>
          <ServerTable<ManagedEntity> path={ELEMENTS_PATH} query={{ region: level.region, site_cluster: cluster ?? undefined }}
            rowKey={(e) => `${e.managedElementRef}|${e.managedFunctionRef ?? ""}`} empty={t("dashboard.map.elements.empty")} columns={[
              { header: t("dashboard.map.elements.element"), render: (e) => <Link to={`/elements/${encodeURIComponent(e.managedElementRef)}`}>{e.managedElementRef}</Link> },
              { header: t("dashboard.map.elements.function"), render: (e) => e.managedFunctionRef ? <code className="small">{e.managedFunctionRef}</code> : <span className="muted">—</span> },
              { header: t("dashboard.map.elements.siteCluster"), render: (e) => e.siteCluster ?? <span className="muted">—</span> },
              { header: t("dashboard.map.elements.type"), render: (e) => e.entityType ?? "—" },
              { header: t("dashboard.map.elements.vendor"), render: (e) => e.vendorName ?? "—" },
            ]} />
          <div className="row between">
            <button type="button" className="btn small" onClick={() => setLevel({ region: level.region })}><Icon name="back" size={14} />{t("dashboard.map.back", { region: level.region })}</button>
            <Link className="small" to="/alarms">{t("dashboard.map.alarmConsole")}</Link>
          </div>
        </>
      ) : inRegion ? (
        <QueryState q={clusters} isEmpty={noGroups} empty={<p className="muted">{t("dashboard.map.region.empty")}</p>}>
          {clusters.data && (
            <>
              <div className="tiles">
                {clusters.data.groups.map((g) => <GroupTile key={g.key ?? "∅"} g={g} kind="siteCluster" onOpen={() => setLevel({ region: level.region, cluster: g.key })} />)}
              </div>
              {clusters.data.groups.some((g) => g.key === null) && <p className="small muted">{t("dashboard.map.region.noCluster", { region: level.region })}</p>}
            </>
          )}
        </QueryState>
      ) : (
        <QueryState q={regions} isEmpty={noGroups}
          empty={<p className="muted">{t("dashboard.map.empty")}</p>}>
          {regions.data && (
            <div className="tiles">
              {regions.data.groups.map((g) => <GroupTile key={g.key ?? "∅"} g={g} kind="region" onOpen={g.key === null ? undefined : () => setLevel({ region: g.key! })} />)}
            </div>
          )}
        </QueryState>
      )}
    </Card>
  );
}
