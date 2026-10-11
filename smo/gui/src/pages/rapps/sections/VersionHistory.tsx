/** The version history of one rApp instance (OI-1-sa-rollback): committed upgrades and rollbacks, newest first, shown ten at a time with
 * "show more", and the Roll back action. Rollback is an upgrade back to the newest version not already rolled back; it is resolved like any
 * upgrade once the replacement bootstraps. Used by the instance drawer and by the rApp detail page's lifecycle history. */
import { useState } from "react";

import { ActionButton, DataTable, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { instanceBase, useVersions } from "../data/queries";

/** Rows shown before "show more". */
const STEP = 10;

/** The history table and the Roll back button (RUNNING instances with a rollback target only). */
export function VersionHistory({ id, state, title }: { id: string; state: string; title?: string }) {
  const t = useT();
  const base = instanceBase(id);
  const history = useVersions(id);
  const [shown, setShown] = useState(STEP);
  const target = history.data?.rollbackTarget;
  const versions = history.data?.versions;
  return (
    <>
      <div className="row between">
        <h3>{title ?? t("rapps.versions.title")}</h3>
        {state === "RUNNING" && target && <ActionButton label={t("rapps.versions.rollback")} tone="danger"
          confirm={t("rapps.versions.rollback.confirm", { package: target.previousPackageId.slice(0, 8) })}
          action={{ method: "POST", path: `${base}/rollback`, success: t("rapps.versions.rollback.done") }} />}
      </div>
      <DataTable rows={versions?.slice(0, shown)} loading={history.isLoading} error={history.error} rowKey={(v) => v.versionId}
        empty={t("rapps.versions.empty")} columns={[
          { header: t("rapps.versions.col.kind"), render: (v) => <StateBadge state={v.kind} /> },
          { header: t("rapps.versions.col.from"), render: (v) => <code>{v.previousPackageId.slice(0, 8)}</code> },
          { header: t("rapps.versions.col.to"), render: (v) => <code>{v.packageId.slice(0, 8)}</code> },
          { header: t("rapps.versions.col.rolledBack"), render: (v) => (v.kind === "UPGRADE" ? (v.rolledBackByVersionId ? t("rapps.versions.yes") : t("rapps.versions.no")) : "—") },
          { header: t("rapps.versions.col.committed"), render: (v) => formatTime(v.committedAt) },
        ]} />
      {versions && versions.length > shown && (
        <div className="row between pager">
          <span className="muted small">{t("rapps.versions.shown", { shown, total: versions.length })}</span>
          <button type="button" className="btn small" onClick={() => setShown(shown + STEP)}>{t("rapps.versions.more")}</button>
        </div>
      )}
    </>
  );
}
