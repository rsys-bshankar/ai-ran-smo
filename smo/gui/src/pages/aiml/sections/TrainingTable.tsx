/** The training-job columns and dialogs shared by the Training tab (`TrainingJobs`, a server table) and the model's runtime box (one model's
 * jobs): job, target, producer, status, the step the runtime reported, its NFO runtime, the metrics (view, or write back as the trainer), and the
 * role-gated Suspend / Resume (`POST /aimgf/training-jobs/{id}/suspend|resume`, feature 7) and Cancel. The progress column draws the epoch the
 * runtime reported out of its total, with AIMgF's estimate of the time left (`epoch`, `totalEpochs`, `etaSeconds`, GUI-9.8). */
import { useState, type ReactNode } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { TrainingJob } from "../../../api/types";
import { ActionButton, Can, DataTable, Id, Json, Modal, StateBadge, type Column } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { UsageMeter } from "../../../kit/Meter";
import { formatDuration, parseJsonObject } from "../../../lib/domain";
import { TRAINING_JOBS, useModelNames } from "../data/queries";

// OI-5-aiml-trainingjob-steps: the steps a run passes through, reported by its runtime
const STEP_LABEL: Record<TrainingJob["currentStep"], TKey> = { DATA_EXTRACTION: "aiml.step.label.dataExtraction", TRAINING: "aiml.step.label.training", TRAINED_MODEL: "aiml.step.label.trainedModel" };

/** The columns plus the metric dialogs they open (render `dialogs` next to the table). */
export function useTrainingColumns(): { columns: Column<TrainingJob>[]; dialogs: ReactNode } {
  const modelName = useModelNames();
  const t = useT();
  const [metricsFor, setMetricsFor] = useState<TrainingJob | null>(null);
  const [writeFor, setWriteFor] = useState<TrainingJob | null>(null);
  const columns: Column<TrainingJob>[] = [
    { header: t("aiml.col.job"), render: (j) => <Id value={j.trainingJobId} /> },
    { header: t("aiml.col.target"), render: (j) => j.modelId ? (modelName(j.modelId) ?? <Id value={j.modelId} />) : <>{t("aiml.training.group")} <Id value={j.modelCoordinationGroupId} /></> },
    { header: t("aiml.col.producer"), render: (j) => j.producerId },
    { header: t("aiml.col.status"), render: (j) => <StateBadge state={j.status} /> },
    { header: t("aiml.col.step"), render: (j) => j.steps ? <span className="small">{t(STEP_LABEL[j.currentStep])} <span className="muted">({j.steps[j.currentStep].toLowerCase().replace("_", " ")})</span></span> : "—" },
    { header: t("aiml.col.progress"), render: (j) => <TrainingProgress job={j} /> },
    { header: t("aiml.col.runtime"), render: (j) => <Id value={j.nfDeploymentId} /> },
    { header: t("aiml.col.metrics"), render: (j) => <div className="row gap">
      {j.modelMetrics && <button type="button" className="btn small" onClick={() => setMetricsFor(j)}>{t("aiml.training.view")}</button>}
      <Can method="POST" path={`${TRAINING_JOBS}/${j.trainingJobId}/model-metrics`}><button type="button" className="btn small" onClick={() => setWriteFor(j)}>{j.modelMetrics ? t("aiml.training.update") : t("aiml.training.writeBack")}</button></Can>
      {!j.modelMetrics && <span className="muted">—</span>}
    </div> },
    { header: "", className: "actions", render: (j) => <JobControls job={j} /> },
  ];
  const dialogs = <>
    {metricsFor && <Modal title={t("aiml.training.metrics")} onClose={() => setMetricsFor(null)}><Json value={metricsFor.modelMetrics} /></Modal>}
    {writeFor && <WriteMetrics job={writeFor} onClose={() => setWriteFor(null)} />}
  </>;
  return { columns, dialogs };
}

/** "epoch 7 of 20" with a bar and "about 4 min 12 s left" when AIMgF estimated it; "—" when the runtime reported no epoch. */
export function TrainingProgress({ job }: { job: TrainingJob }) {
  const t = useT();
  if (job.epoch == null || !job.totalEpochs) return <span className="muted">—</span>;
  return (
    <span className="col" style={{ gap: 2, minWidth: 110 }}>
      <UsageMeter used={job.epoch} limit={job.totalEpochs} label={t("aiml.training.epochOf", { epoch: job.epoch, total: job.totalEpochs })} />
      <span className="xs muted">{t("aiml.training.epoch", { epoch: job.epoch, total: job.totalEpochs })}{job.etaSeconds != null ? t("aiml.training.eta", { time: formatDuration(job.etaSeconds) }) : ""}</span>
    </span>
  );
}

/** Suspend (IN_PROGRESS), Resume (SUSPENDED) and Cancel (either), each role-gated by the BFF table. */
function JobControls({ job }: { job: TrainingJob }) {
  const base = `${TRAINING_JOBS}/${job.trainingJobId}`;
  const t = useT();
  return (
    <div className="row gap end">
      {job.status === "IN_PROGRESS" && <ActionButton label={t("aiml.training.suspend")} action={{ method: "POST", path: `${base}/suspend`, success: t("aiml.training.suspended") }} />}
      {job.status === "SUSPENDED" && <ActionButton label={t("aiml.training.resume")} tone="primary" action={{ method: "POST", path: `${base}/resume`, success: t("aiml.training.resumed") }} />}
      {["IN_PROGRESS", "SUSPENDED"].includes(job.status) && (
        <ActionButton label={t("components.cancel")} confirm={t("aiml.training.cancelConfirm")} action={{ method: "DELETE", path: base, success: t("aiml.training.cancelled") }} />
      )}
    </div>
  );
}

/** A client table of training jobs the caller already has (one model's). */
export function TrainingTable({ rows, loading, error }: { rows?: TrainingJob[]; loading?: boolean; error?: unknown }) {
  const { columns, dialogs } = useTrainingColumns();
  const t = useT();
  return <><DataTable rows={rows} loading={loading} error={error} rowKey={(j) => j.trainingJobId} empty={t("aiml.training.empty")} columns={columns} />{dialogs}</>;
}

/** Write back what the trainer (MLTF) reports for a job; replaces the stored metrics wholesale. */
function WriteMetrics({ job, onClose }: { job: TrainingJob; onClose: () => void }) {
  const [text, setText] = useState(JSON.stringify(job.modelMetrics ?? { accuracy: 0.93, loss: 0.12 }, null, 2));
  const t = useT();
  const parsed = parseJsonObject(text, t);
  const action = useSmoAction();
  return (
    <Modal title={t("aiml.training.write.title")} onClose={onClose}>
      <p className="muted small">{t("aiml.training.write.intro")} <Id value={job.trainingJobId} />{t("aiml.training.write.intro.end")}</p>
      <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-label={t("aiml.training.write.aria")} />
      {!parsed.ok && <p className="text-bad small">{parsed.error}</p>}
      <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button>
        <button type="button" className="btn primary" disabled={!parsed.ok || action.isPending} onClick={() => parsed.ok && action.mutate(
          { method: "POST", path: `${TRAINING_JOBS}/${job.trainingJobId}/model-metrics`, json: parsed.value, success: t("aiml.training.recorded") }, { onSuccess: onClose })}>{t("aiml.save")}</button>
      </div>
    </Modal>
  );
}
