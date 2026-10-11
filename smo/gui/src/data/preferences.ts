/** The user's console preferences (BRIEF §4d): the type, the defaults, the closed sets each field takes, and the browser copy.
 *
 * The preferences live with the user in the GUI BFF (`GET`/`PUT /api/me/preferences`, gui-bff/app/preferences.py, which validates them against
 * the same sets). The browser keeps a copy under `localStorage["smo.prefs"]` only so the first paint is already right: `public/theme-boot.js`
 * applies it before React mounts, and `shell/ThemeProvider.tsx` re-applies the server's copy once it arrives. Nothing here trusts the copy:
 * `sanitize` drops any value outside its set before it reaches `<html>`. */

/** One user's preferences. */
export interface Preferences {
  theme: "dark" | "light" | "system";
  size: "s" | "m" | "l" | "xl";
  accent: "volt" | "blue" | "teal" | "amber" | "radisys";
  startPage: string;
  rowsPerPage: 25 | 50 | 100;
  timeZone: "local" | "UTC";
  clock: "12h" | "24h";
  reduceMotion: boolean;
  alarmSound: boolean;
  /** The language of the console's words (GUI-6.3, i18n/index.tsx). */
  language: "en" | "de";
}

/** The values a user who never saved gets (the same as the BFF's). */
export const DEFAULT_PREFERENCES: Preferences = {
  theme: "dark", size: "m", accent: "volt", startPage: "/", rowsPerPage: 50, timeZone: "local", clock: "24h", reduceMotion: false, alarmSound: false, language: "en",
};

/** The closed set of each enumerated field, in the order the Preferences page offers them. */
export const CHOICES = {
  theme: ["dark", "light", "system"] as const,
  size: ["s", "m", "l", "xl"] as const,
  accent: ["volt", "blue", "teal", "amber", "radisys"] as const,
  rowsPerPage: [25, 50, 100] as const,
  timeZone: ["local", "UTC"] as const,
  clock: ["24h", "12h"] as const,
  language: ["en", "de"] as const,
  startPage: ["/", "/flows", "/rapps", "/approvals", "/decisions", "/safeguards", "/aiml", "/policy", "/alarms", "/kpis", "/topology",
    "/configuration", "/software", "/infrastructure", "/data"] as const,
};

export const STORAGE_KEY = "smo.prefs";

/** A copy of `raw` with every field outside its set replaced by its default (an old or tampered copy never reaches the page). */
export function sanitize(raw: unknown): Preferences {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const pick = <K extends keyof typeof CHOICES>(k: K): Preferences[K] =>
    ((CHOICES[k] as readonly unknown[]).includes(r[k]) ? r[k] : DEFAULT_PREFERENCES[k as keyof Preferences]) as Preferences[K];
  return {
    theme: pick("theme"), size: pick("size"), accent: pick("accent"), startPage: pick("startPage"), rowsPerPage: pick("rowsPerPage"),
    timeZone: pick("timeZone"), clock: pick("clock"), language: pick("language"),
    reduceMotion: typeof r.reduceMotion === "boolean" ? r.reduceMotion : false,
    alarmSound: typeof r.alarmSound === "boolean" ? r.alarmSound : false,
  };
}

/** The browser copy, or the defaults when there is none or it cannot be read (private window, blocked storage). */
export function readCached(): Preferences {
  try {
    return sanitize(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}"));
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

/** Keep `p` as the browser copy; a storage that refuses is ignored (the server copy is the real one). */
export function writeCached(p: Preferences): void {
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(p)); } catch { /* storage unavailable: only the first paint is affected */ }
}

/** "dark" or "light": the theme `p` resolves to now ("system" follows the browser). */
export function resolvedTheme(p: Pick<Preferences, "theme">): "dark" | "light" {
  if (p.theme !== "system") return p.theme;
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Put `p` on `<html>`: `data-theme`, `data-accent`, `data-size` (the root font size) and `data-motion`. */
export function applyToDocument(p: Preferences, root: HTMLElement = document.documentElement): void {
  root.dataset.theme = resolvedTheme(p);
  root.dataset.accent = p.accent;
  root.dataset.size = p.size;
  if (p.reduceMotion) root.dataset.motion = "reduce"; else delete root.dataset.motion;
}

/** Formats a timestamp by the user's time zone and clock preference ("—" for a missing or unreadable one). */
export function formatWith(p: Pick<Preferences, "timeZone" | "clock">, iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { timeZone: p.timeZone === "UTC" ? "UTC" : undefined, hour12: p.clock === "12h" });
}
