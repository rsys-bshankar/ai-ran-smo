/** A server table paged by cursor (SCALE.md P1, keyset): for routes that answer `{items, limit, nextCursor, hasMore}` when asked with `after`
 * (RAN NF OAM alarms in the console order, decision records). The first page is asked with an empty `after=`; "Next" asks with the previous
 * answer's `nextCursor`, "Previous" goes back to a cursor already seen (kept in a stack, so no page is read twice to step back). A filter change,
 * or a change of `resetKey` (a "N new — show" bar), returns to the first page. Same look as `ServerTable`: the shared `Pager` footer, which
 * reads "· more" since a keyset route does not count. */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { keepPreviousData } from "@tanstack/react-query";

import type { Query } from "../api/client";
import { POLL, useSmoPage } from "../api/hooks";
import { DataTable, type Column } from "../components/ui";
import { scopeKey, useScope } from "../data/scope";
import { useT } from "../i18n";
import { usePreferences } from "../shell/ThemeProvider";
import { Pager } from "./Pager";
import { Stale } from "./states";

/** The keyset envelope (a `Page` with the cursor of the next page). */
interface KeysetPage<T> { items: T[]; limit: number; nextCursor?: string | null; hasMore?: boolean }

/** Props of {@link KeysetTable}. */
export interface KeysetTableProps<T> {
  path: string | null;
  query?: Query;
  columns: Column<T>[];
  rowKey: (r: T) => string;
  refetchInterval?: number | false;
  onRowClick?: (r: T) => void;
  selectedKey?: string | null;
  empty?: ReactNode;
  onRows?: (rows: T[]) => void;
  /** Any value; when it changes the table goes back to the first page. */
  resetKey?: unknown;
}

/** The table and its pager. */
export function KeysetTable<T>({ path, query, columns, rowKey, refetchInterval = POLL.lists, onRowClick, selectedKey, empty, onRows, resetKey }: KeysetTableProps<T>) {
  const { prefs } = usePreferences();
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [cursors, setCursors] = useState<string[]>([""]);
  // a change of the global scope (data/scope.ts, added to the query by useSmoPage) is a filter change too
  const t = useT();
  const scope = scopeKey(useScope());
  const filterKey = JSON.stringify([query ?? {}, scope]);
  useEffect(() => { setCursors([""]); }, [filterKey, resetKey, limit]);
  const index = cursors.length - 1;
  const q = useMemo(() => ({ ...(query ?? {}), after: cursors[index], limit }), [filterKey, cursors, index, limit]); // eslint-disable-line react-hooks/exhaustive-deps
  const page = useSmoPage<T>(path, q, { refetchInterval, placeholderData: keepPreviousData });
  const data = page.data as KeysetPage<T> | undefined;
  const rows = data?.items;
  useEffect(() => { if (rows && onRows) onRows(rows); }, [rows, onRows]);
  if (path === null) return null;
  const go = (offset: number) => {
    const target = Math.round(offset / limit);
    if (target > index && data?.nextCursor) setCursors((c) => [...c, data.nextCursor!]);
    else if (target < index) setCursors((c) => c.slice(0, Math.max(1, target + 1)));
  };
  return (
    <>
      <DataTable rows={rows} columns={columns} rowKey={rowKey} loading={page.isLoading} error={page.data ? undefined : page.error}
        empty={empty} onRowClick={onRowClick} selectedKey={selectedKey} />
      {page.error && page.data && <div className="error-box" role="alert">{t("kit.table.refreshFailed", { error: page.error.message })} <Stale updatedAt={page.dataUpdatedAt} after={0} /></div>}
      {data && (
        <Pager offset={index * limit} limit={limit} shown={data.items.length} hasMore={!!data.nextCursor || !!data.hasMore}
          onOffset={go} onLimit={setLimit} />
      )}
    </>
  );
}
