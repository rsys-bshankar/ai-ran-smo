/** The five explicit states every box renders (SCALE.md P10): loading skeleton, empty, error with retry, partial ("2 of 21 modules
 * didn't answer") and stale ("updated 40 s ago"). A section picks the one that applies with `QueryState`, so no box ever shows a
 * blank area or a spinner forever. */
import { useEffect, useState, type ReactNode } from "react";

import { useT } from "../i18n";

/** A few shimmering lines while the first answer is on its way. */
export function Skeleton({ lines = 3 }: { lines?: number }) {
  const t = useT();
  return <div aria-busy="true" aria-label={t("kit.loading")}>{Array.from({ length: lines }, (_, i) => <span key={i} className="skeleton" style={{ width: `${90 - i * 12}%` }} />)}</div>;
}

/** Nothing to show: a title and an optional hint or action. */
export function Empty({ title, children }: { title?: ReactNode; children?: ReactNode }) {
  const t = useT();
  return <div className="empty"><strong>{title === undefined ? t("kit.empty") : title}</strong>{children}</div>;
}

/** An error with a retry button (the query's `refetch`). */
export function ErrorRetry({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const t = useT();
  return (
    <div className="error-box row between" role="alert">
      <span>{error instanceof Error ? error.message : String(error)}</span>
      {onRetry && <button type="button" className="btn small" onClick={onRetry}>{t("kit.retry")}</button>}
    </div>
  );
}

/** "Updated 40 s ago": shown when the data is older than `after` seconds (a dropped refresh). Re-renders every 5 s. */
export function Stale({ updatedAt, after = 60 }: { updatedAt: number; after?: number }) {
  const t = useT();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 5_000); return () => clearInterval(timer); }, []);
  const age = Math.round((now - updatedAt) / 1000);
  if (!updatedAt || age < after) return null;
  return <span className="stale" title={new Date(updatedAt).toLocaleString()}>{t("kit.stale.updated", { age: age >= 120 ? t("kit.stale.minutes", { n: Math.round(age / 60) }) : t("kit.stale.seconds", { n: age }) })}</span>;
}

/** A query's state, as React Query hands it over. */
export interface QueryLike { isLoading?: boolean; isPending?: boolean; error?: unknown; data?: unknown; refetch?: () => unknown; dataUpdatedAt?: number }

/** Renders loading / error / empty for a query, or `children` once there is data. `isEmpty` decides what counts as empty (default: an empty array). */
export function QueryState({ q, isEmpty, empty, lines, children }: { q: QueryLike; isEmpty?: (d: unknown) => boolean; empty?: ReactNode; lines?: number; children: ReactNode }) {
  if (q.error && q.data === undefined) return <ErrorRetry error={q.error} onRetry={q.refetch ? () => void q.refetch!() : undefined} />;
  if (q.data === undefined) return <Skeleton lines={lines} />;
  const blank = isEmpty ? isEmpty(q.data) : Array.isArray(q.data) && q.data.length === 0;
  if (blank) return <>{typeof empty === "string" || empty === undefined ? <Empty title={empty} /> : empty}</>;
  return <>{children}</>;
}
