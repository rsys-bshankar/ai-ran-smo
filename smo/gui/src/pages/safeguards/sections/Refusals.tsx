/** Section `safeguards.refusals` (tab "Refusals"): every time the platform refused an rApp, newest first, paged on the server and bounded in time
 * (default the last 7 days); filters are the route's own `code`, `invoker_id` and `since`. The reason is a badge with its code. A refusal records
 * no managed element, so the route cannot be scoped (GUI-9.3): under a scope the card says "network-wide". */
import { useMemo, useState } from "react";

import type { RefusalCode, SafeguardRefusal } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { ScopeNote } from "../../../kit/ScopeNote";
import { Segmented } from "../../../kit/Segmented";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime, REFUSAL_CODES, REFUSAL_MEANING } from "../../../lib/domain";
import { REFUSALS, refusalSince, type RefusalRange } from "../data/queries";

/** The refusal list. */
export function Refusals() {
  const t = useT();
  const [code, setCode] = useState<RefusalCode | "">("");
  const [invoker, setInvoker] = useState("");
  const [range, setRange] = useState<RefusalRange>("7d");
  const since = useMemo(() => refusalSince(range), [range]);
  return (
    <Card section="safeguards.refusals" title={t("safeguards.refusals.title")} sub={<>{t("safeguards.refusals.sub")} <ScopeNote always /></>} actions={<>
      <Segmented<RefusalRange> label={t("safeguards.refusals.range")} value={range} onChange={setRange}
        options={[{ id: "24h", label: "24 h" }, { id: "7d", label: "7 d" }, { id: "30d", label: "30 d" }, { id: "all", label: t("safeguards.refusals.range.all") }]} />
      <select value={code} onChange={(e) => setCode(e.target.value as RefusalCode | "")} aria-label={t("safeguards.reason")}>
        <option value="">{t("safeguards.refusals.anyReason")}</option>
        {REFUSAL_CODES.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
      <input placeholder={t("safeguards.refusals.invoker.placeholder")} value={invoker} onChange={(e) => setInvoker(e.target.value)} aria-label={t("safeguards.refusals.invoker.aria")} />
    </>}>
      <p className="muted small">{t("safeguards.refusals.intro")}</p>
      <ServerTable<SafeguardRefusal> path={REFUSALS} query={{ code: code || undefined, invoker_id: invoker.trim() || undefined, since }}
        rowKey={(r) => r.refusalId} empty={t("safeguards.refusals.empty")} columns={[
          { header: t("safeguards.refusals.when"), render: (r) => formatTime(r.occurredAt) },
          { header: t("safeguards.refusals.rapp"), render: (r) => <Id value={r.invokerId} /> },
          { header: t("safeguards.refusals.refusedBecause"), render: (r) => <Badge tone={r.refusal === "RAPP_KILLED" ? "bad" : "warn"} title={t(REFUSAL_MEANING[r.refusal])}>{r.refusal}</Badge> },
          { header: t("safeguards.refusals.requestedBy"), render: (r) => r.requestedBy ?? <span className="muted">—</span> },
          { header: t("safeguards.refusals.detail"), render: (r) => r.detail ?? <span className="muted">—</span> },
          { header: t("safeguards.refusals.announced"), render: (r) => (r.announced ? t("safeguards.refusals.yes") : <span className="muted">{t("safeguards.refusals.no")}</span>) },
        ]} />
    </Card>
  );
}
