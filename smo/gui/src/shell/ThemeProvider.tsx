/** Applies the user's preferences to the whole console and holds them for every page (BRIEF §4d).
 *
 * On mount it applies the browser copy (`data/preferences.ts`; `public/theme-boot.js` already did so before React), then, once signed in,
 * fetches the server copy (`GET /api/me/preferences`), applies it and refreshes the browser copy. `save` writes through the BFF (`PUT`).
 * The Preferences page previews unsaved changes with `preview`: they are applied to `<html>` only while that page holds them, and dropped when
 * it unmounts. "system" follows `prefers-color-scheme` live. The `language` preference is handed to `I18nProvider` (i18n/index.tsx), so every page
 * below answers `useT()` in it. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { KEYS } from "../data/keys";
import { applyToDocument, readCached, sanitize, writeCached, type Preferences } from "../data/preferences";
import { I18nProvider } from "../i18n";

/** What `usePreferences` hands a component. */
export interface PreferencesApi {
  /** The saved preferences (the browser copy until the server's arrives). */
  prefs: Preferences;
  /** Unsaved preferences to show on the page now, or null to show the saved ones again. */
  preview: (p: Preferences | null) => void;
  /** Store `p` for this user; resolves when the BFF accepted it. */
  save: (p: Preferences) => Promise<void>;
  saving: boolean;
}

const Ctx = createContext<PreferencesApi | null>(null);

/** The provider; place it inside `AuthProvider` (it needs to know whether someone is signed in). */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const [cached] = useState(readCached);
  const [draft, setDraft] = useState<Preferences | null>(null);
  const server = useQuery<Preferences, ApiError>({
    queryKey: KEYS.preferences,
    queryFn: async ({ signal }) => sanitize(await api("/me/preferences", { signal })),
    enabled: !!me && !me.mfaEnrolmentRequired,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const prefs = server.data ?? cached;
  const shown = draft ?? prefs;

  useEffect(() => { if (server.data) writeCached(server.data); }, [server.data]);
  useEffect(() => {
    applyToDocument(shown);
    if (shown.theme !== "system" || typeof window.matchMedia !== "function") return;
    // "system": follow the operating system's switch between light and dark while the console is open
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyToDocument(shown);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [shown]);

  const mutation = useMutation<Preferences, ApiError, Preferences>({
    mutationFn: (p) => api<Preferences>("/me/preferences", { method: "PUT", json: p }),
    onSuccess: (saved) => {
      const clean = sanitize(saved);
      qc.setQueryData(KEYS.preferences, clean);
      writeCached(clean);
      setDraft(null);
    },
  });
  const save = useCallback(async (p: Preferences) => { await mutation.mutateAsync(p); }, [mutation]);
  const value = useMemo<PreferencesApi>(() => ({ prefs, preview: setDraft, save, saving: mutation.isPending }), [prefs, save, mutation.isPending]);
  // GUI-6.3: the words follow the shown preferences too, so the Preferences page previews a language before it is saved
  return <Ctx.Provider value={value}><I18nProvider language={shown.language}>{children}</I18nProvider></Ctx.Provider>;
}

/** The current user's preferences. Outside a `ThemeProvider` (a component test) it answers the browser copy and saves nothing. */
export function usePreferences(): PreferencesApi {
  return useContext(Ctx) ?? { prefs: readCached(), preview: () => {}, save: async () => {}, saving: false };
}
