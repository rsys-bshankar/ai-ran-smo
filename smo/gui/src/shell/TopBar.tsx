/** The top bar (64 px, BRIEF §2, handoff `Topbar.dc.html`): breadcrumb (section / page), the scope picker (GUI-9.3, `ScopePicker.tsx`), the ⌘K jump search, a live-data chip, the
 * notifications button (critical alarms and waiting approvals from the "nav" summary, with a short list of where to go), the moon button to the
 * Preferences page, and help (keyboard shortcuts and where the runbook lives). The live chip says "Live · pushed" while the summary stream
 * (`shell/LiveEvents.tsx`, SCALE.md P7) is open, and "Live · polling" while it is not (connecting, refused, or a browser without EventSource). */
import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { useLive } from "../data/events";
import { count, useSummary } from "../data/summary";
import { navKey, useT } from "../i18n";
import { Icon } from "../kit/icons";
import { formatCount } from "../kit/Kpi";
import { GlobalSearch } from "./GlobalSearch";
import { crumbOf } from "./nav";
import { ScopePicker } from "./ScopePicker";

/** The notifications button and its popover. */
function Notifications() {
  const t = useT();
  const nav = useSummary("nav", { refetchInterval: 30_000 });
  const [open, setOpen] = useState(false);
  const critical = count(nav.data, "alarms.critical");
  const approvals = count(nav.data, "approvals.PENDING");
  const halted = (count(nav.data, "configJobs.HALTED") ?? 0) + (count(nav.data, "campaigns.HALTED") ?? 0);
  const total = (critical ?? 0) + (approvals ?? 0) + halted;
  const rows = [
    { n: critical, text: t("shell.notifications.critical"), to: "/alarms", tone: "d-bad" },
    { n: approvals, text: t("shell.notifications.approvals"), to: "/approvals", tone: "d-warn" },
    { n: count(nav.data, "configJobs.HALTED"), text: t("shell.notifications.configJobs"), to: "/configuration", tone: "d-warn" },
    { n: count(nav.data, "campaigns.HALTED"), text: t("shell.notifications.campaigns"), to: "/software", tone: "d-bad" },
  ].filter((r) => (r.n ?? 0) > 0);
  return (
    <div className="rel">
      <button type="button" className="btn icon" aria-label={t("shell.notifications.aria", { n: total })} aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name="bell" />
        {total > 0 && <span className="icon-count">{formatCount(total, 99)}</span>}
      </button>
      {open && (
        <div className="popover" role="dialog" aria-label={t("shell.notifications.title")}>
          <strong>{t("shell.notifications.heading")}</strong>
          {rows.length === 0 ? <span className="muted small">{t("shell.notifications.none")}</span> : rows.map((r) => (
            <Link key={r.to} to={r.to} className="row" onClick={() => setOpen(false)}><span className={`dot ${r.tone}`} /><span className="grow">{formatCount(r.n)} {r.text}</span>→</Link>
          ))}
          {nav.data && nav.data.partial.length > 0 && <span className="xs muted">{t("shell.notifications.partial", { modules: nav.data.partial.join(", ") })}</span>}
        </div>
      )}
    </div>
  );
}

/** The runbook the help names (a file path, not translated). */
const RUNBOOK = "smo/DEMO_RUNBOOK.md";

/** The help button and its popover. */
function Help() {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <div className="rel">
      <button type="button" className="btn icon" aria-label={t("shell.help.title")} aria-expanded={open} onClick={() => setOpen(!open)}><Icon name="help" /></button>
      {open && (
        <div className="popover" role="dialog" aria-label={t("shell.help.title")}>
          <strong>{t("shell.help.shortcuts")}</strong>
          <span className="small"><kbd>⌘K</kbd> / <kbd>{t("shell.help.ctrlK")}</kbd> {t("shell.help.jump")}</span>
          <span className="small"><kbd>{t("shell.help.esc")}</kbd> {t("shell.help.close")}</span>
          <strong>{t("shell.help.guides")}</strong>
          <span className="small muted">{t("shell.help.walkthrough")} <code>{RUNBOOK}</code>{t("shell.help.boards")} <Link to="/flows" onClick={() => setOpen(false)}>{t(navKey("/flows"))}</Link>.</span>
        </div>
      )}
    </div>
  );
}

/** The top bar. */
export function TopBar() {
  const t = useT();
  const { pathname } = useLocation();
  const [section, page] = crumbOf(pathname, t);
  const { connected } = useLive();
  return (
    <header className="topbar">
      <nav className="crumb" aria-label={t("shell.crumb.aria")}><span>{section}</span><span aria-hidden>/</span><strong aria-current="page">{page}</strong></nav>
      <ScopePicker />
      <GlobalSearch />
      <div className="topbar-tools">
        {connected
          ? <span className="chip" title={t("shell.live.pushed.title")}><span className="dot d-ok" />{t("shell.live.pushed")}</span>
          : <span className="chip" title={t("shell.live.polling.title")}><span className="dot d-warn" />{t("shell.live.polling")}</span>}
        <Notifications />
        <Link className="btn icon" to="/preferences" aria-label={t("shell.prefs.aria")} title={t("shell.prefs.title")}><Icon name="moon" /></Link>
        <Help />
      </div>
    </header>
  );
}
