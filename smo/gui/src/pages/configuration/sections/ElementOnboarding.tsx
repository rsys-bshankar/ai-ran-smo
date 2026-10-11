/** Configuration · element onboarding (`configuration.onboarding`, MGT-14.6): new elements on their way from discovered to configured, paged by the
 * server (`GET /element-onboarding?status=&software_check=`): which template was selected, the software check against its baseline, why one
 * failed, and the config job that applied it. Row buttons follow RAN NF OAM's state machine (`onboardingActions` in lib/lifecycle.ts): **Apply**
 * once a template is selected and **Apply again** after a success or a failure (`POST /{me}/apply`, with the software version the element runs),
 * **Select…** wherever the template is not being written (`POST /{me}/select`, best match or a named template); "Select a template for an
 * element…" covers an element that has no row yet. They are operator actions, role-gated with `Can` (the BFF sets `requestedBy`). A row click
 * opens the detail drawer. The dialogs are in OnboardingDialogs.tsx. */
import { useState } from "react";
import { Link } from "react-router-dom";

import type { ElementOnboarding as Onboarding } from "../../../api/types";
import { Can, Card, Id, StateBadge, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { ServerTable } from "../../../kit/ServerTable";
import { MiniSteps, type StepState } from "../../../kit/Timeline";
import { ONBOARDING_MEANING, onboardingActions } from "../../../lib/lifecycle";
import { elementHref } from "../../element/data/types";
import { ONBOARDING_PATH, onboardingPath } from "../data/queries";
import { ApplyModal, OnboardingDrawer, SelectModal } from "./OnboardingDialogs";

const STATES = ["DISCOVERED", "NO_TEMPLATE", "TEMPLATE_SELECTED", "APPLYING", "ONBOARDED", "FAILED"];

/** discovered → selected → applying → onboarded, as four steps. */
function steps(status: string): StepState[] {
  const order = ["DISCOVERED", "TEMPLATE_SELECTED", "APPLYING", "ONBOARDED"];
  if (status === "FAILED") return ["done", "done", "fail", "todo"];
  if (status === "NO_TEMPLATE") return ["done", "warn", "todo", "todo"];
  const at = order.indexOf(status);
  return order.map((_, i) => (i < at || status === "ONBOARDED" ? "done" : i === at ? "now" : "todo"));
}

/** The tone of a software check. */
const CHECK_TONE: Record<string, "ok" | "bad" | "mute"> = { MATCH: "ok", MISMATCH: "bad", NOT_CHECKED: "mute" };

/** What a row's buttons open: the apply dialog, the select dialog, or nothing. */
type Opened = { kind: "apply"; row: Onboarding } | { kind: "select"; element: string | null } | null;

/** The table's columns, in the language of `t`; `open` opens a row's dialog (the clicks do not also open the row's drawer). */
function columns(open: (o: Opened) => void, t: Translator): Column<Onboarding>[] {
  return [
    { header: t("configuration.onboarding.col.element"), render: (o) => <Link to={elementHref(o.managedElementRef)} onClick={(e) => e.stopPropagation()}>{o.managedElementRef}</Link> },
    { header: t("configuration.onboarding.col.template"), render: (o) => (o.templateName ? <span className="mono small">{o.templateName}</span> : <span className="muted">—</span>) },
    { header: t("configuration.onboarding.col.softwareCheck"), render: (o) => (
      <span className="row wrap"><Badge tone={CHECK_TONE[o.softwareCheck ?? ""] ?? "mute"}>{o.softwareCheck ?? "NOT_CHECKED"}</Badge>
        <span className="small mono">{o.softwareVersion ?? <span className="muted">{t("configuration.onboarding.notReported")}</span>}{o.softwareBaseline ? ` / ${t("configuration.onboarding.baseline", { version: o.softwareBaseline })}` : ""}</span></span>
    ) },
    { header: t("configuration.onboarding.col.progress"), render: (o) => <MiniSteps states={steps(o.status)} label={t("configuration.onboarding.progress", { status: o.status })} /> },
    { header: t("configuration.onboarding.col.status"), render: (o) => <span className="row wrap"><span title={t(ONBOARDING_MEANING[o.status])}><StateBadge state={o.status} /></span>{o.detail && <span className="small muted" title={o.detail}>{o.detail.length > 70 ? `${o.detail.slice(0, 70)}…` : o.detail}</span>}</span> },
    { header: t("configuration.onboarding.col.configJob"), render: (o) => (o.configJobId ? <Link to={`/configuration?job=${o.configJobId}#jobs`} onClick={(e) => e.stopPropagation()}><Id value={o.configJobId} /></Link> : <span className="muted">—</span>) },
    { header: "", className: "actions", render: (o) => {
      const allowed = onboardingActions(o.status);
      return (
        <span className="row">
          {allowed.apply && <Can method="POST" path={onboardingPath(o.managedElementRef, "apply")}>
            <button type="button" className="btn primary small" onClick={(e) => { e.stopPropagation(); open({ kind: "apply", row: o }); }}>{o.status === "TEMPLATE_SELECTED" ? t("configuration.onboarding.apply") : t("configuration.onboarding.applyAgain")}</button>
          </Can>}
          {allowed.select && <Can method="POST" path={onboardingPath(o.managedElementRef, "select")}>
            <button type="button" className="btn small" onClick={(e) => { e.stopPropagation(); open({ kind: "select", element: o.managedElementRef }); }}>{t("configuration.onboarding.select")}</button>
          </Can>}
        </span>
      );
    } },
  ];
}

/** The card: filters, the server-paged table, and the dialog or drawer a row opened. */
export function ElementOnboarding() {
  const t = useT();
  const [status, setStatus] = useState("");
  const [check, setCheck] = useState("");
  const [opened, setOpened] = useState<Opened>(null);
  const [detail, setDetail] = useState<Onboarding | null>(null);
  const query = { ...(status ? { status } : {}), ...(check ? { software_check: check } : {}) };
  return (
    <Card section="configuration.onboarding" title={t("configuration.tab.onboarding")} sub={t("configuration.onboarding.sub")}
      actions={<>
        <label className="row small">{t("configuration.onboarding.status")}<select aria-label={t("configuration.onboarding.status.aria")} value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">{t("configuration.all")}</option>{STATES.map((s) => <option key={s}>{s}</option>)}</select></label>
        <label className="row small">{t("configuration.onboarding.software")}<select aria-label={t("configuration.onboarding.col.softwareCheck")} value={check} onChange={(e) => setCheck(e.target.value)}>
          <option value="">{t("configuration.all")}</option>{["MATCH", "MISMATCH", "NOT_CHECKED"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <Can method="POST" path={onboardingPath("any", "select")}>
          <button type="button" className="btn" onClick={() => setOpened({ kind: "select", element: null })}>{t("configuration.onboarding.selectFor")}</button>
        </Can>
      </>}>
      <p className="muted small">
        {t("configuration.onboarding.intro")}
      </p>
      <ServerTable<Onboarding> path={ONBOARDING_PATH} query={query} columns={columns(setOpened, t)} rowKey={(o) => o.managedElementRef} onRowClick={setDetail}
        empty={t("configuration.onboarding.empty")} />
      {opened?.kind === "apply" && <ApplyModal row={opened.row} onClose={() => setOpened(null)} />}
      {opened?.kind === "select" && <SelectModal element={opened.element} onClose={() => setOpened(null)} />}
      {detail && <OnboardingDrawer row={detail} onClose={() => setDetail(null)} />}
    </Card>
  );
}
