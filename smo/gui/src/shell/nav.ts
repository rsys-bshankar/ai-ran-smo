/** The console's navigation: every page in the sidebar, grouped as the redesign draws it (BRIEF §2, handoff `Sidebar.dc.html`), with its icon
 * and the summary count its badge shows. The top bar's breadcrumb and the ⌘K search read the same table, so a page added here appears in all three.
 * A page with `minRole` is hidden from lower roles (the route itself is also guarded in main.tsx). The names are the catalog's (`nav.<path>`,
 * `shell.nav.*`, GUI-6.4); the table also carries them in English. */
import type { Role } from "../auth/rbac";
import { navKey, translate, type Params, type TKey } from "../i18n";
import type { IconName } from "../kit/icons";

/** A message in English, for the English names this table also carries (`label`, `title`, `crumbOf`'s default). */
const english = (key: TKey, params?: Params) => translate("en", key, params);

/** A sidebar badge: the sum of the summary counts `keys` (hidden when one is unknown, so a partial sum never reads as the total), plus the counts
 * `extra` that are known (a count a BFF of an older build does not serve yet adds nothing, rather than hiding the badge). `titleKey` is its
 * tooltip's message (`{n}` the number). */
export interface NavBadge { keys: string[]; extra?: string[]; tone: "bad" | "warn"; titleKey: TKey }
/** One sidebar entry. `label` is its English name (shown through `nav.<to>`, `navKey`); `badge` names summary counts (gui-bff/app/summary.py,
 * page "nav") whose sum the badge shows, in `tone`. */
export interface NavItem { to: string; label: string; icon: IconName; minRole?: Role; badge?: NavBadge }

/** One titled group of entries: `title` in English, `titleKey` the message shown. */
export interface NavGroup { title: string; titleKey: TKey; items: NavItem[] }

/** An entry, its English label read from the catalog. */
const item = (to: string, icon: IconName, more: Pick<NavItem, "minRole" | "badge"> = {}): NavItem => ({ to, label: english(navKey(to)), icon, ...more });
/** A group, its English title read from the catalog. */
const group = (titleKey: TKey, items: NavItem[]): NavGroup => ({ title: english(titleKey), titleKey, items });

/** The groups, in sidebar order. */
export const NAV_GROUPS: NavGroup[] = [
  group("shell.nav.group.overview", [
    item("/", "dashboard"),
    item("/flows", "flows"),
  ]),
  group("shell.nav.group.automation", [
    item("/rapps", "rapps"),
    item("/approvals", "approvals", { badge: { keys: ["approvals.PENDING"], extra: ["modelGates.waiting", "configJobs.PENDING_APPROVAL"], tone: "warn", titleKey: "shell.nav.badge.approvals" } }),
    item("/decisions", "decisions"),
    item("/safeguards", "safeguards"),
    item("/aiml", "aiml"),
    item("/policy", "intents"),
  ]),
  group("shell.nav.group.network", [
    item("/alarms", "alarms", { badge: { keys: ["alarms.critical", "alarms.major"], tone: "bad", titleKey: "shell.nav.badge.alarms" } }),
    item("/kpis", "kpis"),
    item("/topology", "topology"),
    item("/configuration", "config", { badge: { keys: ["configJobs.HALTED"], tone: "warn", titleKey: "shell.nav.badge.configJobs" } }),
    item("/software", "software", { badge: { keys: ["campaigns.HALTED"], tone: "bad", titleKey: "shell.nav.badge.campaigns" } }),
    item("/infrastructure", "infra"),
    item("/data", "data"),
  ]),
  group("shell.nav.group.account", [
    item("/preferences", "prefs"),
    item("/security", "security"),
    item("/exports", "download", { minRole: "operator" }),
    item("/admin", "admin", { minRole: "admin" }),
  ]),
];

/** Every entry, flat, in sidebar order. */
export const NAV: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** The pages that are not in the sidebar but have a breadcrumb: a path prefix, its section (a sidebar page's path) and its name. */
const DETAIL_CRUMBS: { prefix: string; section: string; labelKey: TKey }[] = [
  { prefix: "/rapps/", section: "/rapps", labelKey: "shell.crumb.rappDetail" },
  { prefix: "/decisions/", section: "/decisions", labelKey: "shell.crumb.decisionRecord" },
  { prefix: "/elements/", section: "/topology", labelKey: "shell.crumb.elementDetail" },
  { prefix: "/flows/", section: "/flows", labelKey: "shell.crumb.flowBoard" },
];

/** [section, page] for the breadcrumb of `pathname`: the sidebar group and entry, or a detail page's parent, in the language of `t` (English by
 * default). */
export function crumbOf(pathname: string, t: (key: TKey, params?: Params) => string = english): [string, string] {
  for (const d of DETAIL_CRUMBS) if (pathname.startsWith(d.prefix) && pathname.length > d.prefix.length) return [t(navKey(d.section)), t(d.labelKey)];
  for (const g of NAV_GROUPS) {
    const hit = g.items.find((i) => (i.to === "/" ? pathname === "/" : pathname === i.to || pathname.startsWith(`${i.to}/`)));
    if (hit) return [t(g.titleKey), t(navKey(hit.to))];
  }
  return [t("shell.nav.group.overview"), t(navKey("/"))];
}
