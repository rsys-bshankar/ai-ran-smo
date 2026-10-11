/** Element detail · config history (`element.history`): every dispatched write to this element, newest first, paged by the server
 * (`GET /managed-entities/{me}/config-history`, offset-paged: the route has no keyset cursor), optionally of one managed function. Pick an
 * older snapshot as "from" and a newer one as "to" to compare them below (`?from=`, `?to=`). "Undo job" rolls back the job that wrote a
 * snapshot (`POST /config-jobs/{id}/rollback`, with a preview; operator). */
import { useState } from "react";
import { Link } from "react-router-dom";

import { Card, DataTable, Id, StateBadge } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Pager } from "../../../kit/Pager";
import { ErrorRetry } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { usePreferences } from "../../../shell/ThemeProvider";
import { JobRollback } from "../../configuration/sections/JobRollback";
import { useFromSnapshot, useHistory, useToSnapshot } from "../data/queries";
import type { Snapshot } from "../data/types";

/** "attr a → b, attr c → d" of what a write changed (the attributes it wrote, from the value before). */
function changed(s: Snapshot, t: Translator): string {
  if (s.after === null) return s.operation === "delete" ? t("element.history.deleted") : s.operation === "remove" ? t("element.history.removed") : t("element.history.notApplied");
  return Object.entries(s.after).slice(0, 3).map(([k, v]) => `${k} ${JSON.stringify(s.before?.[k] ?? null)} → ${JSON.stringify(v)}`).join(", ")
    + (Object.keys(s.after).length > 3 ? ` +${Object.keys(s.after).length - 3}` : "");
}

/** The history card of element `me`. */
export function ConfigHistory({ me }: { me: string }) {
  const { prefs } = usePreferences();
  const t = useT();
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [offset, setOffset] = useState(0);
  const [fnText, setFnText] = useState("");
  const [fn, setFn] = useState("");
  const page = useHistory(me, { limit, offset, ...(fn ? { managed_function_ref: fn } : {}) });
  const [from, setFrom] = useFromSnapshot();
  const [to, setTo] = useToSnapshot();
  const pick = (s: Snapshot, which: "from" | "to") => (which === "from" ? setFrom(from === s.snapshotId ? null : s.snapshotId) : setTo(to === s.snapshotId ? null : s.snapshotId));
  return (
    <Card section="element.history" title={t("element.history.title")} sub={t("element.history.sub")}
      actions={<form className="row" onSubmit={(e) => { e.preventDefault(); setFn(fnText.trim()); setOffset(0); }}>
        <label className="search"><input aria-label={t("element.history.function.aria")} placeholder={t("element.history.function.placeholder")} value={fnText} onChange={(e) => setFnText(e.target.value)} /></label>
        <button type="submit" className="btn small">{t("element.history.filter")}</button>
      </form>}>
      {page.error && !page.data ? <ErrorRetry error={page.error} onRetry={() => void page.refetch()} /> : (
        <DataTable<Snapshot> rows={page.data?.items} loading={page.isLoading} rowKey={(s) => s.snapshotId} selectedKey={to} empty={t("element.history.empty")}
          columns={[
            { header: t("element.history.compare"), render: (s) => (
              <span className="row">
                <button type="button" className={`chip${from === s.snapshotId ? " on" : ""}`} aria-pressed={from === s.snapshotId} onClick={() => pick(s, "from")}>{t("element.history.from")}</button>
                <button type="button" className={`chip${to === s.snapshotId ? " on" : ""}`} aria-pressed={to === s.snapshotId} onClick={() => pick(s, "to")}>{t("element.history.to")}</button>
              </span>
            ) },
            { header: t("element.history.when"), render: (s) => <span className="mono small muted">{formatTime(s.createdAt)}</span> },
            { header: t("element.history.job"), render: (s) => <Link to={`/configuration?job=${s.jobId}#jobs`}><Id value={s.jobId} /></Link> },
            { header: t("element.history.function"), render: (s) => <span className="mono small">{s.managedFunctionRef ?? "—"}</span> },
            { header: t("element.history.change"), render: (s) => <span className="mono small" title={s.beforeError ?? undefined}>{changed(s, t)}</span> },
            { header: t("element.history.result"), render: (s) => <StateBadge state={s.subChangeStatus} /> },
            { header: "", className: "actions", render: (s) => (s.subChangeStatus === "APPLIED" ? <JobRollback jobId={s.jobId} label={t("element.history.undo")} /> : null) },
          ]} />
      )}
      {page.data && <Pager offset={offset} limit={limit} shown={page.data.items.length} total={page.data.total} hasMore={page.data.hasMore}
        onOffset={setOffset} onLimit={(l) => { setLimit(l); setOffset(0); }} />}
    </Card>
  );
}
