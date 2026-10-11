/** Configuration · job tiles (`configuration.tiles`): config jobs by state from the BFF summary (`useSummary("configuration")`, true counts,
 * SCALE.md P2). The halted tile (jobs waiting for an operator) and the failed tile filter the job list. */
import { count, sum, useSummary } from "../../../data/summary";
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { ErrorRetry } from "../../../kit/states";
import { useJobStatus } from "../data/queries";

/** The tile row. */
export function ConfigTiles() {
  const t = useT();
  const s = useSummary("configuration");
  const [status, setStatus] = useJobStatus();
  if (s.error && !s.data) return <section data-section="configuration.tiles"><ErrorRetry error={s.error} onRetry={() => void s.refetch()} /></section>;
  const c = (k: string) => count(s.data, `configJobs.${k}`);
  const fmt = (n: number | null) => (n === null ? null : formatCount(n));
  const toggle = (k: string) => setStatus(status === k ? "all" : k);
  return (
    <div className="grid g5" data-section="configuration.tiles">
      <Kpi label={t("configuration.tiles.halted")} value={fmt(c("HALTED"))} foot={t("configuration.tiles.halted.foot")} tone={c("HALTED") ? "hot" : undefined}
        onClick={() => toggle("HALTED")} active={status === "HALTED"} title={t("configuration.tiles.halted.title")} />
      <Kpi label={t("configuration.tiles.running")} value={fmt(sum(s.data, ["configJobs.PENDING", "configJobs.PROCESSING"]))} foot={t("configuration.tiles.running.foot", { n: fmt(c("PENDING")) ?? "—" })}
        onClick={() => toggle("PROCESSING")} active={status === "PROCESSING"} />
      <Kpi label={t("configuration.tiles.completed")} value={fmt(c("COMPLETED"))} onClick={() => toggle("COMPLETED")} active={status === "COMPLETED"} />
      <Kpi label={t("configuration.tiles.failed")} value={fmt(sum(s.data, ["configJobs.PARTIAL_SUCCESS", "configJobs.FAILED"]))}
        foot={t("configuration.tiles.failed.foot", { n: fmt(c("FAILED")) ?? "—" })} tone={c("FAILED") ? "warm" : undefined} onClick={() => toggle("FAILED")} active={status === "FAILED"} />
      <Kpi label={t("configuration.tiles.all")} value={fmt(c("total"))} foot={s.data?.partial.length ? t("configuration.tiles.all.partial") : t("configuration.tiles.all.foot")}
        onClick={() => setStatus("all")} active={status === "all"} />
    </div>
  );
}
