/** The fleet funnel of each flow, built ONLY from the BFF summary's true state counts (SCALE.md P2): a funnel row is a state count that
 * matches a step of the flow (flow 07's instance states, flow 06's package states …). A flow whose steps have no state count (02, 04, 08,
 * 10, 16, 19) has no entry: the box hides with a gap note (a per-step aggregate across all subjects needs a server route, SCALE.md §5). */
import type { SummaryPage } from "../../../data/summary";
import type { TKey } from "../../../i18n";

/** One funnel row: what the subjects at this point are (a catalog key), the summary key that counts them, and a tone for a stuck or failed state. */
export interface FunnelRow { labelKey: TKey; key: string; tone?: "ok" | "warn" | "bad" }
/** A flow's funnel: which summary page serves it, the key of the whole fleet, the plural catalog key naming that many subjects, and its rows. */
export interface FunnelDef { page: SummaryPage; totalKey: string; nounKey: TKey; rows: FunnelRow[] }

/** The funnels, by flow id. */
export const FUNNELS: Record<string, FunnelDef> = {
  "01": { page: "rapps", totalKey: "packages.total", nounKey: "flows.funnel.noun.packages", rows: [
    { labelKey: "flows.funnel.onboardingValidating", key: "packages.ONBOARDING" }, { labelKey: "flows.funnel.validationFailed", key: "packages.FAILED", tone: "bad" },
    { labelKey: "flows.funnel.availableDeployable", key: "packages.AVAILABLE", tone: "ok" }, { labelKey: "flows.funnel.instancesBootstrapping", key: "instances.DEPLOYING", tone: "warn" },
    { labelKey: "flows.funnel.instancesRunning", key: "instances.RUNNING", tone: "ok" }] },
  "06": { page: "rapps", totalKey: "packages.total", nounKey: "flows.funnel.noun.packages", rows: [
    { labelKey: "flows.funnel.onboarding", key: "packages.ONBOARDING" }, { labelKey: "flows.funnel.available", key: "packages.AVAILABLE", tone: "ok" },
    { labelKey: "flows.funnel.primed", key: "packages.PRIMED", tone: "ok" }, { labelKey: "flows.funnel.deprecated", key: "packages.DEPRECATED" }, { labelKey: "flows.funnel.failed", key: "packages.FAILED", tone: "bad" }] },
  "07": { page: "rapps", totalKey: "instances.total", nounKey: "flows.funnel.noun.instances", rows: [
    { labelKey: "flows.funnel.deployingBootstrap", key: "instances.DEPLOYING", tone: "warn" }, { labelKey: "flows.funnel.running", key: "instances.RUNNING", tone: "ok" },
    { labelKey: "flows.funnel.faultedRecover", key: "instances.FAULTED", tone: "bad" }, { labelKey: "flows.funnel.upgrading", key: "instances.UPGRADING", tone: "warn" },
    { labelKey: "flows.funnel.terminated", key: "instances.UNDEPLOYED" }] },
  "03": { page: "configuration", totalKey: "configJobs.total", nounKey: "flows.funnel.noun.configJobs", rows: [
    { labelKey: "flows.funnel.pending", key: "configJobs.PENDING" }, { labelKey: "flows.funnel.processing", key: "configJobs.PROCESSING" }, { labelKey: "flows.funnel.halted", key: "configJobs.HALTED", tone: "warn" },
    { labelKey: "flows.funnel.completed", key: "configJobs.COMPLETED", tone: "ok" }, { labelKey: "flows.funnel.partialSuccess", key: "configJobs.PARTIAL_SUCCESS", tone: "warn" },
    { labelKey: "flows.funnel.failed", key: "configJobs.FAILED", tone: "bad" }] },
  "09": { page: "intents", totalKey: "intents.total", nounKey: "flows.funnel.noun.intents", rows: [
    { labelKey: "flows.funnel.activated", key: "intents.ACTIVATED", tone: "ok" }, { labelKey: "flows.funnel.deactivated", key: "intents.DEACTIVATED" }] },
  "15": { page: "infrastructure", totalKey: "deployments.total", nounKey: "flows.funnel.noun.deployments", rows: [
    { labelKey: "flows.funnel.instantiating", key: "deployments.INSTANTIATING" }, { labelKey: "flows.funnel.running", key: "deployments.RUNNING", tone: "ok" },
    { labelKey: "flows.funnel.updatingScale", key: "deployments.UPDATING" }, { labelKey: "flows.funnel.abnormalHeal", key: "deployments.ABNORMAL", tone: "bad" },
    { labelKey: "flows.funnel.terminating", key: "deployments.TERMINATING", tone: "warn" }] },
};
