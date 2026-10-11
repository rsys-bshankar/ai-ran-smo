/** The top bar's search (⌘K / Ctrl+K focuses it). Two parts, in one keyboard list: the client-side jumps (every page of the sidebar, every
 * lifecycle flow board) and, for typed text of 2 characters or more, the BFF's cross-object typeahead `GET /api/search?q=&limit=5`
 * (gui-bff/app/search.py: managed elements, rApps, alarms by element, AI/ML models, a decision record by id), asked 200 ms after the last key and
 * shown grouped by kind. A kind whose module did not answer is named under the list ("not searched: …"). While the server answer is missing
 * (loading, failed, or before 2 characters) the identifier jumps stand in: a UUID offers the decision record and the rApp instance, a single
 * token the managed element and its alarms. */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { roleAtLeast } from "../auth/rbac";
import { navKey, translate, useT, type Params, type TKey } from "../i18n";
import { FLOWS } from "../lib/flows";
import { Icon } from "../kit/icons";
import { NAV } from "./nav";

/** One entry of the jump list (`group` titles the server's kinds). */
export interface Jump { label: string; hint: string; to: string; group?: string }

/** The kinds `GET /api/search` groups by. */
export type SearchKind = "element" | "rapp" | "alarm" | "model" | "decision";

/** The body of `GET /api/search`. */
export interface SearchAnswer { q: string; groups: { type: SearchKind; items: { id: string; label: string; hint?: string | null; to: string }[] }[]; partial: SearchKind[] }

/** The title of each kind's group (a catalog key). */
export const KIND_TITLE: Record<SearchKind, TKey> = {
  element: "shell.search.kind.element", rapp: "shell.search.kind.rapp", alarm: "shell.search.kind.alarm", model: "shell.search.kind.model", decision: "shell.search.kind.decision",
};

/** A translator, as the helpers below take one (`useT()` in the component; English by default). */
type Tr = (key: TKey, params?: Params) => string;
const english: Tr = (key, params) => translate("en", key, params);

/** A kind's title in the language of `t` (the kind itself when this build does not know it). */
const kindTitle = (kind: SearchKind, t: Tr): string => (KIND_TITLE[kind] ? t(KIND_TITLE[kind]) : kind);

/** The server is asked once the text is this long (the BFF answers 400 below it). */
export const MIN_SERVER_QUERY = 2;
/** The server is asked this long after the last key. */
export const SEARCH_DEBOUNCE_MS = 200;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The identifier jumps for `text` (a UUID: decision record and rApp instance; one token: managed element and its alarms). */
export function idJumps(text: string, t: Tr = english): Jump[] {
  const raw = text.trim();
  if (UUID.test(raw)) {
    return [{ label: t("shell.search.decisionRecord", { id: raw.slice(0, 8) }), hint: t(navKey("/decisions")), to: `/decisions/${raw}` },
      { label: t("shell.search.rappInstance", { id: raw.slice(0, 8) }), hint: t(navKey("/rapps")), to: `/rapps/${raw}` }];
  }
  if (raw.length >= 2 && !/\s/.test(raw)) {
    return [{ label: t("shell.search.element", { id: raw }), hint: t("shell.crumb.elementDetail"), to: `/elements/${encodeURIComponent(raw)}` },
      { label: t("shell.search.alarmsOn", { id: raw }), hint: t(navKey("/alarms")), to: `/alarms?me=${encodeURIComponent(raw)}` }];
  }
  return [];
}

/** The jumps for `text` without a server answer: matching pages and flows, then identifier jumps (in the language of `t`). At most 8. */
export function jumpsFor(text: string, pages: Jump[], t: Tr = english): Jump[] {
  const q = text.trim().toLowerCase();
  if (!q) return pages.slice(0, 8);
  const hits = pages.filter((p) => p.label.toLowerCase().includes(q) || p.hint.toLowerCase().includes(q));
  return [...hits, ...idJumps(text, t)].slice(0, 8);
}

/** The whole list for `text`: up to 5 matching pages and flows, then the server's groups (each item titled by its kind); with no server
 * answer, the identifier jumps instead (titles and jumps in the language of `t`). */
export function resultsFor(text: string, pages: Jump[], answer: SearchAnswer | undefined, t: Tr = english): Jump[] {
  if (!answer) return jumpsFor(text, pages, t);
  const q = text.trim().toLowerCase();
  const hits = pages.filter((p) => p.label.toLowerCase().includes(q) || p.hint.toLowerCase().includes(q)).slice(0, 5);
  const found = answer.groups.flatMap((g) => g.items.map((i) => ({ label: i.label, hint: i.hint ?? "", to: i.to, group: kindTitle(g.type, t) })));
  return [...hits, ...(found.length > 0 ? found : idJumps(text, t))];
}

/** `value`, once it has stopped changing for `ms`. */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** The BFF's grouped answer for `q` (no call below {@link MIN_SERVER_QUERY} characters; answers are kept 10 s, as the BFF caches them). */
export function useServerSearch(q: string) {
  const text = q.trim();
  return useQuery<SearchAnswer, ApiError>({
    queryKey: ["bff", "search", text],
    queryFn: ({ signal }) => api<SearchAnswer>("/search", { query: { q: text, limit: 5 }, signal }),
    enabled: text.length >= MIN_SERVER_QUERY,
    staleTime: 10_000,
    retry: false,
  });
}

/** The search box and its result list. */
export function GlobalSearch() {
  const t = useT();
  const { me } = useAuth();
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const pages = useMemo<Jump[]>(() => [
    ...NAV.filter((n) => !n.minRole || (me && roleAtLeast(me.role, n.minRole))).map((n) => ({ label: t(navKey(n.to)), hint: t("shell.search.page"), to: n.to })),
    ...FLOWS.map((f) => ({ label: t("shell.search.flow", { id: f.id, title: f.title }), hint: t("shell.search.lifecycleFlow"), to: `/flows/${f.id}` })),
  ], [me, t]);
  const debounced = useDebounced(text, SEARCH_DEBOUNCE_MS);
  const server = useServerSearch(debounced);
  const answer = debounced.trim() === text.trim() && text.trim().length >= MIN_SERVER_QUERY ? server.data : undefined;
  const results = useMemo(() => resultsFor(text, pages, answer, t), [text, pages, answer, t]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.current?.focus(); setOpen(true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (j: Jump | undefined) => {
    if (!j) return;
    navigate(j.to);
    setText(""); setOpen(false); input.current?.blur();
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); go(results[cursor]); }
    else if (e.key === "Escape") { setOpen(false); input.current?.blur(); }
  };
  return (
    <div className="topbar-search">
      <label className="search">
        <Icon name="search" size={16} />
        <input ref={input} type="search" aria-label={t("shell.search.aria")} placeholder={t("shell.search.placeholder")} value={text}
          role="combobox" aria-expanded={open} aria-controls="global-search-results" aria-autocomplete="list"
          onChange={(e) => { setText(e.target.value); setCursor(0); setOpen(true); }} onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)} onKeyDown={onKey} />
        <kbd>⌘K</kbd>
      </label>
      {open && results.length > 0 && (
        <ul className="search-results" id="global-search-results" role="listbox">
          {results.map((r, i) => (
            <SearchRow key={`${r.group ?? ""}${r.to}${r.label}`} jump={r} on={i === cursor} first={!!r.group && results[i - 1]?.group !== r.group} onGo={go} />
          ))}
          {server.isFetching && <li className="xs muted search-note" aria-live="polite">{t("shell.search.searching")}</li>}
          {answer && answer.partial.length > 0 && <li className="xs muted search-note">{t("shell.search.notSearched", { kinds: answer.partial.map((k) => kindTitle(k, t)).join(", ") })}</li>}
          {server.isError && <li className="xs muted search-note">{t("shell.search.unavailable", { error: server.error.message })}</li>}
        </ul>
      )}
    </div>
  );
}

/** One row of the list; the first row of a server kind carries the kind's title above it. */
function SearchRow({ jump, on, first, onGo }: { jump: Jump; on: boolean; first: boolean; onGo: (j: Jump) => void }) {
  return (
    <li className={on ? "on" : ""} role="option" aria-selected={on}>
      {first && <span className="eyebrow search-group">{jump.group}</span>}
      <button type="button" onMouseDown={(e) => { e.preventDefault(); onGo(jump); }}>
        <span className="grow">{jump.label}</span><span className="xs muted">{jump.hint}</span>
      </button>
    </li>
  );
}
