/** Software · campaign detail (`software.detail`): the selected campaign (`GET /software-campaigns/{id}`) and its report (`/report`): what it
 * upgrades and how (selector, wave size, pause, gate, on gate failure, job timeout, rollback order), why it halted (GATE_FAILED, WAVE_PAUSE with a countdown, OPERATOR_HALT),
 * the controls, the totals, the wave strip (a wave opens its elements below) and the event log, newest first. */
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { Timeline } from "../../../kit/Timeline";
import { describeSeconds, formatTime } from "../../../lib/domain";
import { useCampaign, useCampaignReport, useSelectedCampaign, useSelectedWave } from "../data/queries";
import { HALTED_MEANING, WAVE_STATE_LABEL, waveState, type Campaign, type CampaignReport } from "../data/types";
import { CampaignActions } from "./CampaignActions";
import { Countdown } from "./Countdown";

/** The detail card of the selected campaign; a wave button opens that wave's elements below. */
export function CampaignDetail() {
  const t = useT();
  const [id] = useSelectedCampaign();
  const [wave, onWave] = useSelectedWave();
  const campaign = useCampaign(id);
  const report = useCampaignReport(id);
  if (!id) return <Card section="software.detail" title={t("software.detail.title")}><Empty title={t("software.detail.none.title")}>{t("software.detail.none.body")}</Empty></Card>;
  if (campaign.error && !campaign.data) return <Card section="software.detail" title={t("software.detail.title")}><ErrorRetry error={campaign.error} onRetry={() => void campaign.refetch()} /></Card>;
  const c = campaign.data;
  if (!c) return <Card section="software.detail" title={t("software.detail.title")}><Skeleton lines={8} /></Card>;
  return (
    <Card section="software.detail" title={c.name} sub={<span className="row wrap"><Id value={c.campaignId} /><StateBadge state={c.status} />
      <Badge tone="mute" plain>{t("software.detail.onGateFailure", { value: c.onGateFailure })}</Badge>{c.softwareVersion && <span className="mono small">→ {c.softwareVersion}</span>}</span>}>
      <p className="small muted">{describeCampaign(c, t)}</p>
      <Halted c={c} />
      {c.status !== "HALTED" && <CampaignActions campaign={c} />}
      {report.error && !report.data && <ErrorRetry error={report.error} onRetry={() => void report.refetch()} />}
      {report.data && <Totals r={report.data} />}
      {report.data && <>
        <div className="eyebrow">{t("software.detail.waves", { started: Math.min(c.wave, c.waveCount), total: c.waveCount })}</div>
        <div className="sw-waves" role="group" aria-label={t("software.detail.waves.aria")}>
          {report.data.waves.map((w) => {
            const state = waveState(w, c);
            return (
              <button key={w.wave} type="button" className={`sw-wave ${state.replace(" ", "-")}${wave === w.wave ? " on" : ""}`} aria-pressed={wave === w.wave} onClick={() => onWave(w.wave)}>
                <span className="mono xs">W{w.wave}</span><span className="small">{t(WAVE_STATE_LABEL[state])}</span><span className="xs">{t("software.detail.wave.elements", { count: w.elements.length })}</span>
              </button>
            );
          })}
        </div>
      </>}
      <div className="eyebrow">{t("software.detail.events")}</div>
      {c.events.length === 0 ? <p className="muted small">{t("software.detail.events.none")}</p> : (
        <Timeline label={t("software.detail.events.aria")} items={[...c.events].reverse().slice(0, 50).map((e, i) => ({
          key: `${e.at}-${i}`, state: e.event === "HALTED" || e.event.includes("FAIL") ? "fail" : "done",
          title: e.wave ? t("software.detail.event.wave", { event: e.event, wave: e.wave }) : e.event,
          meta: <span className="xs muted">{formatTime(e.at)}{e.by ? t("software.detail.event.by", { user: e.by }) : ""}</span>, detail: e.detail ?? undefined,
        }))} />
      )}
    </Card>
  );
}

/** "selector vendorName=…, region=… · 812 elements · wave size 100 · pause 15 min · gate: ≤ 0 new critical/major alarms · no job timeout ·
 * rollback: last wave first · requested by …", in the language of `t`. */
function describeCampaign(c: Campaign, t: Translator): string {
  const sel = c.selector ? Object.entries(c.selector).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(", ") : null;
  return [sel ? t("software.describe.selector", { selector: sel }) : t("software.describe.named"), t("software.describe.elements", { count: c.elements.length }),
    t("software.describe.waveSize", { size: c.waveSize }),
    c.wavePauseSeconds ? t("software.describe.pause", { pause: describeSeconds(c.wavePauseSeconds) }) : t("software.describe.noPause"),
    t("software.describe.gate", { max: c.gateMaxNewAlarms }),
    c.jobTimeoutSeconds ? t("software.describe.timeout", { timeout: describeSeconds(c.jobTimeoutSeconds) }) : t("software.describe.noTimeout"),
    t(c.rollbackOrder === "reverse" ? "software.describe.rollback.reverse" : "software.describe.rollback.all"),
    t("software.describe.requestedBy", { user: c.requestedBy })].join(" · ");
}

/** The halted callout: reason, its meaning, the detail, the pause countdown and the controls. */
function Halted({ c }: { c: Campaign }) {
  const t = useT();
  if (c.status !== "HALTED") return null;
  const reason = c.haltedReason ?? "HALTED";
  const meaning = HALTED_MEANING[reason];
  return (
    <Callout tone={reason === "WAVE_PAUSE" ? "warn" : "bad"} title={t("software.halted.title", { wave: c.wave, reason })}
      actions={<CampaignActions campaign={c} />}>
      <span>{t(meaning ?? "software.halted.other")}{c.haltedDetail ? ` — ${c.haltedDetail}` : ""}.</span>
      {reason === "WAVE_PAUSE" && c.nextWaveAt && <span> {t("software.halted.pause.starts", { wave: c.wave + 1 })} <Countdown until={c.nextWaveAt} /> {t("software.halted.pause.unless")}</span>}
      {c.wave < c.waveCount && reason !== "WAVE_PAUSE" && <span> {t("software.halted.notStarted", { from: c.wave + 1, to: c.waveCount })}</span>}
    </Callout>
  );
}

/** The five totals of the report. */
function Totals({ r }: { r: CampaignReport }) {
  const t = useT();
  const box = (label: string, value: number, tone?: string) => (
    <div className="inset" key={label}><div className="xs muted">{label}</div><div className={`num${tone ? ` ${tone}` : ""}`}>{value.toLocaleString("en-US")}</div></div>
  );
  return (
    <>
      <div className="sw-insets">
        {box(t("software.totals.elements"), r.summary.elements)}{box(t("software.totals.started"), r.summary.started)}{box(t("software.totals.completed"), r.summary.completed, "t-ok")}
        {box(t("software.totals.failed"), r.summary.failed, r.summary.failed ? "t-bad" : undefined)}{box(t("software.totals.notReached"), r.summary.notReached)}
        {r.summary.reverted > 0 && box(t("software.totals.reverted"), r.summary.reverted, "t-warn")}
      </div>
      {r.attention.length > 0 && (
        <Callout tone="warn" title={t("software.totals.attention", { count: r.attention.length })}>
          {r.attention.slice(0, 5).map((a) => `${a.managedElementRef}: ${a.problem}`).join(" · ")}{r.attention.length > 5 ? t("software.totals.attention.more", { n: r.attention.length - 5 }) : ""}
        </Callout>
      )}
    </>
  );
}
