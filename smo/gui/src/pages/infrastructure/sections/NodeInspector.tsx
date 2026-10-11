/** Infrastructure → Topology, the inspector (`infrastructure.inspector`): the selected topology node's kind, health and why, its TEIV attributes,
 * what sits under it and what runs on it, and, for a resource, its CPU and memory utilisation from FOCOM (`GET /focom/resources/{id}/utilisation`,
 * GUI-9.8b: the newest CPU_UTILIZATION / MEMORY_UTILIZATION performance records; "—" when none was ingested). GPU utilisation is measured by no
 * module and stays "—"; the other kinds are not measured (utilisation is per resource). A resource can be
 * deprovisioned from here (the same role-gated call as the inventory tab); a workload links to the NF deployments tab, where its actions live. */
import type { ReactNode } from "react";

import { ActionButton, Card, KeyValue } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Empty } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { formatPercent, useResourceUtilisation } from "../data/queries";
import { HEALTH_LABEL, HEALTH_TONE, KIND_LABEL } from "../data/topology";
import type { TopoTree } from "../data/types";

/** Renders an attribute value: text as is, null as "—", anything else as JSON. */
function show(v: unknown) {
  if (v === null || v === undefined || v === "") return <span className="muted">—</span>;
  return typeof v === "object" ? <code className="small">{JSON.stringify(v)}</code> : String(v);
}

/** The utilisation block of a node: a resource's CPU and memory (one FOCOM read while it is selected); any other kind says it is not measured. */
function UtilisationOf({ kind, resourceId }: { kind: string; resourceId: string }) {
  const t = useT();
  const u = useResourceUtilisation(kind === "resource" ? resourceId : null);
  if (kind !== "resource") return <p className="small muted">{t("infrastructure.inspector.notMeasured")}</p>;
  const d = u.data;
  return (
    <>
      <KeyValue items={[["CPU", formatPercent(d?.cpuPercent)], [t("infrastructure.col.memory"), formatPercent(d?.memoryPercent)], ["GPU", "—"], [t("infrastructure.inspector.measured"), d?.at ? formatTime(d.at) : "—"]]} />
      {u.error && <p className="small t-warn">{t("infrastructure.inspector.unavailable", { message: u.error.message })}</p>}
      {d && d.cpuPercent === null && d.memoryPercent === null && <p className="gap-note">{t("infrastructure.inspector.noRecord")}</p>}
      <p className="gap-note">{t("infrastructure.inspector.noGpu")}</p>
    </>
  );
}

/** The inspector. */
export function NodeInspector({ tree, selected }: { tree: TopoTree | null; selected: string | null }) {
  const t = useT();
  const n = selected ? tree?.nodes.get(selected) : undefined;
  if (!tree || !n) {
    return <Card section="infrastructure.inspector" title={t("infrastructure.inspector.title")}><Empty title={t("infrastructure.inspector.select.title")}>{t("infrastructure.inspector.select.body")}</Empty></Card>;
  }
  const kids = n.children.map((id) => tree.nodes.get(id)!);
  const workloads = kids.filter((k) => k.kind === "workload");
  const parent = n.parent ? tree.nodes.get(n.parent) : undefined;
  return (
    <Card section="infrastructure.inspector" title={<span className="mono">{n.name}</span>} sub={t(KIND_LABEL[n.kind])}
      actions={<Badge tone={HEALTH_TONE[n.health]}>{t(HEALTH_LABEL[n.health])}</Badge>}>
      <p className="small muted">{n.healthWhy}</p>
      <KeyValue items={[
        ["ID", <code className="small">{n.ref}</code>],
        [t("infrastructure.inspector.under"), parent ? `${t(KIND_LABEL[parent.kind])} ${parent.name}` : "—"],
        ...Object.entries(n.attributes).filter(([k]) => k !== "name").slice(0, 12).map(([k, v]) => [k, show(v)] as [string, ReactNode]),
      ]} />
      <div className="eyebrow">{t("infrastructure.inspector.utilisation")}</div>
      <UtilisationOf kind={n.kind} resourceId={n.ref} />
      {kids.length > 0 && <>
        <div className="eyebrow">{t("infrastructure.inspector.underIt", { n: kids.length })}</div>
        <KeyValue items={(["dm", "pool", "resource", "workload"] as const).map((k) => [t(KIND_LABEL[k]), kids.filter((c) => c.kind === k).length] as [string, number]).filter(([, c]) => c > 0)} />
      </>}
      {n.kind === "ocloud" && <>
        <div className="eyebrow">{t("infrastructure.inspector.runsOn", { n: workloads.length })}</div>
        {workloads.length ? (
          <div className="row wrap">
            {workloads.slice(0, 10).map((w) => <span key={w.id} className="chip">{w.name} <Badge tone={HEALTH_TONE[w.health]} plain>{w.state}</Badge></span>)}
            {workloads.length > 10 && <a className="chip" href="#nfo">{t("infrastructure.inspector.moreWorkloads", { n: workloads.length - 10 })}</a>}
          </div>
        ) : <span className="small muted">{t("infrastructure.inspector.noWorkload")}</span>}
      </>}
      {(n.kind === "resource" || n.kind === "pool") && <p className="gap-note">{t(n.kind === "resource" ? "infrastructure.inspector.unknownPlacement.resource" : "infrastructure.inspector.unknownPlacement.pool")}</p>}
      {n.kind === "workload" && <a className="btn small" href="#nfo">{t("infrastructure.inspector.openInNfo")}</a>}
      {n.kind === "resource" && (
        <div className="row end">
          <ActionButton label={t("infrastructure.resource.deprovision")} tone="danger" confirm={t("infrastructure.resource.deprovision.confirm")} action={{ method: "DELETE", path: `/focom/resources/${n.ref}`, success: t("infrastructure.resource.deprovisioned") }} />
        </div>
      )}
    </Card>
  );
}
