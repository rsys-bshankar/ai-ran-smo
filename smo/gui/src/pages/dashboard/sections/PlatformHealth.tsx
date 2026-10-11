/** SMO platform health (`dashboard.platform`, anchor `#health`): one tile per SMO module (up or down, latency or error) and the readiness /
 * version / build table (PR-OBS-8.2). Read from `GET /api/modules/status`, the BFF's check of every module's /health, /ready and /version
 * through R1 Termination. The header carries "Modules healthy n/m", which scripts/gui_smoke.py reads from the page text. */
import { Card, DataTable } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { moduleRows } from "../../../lib/domain";
import { useModulesStatus } from "../data/queries";

/** The card. */
export function PlatformHealth() {
  const t = useT();
  const status = useModulesStatus();
  const modules = status.data?.modules ?? [];
  const healthy = modules.filter((m) => m.healthy).length;
  return (
    <Card section="dashboard.platform" className="s2 anchor" title={t("dashboard.platform.title")}
      sub={status.data ? t("dashboard.platform.sub", { time: new Date(status.data.checkedAt).toLocaleTimeString() }) : t("dashboard.platform.sub.loading")}
      actions={status.data && <Badge tone={healthy === modules.length ? "ok" : "bad"}>{t("dashboard.platform.healthy", { healthy, total: modules.length })}</Badge>}>
      <div id="health" />
      {status.error && !status.data && <ErrorRetry error={status.error} onRetry={() => void status.refetch()} />}
      {!status.data && !status.error && <Skeleton lines={3} />}
      <div className="health-grid">
        {modules.map((m) => (
          <div key={m.module} className={`health-tile ${m.healthy ? "up" : "down"}`} title={m.error ?? `HTTP ${m.statusCode}`}>
            <span className="dot" />
            <div><strong>{m.module}</strong><span className="muted small">{m.healthy ? t("dashboard.platform.up", { ms: m.latencyMs }) : t("dashboard.platform.down", { error: m.error ?? `HTTP ${m.statusCode}` })}</span></div>
          </div>
        ))}
      </div>
      {status.data && (
        <DataTable rows={moduleRows(status.data.modules)} rowKey={(r) => r.module} columns={[
          { header: t("dashboard.platform.module"), render: (r) => <strong>{r.module}</strong> },
          { header: t("dashboard.platform.readiness"), render: (r) => <Badge tone={r.readiness === "READY" ? "ok" : r.readiness === "UNKNOWN" ? "mute" : "bad"}>{r.readiness}</Badge> },
          { header: t("dashboard.platform.version"), render: (r) => r.version },
          { header: t("dashboard.platform.build"), render: (r) => <><code>{r.buildSha}</code>{r.skewed && <Badge tone="warn" title={t("dashboard.platform.build.skewed")}>{t("dashboard.platform.build.differs")}</Badge>}</> },
          { header: t("dashboard.platform.builtAt"), render: (r) => r.builtAt },
        ]} />
      )}
    </Card>
  );
}
