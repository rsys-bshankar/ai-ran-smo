/**
 * The TypeScript shapes of what the BFF and the SMO modules answer, written by hand from each module's own view functions
 * (smo/<module>/app/main.py); `docs/openapi/<module>.json` is the contract and wins on any difference.
 *
 * Field names are the camelCase the modules send. A field marked `| null` or `?` can be absent on an older module build, so
 * pages must not assume it. Nothing here is generated, so a module change that adds a field needs the type changed by hand.
 */

// local / totpEnrolled / mfaEnrolmentRequired: PR-SEC-7 (a user of the identity provider is not local and has no one-time code here;
// mfaEnrolmentRequired is true for a local admin who must enrol one before anything else, GUI_ADMIN_MFA_REQUIRED)
/** GUI-5: a console user's region/tenant scope as the BFF shows it: the claim, null when unscoped, "INVALID" for a stored claim that no longer reads (it permits nothing). */
export type UserScope = AuthzScope | "INVALID" | null;
export interface Me { username: string; role: "viewer" | "operator" | "admin"; csrfToken?: string; local?: boolean; totpEnrolled?: boolean; mfaEnrolmentRequired?: boolean; scope?: UserScope }
/** `GET /api/me/totp`: whether one-time codes are available and set up for the signed-in user, and the state of the recovery codes. */
export interface TotpStatus {
  available: boolean; enrolled: boolean; pending: boolean; recoveryCodesLeft: number; reason?: string;
  // GUI-9.8: one entry per recovery-code slot (1..10) saying whether it was used and when; never the codes themselves (absent on an older BFF)
  recoveryCodes?: { slot: number; used: boolean; usedAt: string | null }[];
}
export interface TotpBegin { secret: string; otpauthUri: string; issuer: string; account: string }
export interface TotpConfirmed { status: string; recoveryCodes: string[]; recoveryCodesLeft: number }

/** One module's health row from the BFF status call. `ready`, `version`, `buildSha` and `builtAt` (PR-OBS-8.2) are null when the module could not be asked (down, or an older build with no /version). */
export interface ModuleStatus {
  module: string; healthy: boolean; latencyMs: number; statusCode: number | null; error: string | null;
  ready: boolean | null; version: string | null; buildSha: string | null; builtAt: string | null;
}
export interface ModulesStatus { checkedAt: string; modules: ModuleStatus[] }

// ---- Onboarding / rApp Management
/**
 * An onboarded rApp package (rApp Management / Onboarding): identity, lifecycle state, whether its signature verified, the NF deployment descriptor it carries and the ASD and SME declarations read from the CSAR.
 */
export interface Package {
  packageId: string; name: string; version: string; vendor: string | null; applicationType: string;
  state: string; toscaEntryDefinitions: string | null; signatureVerified: boolean; nfDeploymentDescriptorId: string | null;
  aiCapabilities: Record<string, unknown> | null;
  // Real ASD schema fields (asd_types.yaml's tosca.nodes.asd node type,
  // grounded against nonrtric-plt-rappmanager's own sample CSARs) — null
  // for a package whose ASD doesn't declare them (e.g. built before this
  // pass, or a synthetic test fixture).
  descriptorId: string | null; descriptorInvariantId: string | null;
  descriptorVersion: string | null; schemaVersion: string | null;
  // Real CSAR-bundled Files/Sme/providers + Files/Sme/serviceapis
  // declarations (HISTORY.md §7's Onboarding/rApp Mgmt finding 3) — null
  // for a package whose CSAR declares neither directory. Registered per
  // instance at bootstrap-complete, not here at onboarding time.
  smeDeclarations: { providers: Record<string, unknown>[]; serviceApis: Record<string, unknown>[] } | null;
}
/** PR-SEC-10: which managed elements, by region and tenant, a caller (an rApp instance) may touch. A key that is left out does not restrict that axis; no claim at all is unscoped. */
export interface AuthzScope { regions?: string[]; tenants?: string[] }
/** An rApp instance as the list shows it: its package, state and autonomy mode (fixed at onboarding, HISTORY.md OI-6.3) and its authorization scope (PR-SEC-10.3). */
export interface InstanceSummary {
  instanceId: string; packageId: string; state: string;
  // HISTORY.md OI-6.3: fixed at onboarding (CreateInstance), SHADOW by default.
  autonomyMode: string;
  // PR-SEC-10.3: the scope claim the instance was created with; null or absent: unscoped (it may touch every managed element, as before).
  authzScope?: AuthzScope | null;
}
/**
 * One rApp instance in full: the summary plus its workload reference, configuration, pending upgrade, the SME service ids it registered, its autonomous region scope and the outcome of its last teardown.
 */
export interface Instance extends InstanceSummary {
  workloadRef: string | null; configuration: Record<string, unknown> | null; pendingUpgradeInstanceId: string | null;
  // The real SME serviceId(s) this instance registered at bootstrap-complete
  // from its package's own smeDeclarations — null if the package declared
  // none, or before bootstrap-complete has run.
  smeServiceIds: string[] | null;
  // AUTONOMOUS's own pre-configured RAN node/cell/slice scope — opaque,
  // meaningless for ASSIST/SHADOW.
  regionScope: Record<string, unknown> | null;
  // Outcome of the most recent best-effort teardown (NFO terminate,
  // usage/stop) this row performed — on TERMINATE — or inherited through an
  // upgrade commit/rollback/timeout. Each step: DONE, SKIPPED…, or FAILED: …
  lastTeardown?: InstanceTeardown | null;
}
/**
 * The outcome of an instance's last best-effort teardown (NFO terminate, usage stop) and why it ran (terminate or an upgrade commit, rollback or timeout); each step reads DONE, SKIPPED or FAILED with a reason.
 */
export interface InstanceTeardown {
  instanceId: string; reason: "TERMINATE" | "UPGRADE_COMMIT" | "UPGRADE_ROLLBACK" | "UPGRADE_TIMEOUT" | string;
  nfoTerminate: string; usageStop: string; at: string;
}
/** One entry of rApp Management's version history (OI-1-sa-rollback): a committed upgrade or rollback, with what the retired instance ran. */
export interface InstanceVersion {
  versionId: string; kind: "UPGRADE" | "ROLLBACK"; instanceId: string; packageId: string;
  previousInstanceId: string; previousPackageId: string; previousConfiguration: Record<string, unknown> | null;
  rolledBackByVersionId: string | null; committedAt: string;
}
/**
 * An instance's version history: its rollback target (when there is one) and every committed upgrade or rollback.
 */
export interface InstanceVersions {
  instanceId: string; packageId: string; state: string; workloadRef: string | null;
  rollbackTarget: InstanceVersion | null; versions: InstanceVersion[];
}
export interface PerfReport { reportId: string; metrics: Record<string, unknown>; reportedAt: string }
export interface FaultReport { faultId: string; severity: string; description: string | null; reportedAt: string }

// ---- AI/ML Workflow
/**
 * A registered AI/ML model as MLMR stores it (identity, type, version, artifact location, data types and target environments); the lifecycle state lives in `ModelLifecycle`.
 */
export interface Model {
  modelId: string; modelType: string; version: string;
  artifactLocation: string | null; description: string | null; author: string | null; owner: string | null;
  inputDataType: string | null; outputDataType: string | null; targetEnvironments: Record<string, unknown>[];
}
/** AIMgF's own model_lifecycle row: the model and runtime states, the training job, the cleared node groups, the serving deployment and the two approvals. MLMR's `Model` carries none of this (docs/ARCHITECTURE.md, MLMR). */
export interface ModelLifecycle {
  modelId: string; modelLifecycleState: string; runtimeLifecycleState: string; trainingJobId: string | null;
  clearedNodeGroups: string[]; nfDeploymentDescriptorId: string | null; nfDeploymentId: string | null;
  trainingApproved: boolean; validationApproved: boolean;
}
/** One certification decision on a model: who decided, the decision and the rationale. */
export interface CertificationRecord {
  certificationRecordId: string; modelId: string; decision: string; decidedBy: string; rationale: string | null; decidedAt: string;
}
/**
 * A training job at AIMgF: its model or coordination group, producer, status and datasets, the metrics it reported, the NFO deployment that runs it (OI-6.2) and the furthest step reached with each step's status (OI-5-aiml-trainingjob-steps), and the step and epoch progress its runtime reported (GUI-9.8).
 */
export interface TrainingJob {
  trainingJobId: string; modelId: string | null; modelCoordinationGroupId: string | null; producerId: string;
  status: string; runId: string | null; trainingDataset: string | null; validationDataset: string | null;
  modelMetrics: Record<string, unknown> | null;
  // HISTORY.md OI-6.2: MLTF's own real NFO-backed execution
  // runtime — set on request, cleared once the run completes.
  nfDeploymentId: string | null;
  // OI-5-aiml-trainingjob-steps: the furthest step reported, and each
  // step's status (derived from it and `status`)
  currentStep: "DATA_EXTRACTION" | "TRAINING" | "TRAINED_MODEL";
  steps: Record<"DATA_EXTRACTION" | "TRAINING" | "TRAINED_MODEL", string>;
  // GUI-9.8: how far the run is, as its runtime reported it, and the estimated seconds left (null when not computable); absent on an older AIMgF
  epoch?: number | null; totalEpochs?: number | null; etaSeconds?: number | null; progressUpdatedAt?: string | null;
}
/**
 * An inference job: the model it serves, its status, where results are notified and a reference to the model's already-live serving deployment (OI-6.2), not a new deployment of its own.
 */
export interface InferenceJob {
  inferenceJobId: string; modelId: string; status: string; notificationDestination: string | null;
  // HISTORY.md OI-6.2: a reference to the model's own already-live
  // serving deployment (ModelLifecycle.nfDeploymentId) — not a new NFO
  // deployment of this job's own.
  nfDeploymentId: string | null;
}
/**
 * A model coordination group: the member models and use cases, the shared feature pipeline and how a retrain propagates to the members.
 */
export interface CoordinationGroup {
  groupId: string; groupType: string; memberModelIds: string[]; memberUseCases: string[];
  sharedFeaturePipelineRef: string | null; retrainPropagation: string;
}
export interface MlmfSubscription { subscriptionId: string; modelId: string; metricTypes: string[]; dmeTypeId: string; guardKpiFloor: Record<string, number> | null; notificationDestination: string | null }
export interface MlmfReport { reportId: string; subscriptionId: string; metrics: Record<string, unknown>; breachedFloor: boolean; reportedAt: string }

// ---- RAN NF OAM
/**
 * A RAN alarm as RAN NF OAM lists it: the managed element and, since W10-alarm-cellref, the cell it is about (null: the whole element), severity, acknowledgement state, correlation and the clear details.
 */
export interface Alarm {
  alarmId: string; sourceAlarmId: string; managedElementRef: string; severity: string; ackState: string;
  managedFunctionRef: string | null;  // W10-alarm-cellref: the cell it is about; null = the whole element
  raisedAt: string | null; correlationGroup: string | null; probableCause: string | null; specificProblem: string | null;
  rootCauseIndicator: boolean; correlatedNotifications: string[]; proposedRepairActions: string | null;
  alarmType: string | null; ackUserId: string | null; changedAt: string | null; clearedAt: string | null; clearUserId: string | null;
  // PR-GUI-9.8: when it was acknowledged (null while unacknowledged; absent from an older backend) and cleared (`clearedAt` again)
  ackTime?: string | null; clearTime?: string | null;
}
export interface PmSubscription { subscriptionId: string; managedElementRef: string; counterType: string; deliveryMethod: string; southboundEngine: string; granularityPeriod: number | null }
export interface FmSubscription { subscriptionId: string; managedElementRef: string; deliveryMethod: string; southboundEngine: string }
/**
 * A registered O1 endpoint: the managed element, the adaptor URI, supported protocols, health, and (PR-SEC-10.2) the region and tenant that decide which scoped callers may touch it; unset means unscoped callers only.
 */
export interface O1Endpoint {
  endpointId: string; managedElementRef: string; adaptorUri: string; protocolSupport: string[]; registeredVia: string; healthStatus: string; lastHeartbeatAt: string | null;
  // PR-SEC-10.2: where the element is and whom it belongs to; null or absent: not set (such an element is for unscoped callers only).
  region?: string | null; tenant?: string | null;
}
/** MGT-4: the change window a job was asked for and the approval it waited for (all null for a job that never waited). */
export interface ChangeWindowFields {
  scheduledAt?: string | null; windowEnd?: string | null; decidedBy?: string | null; decidedAt?: string | null; decisionReason?: string | null;
}
export interface ConfigJobSummary extends ChangeWindowFields { jobId: string; requestedBy: string; scope: string; status: string; msacRole: string | null; createdAt?: string | null }
/**
 * The KPI guard of a config job: the KPI to watch, the baseline and observation windows in minutes, the regression threshold and its direction, the minimum sample count and whether a regression rolls the job back.
 */
export interface KpiGuardSettings {
  kpi: string; baselineMinutes: number; observationMinutes: number; maxRegressionPercent: number; direction: "higher" | "lower"; minSamples: number;
  revert: boolean; msacRole: string | null;
}
/**
 * The KPI guard's verdict (OK, REGRESSED, INSUFFICIENT_DATA or ERROR) with, per element, the baseline, the observed value and the change; `reverted` and `revertJobId` say whether a rollback job was started.
 */
export interface KpiGuardResult {
  verdict: "OK" | "REGRESSED" | "INSUFFICIENT_DATA" | "ERROR"; reverted?: boolean; revertJobId?: string | null; error?: string; checkedAt?: string;
  elements?: { managedElementRef: string; verdict: string; baseline: number | null; observed: number | null; changePercent: number | null }[];
}
/**
 * One element-level change of a config job: the operation, its status, and the rejection reason and detail when it was refused, with its wave and attempt count.
 */
export interface ConfigSubChange {
  managedElementRef: string; managedFunctionRef?: string | null; operation: string; status: string; rejectionReason: string | null;
  rejectionDetail?: string | null; wave?: number; attempts?: number;
}
/**
 * A CM write job in full: status, the wave plan and progress, why it is halted, its KPI guard and result, and its sub-changes. Most fields are optional because the list rows and older builds send fewer.
 */
export interface ConfigJob extends ChangeWindowFields {
  jobId: string; status: string; requestedBy?: string; rollbackOf?: string | null; rollbackForced?: boolean;
  waveSize?: number | null; waveCount?: number; currentWave?: number; wavePauseSeconds?: number; onGateFailure?: string; gateMaxNewAlarms?: number;
  haltedReason?: string | null; haltedDetail?: string | null; nextWaveAt?: string | null;
  kpiGuard?: KpiGuardSettings | null; kpiGuardResult?: KpiGuardResult | null; kpiGuardCheckedAt?: string | null;
  subChanges: ConfigSubChange[];
}
/** POST /config-jobs/{id}/rollback with dryRun: what would be written, and what has changed since the job wrote it. */
export interface RollbackPreview {
  dryRun: true; rollbackOf: string; status: "VALIDATED" | "CHANGED_SINCE"; changes: unknown[];
  changedSince: { managedElementRef: string; managedFunctionRef: string | null; attribute: string; expected: unknown; actual: unknown }[];
}
export interface KpiCounterSpec { counter: string; variable?: string | null; aggregation: "sum" | "avg" | "min" | "max" | "last" | "count" }
export interface KpiDef { name: string; formula: string; counters: KpiCounterSpec[]; unit: string | null; description: string | null }
/**
 * A scheduled KPI computation: the KPI, the interval and lookback in seconds, the grouping, the optional element or cell, and when it last ran and with what status.
 */
export interface KpiScheduleRow {
  scheduleId: string; kpi: string; intervalSeconds: number; lookbackSeconds: number; groupBy: string; managedElementRef: string | null; cellId: string | null;
  enabled: boolean; lastRunAt: string | null; lastStatus: "OK" | "ERROR" | null; lastDetail: string | null; nextRunAt: string | null;
}
export interface SwmJob { jobId: string; managedElementRef: string; ruInstanceId: string | null; phase: string; status: string }

// ---- Intent Service
/** A TS 28.312 intent as Intent Service lists it: summary keys plus the full intent under `attributes`. */
export interface Intent {
  intentId: string; intentAdminState: string; intentPriority: number; rmioId: string; intentMgmtPurpose: string | null; rmihId: string; userLabel: string | null; attributes: Record<string, unknown>;
  // GUI-9.8: from the newest fulfilment / conflict reports (null until a fulfilment report exists; absent on an older Intent Service)
  fulfilmentPercent?: number | null; fulfilled?: boolean | null; inConflict?: boolean;
}
/** Wave 6: a TS 28.312 IntentReport — every report kind lives under `attributes` (intentFulfilmentReport, intentConflictReports, ...). */
export interface IntentReport { reportId: string; intentId: string; attributes: Record<string, unknown> & { lastUpdatedTime: string } }
export interface RmihCapability { intentHandlingCapabilityId: string; supportedExpectationObjectType: string; supportedExpectationTargetInfoList: { supportedTargetName: string }[] }
export interface Rmih { rmihId: string; smeServiceId: string; notificationDestination: string; intentHandlingScope: string[] | null; attributes: { intentHandlingCapabilityList: RmihCapability[]; supportedNegotiationFunctionalities: string[] | null } }
/**
 * A record of one inference-driven dispatch decision (HISTORY.md OI-6.3, rApp Autonomy Modes). It is distinct from `Intent` because not
 * every mode produces an intent: SHADOW never does and ASSIST does not until an operator resolves the dispatch.
 */
export interface AutonomyDispatch {
  dispatchId: string; instanceId: string; modelId: string | null; autonomyMode: string;
  expectations: Record<string, unknown>[]; priority: number; rmihId: string;
  intentMgmtPurpose: string | null; intentHandlingScope: string | null;
  regionScope: Record<string, unknown> | null; status: string; intentId: string | null; createdAt: string;
  rejectedBy?: string | null; rejectionReason?: string | null;
}

// ---- NFO / FOCOM / SO / SA / Analytics / DME
/**
 * An NF deployment at the NFO: name, state, cluster, descriptor, workload reference and, for ABNORMAL, why (OI-3-nfo-abnormal).
 */
export interface NfDeployment {
  nfDeploymentId: string; name: string; state: string; clusterId: string; nfDeploymentDescriptorId: string;
  workloadRef: string | null; requiredResourceTypeId: string | null;
  abnormalReason: string | null;  // OI-3-nfo-abnormal: why it is ABNORMAL
}
export interface NfResource { resourceLinkId: string; resourceRef: string; vresourceType: string }
export interface ResourcePool { resourcePoolId: string; name: string; description: string | null; oCloudId: string }
export interface ResourceType { resourceTypeId: string; name: string; description: string | null; vendor: string | null; model: string | null; version: string | null; resourceKind: string | null; resourceClass: string | null }
export interface DeploymentManager { deploymentManagerId: string; name: string; description: string | null; oCloudId: string; serviceUri: string; capacity: unknown }
export interface OCloudResource { resourceId: string; resourceTypeId: string; resourcePoolId: string; parentId: string | null; description: string | null; globalAssetId: string | null }
export interface OCloudAlarm { alarmId: string; resourceRef: string; severity: string }
export interface OCloudMetric { resourceRef: string; metricName: string; value: number }
export interface Topology { entities: Record<string, { id: string; attributes: Record<string, unknown> }[]>[]; relationships: Record<string, { id: string; aSide: string; bSide: string }[]>[] }
export interface OrderStep { stepType: string; targetModule: string; status: string; error?: string; result?: Record<string, unknown>; [k: string]: unknown }
export interface ServiceOrder { orderId: string; scope: string; steps: OrderStep[]; homingDecision: Record<string, unknown> | null; rmihRegistration: string }
export interface Monitor { monitorId: string; targetOrderId: string | null; targetCoordinationGroupId: string | null; targetRappInstanceId: string | null; analyticsSubscriptionId: string | null; thresholds: Record<string, number> }
export interface RemedialAction { actionId: string; monitorId: string; actionType: string; autoExecuted: boolean; outcome: string | null }
export interface AnalyticsReport { reportId: string; analyticsType: string; output: Record<string, unknown> }
export interface AnalyticsProducer { producerId: string; analyticsType: string; mdaType: string | null; dmeInputTypes: string[]; outputSchema: Record<string, unknown> }
export interface AnalyticsSubscription { subscriptionId: string; analyticsType: string; requestedBy: string; notificationDestination: string | null; scope: Record<string, unknown> | null }
export interface DmeType { dmeTypeId: string; dmeTypeIdStruct: Record<string, string>; typeName: string; producerIds: string[]; typeStatus: string }
export interface DmeProducer { producerId: string; producerHealthCallbackUrl: string; jobCallbackUrl: string; supportedTypeIds: string[] }

// ---- BFF admin
/** A console user as `GET /api/admin/users` lists it. */
export interface GuiUser {
  username: string; role: "viewer" | "operator" | "admin"; active: boolean; createdAt: string; breakGlass?: boolean; totpEnrolled?: boolean;
  // GUI-9.8: the user's newest audit row and newest successful sign-in (null: none; absent on an older BFF)
  lastActiveAt?: string | null; lastSignInAt?: string | null;
  // GUI-5: the user's region/tenant scope (absent on an older BFF)
  scope?: UserScope;
}
export interface AuditEntry { id: number; at: string; username: string | null; role: string | null; action: string; method: string | null; path: string | null; statusCode: number | null; detail: string | null }

// ---- GUI pass 2: DME, SME registries, onboarding/NFO detail
/** A DME data job (a consumer's subscription to a data type). */
export interface DataJob {
  dataJobId: string; dataDeliveryMode: string; dmeTypeId: string; productionJobDefinition: Record<string, unknown>; dataDeliveryMethod: string; deliveryDetails: Record<string, unknown>; consumerId: string; status: string;
  // GUI-9.8: when a producer last delivered (null: never), the declared interval, and whether two intervals passed without one (null: no interval declared)
  lastDeliveryAt?: string | null; expectedIntervalSeconds?: number | null; late?: boolean | null;
}
export interface DataOffer { offerId: string; dmeTypeId: string; dataDeliveryMethodsOffered: string[]; committedMethod: string | null; dataAvailabilityNotificationUri: string | null; dataOfferTerminationNotificationUri: string }
export interface DmeTypeSubscription { subscriptionId: string; notificationDestination: string; owner: string }
export interface SmeProvider { apfId: string; providerDomainInfo: string | null; serviceCount: number }
export interface SmeService { serviceId: string; serviceName: string; producerId: string; endpoint: string; version: string; fullApiVersions: string[]; serviceCapabilities: Record<string, unknown>; aefProfiles: Record<string, unknown>[] }
export interface SmeInvoker { apiInvokerId: string; apiInvokerPublicKey: string; keyAuthentication: boolean; trusted: boolean }
export interface TrustedInvoker { apiInvokerId: string; notificationDestination: string; requestTestNotification: boolean; securityInfo: Record<string, unknown>[] }
/**
 * A CAPIF event subscription at SME: the subscriber, the event types, the callback URI and the optional API, invoker and AEF filters (OI-5-sme-filters).
 */
export interface CapifEventSubscription {
  subscriptionId: string; subscriberId: string; eventTypes: string[]; callbackUri: string;
  apiIds: string[] | null; apiInvokerIds: string[] | null; aefIds: string[] | null;  // CAPIFEventFilter (OI-5-sme-filters)
}
export interface PackageUsage { registrationId: string; consumerId: string; stoppedAt: string | null; active: boolean }
export interface PackageArtifact { artifactId: string; path: string; accessUrl: string }
export interface NfDescriptor { nfDeploymentDescriptorId: string; packageId: string; name: string; requiredResourceTypeId: string | null; workloadTemplate: Record<string, unknown> }
export interface LcmOperation { operationId: string; operationType: string; status: string }
export interface InventorySubscription { subscriptionId: string; callback: string; consumerSubscriptionId: string | null; resourceTypeId: string | null }
export interface FeatureGroup { featureGroupId: string; featureGroupName: string; featureList: string; datalakeSource: string; host: string; port: string; bucket: string; dbOrg: string; measurement: string; enableDme: boolean; measuredObjClass: string | null; sourceName: string | null; dmeTypeId: string | null; dmeDataJobId: string | null }

// ---------------------------------------------------------------- rApp safeguards (AI-10.x)
/**
 * The rate and size limits RAN NF OAM holds for one rApp invoker (AI-10.x): config jobs per hour, elements per job and change percent; null means no limit on that axis. `configJobsLastHour` is the current usage.
 */
export interface RappLimits {
  invokerId: string; maxConfigJobsPerHour: number | null; maxElementsPerJob: number | null; maxChangePercent: number | null;
  updatedAt: string; configJobsLastHour: number;
}
export interface RappKill { invokerId: string; killedBy: string; reason: string | null; killedAt: string }
/** GET /rapp-mgmt/instances/{id}/safeguards: what holds one instance in check at RAN NF OAM. `invokerId` is null once it is terminated. */
export interface InstanceSafeguards { instanceId: string; invokerId: string | null; killed: boolean; kill: RappKill | null; limits: RappLimits | null; approvalPolicy?: ApprovalPolicy | null }
export type RefusalCode = "RAPP_KILLED" | "RAPP_RATE_LIMITED" | "RAPP_BLAST_RADIUS_EXCEEDED" | "RAPP_MAGNITUDE_EXCEEDED" | "SCOPE_DENIED";
/**
 * One refusal RAN NF OAM recorded when a safeguard stopped an rApp write: when, the invoker, the refusal code and whether it was announced to subscribers.
 */
export interface SafeguardRefusal {
  refusalId: string; occurredAt: string; invokerId: string; requestedBy: string | null; refusal: RefusalCode; detail: string | null; announced: boolean;
}
export interface SafeguardSubscription { subscriptionId: string; callbackUri: string; refusals: RefusalCode[]; createdAt: string }

// ---------------------------------------------------------------- human approval of rApp actions (AI-11) and the decision record (AI-13)
export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "REFUSED";
/** `requiredApprovals` is absent for the usual single approval and 2 when two different people must approve. */
export interface ApprovalPolicy { invokerId: string; timeoutSeconds: number; onTimeout: "EXPIRE" | "REJECT"; requiredApprovals?: 1 | 2; setBy: string | null; updatedAt: string }
/** One person's approval of a request that is still waiting for another (or the approval that decided it). */
export interface ApprovalVote { by: string; at: string | null; reason: string | null }
/** Why the rApp is asking (AI-13.1): references and text it supplied with the action. */
export interface DecisionContext { inputsRef?: string | null; modelVersion?: string | null; rationale?: string | null; actionId?: string | null }
/**
 * A human-approval request for an rApp action (AI-11): who asked, the managed elements and change count, its status and expiry, who decided and why, the resulting job and the decision context (AI-13.1).
 */
export interface Approval {
  approvalId: string; invokerId: string; requestedBy: string; status: ApprovalStatus; managedElements: string[]; changeCount: number;
  createdAt: string; expiresAt: string; onTimeout: "EXPIRE" | "REJECT"; decidedBy: string | null; decidedAt: string | null; decisionReason: string | null;
  jobId: string | null; refusalCode: string | null; correlationId: string | null; decision: DecisionContext | null;
  /** How many different people must approve (1 or 2) and the approvals given so far; absent from a RAN NF OAM that predates two-person approval. */
  requiredApprovals?: number; approvals?: ApprovalVote[];
}
/** GET /rapp-approvals/{id}: the request with the changes it asks for. */
export interface ApprovalDetail extends Approval {
  changes: { managedElementRef: string; managedFunctionRef?: string | null; operation?: string; attributeChanges?: Record<string, unknown> }[];
  accessScope: string | null;
}
export type Disposition = "DIRECT" | "APPROVED" | "ROLLBACK" | "REJECTED" | "EXPIRED" | "REFUSED";
export type IntegrityStatus = "VERIFIED" | "UNCHAINED" | "MISMATCH";
/**
 * One entry of the rApp decision record (AI-13): what was decided and how (`disposition`), by whom, the context the rApp gave, a content hash, and the audit-chain integrity verdict when requested.
 */
export interface DecisionRecord {
  decisionId: string; occurredAt: string; invokerId: string; requestedBy: string; disposition: Disposition; jobId: string | null; approvalId: string | null;
  actionId: string | null; inputsRef: string | null; modelVersion: string | null; rationale: string | null; approvedBy: string | null; decidedBy: string | null;
  decidedAt: string | null; managedElements: string[]; changeCount: number; correlationId: string | null; contentHash: string; auditSeq: number | null;
  /** Who approved, in order, when the request needed two approvals; null for every other record. */
  approvers?: string[] | null;
  integrity?: { status: IntegrityStatus; reason?: string; auditSeq?: number; auditHash?: string };
}
/** A list answer with its envelope kept (`useSmo` unwraps it): `total` is absent and `hasMore` present under `?total=false`. */
export interface Page<T> { items: T[]; limit: number; offset: number; total?: number; hasMore?: boolean }

// ---------------------------------------------------------------- zero-touch onboarding (MGT-14) and software campaigns (MGT-15)
export type OnboardingStatus = "DISCOVERED" | "NO_TEMPLATE" | "TEMPLATE_SELECTED" | "APPLYING" | "ONBOARDED" | "FAILED";
export type SoftwareCheck = "NOT_CHECKED" | "MATCH" | "MISMATCH";
export interface TemplateChange { managedFunctionRef?: string | null; attributeChanges: Record<string, unknown>; operation: string }
/** A stored onboarding template as RAN NF OAM returns it: what is written to a newly discovered element of this type and vendor, and when it applies by itself. */
export interface OnboardingTemplate {
  name: string; description: string | null; entityType: string; vendorName: string | null; softwareBaseline: string | null; requireBaseline: boolean;
  autoApply: boolean; enabled: boolean; changes: TemplateChange[]; createdAt: string | null; updatedAt: string | null;
}
/** One element's onboarding record: its state, the template chosen, the software version it reported with the baseline check, and the config job of the last apply. */
export interface ElementOnboarding {
  managedElementRef: string; status: OnboardingStatus; templateName: string | null; softwareVersion: string | null; softwareBaseline: string | null;
  softwareCheck: SoftwareCheck; configJobId: string | null; detail: string | null; createdAt: string | null; updatedAt: string | null;
}
export type CampaignStatus = "PENDING" | "RUNNING" | "HALTED" | "COMPLETED" | "ABORTED" | "ROLLING_BACK" | "ROLLED_BACK" | "ROLLBACK_FAILED";
/** A software campaign as the list shows it: its state, the wave it has reached out of the wave count and, for a halted one, why it is held (`name`, `softwareVersion` and `createdAt` come with the list rows). */
export interface CampaignSummary {
  campaignId: string; status: CampaignStatus; wave: number; waveCount: number; haltedReason: string | null; name?: string; softwareVersion?: string | null; createdAt?: string | null;
}
export interface CampaignEvent { at: string; event: string; wave: number; detail: string | null; by: string | null; job?: string }
export interface CampaignJob { managedElementRef: string; jobId: string; phase: string; status: string; revert: "COMPLETED" | "IN_PROGRESS" | "FAILED" | null; timedOut?: boolean }
/** The full report of one campaign (the drawer): its settings, the event log, the totals, each wave with the element jobs, and the elements that need attention. */
export interface CampaignReport extends CampaignSummary {
  name: string; requestedBy: string; softwareVersion: string | null; selector: Record<string, string> | null; elements: string[]; waveSize: number | null;
  wavePauseSeconds: number; gateMaxNewAlarms: number; onGateFailure: "halt" | "rollback"; jobTimeoutSeconds: number | null; rollbackOrder: "all" | "reverse";
  haltedDetail: string | null; nextWaveAt: string | null; createdAt: string | null; finishedAt: string | null; events: CampaignEvent[];
  summary: { elements: number; started: number; notReached: number; completed: number; failed: number; inProgress: number; reverted: number };
  waves: { wave: number; elements: string[]; started: boolean; jobs: CampaignJob[] }[];
  attention: { managedElementRef: string; problem: string }[];
}
export interface CampaignPreview { dryRun: true; status: "VALIDATED"; waveCount: number; waves: string[][] }
