/** Software · element software jobs (`software.jobs`): every per-element software job (flow 19: DOWNLOAD → INSTALL → ACTIVATE), paged by
 * the server (`GET /software-management-jobs`), optionally of one element (`?managed_element_ref=`, the route's only filter). A job opens
 * its flow-19 board, an element its Element detail page. */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Card, Id, StateBadge, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { elementHref } from "../../element/data/types";
import { SWM_JOBS_PATH } from "../data/queries";
import { flow19Href, type SwmJob } from "../data/types";

/** The table's columns, headed in the language of `t`. */
function columnsOf(t: Translator): Column<SwmJob>[] {
  return [
    { header: t("software.jobs.col.job"), render: (j) => <Link to={flow19Href(j.jobId)}><Id value={j.jobId} /></Link> },
    { header: t("software.jobs.col.element"), render: (j) => <Link to={elementHref(j.managedElementRef)}>{j.managedElementRef}</Link> },
    { header: "RU", render: (j) => j.ruInstanceId ?? "—" },
    { header: t("software.jobs.col.phase"), render: (j) => j.phase },
    { header: t("software.jobs.col.status"), render: (j) => <StateBadge state={j.status} /> },
    { header: t("software.jobs.col.campaign"), render: (j) => (j.campaignId ? <span className="small"><Id value={j.campaignId} /> {t("software.jobs.wave", { wave: j.campaignWave ?? "" })}</span> : <span className="muted">—</span>) },
    { header: t("software.jobs.col.undoes"), render: (j) => (j.rollbackOf ? <Id value={j.rollbackOf} /> : <span className="muted">—</span>) },
  ];
}

/** The card. */
export function ElementJobs() {
  const t = useT();
  const columns = useMemo(() => columnsOf(t), [t]);
  const [text, setText] = useState("");
  const [me, setMe] = useState("");
  return (
    <Card section="software.jobs" title={t("software.jobs.title")} sub={t("software.jobs.sub")}
      actions={<form className="row" onSubmit={(e) => { e.preventDefault(); setMe(text.trim()); }}>
        <label className="search"><input aria-label={t("software.jobs.filter.aria")} placeholder={t("software.jobs.filter.placeholder")} value={text} onChange={(e) => setText(e.target.value)} /></label>
        <button type="submit" className="btn small">{t("software.jobs.filter")}</button>
        {me && <button type="button" className="btn small ghost" onClick={() => { setText(""); setMe(""); }}>{t("software.jobs.clear")}</button>}
      </form>}>
      <ServerTable<SwmJob> path={SWM_JOBS_PATH} query={me ? { managed_element_ref: me } : undefined} columns={columns} rowKey={(j) => j.jobId}
        empty={me ? t("software.jobs.empty.element", { element: me }) : t("software.jobs.empty")} />
    </Card>
  );
}
