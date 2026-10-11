/** Recent sign-ins (`security.signins`, handoff `Security.dc.html`): the signed-in user's own newest sign-ins, failed sign-ins and sign-outs
 * from the BFF's audit log (`GET /api/me/sign-ins?limit=20`, GUI-9.8; never another user's rows), newest first, with what the BFF recorded about
 * each. A failed sign-in the user does not recognise is the reason to change the password; the box says so when there is one. */
import { DataTable, Card } from "../../../components/ui";
import { Badge } from "../../../kit/Badge";
import { useT, type Translator } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { formatTime } from "../../../lib/domain";
import { SIGN_IN_LIMIT, useSignIns } from "../data/queries";

/** The badge tone and label of an audit action, in the language of `t` (an action it does not know is shown as the BFF names it). */
export function actionLabel(action: string, t: Translator): { tone: "ok" | "bad" | "mute" | "info"; text: string } {
  if (action === "LOGIN") return { tone: "ok", text: t("security.signins.action.login") };
  if (action === "LOGIN_FAILED") return { tone: "bad", text: t("security.signins.action.loginFailed") };
  if (action === "LOGOUT") return { tone: "mute", text: t("security.signins.action.logout") };
  if (action === "TOKEN") return { tone: "info", text: t("security.signins.action.token") };
  return { tone: "mute", text: action.toLowerCase().replace(/_/g, " ") };
}

/** The box. */
export function SignIns() {
  const t = useT();
  const rows = useSignIns();
  const failed = (rows.data ?? []).filter((r) => r.action === "LOGIN_FAILED").length;
  return (
    <Card section="security.signins" title={t("security.signins.title")} sub={t("security.signins.sub", { limit: SIGN_IN_LIMIT })}>
      {failed > 0 && <Callout tone="warn" title={t("security.signins.failed", { count: failed })}>{t("security.signins.failed.body")}</Callout>}
      <DataTable rows={rows.data?.map((r, i) => ({ ...r, key: `${r.at}-${i}` }))} loading={rows.isLoading} error={rows.data ? undefined : rows.error}
        rowKey={(r) => r.key} empty={t("security.signins.empty")} columns={[
          { header: t("security.signins.when"), render: (r) => <span className="small">{formatTime(r.at)}</span> },
          { header: t("security.signins.what"), render: (r) => { const a = actionLabel(r.action, t); return <Badge tone={a.tone}>{a.text}</Badge>; } },
          { header: t("security.signins.detail"), render: (r) => r.detail ? <span className="small muted">{r.detail}</span> : <span className="muted">—</span> },
        ]} />
    </Card>
  );
}
