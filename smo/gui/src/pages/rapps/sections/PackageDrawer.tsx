/** The package drawer: identity, ASD descriptor, SME registration, artifacts, priming and the usage registrations that guard delete and
 * deprime (call flow 06). Opened by a row click in `PackagesTable`. */
import { Link } from "react-router-dom";

import type { Package } from "../../../api/types";
import { ActionButton, DataTable, Drawer, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { ENGLISH, formatTime } from "../../../lib/domain";
import { packageBase, usePackageArtifacts, usePackageUsage } from "../data/queries";

/** What Onboarding reports about a package's signature (GUI-10.2). Onboarding serves only `signatureVerified`: true when the CSAR's signature
 * verified against the publisher keys of its trust store (`ONBOARDING_TRUST_STORE`), false when it was accepted without that (unsigned, with no
 * signature required); it serves no trust-store mode and no signer, so neither is claimed here. */
export function signatureText(verified: boolean | null | undefined, t: Translator = ENGLISH): string {
  if (verified === true) return t("rapps.package.signature.verified");
  if (verified === false) return t("rapps.package.signature.unsigned");
  return t("rapps.package.signature.unknown");
}

/** The drawer of `pkg`. */
export function PackageDrawer({ pkg, onClose }: { pkg: Package; onClose: () => void }) {
  const t = useT();
  const base = packageBase(pkg.packageId);
  const artifacts = usePackageArtifacts(pkg.packageId);
  const usage = usePackageUsage(pkg.packageId);
  const active = (usage.data ?? []).filter((u) => u.active);
  return (
    <Drawer title={`${pkg.name} ${pkg.version}`} onClose={onClose}>
      <div className="row between"><StateBadge state={pkg.state} /><Link className="btn small" to={`/flows/06?subject=${pkg.packageId}`}>{t("rapps.package.flow")}</Link></div>
      <KeyValue items={[
        [t("rapps.package.id"), <code>{pkg.packageId}</code>], [t("rapps.package.vendorType"), `${pkg.vendor ?? "—"} / ${pkg.applicationType}`],
        [t("rapps.package.tosca"), pkg.toscaEntryDefinitions], [t("rapps.package.signature"), signatureText(pkg.signatureVerified, t)],
        [t("rapps.package.nfDescriptor"), pkg.nfDeploymentDescriptorId && <code>{pkg.nfDeploymentDescriptorId}</code>],
        [t("rapps.package.instances"), usage.data ? String(active.length) : "…"],
        [t("rapps.package.ai"), pkg.aiCapabilities ? <code className="small">{JSON.stringify(pkg.aiCapabilities)}</code> : <span className="muted">{t("rapps.package.ai.none")}</span>],
      ]} />
      <h3>{t("rapps.package.asd")}</h3>
      <KeyValue items={[
        [t("rapps.package.asd.id"), pkg.descriptorId ? <code className="small">{pkg.descriptorId}</code> : <span className="muted">{t("rapps.package.asd.none")}</span>],
        [t("rapps.package.asd.invariant"), pkg.descriptorInvariantId ? <code className="small">{pkg.descriptorInvariantId}</code> : <span className="muted">{t("rapps.package.asd.none")}</span>],
        [t("rapps.package.asd.version"), pkg.descriptorVersion ?? <span className="muted">{t("rapps.package.asd.none")}</span>],
        [t("rapps.package.asd.schema"), pkg.schemaVersion ?? <span className="muted">{t("rapps.package.asd.none")}</span>],
      ]} />
      <h3>{t("rapps.package.sme")}</h3>
      <p className="muted small">
        {pkg.smeDeclarations
          ? t("rapps.package.sme.declares", { providers: pkg.smeDeclarations.providers.length, apis: pkg.smeDeclarations.serviceApis.length })
          : t("rapps.package.sme.none")}
      </p>
      <h3>{t("rapps.package.artifacts")}</h3>
      <DataTable rows={artifacts.data} error={artifacts.error} rowKey={(a) => a.artifactId} empty={t("rapps.package.artifacts.none")} columns={[
        { header: t("rapps.package.artifacts.path"), render: (a) => <code className="small">{a.path}</code> }, { header: t("rapps.package.artifacts.url"), render: (a) => <code className="small clip">{a.accessUrl}</code> },
      ]} />
      <h3>{t("rapps.package.priming")}</h3>
      <p className="muted small">
        {pkg.state === "PRIMED"
          ? active.length
            ? t("rapps.package.priming.blocked", { n: active.length })
            : t("rapps.package.priming.free")
          : pkg.state === "AVAILABLE"
            ? t("rapps.package.priming.available")
            : t("rapps.package.priming.other", { state: pkg.state })}
      </p>
      {(pkg.state === "AVAILABLE" || pkg.state === "PRIMED") && (
        <div className="row gap">
          {pkg.state === "AVAILABLE"
            ? <ActionButton label={t("rapps.package.prime")} action={{ method: "POST", path: `${base}/prime`, success: t("rapps.package.prime.done") }} />
            : <ActionButton label={t("rapps.package.deprime")} disabled={active.length > 0} title={active.length ? t("rapps.package.deprime.blocked") : undefined} action={{ method: "POST", path: `${base}/deprime`, success: t("rapps.package.deprime.done") }} />}
        </div>
      )}
      <h3>{t("rapps.package.usage")}</h3>
      <p className="muted small">{active.length ? t("rapps.package.usage.active", { n: active.length }) : t("rapps.package.usage.free")}</p>
      <DataTable rows={usage.data} error={usage.error} rowKey={(u) => u.registrationId} empty={t("rapps.package.usage.none")} columns={[
        { header: t("rapps.package.usage.consumer"), render: (u) => <Id value={u.consumerId} /> },
        { header: t("rapps.package.usage.state"), render: (u) => u.active ? <StateBadge state="ACTIVE" /> : <>{t("rapps.package.usage.stopped")} {formatTime(u.stoppedAt)}</> },
        // call flow 06: rApp Management opens and closes registrations; an admin may only release an orphan (Onboarding refuses one an instance still holds)
        { header: "", className: "actions", render: (u) => u.active && <ActionButton label={t("rapps.package.usage.release")} title={t("rapps.package.usage.release.title")}
          action={{ method: "POST", path: `${base}/usage/${u.registrationId}/release`, success: t("rapps.package.usage.release.done") }} /> },
      ]} />
    </Drawer>
  );
}
