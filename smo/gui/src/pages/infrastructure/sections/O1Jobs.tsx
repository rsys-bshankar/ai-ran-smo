/** Infrastructure → O1 endpoints & jobs, the two job boxes: CM write jobs (`infrastructure.config-jobs`, server-paged with a status filter, a
 * row opens the shared config-job drawer, "New config write" opens `ConfigWrite`) and software management jobs (`infrastructure.swm-jobs`,
 * server-paged, with "Start software update" (an element and the package's remote path, flow 19a) and the phase advance / fail / abort buttons). */
import { useState } from "react";

import type { ConfigJobSummary, SwmJob } from "../../../api/types";
import { ConfigJobDrawer } from "../../../components/ConfigJobDrawer";
import { ActionButton, Can, Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { PATHS, useEndpointChoices } from "../data/queries";
import { ConfigWrite } from "./ConfigWrite";

const JOB_STATES = ["PENDING", "PROCESSING", "HALTED", "COMPLETED", "PARTIAL_SUCCESS", "FAILED"] as const;

/** The CM write jobs box. */
export function ConfigJobs() {
  const t = useT();
  const [writing, setWriting] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  return (
    <Card section="infrastructure.config-jobs" title={t("infrastructure.configJobs.title")} sub={t("infrastructure.configJobs.sub")} actions={<>
      <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("infrastructure.col.status")}><option value="">{t("infrastructure.configJobs.anyStatus")}</option>{JOB_STATES.map((s) => <option key={s}>{s}</option>)}</select>
      <Can method="POST" path={PATHS.configJobs}><button type="button" className="btn primary" onClick={() => setWriting(true)}>{t("infrastructure.configJobs.new")}</button></Can>
    </>}>
      <ServerTable<ConfigJobSummary> path={PATHS.configJobs} query={{ status: status || undefined }} rowKey={(j) => j.jobId} empty={t("infrastructure.configJobs.empty")} onRowClick={(j) => setJob(j.jobId)} columns={[
        { header: t("infrastructure.col.job"), render: (j) => <Id value={j.jobId} /> }, { header: t("infrastructure.col.requestedBy"), render: (j) => j.requestedBy },
        { header: t("infrastructure.col.scope"), render: (j) => j.scope }, { header: t("infrastructure.col.status"), render: (j) => <StateBadge state={j.status} /> },
      ]} />
      {writing && <ConfigWrite onClose={() => setWriting(false)} />}
      {job && <ConfigJobDrawer id={job} onClose={() => setJob(null)} />}
    </Card>
  );
}

/** The software management jobs box. */
export function SoftwareJobs() {
  const t = useT();
  const [starting, setStarting] = useState(false);
  return (
    <Card section="infrastructure.swm-jobs" title={t("infrastructure.swm.title")} actions={<Can method="POST" path={PATHS.swmJobs}>
      {starting ? <StartSoftwareUpdate onDone={() => setStarting(false)} /> : <button type="button" className="btn" onClick={() => setStarting(true)}>{t("infrastructure.swm.open")}</button>}
    </Can>}>
      <ServerTable<SwmJob> path={PATHS.swmJobs} rowKey={(j) => j.jobId} empty={t("infrastructure.swm.empty")} columns={[
        { header: t("infrastructure.col.job"), render: (j) => <Id value={j.jobId} /> }, { header: t("infrastructure.col.managedElement"), render: (j) => j.managedElementRef },
        { header: t("infrastructure.col.phase"), render: (j) => <code>{j.phase}</code> }, { header: t("infrastructure.col.status"), render: (j) => <StateBadge state={j.status} /> },
        { header: "", className: "actions", render: (j) => !["COMPLETED", "FAILED"].includes(j.status) && (
          <div className="row gap end">
            <ActionButton label={t("infrastructure.swm.phaseOk", { phase: j.phase })} action={{ method: "POST", path: `${PATHS.swmJobs}/${j.jobId}/advance`, query: { succeeded: true }, success: t("infrastructure.swm.advanced") }} />
            <ActionButton label={t("infrastructure.swm.failed")} action={{ method: "POST", path: `${PATHS.swmJobs}/${j.jobId}/advance`, query: { succeeded: false }, success: t("infrastructure.swm.jobFailed") }} />
            <ActionButton label={t("infrastructure.swm.abort")} tone="danger" action={{ method: "POST", path: `${PATHS.swmJobs}/${j.jobId}/abort`, success: t("infrastructure.swm.aborted") }} />
          </div>
        ) },
      ]} />
    </Card>
  );
}

/** The element picker, the package's remote path and version, and the start button; its element list (one bounded page) is read only once the
 * operator opens it. */
function StartSoftwareUpdate({ onDone }: { onDone: () => void }) {
  const t = useT();
  const [me, setMe] = useState("");
  const [path, setPath] = useState("");
  const [version, setVersion] = useState("");
  const choices = useEndpointChoices(true);
  return (
    <>
      <select value={me} onChange={(e) => setMe(e.target.value)} aria-label={t("infrastructure.col.managedElement")}><option value="">{t("infrastructure.swm.pickElement")}</option>{choices.data?.items.map((e) => <option key={e.endpointId}>{e.managedElementRef}</option>)}</select>
      <input value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("infrastructure.swm.remotePath")} aria-label={t("infrastructure.swm.remotePath")} />
      <input value={version} onChange={(e) => setVersion(e.target.value)} placeholder={t("infrastructure.swm.version")} aria-label={t("infrastructure.swm.version")} />
      <ActionButton label={t("infrastructure.swm.start")} disabled={!me || !path.trim()} onDone={onDone} action={{ method: "POST", path: PATHS.swmJobs,
        query: { managed_element_ref: me, remote_file_path: path.trim(), ...(version.trim() ? { software_version: version.trim() } : {}) }, success: t("infrastructure.swm.started") }} />
      <button type="button" className="btn ghost" onClick={onDone}>{t("components.cancel")}</button>
    </>
  );
}
