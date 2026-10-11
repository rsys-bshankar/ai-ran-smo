/**
 * The session state of the SPA: `AuthProvider` fetches the signed-in user (GET /api/me) and the BFF's permission table, offers `login`,
 * `loginWithCode` and `logout`, and exposes `can(method, path)` through the `useAuth` hook.
 * `can` only decides what to show; the BFF re-checks every call (see rbac.ts). Mounted once in main.tsx, inside the toast and query providers.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import { useOptionalToast } from "../components/Toast";
import type { Me } from "../api/types";
import { isChallenge, type LoginChallenge, type SignedIn } from "../lib/mfa";
import { can as canWith, type PermissionRule, type QueryValues, type Role } from "./rbac";

/**
 * What `useAuth` returns: the user (null when signed out), whether that is still loading, the role, the two sign-in steps, sign-out and the permission check.
 */
interface AuthState {
  me: Me | null;
  loading: boolean;
  role: Role | undefined;
  /** Null when signed in; a challenge when the account has a one-time code and the second step (`loginWithCode`) is next. */
  login: (username: string, password: string) => Promise<LoginChallenge | null>;
  loginWithCode: (challenge: string, code: string) => Promise<SignedIn>;
  logout: () => Promise<void>;
  can: (method: string, path: string, query?: QueryValues) => boolean;
  /** The BFF's permission table (`GET /api/permissions`), empty until read: the Admin role matrix is drawn from it (GUI-10.4). */
  rules: PermissionRule[];
}

const AuthContext = createContext<AuthState | null>(null);

const isMeQuery = (key: readonly unknown[]) => key[0] === "bff" && key[1] === "me";

/**
 * Provides the session to the app and keeps it in the react-query cache under ["bff", "me"].
 * Loads the user once (a 401 means "signed out", not an error) and the permission table after that, except while a local admin must still enrol
 * a one-time code (every other route answers 403 until then). Listens for the window event `smo:unauthorized` (fired by `api` on any 401) and
 * clears the user, which sends the router back to the login page. Starting a session or signing out removes every other cached query, so the
 * next user never sees the previous user's data; the "me" entry itself is updated in place because clearing the whole client would orphan the
 * observer and leave the app stuck on the login page. Sign-out also follows the identity provider's end-session URL when the BFF returns an http(s) one (PR-SEC-6).
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const toast = useOptionalToast();
  const meQuery = useQuery<Me | null, ApiError>({
    queryKey: ["bff", "me"],
    queryFn: async () => {
      try {
        return await api<Me>("/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
    retry: false,
  });
  const me = meQuery.data ?? null;

  const rulesQuery = useQuery<{ role: Role; rules: PermissionRule[] }, ApiError>({
    queryKey: ["bff", "permissions", me?.username],
    queryFn: () => api("/permissions"),
    enabled: me !== null && !me.mfaEnrolmentRequired,       // every other route answers 403 until the admin has enrolled
    staleTime: 5 * 60_000,
  });

  // Any 401 from the BFF (expired or revoked session) drops back to login.
  useEffect(() => {
    const onUnauthorized = () => qc.setQueryData(["bff", "me"], null);
    window.addEventListener("smo:unauthorized", onUnauthorized);
    return () => window.removeEventListener("smo:unauthorized", onUnauthorized);
  }, [qc]);

  const startSession = useCallback((result: SignedIn) => {
    // Update the live "me" entry in place (qc.clear() would orphan the
    // observer above, leaving the app stuck on the login page), then drop
    // whatever the previous user had cached.
    qc.setQueryData(["bff", "me"], result);
    qc.removeQueries({ predicate: (q) => !isMeQuery(q.queryKey) });
  }, [qc]);

  const login = useCallback(async (username: string, password: string) => {
    const result = await api<SignedIn | LoginChallenge>("/login", { method: "POST", json: { username, password } });
    if (isChallenge(result)) return result;       // PR-SEC-7.2: no session yet
    startSession(result);
    return null;
  }, [startSession]);

  const loginWithCode = useCallback(async (challenge: string, code: string) => {
    const result = await api<SignedIn>("/login/totp", { method: "POST", json: { challenge, code } });
    startSession(result);
    return result;
  }, [startSession]);

  // GUI-10.7: a failed `POST /logout` (network, 5xx) is caught: the user is signed out here anyway and told that the server did not confirm it
  // (the session cookie then ends when it expires, or with "Revoke sessions"); it never surfaces as an unhandled rejection.
  const logout = useCallback(async () => {
    let endSessionUrl: string | undefined;
    try {
      endSessionUrl = (await api<{ endSessionUrl?: string }>("/logout", { method: "POST" })).endSessionUrl;
    } catch (e) {
      // a 401 means the session had already ended: nothing to tell
      if (!(e instanceof ApiError && e.status === 401)) toast?.push({ tone: "error", text: `Signed out in this browser, but the server did not confirm it (${(e as Error).message}); the session ends when it expires.` });
    } finally {
      qc.setQueryData(["bff", "me"], null);
      qc.removeQueries({ predicate: (q) => !isMeQuery(q.queryKey) });
    }
    // PR-SEC-6: a user who signed in through the identity provider also leaves its session (RP-initiated logout), when it has an end-session page.
    if (endSessionUrl && /^https?:\/\//.test(endSessionUrl)) window.location.assign(endSessionUrl);
  }, [qc, toast]);

  const value = useMemo<AuthState>(() => {
    const rules = rulesQuery.data?.rules ?? [];
    return {
      me,
      loading: meQuery.isLoading,
      role: me?.role,
      login,
      loginWithCode,
      logout,
      can: (method, path, query) => canWith(rules, me?.role, method, path, query),
      rules,
    };
  }, [me, meQuery.isLoading, rulesQuery.data, login, loginWithCode, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The auth state when there is a provider, else null: for a shell part that also renders without one (the scope picker in a test). */
export function useOptionalAuth(): AuthState | null {
  return useContext(AuthContext);
}

/** Returns the session state; throws when used outside `AuthProvider`. */
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
