/** The Alarms page's API knowledge (STRUCTURE.md rule 4): the RAN NF OAM and FOCOM alarm routes and their query parameters (the OpenAPI `GET`
 * parameters: `severity`, `managed_element_ref`, `managed_function_ref`, `ack_state`, `open_only`, `probable_cause`, `region`, keyset `after` for
 * RAN alarms; `severity`, `resource_ref` for O-Cloud alarms), the group-by counts (`/alarms/counts`), the server's correlation of one alarm
 * (`/alarms/{id}/correlated`), and polling (5 s, or 60 s while the summary stream pushes the counts). Sections call the hooks here, never `useSmo`
 * with a path. */
import type { Query } from "../../../api/client";
import { POLL, useSmo, useSmoPage } from "../../../api/hooks";
import type { Alarm, O1Endpoint } from "../../../api/types";
import { LIVE_SUMMARY_POLL, useLive } from "../../../data/events";
import { useSummary } from "../../../data/summary";
import { formatDuration } from "../../../lib/domain";

/** RAN NF alarms (O1 FaultMnS). */
export const RAN_ALARMS = "/ran-nf-oam/alarms";
/** O-Cloud infrastructure alarms (FOCOM, O2ims). */
export const OCLOUD_ALARMS = "/focom/alarms";
/** FM subscriptions: RAN NF OAM as a DME producer of RAN.FaultRecords. */
export const FM_SUBSCRIPTIONS = "/ran-nf-oam/fm-subscriptions";
/** The registered O1 endpoints (the managed elements an FM subscription can name). */
export const O1_ENDPOINTS = "/ran-nf-oam/o1-adaptor-endpoints";
/** The correlation window asked of the server: alarms of the same element raised within this many seconds of the selected one. */
export const SAME_ELEMENT_WINDOW_S = 60;

/** The server-side filters of the RAN alarm table; an empty string leaves the parameter out. */
export interface RanFilter {
  severity: string; managedElement: string; managedFunction: string;
  /** "", "ACKNOWLEDGED" or "UNACKNOWLEDGED". */
  ackState?: string;
  probableCause?: string;
  /** Include cleared alarms (otherwise `open_only=true`, unless the severity filter is "cleared" itself). */
  showCleared?: boolean;
  region?: string;
}

/** The fields the group-by view can group on (`GET /alarms/counts?group_by=`), each also a list filter, with its label. */
export const GROUP_BY = [
  { key: "probable_cause", label: "Probable cause" },
  { key: "managed_element_ref", label: "Managed element" },
  { key: "severity", label: "Severity" },
  { key: "ack_state", label: "Ack state" },
  { key: "region", label: "Region" },
] as const;
export type GroupByKey = (typeof GROUP_BY)[number]["key"];

/** The list filters of `f` as route parameters (no paging). */
export function ranAlarmFilters(f: RanFilter): Query {
  return {
    severity: f.severity || undefined,
    managed_element_ref: f.managedElement.trim() || undefined,
    managed_function_ref: f.managedFunction.trim() || undefined,
    ack_state: f.ackState || undefined,
    probable_cause: f.probableCause?.trim() || undefined,
    region: f.region?.trim() || undefined,
    open_only: f.showCleared || f.severity === "cleared" ? undefined : true,
  };
}

/** `filters` narrowed to one group of a group-by view (the group's key becomes that field's filter). */
export function withGroup(filters: Query, by: GroupByKey, key: string): Query {
  return { ...filters, [by]: key };
}

/** The true counts per severity, unacknowledged and mean time to acknowledge, from the BFF summary. */
export function useAlarmSummary() {
  return useSummary("alarms");
}

/** How often the alarm list re-reads: every 5 s while polling, once a minute while the summary stream pushes changes (each alarm count change
 * refetches it at once, `data/events.ts`). */
export function useAlarmPoll(): number {
  return useLive().connected ? LIVE_SUMMARY_POLL : POLL.alarms;
}

/** The groups of the RAN alarms under `filters` (`GET /alarms/counts?group_by=`, top 50 by count; null `by`: no call). */
export function useAlarmGroups(by: GroupByKey | null, filters: Query) {
  return useSmo<{ groupBy: string; groups: { key: string | null; count: number }[] }>(by ? `${RAN_ALARMS}/counts` : null, { ...filters, group_by: by ?? undefined },
    { refetchInterval: POLL.lists });
}

/** The alarms raised in each of the last 24 hours (oldest first), for the trend sparkline; the same cache entry as the Dashboard's. */
export function useAlarmHours() {
  return useSmo<{ groupBy: string; groups: { key: string; count: number }[] }>(`${RAN_ALARMS}/counts`, { group_by: "hour" }, { refetchInterval: 60_000 });
}

/** The answer of `GET /alarms/{id}/correlated`. */
export interface Correlated { alarmId: string; rule: string; windowSeconds: number; items: Alarm[]; truncated: boolean }

/** The alarms the server correlates with `alarmId` (same element, ±{@link SAME_ELEMENT_WINDOW_S} s; null: nothing selected, no call). */
export function useCorrelated(alarmId: string | null) {
  // useSmoPage, not useSmo: the answer carries `items`, and useSmo would unwrap it to the bare list, losing the rule and the window
  const q = useSmoPage<Alarm>(alarmId ? `${RAN_ALARMS}/${alarmId}/correlated` : null, { window_seconds: SAME_ELEMENT_WINDOW_S }, { refetchInterval: POLL.lists });
  return q as unknown as Omit<typeof q, "data"> & { data: Correlated | undefined };
}

/** MGT-8.2: one change of an alarm (`GET /alarms/{id}/history`, oldest first). */
export interface AlarmHistoryEntry { at: string; event: "RAISED" | "ACKNOWLEDGED" | "UNACKNOWLEDGED" | "CLEARED" | "SEVERITY_CHANGED"; from: string | null; to: string | null; by: string | null }
/** MGT-8.3: one comment on an alarm (`GET /alarms/{id}/comments`, oldest first). */
export interface AlarmComment { commentId: string; alarmId: string; createdAt: string; author: string; text: string }
/** How many history rows and comments the detail panel reads (the newest past it are behind "all on the …" — no alarm here has that many). */
export const NOTES_LIMIT = 100;

/** GUI-2.4: the history of the selected alarm, every change with who and when (null: nothing selected, no call). */
export function useAlarmHistory(alarmId: string | null) {
  return useSmoPage<AlarmHistoryEntry>(alarmId ? `${RAN_ALARMS}/${alarmId}/history` : null, { limit: NOTES_LIMIT }, { refetchInterval: POLL.lists });
}

/** GUI-2.3: the comments on the selected alarm (null: nothing selected, no call). */
export function useAlarmComments(alarmId: string | null) {
  return useSmoPage<AlarmComment>(alarmId ? `${RAN_ALARMS}/${alarmId}/comments` : null, { limit: NOTES_LIMIT }, { refetchInterval: POLL.lists });
}

/** The O1 endpoints for the FM subscription form's element picker. */
export function useO1Endpoints() {
  return useSmo<O1Endpoint[]>(O1_ENDPOINTS);
}

/** The base path of one alarm's actions (`/ack`, `/clear`). */
export function alarmPath(alarmId: string): string {
  return `${RAN_ALARMS}/${alarmId}`;
}

/** Each correlated alarm with its raise time relative to `alarm` in seconds (negative: before), nearest first. */
export function withDelta(alarm: Alarm, others: Alarm[]): { alarm: Alarm; deltaS: number }[] {
  const at = alarm.raisedAt ? new Date(alarm.raisedAt).getTime() : NaN;
  return others
    .filter((o) => o.alarmId !== alarm.alarmId)
    .map((o) => ({ alarm: o, deltaS: Number.isNaN(at) || !o.raisedAt ? NaN : Math.round((new Date(o.raisedAt).getTime() - at) / 1000) }))
    .sort((a, b) => (Number.isNaN(a.deltaS) ? 1e12 : Math.abs(a.deltaS)) - (Number.isNaN(b.deltaS) ? 1e12 : Math.abs(b.deltaS)));
}

export { formatDuration };
