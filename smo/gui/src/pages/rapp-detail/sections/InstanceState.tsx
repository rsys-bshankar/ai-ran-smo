/** The rApp detail "Lifecycle" box: the instance as rApp Management holds it (state, package, NFO deployment, autonomy, region and access
 * scope) with the lifecycle actions, Upgrade included. Section id `rapp.instance`. */
import { useState } from "react";

import { Card, Json, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { QueryState } from "../../../kit/states";
import { describeScope } from "../../../lib/domain";
import { InstanceActions } from "../../rapps/sections/InstanceActions";
import { UpgradeModal } from "../../rapps/sections/UpgradeModal";
import { useInstance } from "../data/queries";

/** The box of instance `id`. */
export function InstanceState({ id }: { id: string }) {
  const t = useT();
  const inst = useInstance(id);
  const [upgrading, setUpgrading] = useState(false);
  const d = inst.data;
  return (
    <Card section="rapp.instance" title={t("rappDetail.instance.title")} actions={d ? <InstanceActions inst={d} withUpgrade={() => setUpgrading(true)} /> : undefined}>
      <QueryState q={inst} isEmpty={() => false}>
        {d && <KeyValue items={[
          [t("rappDetail.instance.state"), <StateBadge key="s" state={d.state} />], [t("rappDetail.instance.package"), <code key="p">{d.packageId}</code>],
          [t("rappDetail.instance.nfo"), d.workloadRef && <code key="w">{d.workloadRef}</code>],
          [t("rappDetail.instance.pendingUpgrade"), d.pendingUpgradeInstanceId && <code key="u">{d.pendingUpgradeInstanceId}</code>],
          [t("rappDetail.instance.autonomy"), <StateBadge key="a" state={d.autonomyMode} />],
          [t("rappDetail.instance.regionScope"), d.regionScope ? <Json value={d.regionScope} /> : <span className="muted">—</span>],
          [t("rappDetail.instance.accessScope"), <span key="z" title={t("rappDetail.instance.accessScopeTitle")}>{describeScope(d.authzScope, t)}</span>],
        ]} />}
      </QueryState>
      {upgrading && d && <UpgradeModal inst={d} onClose={() => setUpgrading(false)} />}
    </Card>
  );
}
