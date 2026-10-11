/** Section `aiml.training` (Training tab): every training job as a server-paged table filtered by status on the server, with the step its
 * runtime reported, its epoch progress and estimated time left (when the runtime reports epochs), its metrics, and Suspend / Resume / Cancel
 * (role-gated). */
import { useState } from "react";

import type { TrainingJob } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { TRAINING_JOBS, TRAINING_STATUSES } from "../data/queries";
import { useTrainingColumns } from "./TrainingTable";

/** The tab's table. */
export function TrainingJobs() {
  const [status, setStatus] = useState("");
  const { columns, dialogs } = useTrainingColumns();
  const t = useT();
  return (
    <Card section="aiml.training" title={t("aiml.tabs.training")} actions={
      <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("aiml.filterStatus")}><option value="">{t("aiml.all")}</option>{TRAINING_STATUSES.map((s) => <option key={s}>{s}</option>)}</select>}>
      <p className="muted small">{t("aiml.training.note")} <em>{t("aiml.training.note.event")}</em>{t("aiml.training.note.end")}</p>
      <ServerTable<TrainingJob> path={TRAINING_JOBS} query={{ status: status || undefined }} rowKey={(j) => j.trainingJobId} empty={t("aiml.training.empty")} columns={columns} />
      <p className="small muted">{t("aiml.training.progressNote")}</p>
      {dialogs}
    </Card>
  );
}
