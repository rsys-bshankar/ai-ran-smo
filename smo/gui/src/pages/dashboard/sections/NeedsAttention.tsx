/** "Needs your attention" (`dashboard.attention`, handoff `Main.dc.html`, SCALE.md P4, GUI-9.8b): four groups (critical alarms, pending approvals,
 * model guard breaches, SA SMOS escalations), each its newest three items, its true total and "+N more", with a link to the full list. All four
 * come from ONE call, the BFF's `GET /api/summary/attention` (fanned out and cached there), and are pushed by the `summary:attention` event while
 * the stream is open. Under a scope the alarm and approval groups are narrowed; the BFF names the others in `unscoped` and they say
 * "network-wide". A group whose module did not answer has no total and says so. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Card, Id, StateBadge } from "../../../components/ui";
import { attentionGroup, type Attention, type AttentionType } from "../../../data/summary";
import { useT, type TKey, type Translator } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { formatCount } from "../../../kit/Kpi";
import { ScopeNote } from "../../../kit/ScopeNote";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { useNeedsAttention } from "../data/queries";

/** A row of a group as the BFF trims it (gui-bff/app/summary.py `ATTENTION` fields); a missing field reads as a dash. */
type Row = Record<string, unknown>;
/** A field of a row as text ("" when absent). */
const s = (r: Row, k: string) => (r[k] === null || r[k] === undefined ? "" : String(r[k]));

/** How each group is titled (a catalog key), where its full list is, which field identifies a row, and how a row reads (in the language of `t`). */
const GROUPS: { type: AttentionType; titleKey: TKey; to: string; key: string; render: (r: Row, t: Translator) => ReactNode }[] = [
  { type: "critical-alarms", titleKey: "dashboard.attention.criticalAlarms", to: "/alarms", key: "alarmId", render: (a, t) => (
    <span><span className="sev sev-cr">{t("dashboard.attention.critical")}</span> <Link to={`/elements/${encodeURIComponent(s(a, "managedElementRef"))}`}><strong>{s(a, "managedElementRef")}</strong></Link>
      <span className="muted"> · {s(a, "specificProblem") || s(a, "probableCause") || s(a, "alarmType") || t("dashboard.attention.alarm")} · {formatTime(s(a, "raisedAt") || null)}</span></span>
  ) },
  { type: "approvals", titleKey: "dashboard.attention.approvals", to: "/approvals", key: "approvalId", render: (a, t) => {
    const n = Number(a.changeCount ?? 0);
    return <span><strong>{s(a, "invokerId")}</strong><span className="muted"> · {t("dashboard.attention.approvals.changes", { count: n })} · {t("dashboard.attention.approvals.lapses", { time: formatTime(s(a, "expiresAt") || null) })}</span></span>;
  } },
  { type: "mlmf-breaches", titleKey: "dashboard.attention.breaches", to: "/aiml#mlmf", key: "reportId", render: (r, t) => (
    <span>{t("dashboard.attention.breaches.subscription")} <Id value={s(r, "subscriptionId")} /><span className="muted"> · {t("dashboard.attention.breaches.underFloor")} · {formatTime(s(r, "reportedAt") || null)}</span></span>
  ) },
  { type: "escalations", titleKey: "dashboard.attention.escalations", to: "/kpis#assurance", key: "actionId", render: (a, t) => (
    <span><StateBadge state={s(a, "outcome")} /> <strong>{s(a, "actionType")}</strong><span className="muted"> {t("dashboard.attention.escalations.onMonitor")} </span><Id value={s(a, "monitorId")} /></span>
  ) },
];

/** One group: its heading with the total, up to three rows, "+N more" when the total is larger than what is shown. */
function Group({ spec, data }: { spec: (typeof GROUPS)[number]; data: Attention }) {
  const t = useT();
  const g = attentionGroup(data, spec.type);
  const rows = g?.items ?? [];
  const total = g?.total ?? null;
  const more = total !== null ? total - rows.length : 0;
  return (
    <div className="col" style={{ gap: 6 }} data-group={spec.type}>
      <div className="row between">
        <span className="row gap"><span className="eyebrow">{t(spec.titleKey)}</span><ScopeNote summary={data} keys={[spec.type]} /></span>
        <Link className="small" to={spec.to}>{formatCount(total)} →</Link>
      </div>
      {total === null ? <p className="small t-warn">{t("dashboard.attention.unavailable")}</p>
        : rows.length === 0 ? <p className="small muted">{t("dashboard.attention.none")}</p>
        : <ul className="list attention">{rows.map((r, i) => <li key={s(r, spec.key) || i} className="small">{spec.render(r, t)}</li>)}</ul>}
      {more > 0 && <Link className="small" to={spec.to}>{t("dashboard.attention.more", { n: formatCount(more) })}</Link>}
    </div>
  );
}

/** The card. Its badge is the sum of the four totals (unknown when any of them is). */
export function NeedsAttention() {
  const t = useT();
  const q = useNeedsAttention();
  const data = q.data;
  const totals = data ? GROUPS.map((g) => attentionGroup(data, g.type)?.total ?? null) : [null];
  const all = totals.some((n) => n === null) ? null : totals.reduce<number>((a, n) => a + (n ?? 0), 0);
  return (
    <Card section="dashboard.attention" title={t("dashboard.attention.title")} actions={<Badge tone={all ? "bad" : "ok"} plain>{formatCount(all)}</Badge>}>
      {!data ? (q.error ? <ErrorRetry error={q.error} onRetry={() => void q.refetch()} /> : <Skeleton lines={6} />) : (
        <div className="stack" style={{ gap: 14 }}>
          {GROUPS.map((g) => <Group key={g.type} spec={g} data={data} />)}
          {data.partial.length > 0 && <p className="xs muted">{t("dashboard.attention.partial", { services: data.partial.join(", ") })}</p>}
        </div>
      )}
    </Card>
  );
}
