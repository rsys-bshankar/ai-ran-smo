/** One flow's board: the subject picker, module chips, sequence lanes, the step timeline with the flow's actions, and the fleet funnel. It
 * calls the selected flow's board hook only (`data/boards.ts`), so only that flow's sources load, for the chosen subject. The page mounts
 * it with `key={flowId}`, so switching flows starts a fresh set of hooks. */
import { useEffect } from "react";

import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { SectionBoundary } from "../../../kit/SectionBoundary";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { progress, type FlowDef } from "../../../lib/flows";
import { BOARDS } from "../data/boards";
import { rememberRecent } from "../data/subjects";
import type { ListProgress } from "./FlowList";
import { FleetFunnel } from "./FleetFunnel";
import { ModuleChips } from "./ModuleChips";
import { SequenceLanes } from "./SequenceLanes";
import { StepTimeline } from "./StepTimeline";
import { SubjectPicker } from "./SubjectPicker";

/** The board of `flow`, following `subjectId` (or the newest subject). */
export function FlowBoard({ flow, subjectId, onSubject, onProgress }: {
  flow: FlowDef; subjectId: string | null; onSubject: (id: string) => void; onProgress: (p: ListProgress | null) => void;
}) {
  const useBoard = BOARDS[flow.id];
  const b = useBoard(subjectId);
  const selectedId = b.selected?.id;
  const p = b.subjects?.length ? progress(b.steps) : null;
  const pKey = p ? `${p.done}/${p.total}/${p.failed}` : "";
  const t = useT();
  useEffect(() => { onProgress(p); }, [pKey]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (selectedId && subjectId === selectedId) rememberRecent(flow.id, selectedId); }, [flow.id, selectedId, subjectId]);
  return (
    <div className="stack">
      <Card section="flows.board" title={t("flows.board.title", { number: flow.number, title: t(flow.title) })} sub={<code className="small">{t("flows.board.doc", { doc: flow.doc })}</code>}>
        <div className="stack">
          <ModuleChips modules={flow.modules} />
          {b.subjectsError && !b.subjects ? <ErrorRetry error={b.subjectsError} onRetry={b.retry} />
            : !b.subjects ? <Skeleton lines={2} />
            : b.subjects.length === 0 ? <Empty title={t("flows.board.noSubject", { noun: t(flow.subject) })}>{b.empty}</Empty>
            : <SubjectPicker flowId={flow.id} noun={t(flow.subject)} subjects={b.subjects} selected={b.selected} onChoose={onSubject} />}
        </div>
      </Card>
      {b.subjects && b.subjects.length > 0 && <>
        <SectionBoundary id="flows.lanes">
          <Card title={t("flows.board.sequence")} sub={t("flows.board.sequence.sub")}><SequenceLanes steps={b.steps} /></Card>
        </SectionBoundary>
        <SectionBoundary id="flows.steps">
          <Card title={t("flows.board.steps")} sub={t("flows.board.steps.sub")}>
            <StepTimeline steps={b.steps} actions={b.actions} alwaysActions={b.alwaysActions} />
            {b.extra}
          </Card>
        </SectionBoundary>
      </>}
      <SectionBoundary id="flows.funnel"><FleetFunnel flowId={flow.id} subject={t(flow.subject)} /></SectionBoundary>
    </div>
  );
}
