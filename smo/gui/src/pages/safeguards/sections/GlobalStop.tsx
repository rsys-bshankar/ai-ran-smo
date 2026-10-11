/** Section `safeguards.stopall`: "Stop all rApp writes" and, for an admin, "Resume all". One server call each (rApp Management
 * `PUT|DELETE /rapp-mgmt/kill-all`, GUI-9.6): the stop stops every live instance the way the per-rApp stop does (an rApp already stopped keeps
 * its first stop and reason) and answers how many it stopped, how many were already stopped and which failed; the resume answers how many it
 * resumed. The confirm dialog shows the live and stopped counts (`GET /rapp-mgmt/kill-all`). The stop is shown to a role allowed the `PUT`
 * (operator), the resume to one allowed the `DELETE` (admin). */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useAuth } from "../../../auth/AuthContext";
import { Field, Modal } from "../../../components/ui";
import { invalidateAfter } from "../../../data/keys";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { formatCount } from "../../../kit/Kpi";
import { KILL_ALL, resumeAll, stopAll, useKillAllCount, type KillAllResult, type ResumeAllResult } from "../data/queries";

/** Where the flow is. */
type Phase =
  | { stage: "idle" } | { stage: "confirm-stop" } | { stage: "confirm-resume" } | { stage: "running"; what: "stop" | "resume" }
  | { stage: "stopped"; result: KillAllResult } | { stage: "resumed"; result: ResumeAllResult } | { stage: "error"; error: string };

/** The buttons, their confirm dialogs and the result. */
export function GlobalStop() {
  const t = useT();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [phase, setPhase] = useState<Phase>({ stage: "idle" });
  const [reason, setReason] = useState("");
  const counting = phase.stage === "confirm-stop" || phase.stage === "confirm-resume";
  const counts = useKillAllCount(counting);
  const mayStop = can("PUT", KILL_ALL);
  const mayResume = can("DELETE", KILL_ALL);
  if (!mayStop && !mayResume) return null;
  const live = counts.data?.instances ?? null;
  const stopped = counts.data?.stopped ?? null;
  const run = async (what: "stop" | "resume") => {
    setPhase({ stage: "running", what });
    try {
      if (what === "stop") setPhase({ stage: "stopped", result: await stopAll(reason) });
      else setPhase({ stage: "resumed", result: await resumeAll() });
    } catch (e) { setPhase({ stage: "error", error: (e as Error).message }); }
    void invalidateAfter(qc, KILL_ALL, ["ran-nf-oam"]);
  };
  const close = () => setPhase({ stage: "idle" });
  const failed = phase.stage === "stopped" || phase.stage === "resumed" ? phase.result.failed : [];
  return (
    <div data-section="safeguards.stopall" className="stack">
      <div className="row gap">
        {mayStop && <button type="button" className="btn danger" disabled={phase.stage === "running"} onClick={() => setPhase({ stage: "confirm-stop" })}>
          {phase.stage === "running" && phase.what === "stop" ? t("safeguards.stopAll.running") : t("safeguards.stopAll.button")}
        </button>}
        {mayResume && <button type="button" className="btn" disabled={phase.stage === "running"} onClick={() => setPhase({ stage: "confirm-resume" })}>
          {phase.stage === "running" && phase.what === "resume" ? t("safeguards.resumeAll.running") : t("safeguards.resumeAll.button")}
        </button>}
      </div>
      {phase.stage === "error" && <div className="error-box" role="alert">{t("safeguards.global.callFailed", { error: phase.error })}</div>}
      {phase.stage === "stopped" && (
        <Callout tone={failed.length ? "bad" : "warn"} title={[t("safeguards.stopAll.done", { count: phase.result.stopped, n: formatCount(phase.result.stopped) }), t("safeguards.stopAll.already", { n: formatCount(phase.result.alreadyStopped) }),
            ...(failed.length ? [t("safeguards.global.failed", { n: failed.length })] : [])].join(" · ")}
          actions={<button type="button" className="btn small" onClick={close}>{t("safeguards.global.dismiss")}</button>}>
          {failed.length > 0 ? <FailedList failed={failed} /> : t("safeguards.stopAll.done.body")}
        </Callout>
      )}
      {phase.stage === "resumed" && (
        <Callout tone={failed.length ? "bad" : "info"} title={[t("safeguards.resumeAll.done", { count: phase.result.resumed, n: formatCount(phase.result.resumed) }),
            ...(failed.length ? [t("safeguards.global.failed", { n: failed.length })] : [])].join(" · ")}
          actions={<button type="button" className="btn small" onClick={close}>{t("safeguards.global.dismiss")}</button>}>
          {failed.length > 0 && <FailedList failed={failed} />}
        </Callout>
      )}
      {phase.stage === "confirm-stop" && (
        <Modal title={t("safeguards.stopAll.confirm.title")} onClose={close}>
          <p>{live === null ? t("safeguards.stopAll.confirm.counting") : <>{t("safeguards.stopAll.confirm.before")}<strong>{formatCount(live - (stopped ?? 0))}</strong>{t("safeguards.stopAll.confirm.after", { count: live - (stopped ?? 0), stopped: formatCount(stopped), live: formatCount(live) })}</>}
            {" "}{t("safeguards.stopAll.confirm.effect")}</p>
          {counts.error && <p className="small t-warn">{t("safeguards.stopAll.confirm.countError", { error: counts.error.message })}</p>}
          <Field label={t("safeguards.reason")} hint={t("safeguards.stopAll.confirm.reason.hint")}><input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} autoFocus /></Field>
          <div className="row gap">
            <button type="button" className="btn danger" onClick={() => void run("stop")}>{t("safeguards.stopAll.confirm.submit")}</button>
            <button type="button" className="btn ghost" onClick={close}>{t("safeguards.cancel")}</button>
          </div>
        </Modal>
      )}
      {phase.stage === "confirm-resume" && (
        <Modal title={t("safeguards.resumeAll.confirm.title")} onClose={close}>
          <p>{stopped === null ? t("safeguards.resumeAll.confirm.counting") : <>{t("safeguards.resumeAll.confirm.before")}<strong>{formatCount(stopped)}</strong>{t("safeguards.resumeAll.confirm.after", { count: stopped })}</>}</p>
          <div className="row gap">
            <button type="button" className="btn primary" disabled={stopped === 0} onClick={() => void run("resume")}>{t("safeguards.resumeAll.button")}</button>
            <button type="button" className="btn ghost" onClick={close}>{t("safeguards.cancel")}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

/** The instances a global call could not stop or resume, with the error of each. */
function FailedList({ failed }: { failed: { instanceId: string; error: string }[] }) {
  return <ul className="list">{failed.map((f) => <li key={f.instanceId}><code>{f.instanceId}</code><span className="grow small">{f.error}</span></li>)}</ul>;
}
