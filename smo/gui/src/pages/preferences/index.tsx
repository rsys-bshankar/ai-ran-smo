/** The Preferences page (route /preferences, BRIEF §4d, handoff `Preferences.dc.html`): language, theme, accent, text size and display defaults, with a
 * live preview and Save / Reset. A change previews on this page only (`ThemeProvider.preview`) until it is saved; leaving the page unsaved drops
 * it. Saving stores it with the user in the GUI BFF (`PUT /api/me/preferences`), so it follows the user to any browser. Sections: README.md. Every word comes from the catalog (i18n/, GUI-6.3); a language preview applies at once, like the theme. */
import { useEffect, useState } from "react";

import { useAuth } from "../../auth/AuthContext";
import { PageHeader } from "../../components/ui";
import { DEFAULT_PREFERENCES, type Preferences as Prefs } from "../../data/preferences";
import { useT } from "../../i18n";
import { Callout } from "../../kit/Callout";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { usePreferences } from "../../shell/ThemeProvider";
import { AccentPicker } from "./sections/AccentPicker";
import { DisplayDefaults } from "./sections/DisplayDefaults";
import { LanguagePicker } from "./sections/LanguagePicker";
import { LivePreview } from "./sections/LivePreview";
import { TextSize } from "./sections/TextSize";
import { ThemePicker } from "./sections/ThemePicker";

/** The page. */
export function Preferences() {
  const { me } = useAuth();
  const t = useT();
  const { prefs, preview, save, saving } = usePreferences();
  const [draft, setDraft] = useState<Prefs>(prefs);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = JSON.stringify(draft) !== JSON.stringify(prefs);
  useEffect(() => { setDraft(prefs); }, [prefs]);
  useEffect(() => { preview(dirty ? draft : null); }, [draft, dirty, preview]);
  useEffect(() => () => preview(null), [preview]);           // leaving the page unsaved: back to the saved preferences
  const set = (patch: Partial<Prefs>) => { setDraft({ ...draft, ...patch }); setSaved(false); };
  const onSave = async () => {
    setError(null);
    try { await save(draft); setSaved(true); } catch (e) { setError((e as Error).message); }
  };
  return (
    <>
      <PageHeader eyebrow={me ? t("preferences.eyebrow", { user: me.username }) : undefined} title={t("preferences.title")}
        subtitle={t("preferences.subtitle")}
        actions={<>
          <button type="button" className="btn" onClick={() => set(DEFAULT_PREFERENCES)} disabled={JSON.stringify(draft) === JSON.stringify(DEFAULT_PREFERENCES)}>{t("preferences.reset")}</button>
          <button type="button" className="btn primary" onClick={onSave} disabled={!dirty || saving}>{saving ? t("preferences.saving") : t("preferences.save")}</button>
        </>} />
      {saved && !dirty && <Callout tone="volt" title={t("preferences.saved.title")}>{t("preferences.saved.body")}</Callout>}
      {dirty && <Callout tone="warn" title={t("preferences.previewing.title")}>{t("preferences.previewing.body")}</Callout>}
      {error && <div className="error-box" role="alert">{t("preferences.saveFailed", { error })}</div>}
      <div className="grid g-main-side">
        <div className="stack">
          <SectionBoundary id="preferences.language"><LanguagePicker value={draft.language} onChange={(language) => set({ language })} /></SectionBoundary>
          <SectionBoundary id="preferences.theme"><ThemePicker value={draft.theme} onChange={(theme) => set({ theme })} /></SectionBoundary>
          <SectionBoundary id="preferences.accent"><AccentPicker value={draft.accent} onChange={(accent) => set({ accent })} /></SectionBoundary>
          <SectionBoundary id="preferences.size"><TextSize value={draft.size} onChange={(size) => set({ size })} /></SectionBoundary>
          <SectionBoundary id="preferences.defaults"><DisplayDefaults value={draft} onChange={set} /></SectionBoundary>
        </div>
        <SectionBoundary id="preferences.preview"><LivePreview /></SectionBoundary>
      </div>
    </>
  );
}
