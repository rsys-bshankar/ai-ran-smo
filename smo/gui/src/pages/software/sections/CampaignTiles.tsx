/** Software · campaign tiles (`software.tiles`): campaigns by state from the BFF summary (`useSummary("software")`, true counts, SCALE.md P2).
 * The running/halted tile filters the campaign list to the halted ones. The mockup's version share, gate failures over 7 days and failed
 * element jobs over 24 h have no route that serves them: they are left out (README, Known limits). */
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { Meter } from "../../../kit/Meter";
import { ErrorRetry } from "../../../kit/states";
import { useUrlParam } from "../../element/data/url";
import { count, sum, useSummary } from "../../../data/summary";

/** The tile row. */
export function CampaignTiles() {
  const t = useT();
  const s = useSummary("software");
  const [status, setStatus] = useUrlParam("status");
  if (s.error && !s.data) return <section data-section="software.tiles"><ErrorRetry error={s.error} onRetry={() => void s.refetch()} /></section>;
  const c = (k: string) => count(s.data, `campaigns.${k}`);
  const running = c("RUNNING"), halted = c("HALTED"), total = c("total");
  const back = sum(s.data, ["campaigns.ROLLING_BACK", "campaigns.ROLLED_BACK"]);
  const parts = [
    { key: "running", value: running ?? 0, tone: "volt" }, { key: "halted", value: halted ?? 0, tone: "bad" },
    { key: "completed", value: c("COMPLETED") ?? 0, tone: "ok" }, { key: "back", value: back ?? 0, tone: "warn" },
  ];
  return (
    <div className="grid g4" data-section="software.tiles">
      <Kpi label={t("software.tiles.running")} value={running === null || halted === null ? null : `${formatCount(running)} · ${formatCount(halted)}`}
        foot={halted ? t("software.tiles.running.waiting", { n: formatCount(halted) }) : t("software.tiles.running.none")} tone={halted ? "hot" : undefined}
        onClick={() => setStatus(status === "HALTED" ? null : "HALTED")} active={status === "HALTED"} title={t("software.tiles.running.title")} />
      <Kpi label={t("software.tiles.completed")} value={c("COMPLETED") === null ? null : formatCount(c("COMPLETED"))} foot={c("ABORTED") !== null ? t("software.tiles.completed.aborted", { n: formatCount(c("ABORTED")) }) : undefined} />
      <Kpi label={t("software.tiles.rolledBack")} value={back === null ? null : formatCount(back)} foot={c("ROLLBACK_FAILED") ? <span className="t-bad">{t("software.tiles.rolledBack.failed", { n: formatCount(c("ROLLBACK_FAILED")) })}</span> : t("software.tiles.rolledBack.none")}
        tone={c("ROLLBACK_FAILED") ? "warm" : undefined} />
      <Kpi label={t("software.tiles.all")} value={total === null ? null : formatCount(total)} foot={s.data?.partial.length ? t("software.tiles.all.partial") : undefined}>
        {total ? <Meter parts={parts} total={total} label={t("software.tiles.all.meter")} /> : null}
      </Kpi>
    </div>
  );
}
