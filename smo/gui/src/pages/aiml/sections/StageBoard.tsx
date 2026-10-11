/** Section `aiml.board`: the models as a kanban by lifecycle stage (Registered, Training, Validating / emulating, Promoted, Active runtime), with
 * each column's count and its first five cards, "+N more" to open the rest, and a red outline (plus the words "under floor") on a model whose
 * newest MLMF report breached its guard-KPI floor. A click selects the model for the detail panel.
 *
 * Column counts are the server's (`/aimgf/model-lifecycles/counts` by lifecycle state, plus MLMR's model total for the models AIMgF has no row
 * for; `data/board.ts` stageCounts); the cards come from four bounded lists (models, lifecycles, MLMF subscriptions, recent MLMF reports;
 * data/queries.ts) grouped with `buildBoard`, so past BOARD_LIMIT models a column holds fewer cards than its count and "+N more" says so. */
import { useState, type ReactNode } from "react";

import { Card, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { formatCount } from "../../../kit/Kpi";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { buildBoard, breachedModels, stageCounts } from "../data/board";
import { BOARD_LIMIT, useLifecycleCounts, useLifecycleIndex, useMlmfSubscriptionIndex, useModelIndex, useRecentMlmfReports } from "../data/queries";
import type { BoardColumn } from "../data/types";

/** How many cards a column shows before "+N more". */
const TOP = 5;

/** The board. `actions` go in the card head (register, view switch). */
export function StageBoard({ selected, onSelect, actions }: { selected: string | null; onSelect: (id: string) => void; actions?: ReactNode }) {
  const models = useModelIndex();
  const lifecycles = useLifecycleIndex();
  const subs = useMlmfSubscriptionIndex();
  const reports = useRecentMlmfReports();
  const counted = useLifecycleCounts();
  const t = useT();
  const failed = models.error ?? lifecycles.error;
  const ready = models.data && lifecycles.data;
  const board = ready ? buildBoard(models.data!.items, lifecycles.data!.items, breachedModels(subs.data?.items ?? [], reports.data?.items ?? [])) : null;
  const total = models.data?.total;
  const cut = total !== undefined && models.data ? total > models.data.items.length : false;
  const server = counted.data ? stageCounts(counted.data.groups, total) : null;
  const off = server?.off ?? board?.off ?? {};
  return (
    <Card section="aiml.board" title={t("aiml.board.title")} sub={t("aiml.board.sub")} actions={actions}>
      {failed && !ready ? <ErrorRetry error={failed} onRetry={() => { void models.refetch(); void lifecycles.refetch(); }} />
        : !board ? <Skeleton lines={4} />
          : board.columns.every((c) => c.cards.length === 0) && Object.keys(off).length === 0 ? <p className="muted">{t("aiml.table.empty")}</p>
            : <>
              <div className="kanban" role="list" aria-label={t("aiml.board.title")}>
                {board.columns.map((c) => <Column key={c.id} column={c} count={server?.columns[c.id]} selected={selected} onSelect={onSelect} />)}
              </div>
              <div className="row between wrap">
                <span className="small muted">
                  {Object.entries(off).filter(([, n]) => n > 0).map(([s, n]) => `${formatCount(n)} ${s.toLowerCase()}`).join(" · ") || t("aiml.board.offNone")}
                  {Object.values(off).some((n) => n > 0) && t("aiml.board.offNote")}
                </span>
                {(subs.error || reports.error) && <span className="gap-note">{t("aiml.board.mlmfDown")}</span>}
              </div>
              <p className="gap-note">
                {t("aiml.board.order")}
                {server ? t("aiml.board.serverCounts")
                  : counted.error ? t("aiml.board.noCounts") : ""}
                {cut && t("aiml.board.cut", { count: total ?? 0, limit: formatCount(BOARD_LIMIT), total: formatCount(total) })}
              </p>
            </>}
    </Card>
  );
}

/** One column: title, count (the server's when given, else its cards), the first five cards (or all, once expanded). */
function Column({ column, count, selected, onSelect }: { column: BoardColumn; count?: number; selected: string | null; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const shown = open ? column.cards : column.cards.slice(0, TOP);
  const more = column.cards.length - TOP;
  const t = useT();
  return (
    <div className="kanban-col" role="listitem" aria-label={`${t(column.titleKey)}: ${count ?? column.cards.length}`} data-stage={column.id}>
      <div className="row between"><span className="eyebrow">{t(column.titleKey)}</span><Badge plain>{formatCount(count ?? column.cards.length)}</Badge></div>
      {shown.map((m) => (
        <button key={m.modelId} type="button" className={`kanban-card${m.breach ? " breach" : ""}${selected === m.modelId ? " on" : ""}`}
          aria-pressed={selected === m.modelId} onClick={() => onSelect(m.modelId)}>
          <span className="row between"><strong className="small">{m.name}</strong><span className="mono xs muted">{m.version}</span></span>
          {m.useCase && <span className="xs muted">{m.useCase}</span>}
          <span className="row between wrap"><StateBadge state={m.state} />{m.breach && <Badge tone="bad">{t("aiml.board.underFloor")}</Badge>}</span>
        </button>
      ))}
      {column.cards.length === 0 && <span className="xs muted">{t("aiml.board.empty")}</span>}
      {more > 0 && <button type="button" className="btn ghost small" onClick={() => setOpen(!open)}>{open ? t("aiml.board.fewer") : t("aiml.board.more", { n: more })}</button>}
    </div>
  );
}
