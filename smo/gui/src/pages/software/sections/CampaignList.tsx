/** Software · campaign list (`software.list`): every software campaign, paged by the server (`GET /software-campaigns?status=`, SCALE.md P1),
 * filtered by state (kept in `?status=`, also set by the running/halted tile). A row opens the campaign's detail (`?campaign=`). */
import { useMemo } from "react";

import { Card, StateBadge, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Meter } from "../../../kit/Meter";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { useUrlParam } from "../../element/data/url";
import { CAMPAIGNS_PATH, useSelectedCampaign } from "../data/queries";
import { CAMPAIGN_STATES, type CampaignRow } from "../data/types";

/** The list's columns, headed in the language of `t`. */
function columnsOf(t: Translator): Column<CampaignRow>[] {
  return [
    { header: t("software.list.col.campaign"), render: (c) => <strong>{c.name}</strong> },
    { header: t("software.list.col.version"), render: (c) => <span className="mono small">{c.softwareVersion ?? "—"}</span> },
    { header: t("software.list.col.status"), render: (c) => <span className="row wrap"><StateBadge state={c.status} />{c.haltedReason && <span className="small muted">{c.haltedReason}</span>}</span> },
    { header: t("software.list.col.waves"), render: (c) => (
      <span className="col" style={{ gap: 4 }}>
        <span className="small">{t("software.list.waves", { done: Math.min(c.wave, c.waveCount), total: c.waveCount })}</span>
        <Meter parts={[{ key: "waves run", value: Math.min(c.wave, c.waveCount), tone: c.status === "HALTED" ? "warn" : "ok" }]} total={c.waveCount} />
      </span>
    ) },
    { header: t("software.list.col.created"), render: (c) => <span className="small muted">{formatTime(c.createdAt)}</span> },
  ];
}

/** The list card. */
export function CampaignList() {
  const t = useT();
  const columns = useMemo(() => columnsOf(t), [t]);
  const [status, setStatus] = useUrlParam("status");
  const [selected, select] = useSelectedCampaign();
  return (
    <Card section="software.list" title={t("software.list.title")} sub={t("software.list.sub")}
      actions={<label className="row small">{t("software.list.state")}
        <select aria-label={t("software.list.state.aria")} value={status ?? ""} onChange={(e) => setStatus(e.target.value || null)}>
          <option value="">{t("software.list.state.all")}</option>{CAMPAIGN_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>}>
      <ServerTable<CampaignRow> path={CAMPAIGNS_PATH} query={status ? { status } : undefined} columns={columns} rowKey={(c) => c.campaignId}
        onRowClick={(c) => select(c.campaignId)} selectedKey={selected} empty={status ? t("software.list.empty.state", { status }) : t("software.list.empty")} />
    </Card>
  );
}
