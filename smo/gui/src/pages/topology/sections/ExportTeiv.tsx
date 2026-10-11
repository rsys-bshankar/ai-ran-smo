/** RAN topology · TEIV export button (in the page header): downloads `GET /ran-nf-oam/topology` (the containment tree in the TEIV wire shape,
 * of the focused element when one is in focus) as a JSON file. A read, so every role may use it. */
import { useState } from "react";

import { smo } from "../../../api/client";
import { useT } from "../../../i18n";
import { TOPOLOGY_EXPORT_PATH, useTopologyParams } from "../data/queries";

/** The button; the file is named after the element in focus, or `ran-topology.json`. */
export function ExportTeiv() {
  const t = useT();
  const { me } = useTopologyParams();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = await smo<unknown>(TOPOLOGY_EXPORT_PATH, { query: me ? { managed_element_ref: me } : undefined });
      const url = URL.createObjectURL(new Blob([JSON.stringify(body, null, 2)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = me ? `ran-topology-${me}.json` : "ran-topology.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="row" data-section="topology.export">
      <button type="button" className="btn" onClick={() => void run()} disabled={busy}>{busy ? t("topology.export.busy") : t("topology.export.button")}</button>
      {error && <span className="small t-bad" role="alert">{error}</span>}
    </span>
  );
}
