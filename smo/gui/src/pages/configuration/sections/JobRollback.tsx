/** Configuration · roll back a config job (used by the staged job detail and by the Element page's config history): a "Roll back…" button,
 * gated by `Can` on `POST /config-jobs/{id}/rollback` (operator in gui-bff/app/rbac.py), that opens a dialog. The dialog first asks for a
 * preview (`dryRun: true`: what would be written, and which values changed since the job wrote them), then rolls back; a value changed later
 * is only overwritten when the operator says so (`force`). The same flow as `components/ConfigJobDrawer.tsx`, whose dialog is not exported. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { RollbackPreview } from "../../../api/types";
import { Can, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { describeDifferences } from "../../../lib/domain";
import { jobPath } from "../data/queries";

/** The button and its dialog; `label` names what is undone ("Roll back applied waves", "Undo this job"; default "Roll back…"). */
export function JobRollback({ jobId, label }: { jobId: string; label?: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <Can method="POST" path={jobPath(jobId, "rollback")}>
      <button type="button" className="btn danger" onClick={() => setOpen(true)}>{label ?? t("components.configJob.rollBackOpen")}</button>
      {open && <RollbackDialog jobId={jobId} onClose={() => setOpen(false)} />}
    </Can>
  );
}

/** Preview, then roll back. */
function RollbackDialog({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const t = useT();
  const path = jobPath(jobId, "rollback");
  const preview = useSmoAction();
  const run = useSmoAction();
  const [plan, setPlan] = useState<RollbackPreview | null>(null);
  const differences = plan ? describeDifferences(plan, t) : [];
  return (
    <Modal title={t("components.rollback.title")} onClose={onClose}>
      <p className="muted small">{t("components.rollback.intro")}</p>
      {!plan && <button type="button" className="btn primary" disabled={preview.isPending}
        onClick={() => preview.mutate({ method: "POST", path, json: { dryRun: true } }, { onSuccess: (d) => setPlan(d as RollbackPreview) })}>
        {preview.isPending ? "…" : t("components.rollback.preview")}
      </button>}
      {plan && <>
        <p>{t("components.rollback.changes", { n: plan.changes.length })}</p>
        {differences.length > 0 && (
          <div className="error-box" role="alert">
            <strong>{t("components.rollback.differences", { n: differences.length })}</strong>
            <ul>{differences.slice(0, 20).map((d) => <li key={d}>{d}</li>)}</ul>
            {t("components.rollback.overwrites")}
          </div>
        )}
        <div className="row gap">
          <button type="button" className={`btn ${differences.length ? "danger" : "primary"}`} disabled={run.isPending}
            onClick={() => run.mutate({ method: "POST", path, json: { force: differences.length > 0 }, success: t("components.rollback.started") }, { onSuccess: onClose })}>
            {run.isPending ? "…" : differences.length ? t("components.rollback.anyway") : t("components.rollback.run")}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>{t("components.cancel")}</button>
        </div>
      </>}
    </Modal>
  );
}
