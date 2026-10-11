/** Section `aiml.inference` (Inference tab): every inference job (MLEF) as a server-paged table filtered by status on the server, with the
 * role-gated resolution of a RUNNING job. Results are pulled through DME, so this view tracks job state only. */
import { useState } from "react";

import type { InferenceJob } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { INFERENCE_JOBS, INFERENCE_STATUSES } from "../data/queries";
import { useInferenceColumns } from "./InferenceTable";

/** The tab's table. */
export function InferenceJobs() {
  const [status, setStatus] = useState("");
  const columns = useInferenceColumns();
  const t = useT();
  return (
    <Card section="aiml.inference" title={t("aiml.inference.title")} actions={
      <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("aiml.filterStatus")}><option value="">{t("aiml.all")}</option>{INFERENCE_STATUSES.map((s) => <option key={s}>{s}</option>)}</select>}>
      <p className="muted small">{t("aiml.inference.note")}</p>
      <ServerTable<InferenceJob> path={INFERENCE_JOBS} query={{ status: status || undefined }} rowKey={(j) => j.inferenceJobId} empty={t("aiml.inference.empty")} columns={columns} />
    </Card>
  );
}
