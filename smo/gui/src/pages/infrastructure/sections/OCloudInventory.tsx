/** Infrastructure → O-Cloud inventory (`infrastructure.inventory`, BRIEF §4e feature 6): a level picker from where the O-Cloud is to what it
 * holds (locations → sites → node clusters → cluster resources → infrastructure resources), then provisioning requests and performance jobs, then
 * the O2-IMS pools, deployment managers and resource types. One level is listed at a time, as a server-paged table of that level's FOCOM route.
 * Picking a resource pool lists its resources below (`infrastructure.pool-resources`), with their CPU and memory utilisation (one batched FOCOM
 * read for the page shown, `GET /focom/utilisation?resource_ids=`, GUI-9.8b), deprovision and the admin "provision a resource". */
import { useCallback, useMemo, useState } from "react";

import type { OCloudResource, ResourcePool } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { parseJsonObject } from "../../../lib/domain";
import { formatPercent, INVENTORY_LEVELS, INVENTORY_POLL, PATHS, useUtilisationBatch } from "../data/queries";
import { levelSpecs } from "./InventoryColumns";

const GROUP_LABEL: Record<"place" | "work" | "ims", TKey> = { place: "infrastructure.inventory.group.place", work: "infrastructure.inventory.group.work", ims: "infrastructure.inventory.group.ims" };

/** The picker and the chosen level's table. */
export function OCloudInventory() {
  const t = useT();
  const [levelId, setLevelId] = useState(INVENTORY_LEVELS[0].id);
  const [pool, setPool] = useState<string | null>(null);
  const level = INVENTORY_LEVELS.find((l) => l.id === levelId)!;
  const spec = useMemo(() => levelSpecs(t), [t])[level.id];
  return (
    <>
      <Card section="infrastructure.inventory" title={t("infrastructure.inventory.title")} sub={t("infrastructure.inventory.sub")}>
        <div className="stack" style={{ gap: 8 }}>
          {(Object.keys(GROUP_LABEL) as (keyof typeof GROUP_LABEL)[]).map((g) => (
            <div key={g} className="row wrap" role="group" aria-label={t(GROUP_LABEL[g])}>
              <span className="eyebrow inv-group">{t(GROUP_LABEL[g])}</span>
              {INVENTORY_LEVELS.filter((l) => l.group === g).map((l, i, all) => (
                <span key={l.id} className="row">
                  <button type="button" className={`btn small${l.id === levelId ? " primary" : ""}`} aria-pressed={l.id === levelId} onClick={() => { setLevelId(l.id); setPool(null); }}>{t(l.labelKey)}</button>
                  {g === "place" && i < all.length - 1 && <span className="muted" aria-hidden>→</span>}
                </span>
              ))}
            </div>
          ))}
        </div>
        <ServerTable<never> key={level.id} path={level.path} rowKey={spec.rowKey} columns={spec.columns} refetchInterval={INVENTORY_POLL}
          empty={t(level.emptyKey)}
          onRowClick={level.id === "pools" ? (r) => setPool((r as unknown as ResourcePool).resourcePoolId) : undefined} selectedKey={level.id === "pools" ? pool : undefined} />
        {level.id === "pools" && !pool && <p className="small muted">{t("infrastructure.inventory.pickPool")}</p>}
      </Card>
      {level.id === "pools" && pool && <PoolResources pool={pool} />}
    </>
  );
}

/** The resources of one pool, with deprovision and the admin provisioning form. */
function PoolResources({ pool }: { pool: string }) {
  const t = useT();
  const [spec, setSpec] = useState('{"resourceTypeId": "", "description": "GPU node"}');
  const parsed = parseJsonObject(spec, t);
  const [shown, setShown] = useState<string[]>([]);
  const onRows = useCallback((rows: OCloudResource[]) => setShown(rows.map((r) => r.resourceId)), []);
  const util = useUtilisationBatch(shown);
  const of = (id: string) => util.data?.find((u) => u.resourceId === id);
  return (
    <Card section="infrastructure.pool-resources" title={<>{t("infrastructure.pool.title")} <code>{pool}</code></>}>
      <ServerTable<OCloudResource> path={PATHS.poolResources(pool)} rowKey={(r) => r.resourceId} empty={t("infrastructure.pool.empty")} refetchInterval={INVENTORY_POLL} onRows={onRows} columns={[
        { header: t("infrastructure.col.resource"), render: (r) => <Id value={r.resourceId} /> },
        { header: t("infrastructure.col.type"), render: (r) => r.resourceTypeId }, { header: t("infrastructure.col.description"), render: (r) => r.description ?? "—" },
        { header: t("infrastructure.col.parent"), render: (r) => <Id value={r.parentId} /> },
        { header: "CPU", render: (r) => <span className="num" title={of(r.resourceId)?.at ?? t("infrastructure.pool.noMeasurement")}>{formatPercent(of(r.resourceId)?.cpuPercent)}</span> },
        { header: t("infrastructure.col.memory"), render: (r) => <span className="num">{formatPercent(of(r.resourceId)?.memoryPercent)}</span> },
        { header: "", className: "actions", render: (r) => <ActionButton label={t("infrastructure.resource.deprovision")} tone="danger" confirm={t("infrastructure.resource.deprovision.confirm")} action={{ method: "DELETE", path: `/focom/resources/${r.resourceId}`, success: t("infrastructure.resource.deprovisioned") }} /> },
      ]} />
      <Can method="POST" path="/focom/resources/provision">
        <details className="admin-tools">
          <summary>{t("infrastructure.pool.admin")}</summary>
          <Field label={t("infrastructure.pool.spec")} hint={parsed.ok ? t("infrastructure.pool.spec.hint") : <span className="text-bad">{parsed.error}</span>}><textarea rows={2} value={spec} onChange={(e) => setSpec(e.target.value)} spellCheck={false} /></Field>
          <ActionButton label={t("infrastructure.pool.provision")} disabled={!parsed.ok} action={{ method: "POST", path: "/focom/resources/provision", json: parsed.ok ? parsed.value : {}, success: t("infrastructure.pool.provisioned") }} />
        </details>
      </Can>
    </Card>
  );
}
