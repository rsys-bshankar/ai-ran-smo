/** The Lifecycle flows page (routes /flows and /flows/:flowId, BRIEF §4 / §4b / §4c, handoff `Flows.dc.html` and `Flow02…Flow19.dc.html`):
 * the flow catalogue on the left, the selected flow's board on the right. The flow comes from the route (default: the first flow; an old
 * `/flows#07` link still opens flow 07), the subject from `?subject=`. Layout only: sections under `sections/`, data under `data/`.
 * Sections and data: README.md. */
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { FLOWS } from "../../lib/flows";
import { FlowBoard } from "./sections/FlowBoard";
import { FlowList, type ListProgress } from "./sections/FlowList";
import "./flows.css";

/** The flow id of the route, else of an old `#NN` hash, else the first flow. */
export function resolveFlowId(param: string | undefined, hash: string): string {
  const ids = FLOWS.map((f) => f.id);
  if (param && ids.includes(param)) return param;
  const h = hash.replace(/^#/, "");
  return ids.includes(h) ? h : ids[0];
}

/** The page. */
export function Flows() {
  const { flowId: param } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const flowId = resolveFlowId(param, typeof window === "undefined" ? "" : window.location.hash);
  const flow = FLOWS.find((f) => f.id === flowId)!;
  const [prog, setProg] = useState<ListProgress | null>(null);
  const subject = params.get("subject");
  const t = useT();
  const onSubject = (id: string) => {
    if (!param) navigate(`/flows/${flowId}?subject=${encodeURIComponent(id)}`, { replace: true });
    else setParams({ subject: id }, { replace: true });
  };
  return (
    <>
      <PageHeader eyebrow={t("flows.eyebrow")} title={t("nav./flows")}
        subtitle={<>{t("flows.subtitle")} <code>{t("flows.subtitle.doc")}</code>{t("flows.subtitle.end")}</>} />
      <div className="flows-layout">
        <SectionBoundary id="flows.list"><FlowList selected={flowId} progress={prog} /></SectionBoundary>
        <SectionBoundary id="flows.board">
          <FlowBoard key={flowId} flow={flow} subjectId={subject} onSubject={onSubject} onProgress={setProg} />
        </SectionBoundary>
      </div>
    </>
  );
}
