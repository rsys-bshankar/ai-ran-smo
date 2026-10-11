/** Sections `aiml.repositories`, `aiml.storages` and `aiml.artifactVersions` (Registry tab, feature 8): MLMR's TS 28.105 model repositories
 * (label, model and group counts), its model storages (profiles, addresses, supported features) and every model's artifact versions as download
 * links (`GET /mlmr/models/{id}/artifact/{version}` through the BFF). All three are server-paged and read-only: the BFF exposes no write on
 * repositories or storages (README, Known limits). MLMR serves no list of a model's artifact versions; they are counted from its
 * `artifactLocation` (`model-artifact:<model id>:<latest version>`, data/board.ts `ARTIFACT_LOCATION_RE`). */
import type { Model } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { artifactVersions } from "../data/board";
import { MODELS, REPOSITORIES, STORAGES, artifactHref } from "../data/queries";
import type { ModelRepository, ModelStorage } from "../data/types";

/** Model repositories. */
export function Repositories() {
  const t = useT();
  return (
    <Card section="aiml.repositories" title={t("aiml.repos.title")} sub={t("aiml.repos.sub")}>
      <ServerTable<ModelRepository> path={REPOSITORIES} rowKey={(r) => r.id} empty={t("aiml.repos.empty")} columns={[
        { header: t("aiml.col.repository"), render: (r) => <Id value={r.id} /> },
        { header: t("aiml.col.label"), render: (r) => r.attributes.userLabel ?? <span className="muted">—</span> },
        { header: t("aiml.col.models"), render: (r) => <span className="num">{r.MLModel.length}</span> },
        { header: t("aiml.col.coordinationGroups"), render: (r) => <span className="num">{r.MLModelCoordinationGroup.length}</span> },
      ]} />
    </Card>
  );
}

/** Model storages. */
export function Storages() {
  const t = useT();
  return (
    <Card section="aiml.storages" title={t("aiml.storages.title")} sub={t("aiml.storages.sub")}>
      <ServerTable<ModelStorage> path={STORAGES} rowKey={(s) => s.storageId} empty={t("aiml.storages.empty")} columns={[
        { header: t("aiml.col.storage"), render: (s) => <Id value={s.storageId} /> },
        { header: t("aiml.col.modelProfiles"), render: (s) => <span className="num">{s.mlModels?.length ?? 0}</span> },
        { header: t("aiml.col.addresses"), render: (s) => s.mlModelsAddresses?.length ? <code className="small">{s.mlModelsAddresses.join(", ")}</code> : <span className="muted">—</span> },
        { header: t("aiml.col.suppFeat"), render: (s) => s.suppFeat ?? <span className="muted">—</span> },
      ]} />
    </Card>
  );
}

/** Artifact versions of every model. */
export function ArtifactVersions() {
  const t = useT();
  return (
    <Card section="aiml.artifactVersions" title={t("aiml.versions.title")} sub={t("aiml.versions.sub")}>
      <ServerTable<Model> path={MODELS} rowKey={(m) => m.modelId} empty={t("aiml.table.empty")} columns={[
        { header: t("aiml.col.model"), render: (m) => <><strong>{m.modelType}</strong> <span className="muted">v{m.version}</span></> },
        { header: t("aiml.col.location"), render: (m) => m.artifactLocation ? <code className="small">{m.artifactLocation}</code> : <span className="muted">—</span> },
        { header: t("aiml.col.versions"), render: (m) => {
          const versions = artifactVersions(m.artifactLocation, m.modelId);
          return versions.length === 0 ? <span className="muted">{t("aiml.none")}</span>
            : <span className="row wrap">{versions.slice(0, 10).map((v) => <a key={v} className="small" href={artifactHref(m.modelId, v)} download>v{v}</a>)}{versions.length > 10 && <span className="xs muted">{t("aiml.versions.older", { n: versions.length - 10 })}</span>}</span>;
        } },
      ]} />
    </Card>
  );
}
