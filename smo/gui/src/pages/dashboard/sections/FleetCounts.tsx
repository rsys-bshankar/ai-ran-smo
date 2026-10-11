/** Fleet (`dashboard.fleet`, handoff `Main.dc.html`): rApp packages and instances, NF deployments and intents by state, AI/ML models, training jobs
 * and managed elements, every number a true count from the summary (`byState`, SCALE.md P2). The pre-redesign Fleet box also counted O1 adaptor
 * endpoints by health and RAN analytics reports by type from the first list page; no summary count exists for those, so they are left out.
 * Under a scope a row whose counts the BFF could not narrow (packages, NF deployments, intents, models) says "network-wide". */
import { Link } from "react-router-dom";

import { Card, StateBadge } from "../../../components/ui";
import { byState, count, type Summary } from "../../../data/summary";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { ScopeNote } from "../../../kit/ScopeNote";
import { useDashboardSummary } from "../data/queries";

/** One fleet row: label, total, and a badge per state with a non-zero count. */
function Row({ label, to, total, states, s, keys }: { label: string; to: string; total: number | null; states?: Record<string, number>; s: Summary | undefined; keys: string[] }) {
  return (
    <Link to={to} className="summary">
      <div className="row between"><span className="row gap">{label} <ScopeNote summary={s} keys={keys} /></span><strong className="num">{formatCount(total)}</strong></div>
      {states && Object.keys(states).length > 0 && (
        <div className="summary-badges">{Object.entries(states).map(([k, v]) => <span key={k}><StateBadge state={k} /> {formatCount(v)}</span>)}</div>
      )}
    </Link>
  );
}

/** A row's props for a `prefix.STATE` family of counts. */
const family = (s: Summary | undefined, prefix: string) => ({ total: count(s, `${prefix}.total`), states: byState(s, prefix), s, keys: [prefix] });

/** The card. */
export function FleetCounts() {
  const t = useT();
  const s = useDashboardSummary().data;
  return (
    <Card section="dashboard.fleet" title={t("dashboard.fleet.title")} sub={t("dashboard.fleet.sub")}>
      <div className="stack" style={{ gap: 8 }}>
        <Row label={t("dashboard.fleet.instances")} to="/rapps#instances" {...family(s, "instances")} />
        <Row label={t("dashboard.fleet.packages")} to="/rapps#packages" {...family(s, "packages")} />
        <Row label={t("dashboard.fleet.deployments")} to="/infrastructure#nfo" {...family(s, "deployments")} />
        <Row label={t("dashboard.fleet.intents")} to="/policy#intents" {...family(s, "intents")} />
        <Row label={t("dashboard.fleet.models")} to="/aiml#models" total={count(s, "models.total")} s={s} keys={["models.total"]} />
        <Row label={t("dashboard.fleet.training")} to="/aiml#training" total={count(s, "trainingJobs.total")} s={s} keys={["trainingJobs.total"]} />
        <Row label={t("dashboard.fleet.elements")} to="/topology" total={count(s, "elements.total")} s={s} keys={["elements.total"]} />
      </div>
    </Card>
  );
}
