/** The console's navigation: every page in the sidebar, grouped as the redesign draws it (BRIEF §2, handoff `Sidebar.dc.html`), with its icon
 * and the summary count its badge shows. The top bar's breadcrumb and the ⌘K search read the same table, so a page added here appears in all three.
 * A page with `minRole` is hidden from lower roles (the route itself is also guarded in main.tsx). */
import type { Role } from "../auth/rbac";
import type { IconName } from "../kit/icons";

/** One sidebar entry. `badge` names summary counts (gui-bff/app/summary.py, page "nav") whose sum the badge shows, in `tone`. */
/** A sidebar badge: the sum of the summary counts `keys` (hidden when one is unknown, so a partial sum never reads as the total), plus the counts
 * `extra` that are known (a count a BFF of an older build does not serve yet adds nothing, rather than hiding the badge). */
export interface NavBadge { keys: string[]; extra?: string[]; tone: "bad" | "warn"; title: string }
export interface NavItem { to: string; label: string; icon: IconName; minRole?: Role; badge?: NavBadge }

/** One titled group of entries. */
export interface NavGroup { title: string; items: NavItem[] }

/** The groups, in sidebar order. */
export const NAV_GROUPS: NavGroup[] = [
  { title: "Overview", items: [
    { to: "/", label: "Dashboard", icon: "dashboard" },
    { to: "/flows", label: "Lifecycle flows", icon: "flows" },
  ] },
  { title: "Automation", items: [
    { to: "/rapps", label: "rApps", icon: "rapps" },
    { to: "/approvals", label: "Approvals", icon: "approvals", badge: { keys: ["approvals.PENDING"], extra: ["modelGates.waiting", "configJobs.PENDING_APPROVAL"], tone: "warn", title: "approval requests, model gates and change windows waiting" } },
    { to: "/decisions", label: "Decisions", icon: "decisions" },
    { to: "/safeguards", label: "Safeguards", icon: "safeguards" },
    { to: "/aiml", label: "AI/ML", icon: "aiml" },
    { to: "/policy", label: "Intents", icon: "intents" },
  ] },
  { title: "Network", items: [
    { to: "/alarms", label: "Alarms", icon: "alarms", badge: { keys: ["alarms.critical", "alarms.major"], tone: "bad", title: "critical and major RAN alarms" } },
    { to: "/kpis", label: "KPIs & Assurance", icon: "kpis" },
    { to: "/topology", label: "RAN topology", icon: "topology" },
    { to: "/configuration", label: "Configuration", icon: "config", badge: { keys: ["configJobs.HALTED"], tone: "warn", title: "config jobs halted" } },
    { to: "/software", label: "Software", icon: "software", badge: { keys: ["campaigns.HALTED"], tone: "bad", title: "software campaigns halted" } },
    { to: "/infrastructure", label: "Infrastructure", icon: "infra" },
    { to: "/data", label: "Data & Exposure", icon: "data" },
  ] },
  { title: "Account", items: [
    { to: "/preferences", label: "Preferences", icon: "prefs" },
    { to: "/security", label: "Account security", icon: "security" },
    { to: "/exports", label: "Exports", icon: "download", minRole: "operator" },
    { to: "/admin", label: "Admin", icon: "admin", minRole: "admin" },
  ] },
];

/** Every entry, flat, in sidebar order. */
export const NAV: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** The pages that are not in the sidebar but have a breadcrumb: a path prefix, its section and its name. */
const DETAIL_CRUMBS: { prefix: string; section: string; label: string }[] = [
  { prefix: "/rapps/", section: "rApps", label: "rApp detail" },
  { prefix: "/decisions/", section: "Decisions", label: "Decision record" },
  { prefix: "/elements/", section: "RAN topology", label: "Element detail" },
  { prefix: "/flows/", section: "Lifecycle flows", label: "Flow board" },
];

/** [section, page] for the breadcrumb of `pathname`: the sidebar group and entry, or a detail page's parent. */
export function crumbOf(pathname: string): [string, string] {
  for (const d of DETAIL_CRUMBS) if (pathname.startsWith(d.prefix) && pathname.length > d.prefix.length) return [d.section, d.label];
  for (const g of NAV_GROUPS) {
    const hit = g.items.find((i) => (i.to === "/" ? pathname === "/" : pathname === i.to || pathname.startsWith(`${i.to}/`)));
    if (hit) return [g.title, hit.label];
  }
  return ["Overview", "Dashboard"];
}
