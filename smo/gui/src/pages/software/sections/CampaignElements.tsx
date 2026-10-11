/** Software · elements of a wave (`software.elements`): the elements of the selected campaign's chosen wave from its report, with each one's
 * software job (phase, status, "timed out" when the job timeout failed it, whether a revert undid it). The job opens its flow-19 board (`/flows/19?subject=<job>`), the element its
 * Element detail page. Elements of a wave that has not started have no job yet. Paged 50 at a time (a wave is at most `waveSize` elements). */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Card, DataTable, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Pager } from "../../../kit/Pager";
import { Empty } from "../../../kit/states";
import { elementHref } from "../../element/data/types";
import { useCampaignReport, useSelectedCampaign, useSelectedWave } from "../data/queries";
import { flow19Href, type WaveJob } from "../data/types";

const PAGE = 50;

/** One row: an element and its job, when the wave has started. */
interface Row { me: string; job: WaveJob | null }

/** The card for the selected wave of the selected campaign. */
export function CampaignElements() {
  const t = useT();
  const [id] = useSelectedCampaign();
  const [wave] = useSelectedWave();
  const report = useCampaignReport(id);
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [id, wave]);
  if (!id) return null;
  const w = report.data?.waves.find((x) => x.wave === wave) ?? report.data?.waves[0];
  const jobs = new Map((w?.jobs ?? []).map((j) => [j.managedElementRef, j]));
  const rows: Row[] = (w?.elements ?? []).map((me) => ({ me, job: jobs.get(me) ?? null }));
  return (
    <Card section="software.elements" title={w ? t("software.elements.title.wave", { wave: w.wave }) : t("software.elements.title")} sub={t("software.elements.sub")}>
      {!report.data && !report.error && <p className="muted small">{t("software.elements.loading")}</p>}
      {report.data && !w && <Empty title={t("software.elements.noWaves")} />}
      {w && (
        <>
          <DataTable<Row> rows={rows.slice(offset, offset + PAGE)} rowKey={(r) => r.me} empty={t("software.elements.empty")} columns={[
            { header: t("software.elements.col.element"), render: (r) => <Link to={elementHref(r.me)}>{r.me}</Link> },
            { header: t("software.elements.col.job"), render: (r) => (r.job ? <Link to={flow19Href(r.job.jobId)} title={t("software.elements.job.open")}><Id value={r.job.jobId} /></Link> : <span className="muted">{t("software.elements.job.notStarted")}</span>) },
            { header: t("software.elements.col.phase"), render: (r) => r.job?.phase ?? "—" },
            { header: t("software.elements.col.status"), render: (r) => (r.job ? <span className="row wrap"><StateBadge state={r.job.status} />{r.job.timedOut && <Badge tone="bad" title={t("software.elements.timedOut.title")}>{t("software.elements.timedOut")}</Badge>}</span> : <span className="muted">—</span>) },
            { header: t("software.elements.col.revert"), render: (r) => (r.job?.revert ? <StateBadge state={r.job.revert} /> : <span className="muted">—</span>) },
          ]} />
          <Pager offset={offset} limit={PAGE} shown={rows.slice(offset, offset + PAGE).length} total={rows.length} onOffset={setOffset} />
        </>
      )}
    </Card>
  );
}
