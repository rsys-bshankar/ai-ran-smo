/** Sections `aiml.trainingTop` and `aiml.waiting`: the Models tab's two side cards. "Training now" lists the newest five IN_PROGRESS training
 * jobs with the true count of them and the step each reached, or its epoch progress and ETA when the runtime reports epochs; "Waiting for governance" lists the models whose next
 * step is a governance decision (approve training or validation, submit, approve, certify), from the lifecycle list the stage board already
 * read (no extra call), with their role-gated decision buttons. A click on a model selects it. */
import type { TrainingJob } from "../../../api/types";
import { Card, StateBadge } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { awaitsDecision } from "../../../lib/domain";
import { useLifecycleIndex, useModelIndex, useModelNames, useRunningTraining } from "../data/queries";
import { ModelActions } from "./ModelActions";
import { TrainingProgress } from "./TrainingTable";

const STEP: Record<TrainingJob["currentStep"], TKey> = { DATA_EXTRACTION: "aiml.step.dataExtraction", TRAINING: "aiml.step.training", TRAINED_MODEL: "aiml.step.trainedModel" };

/** "Training now": top five running jobs. */
export function TrainingNow({ onSelect, onSeeAll }: { onSelect: (id: string) => void; onSeeAll: () => void }) {
  const jobs = useRunningTraining(5);
  const name = useModelNames();
  const total = jobs.data?.total;
  const t = useT();
  return (
    <Card section="aiml.trainingTop" title={t("aiml.trainingNow.title")} actions={<button type="button" className="btn ghost small" onClick={onSeeAll}>{total !== undefined ? t("aiml.trainingNow.running", { count: total }) : t("aiml.trainingNow.all")}</button>}>
      {jobs.error && !jobs.data ? <ErrorRetry error={jobs.error} onRetry={() => void jobs.refetch()} />
        : !jobs.data ? <Skeleton lines={2} />
          : jobs.data.items.length === 0 ? <Empty title={t("aiml.trainingNow.empty")} />
            : <ul className="list">
              {jobs.data.items.map((j) => (
                <li key={j.trainingJobId} className="row between">
                  {j.modelId
                    ? <button type="button" className="btn ghost small" onClick={() => onSelect(j.modelId!)}>{name(j.modelId) ?? j.modelId.slice(0, 8)}</button>
                    : <span className="small">{t("aiml.trainingNow.group", { id: j.modelCoordinationGroupId?.slice(0, 8) ?? "" })}</span>}
                  {j.epoch != null && j.totalEpochs ? <TrainingProgress job={j} /> : <span className="xs muted">{j.steps ? t("aiml.trainingNow.step", { step: t(STEP[j.currentStep]) }) : "—"}</span>}
                  <StateBadge state={j.status} />
                </li>
              ))}
            </ul>}
    </Card>
  );
}

/** "Waiting for governance": up to five models, with the count. */
export function WaitingForGovernance({ onSelect }: { onSelect: (id: string) => void }) {
  const lifecycles = useLifecycleIndex();
  const models = useModelIndex();
  const waiting = (lifecycles.data?.items ?? []).filter(awaitsDecision);
  const t = useT();
  return (
    <Card section="aiml.waiting" title={t("aiml.waiting.title")} sub={lifecycles.data ? t("aiml.waiting.count", { count: waiting.length }) : undefined}>
      {lifecycles.error && !lifecycles.data ? <ErrorRetry error={lifecycles.error} onRetry={() => void lifecycles.refetch()} />
        : !lifecycles.data ? <Skeleton lines={2} />
          : waiting.length === 0 ? <Empty title={t("aiml.waiting.empty")} />
            : <ul className="list">
              {waiting.slice(0, 5).map((l) => {
                const m = models.data?.items.find((x) => x.modelId === l.modelId);
                return (
                  <li key={l.modelId} className="col">
                    <div className="row between">
                      <button type="button" className="btn ghost small" onClick={() => onSelect(l.modelId)}>{m ? `${m.modelType} ${m.version}` : l.modelId.slice(0, 8)}</button>
                      <StateBadge state={l.modelLifecycleState} />
                    </div>
                    {m && <ModelActions model={m} lifecycle={l} />}
                  </li>
                );
              })}
            </ul>}
    </Card>
  );
}
