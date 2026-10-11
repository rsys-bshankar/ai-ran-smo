/** The Packages tab's table (SCALE.md P1): application packages paged by Onboarding (`GET /onboarding/packages?state=`), with the package
 * pipeline counts from the summary on top (each one filters the table), Deploy on AVAILABLE packages and the lifecycle calls a state allows.
 * A row opens the package drawer. Section id `rapps.packages`. */
import { useCallback, useState } from "react";

import type { Package } from "../../../api/types";
import { ActionButton, Can, Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { ServerTable } from "../../../kit/ServerTable";
import { count } from "../../../data/summary";
import { packageActions } from "../../../lib/domain";
import { INSTANCES_PATH, PACKAGE_STATES, PACKAGES_PATH, packageBase, useRappsSummary } from "../data/queries";
import { CreateInstance } from "./CreateInstance";
import { PackageDrawer } from "./PackageDrawer";

/** The pipeline stages the summary counts (gui-bff/app/summary.py PACKAGES). */
const PIPELINE = ["ONBOARDING", "AVAILABLE", "PRIMED", "DEPRECATED", "FAILED"] as const;

/** The table with its pipeline chips and state filter. */
export function PackagesTable() {
  const t = useT();
  const [state, setState] = useState("");
  const [deployFrom, setDeployFrom] = useState<Package | null>(null);
  const [detail, setDetail] = useState<Package | null>(null);
  const summary = useRappsSummary();
  // keep the open drawer on the row as the table refreshes it (its state changes after an action)
  const onRows = useCallback((rows: Package[]) => setDetail((d) => (d ? rows.find((r) => r.packageId === d.packageId) ?? d : d)), []);
  return (
    <Card section="rapps.packages" title={t("rapps.packages.title")} sub={t("rapps.packages.sub")}
      actions={
        <select value={state} onChange={(e) => setState(e.target.value)} aria-label={t("rapps.packages.state.aria")}>
          <option value="">{t("rapps.packages.state.all")}</option>
          {PACKAGE_STATES.map((s) => <option key={s}>{s}</option>)}
        </select>}>
      <div className="row gap wrap" role="group" aria-label={t("rapps.packages.pipeline.aria")}>
        {PIPELINE.map((s) => {
          const n = count(summary.data, `packages.${s}`);
          return (
            <button key={s} type="button" className={`chip${state === s ? " on" : ""}`} aria-pressed={state === s} onClick={() => setState(state === s ? "" : s)}>
              {s} <b className="num">{formatCount(n)}</b>
            </button>
          );
        })}
        <span className="muted small">{t("rapps.packages.inAll", { n: formatCount(count(summary.data, "packages.total")) })}</span>
      </div>
      <ServerTable<Package> path={PACKAGES_PATH} query={{ state: state || undefined }} rowKey={(p) => p.packageId}
        empty={state ? t("rapps.packages.emptyState", { state }) : t("rapps.packages.empty")} onRowClick={setDetail} selectedKey={detail?.packageId ?? null} onRows={onRows}
        columns={[
          { header: t("rapps.packages.col.package"), render: (p) => <><strong>{p.name}</strong> <span className="muted">{p.version}</span><div className="muted small">{p.vendor ?? ""} {p.applicationType}</div></> },
          { header: t("rapps.packages.col.id"), render: (p) => <Id value={p.packageId} /> },
          { header: t("rapps.packages.col.state"), render: (p) => <StateBadge state={p.state} /> },
          { header: t("rapps.packages.col.signature"), render: (p) => (p.signatureVerified ? t("rapps.packages.signature.verified") : <span className="muted">{t("rapps.packages.signature.unverified")}</span>) },
          { header: t("rapps.packages.col.nfDescriptor"), render: (p) => <Id value={p.nfDeploymentDescriptorId} /> },
          {
            header: "", className: "actions", render: (p) => (
              <div className="row gap end">
                {p.state === "AVAILABLE" && <Can method="POST" path={INSTANCES_PATH}><button type="button" className="btn primary" onClick={(e) => { e.stopPropagation(); setDeployFrom(p); }}>{t("rapps.packages.deploy")}</button></Can>}
                {packageActions(p.state, t).map((a) => (
                  <ActionButton key={a.action} label={a.label} tone={a.action === "delete" ? "danger" : "default"}
                    confirm={a.action === "delete" ? t("rapps.packages.delete.confirm", { name: p.name, version: p.version }) : undefined}
                    action={a.action === "delete"
                      ? { method: "DELETE", path: packageBase(p.packageId), success: t("rapps.packages.delete.done") }
                      : { method: "POST", path: `${packageBase(p.packageId)}/${a.action}`, success: t("rapps.packages.action.done", { action: a.label }) }} />
                ))}
              </div>
            ),
          },
        ]} />
      {deployFrom && <CreateInstance pkg={deployFrom} onClose={() => setDeployFrom(null)} />}
      {detail && <PackageDrawer pkg={detail} onClose={() => setDetail(null)} />}
    </Card>
  );
}
