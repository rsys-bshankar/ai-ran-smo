/** The signed-in console frame: sidebar, top bar and the routed page (BRIEF §2). Below 800 px the sidebar stacks above the content
 * (styles.css). Also owns the change-password dialog the sidebar opens, and the one pushed-summary stream of the tab (`LiveEvents`, for the
 * visible page's summary and the sidebar's) and the global scope read from the URL (`ScopeProvider`, GUI-9.3). */
import { Suspense, useState, type FormEvent } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Field, Modal } from "../components/ui";
import { useToast } from "../components/Toast";
import { summaryPageOf } from "../data/events";
import { useT } from "../i18n";
import { Skeleton } from "../kit/states";
import { LiveEvents } from "./LiveEvents";
import { ScopeProvider } from "./ScopeProvider";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

/** The frame; renders nothing until the session is known. */
export function Layout() {
  const { me } = useAuth();
  const [pwOpen, setPwOpen] = useState(false);
  const { pathname } = useLocation();
  if (!me) return null;
  return (
    <ScopeProvider>
    <LiveEvents page={summaryPageOf(pathname)} disabled={!!me.mfaEnrolmentRequired}>
    <div className="shell">
      <Sidebar onChangePassword={() => setPwOpen(true)} />
      <div className="main">
        <TopBar />
        <main className="content" id="main"><Suspense fallback={<Skeleton lines={6} />}><Outlet /></Suspense></main>
      </div>
      {pwOpen && <ChangePassword onClose={() => setPwOpen(false)} />}
    </div>
    </LiveEvents>
    </ScopeProvider>
  );
}

/** The change-password dialog (`POST /api/me/password`). */
function ChangePassword({ onClose }: { onClose: () => void }) {
  const t = useT();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api("/me/password", { method: "POST", json: { currentPassword: current, newPassword: next } });
      toast.push({ tone: "success", text: t("shell.password.changed") });
      onClose();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <Modal title={t("shell.password.title")} onClose={onClose}>
      <form onSubmit={submit} className="form">
        <Field label={t("shell.password.current")}><input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></Field>
        <Field label={t("shell.password.new")} hint={t("shell.password.hint")}><input type="password" autoComplete="new-password" minLength={8} value={next} onChange={(e) => setNext(e.target.value)} required /></Field>
        {error && <div className="error-box">{error}</div>}
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("shell.password.cancel")}</button><button className="btn primary">{t("shell.password.submit")}</button></div>
      </form>
    </Modal>
  );
}
