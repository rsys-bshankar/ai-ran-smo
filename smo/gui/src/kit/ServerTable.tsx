/** The one server-side table every list uses (SCALE.md P1, handoff `ListView.dc.html`): the backend pages, filters and counts; the browser
 * renders one page and never `.filter()`s an unbounded list. `query` holds the filters the module's list route accepts (its OpenAPI `GET`
 * parameters); a filter change goes back to the first page. The page size starts at the user's "rows per page" preference.
 *
 * `withTotal: false` asks `?total=false` (no `COUNT(*)`, the footer reads "· more"), for a list that is only paged forward and can be huge
 * (decisions, audit, alarms history). The table keeps the previous page on screen while the next loads, so it does not jump. */
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

/** Props of {@link ServerTable}. */
export interface ServerTableProps<T> {
  /** The module list route, `/ran-nf-oam/alarms`; null renders nothing (a filter not chosen yet). */
  path: string | null;
  query?: Query;
  columns: Column<T>[];
  rowKey: (r: T) => string;
  withTotal?: boolean;
  refetchInterval?: number | false;
  onRowClick?: (r: T) => void;
  selectedKey?: string | null;
  empty?: ReactNode;
  /** Rows of the current page, after they arrive (a detail panel that needs the selected row). */
  onRows?: (rows: T[]) => void;
}

/** The table and its pager. */
export function ServerTable<T>({ path, query, columns, rowKey, withTotal = true, refetchInterval = POLL.lists, onRowClick, selectedKey, empty, onRows }: ServerTableProps<T>) {
  const { prefs } = usePreferences();
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [offset, setOffset] = useState(0);
  // a change of the global scope (data/scope.ts, added to the query by useSmoPage) is a filter change too
  const t = useT();
  const scope = scopeKey(useScope());
  const filterKey = JSON.stringify([query ?? {}, scope]);
  useEffect(() => { setOffset(0); }, [filterKey]);
  const q = useMemo(() => ({ ...(query ?? {}), limit, offset, total: withTotal ? undefined : false }), [filterKey, limit, offset, withTotal]); // eslint-disable-line react-hooks/exhaustive-deps
  const page = useSmoPage<T>(path, q, { refetchInterval, placeholderData: keepPreviousData });
  const rows = page.data?.items;
  useEffect(() => { if (rows && onRows) onRows(rows); }, [rows, onRows]);
  if (path === null) return null;
  return (
    <>
      <DataTable rows={rows} columns={columns} rowKey={rowKey} loading={page.isLoading} error={page.data ? undefined : page.error}
        empty={empty} onRowClick={onRowClick} selectedKey={selectedKey} />
      {page.error && page.data && <div className="error-box" role="alert">{t("kit.table.refreshFailed", { error: page.error.message })} <Stale updatedAt={page.dataUpdatedAt} after={0} /></div>}
      {page.data && (
        <Pager offset={offset} limit={limit} shown={page.data.items.length} total={page.data.total} hasMore={page.data.hasMore}
          onOffset={setOffset} onLimit={(l) => { setLimit(l); setOffset(0); }} />
      )}
    </>
  );
}
