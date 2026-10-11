/** The rApp directory (PR-GUI-8, GUI-8.4, 8.5): every rApp instance in one searchable list served by the BFF (`/api/rapps`: search, state, owner,
 * has-a-page and pinned filters, server paging). Each row opens the rApp's own page (/rapps/<instance>); the star pins it to the sidebar (at most
 * MAX_PINS per user, kept by the GUI backend). What a rApp's page shows is declared in its package, so a rApp onboarded at run time is in this
 * list and has its page without a GUI build. Section id `rapps.directory`; scripts/gui_rapp_pages_e2e.py reads its markup. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { MAX_PINS, usePinToggle, usePins, useRappDirectory, type RappSummary } from "../../../api/rapps";
import { Card, DataTable, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Pager } from "../../../kit/Pager";

/** The default page size. */
const PAGE = 25;

/**
 * Every rApp instance in one searchable directory (PR-GUI-8, GUI-8.4, 8.5). Each row opens the rApp's own page (/rapps/<instance>); the star pins it to the sidebar (at most `MAX_PINS` per user, kept by the GUI backend, so the
 * star of an unpinned row is disabled at the limit). What a rApp's page shows is declared in its package, so a rApp onboarded at run time is in this list and has its page without a GUI build.
 * The search text is sent 250 ms after the last keystroke, and changing any filter returns to the first page.
 */
export function RappDirectory() {
  const t = useT();
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [state, setState] = useState("");
  const [owner, setOwner] = useState("");
  const [page, setPage] = useState<"" | "true" | "false">("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => { const timer = setTimeout(() => { setSearch(text.trim()); setOffset(0); }, 250); return () => clearTimeout(timer); }, [text]);
  const directory = useRappDirectory({ search, state, owner, hasPage: page === "" ? "" : page === "true", pinned: pinnedOnly ? true : "", limit, offset });
  const pins = usePins();
  const toggle = usePinToggle();
  const full = (pins.data?.items.length ?? 0) >= MAX_PINS;
  const data = directory.data;
  const reset = <T,>(set: (v: T) => void) => (v: T) => { set(v); setOffset(0); };
  return (
    <Card section="rapps.directory" title={t("rapps.directory.title")} sub={t("rapps.directory.sub")}
      actions={<span className="muted small">{data ? t("rapps.directory.count", { count: data.total }) : ""}</span>}>
      <div className="row gap wrap filters" role="search">
        <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder={t("rapps.directory.search")} aria-label={t("rapps.directory.search.aria")} />
        <select value={state} onChange={(e) => reset(setState)(e.target.value)} aria-label={t("rapps.directory.state.aria")}>
          <option value="">{t("rapps.directory.state.all")}</option>{(data?.states ?? []).map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={owner} onChange={(e) => reset(setOwner)(e.target.value)} aria-label={t("rapps.directory.owner.aria")}>
          <option value="">{t("rapps.directory.owner.all")}</option>{(data?.owners ?? []).map((o) => <option key={o}>{o}</option>)}
        </select>
        <select value={page} onChange={(e) => reset(setPage)(e.target.value as "" | "true" | "false")} aria-label={t("rapps.directory.page.aria")}>
          <option value="">{t("rapps.directory.page.any")}</option><option value="true">{t("rapps.directory.page.declares")}</option><option value="false">{t("rapps.directory.page.overview")}</option>
        </select>
        <label className="row gap small"><input type="checkbox" checked={pinnedOnly} onChange={(e) => reset(setPinnedOnly)(e.target.checked)} /> {t("rapps.directory.pinnedOnly")}</label>
      </div>
      <DataTable<RappSummary> rows={data?.items} loading={directory.isLoading} error={directory.error} rowKey={(r) => r.instanceId}
        empty={search || state || owner || page || pinnedOnly ? t("rapps.directory.empty.filtered") : t("rapps.directory.empty")}
        columns={[
          { header: t("rapps.directory.col.rapp"), render: (r) => <><Link to={`/rapps/${r.instanceId}`}><strong>{r.name ?? t("rapps.directory.unknownPackage")}</strong></Link> <span className="muted">{r.version ?? ""}</span></> },
          { header: t("rapps.directory.col.owner"), render: (r) => r.vendor ?? <span className="muted">—</span> },
          { header: t("rapps.directory.col.instance"), render: (r) => <Id value={r.instanceId} /> },
          { header: t("rapps.directory.col.state"), render: (r) => <StateBadge state={r.state} /> },
          { header: t("rapps.directory.col.autonomy"), render: (r) => <StateBadge state={r.autonomyMode} /> },
          { header: t("rapps.directory.col.page"), render: (r) => (r.hasPage ? (r.operatorApiRegistered ? t("rapps.directory.page.declared") : <span className="muted">{t("rapps.directory.page.noApi")}</span>) : <span className="muted">{t("rapps.directory.page.overviewOnly")}</span>) },
          { header: "", className: "actions", render: (r) => (
            <button type="button" className="btn ghost small" aria-pressed={r.pinned} disabled={toggle.isPending || (!r.pinned && full)}
              title={r.pinned ? t("rapps.directory.unpin") : full ? t("rapps.directory.pinFull", { max: MAX_PINS }) : t("rapps.directory.pin")}
              aria-label={t(r.pinned ? "rapps.directory.unpin.aria" : "rapps.directory.pin.aria", { name: r.name ?? r.instanceId })}
              onClick={() => toggle.mutate({ instanceId: r.instanceId, pin: !r.pinned })}>{r.pinned ? "★" : "☆"}</button>) },
        ]} />
      {data && <Pager offset={offset} limit={limit} shown={data.items.length} total={data.total} onOffset={setOffset} onLimit={(l) => { setLimit(l); setOffset(0); }} />}
      <p className="gap-note">{t("rapps.directory.gap")}</p>
    </Card>
  );
}
