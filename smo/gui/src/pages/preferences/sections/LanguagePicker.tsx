/** Preferences › Language (section `preferences.language`, GUI-6.3): the language of the console's words. Each language is named in itself, so a
 * user who cannot read the current one still finds their own. */
import { Card } from "../../../components/ui";
import { LANGUAGE_NAMES, LANGUAGES, useT, type Language } from "../../../i18n";

/** The language choice. */
export function LanguagePicker({ value, onChange }: { value: Language; onChange: (v: Language) => void }) {
  const t = useT();
  return (
    <Card title={t("preferences.language.title")} sub={t("preferences.language.sub")} section="preferences.language">
      <div className="swatches" role="radiogroup" aria-label={t("preferences.language.title")}>
        {LANGUAGES.map((l) => (
          <button key={l} type="button" role="radio" lang={l} aria-checked={value === l} className={`swatch${value === l ? " on" : ""}`} onClick={() => onChange(l)}>
            {LANGUAGE_NAMES[l]}{value === l ? " ✓" : ""}
          </button>
        ))}
      </div>
      <p className="small muted">{t("preferences.language.note")}</p>
    </Card>
  );
}
