/** The subject picker of a flow board (SCALE.md, Flows: a searchable combobox instead of a `<select>` of every subject): typeahead over the
 * subject list the flow already loaded, the last five subjects followed listed first, at most 20 matches shown. Choosing one sets
 * `?subject=` in the URL. Section id `flows.subject`. */
import { useEffect, useId, useState, type KeyboardEvent } from "react";

import { useT } from "../../../i18n";
import { filterSubjects, readRecent } from "../data/subjects";
import type { Subject } from "../data/types";

/** The combobox. `noun` names the subject kind ("package", "model" …). */
export function SubjectPicker({ flowId, noun, subjects, selected, onChoose }: {
  flowId: string; noun: string; subjects: Subject[]; selected: Subject | undefined; onChoose: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const recent = readRecent(flowId);
  const matches = filterSubjects(subjects, text, recent);
  const listId = useId();
  const t = useT();
  useEffect(() => { setActive(0); }, [text]);
  const choose = (s: Subject) => { onChoose(s.id); setText(""); setOpen(false); };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive(Math.min(active + 1, matches.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
    else if (e.key === "Enter" && open && matches[active]) { e.preventDefault(); choose(matches[active]); }
    else if (e.key === "Escape") setOpen(false);
  };
  return (
    <div className="flows-picker" data-section="flows.subject">
      <label className="field inline-field">
        <span className="field-label">{t("flows.picker.follow", { noun })}</span>
        <input type="search" role="combobox" aria-expanded={open} aria-controls={listId} aria-autocomplete="list"
          aria-activedescendant={open && matches[active] ? `${listId}-${active}` : undefined}
          placeholder={selected ? selected.label : t("flows.picker.search", { n: subjects.length, noun })} value={text}
          onChange={(e) => { setText(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)} onKeyDown={onKey} />
      </label>
      {selected && <span className="small muted">{t("flows.picker.following")} <strong>{selected.label}</strong></span>}
      {open && (
        <ul id={listId} role="listbox" className="flows-options" aria-label={t("flows.picker.matches", { noun })}>
          {matches.length === 0 && <li className="muted small">{t("flows.picker.noMatches", { noun })}</li>}
          {matches.map((s, i) => (
            <li key={s.id} id={`${listId}-${i}`} role="option" aria-selected={i === active} className={i === active ? "on" : ""}
              onMouseDown={(e) => { e.preventDefault(); choose(s); }}>
              {s.label}{recent.includes(s.id) && <span className="small muted">{t("flows.picker.recent")}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
