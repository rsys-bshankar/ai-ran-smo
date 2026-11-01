package io.smo.sdk;

import java.util.List;
import java.util.Set;

/** Every route the SDK's namespace clients call. The list {@link #ALL} is what the contract test checks against {@code docs/openapi/}. */
public final class Routes {
    private Routes() {
    }

    // DME (data)
    public static final Route DME_TYPES = Route.get("/dme/dme-types", "data_category");
    public static final Route DME_JOBS_LIST = Route.get("/dme/data-jobs", "dme_type_id", "consumer_id", "limit", "offset");
    public static final Route DME_JOB_CREATE = Route.post("/dme/data-jobs",
            Set.of("dmeTypeId", "dataDeliveryMode", "dataDeliveryMethod", "consumerId", "deliveryDetails", "productionJobDefinition", "lifecycleStage"));
    public static final Route DME_JOB_GET = Route.get("/dme/data-jobs/{}");
    public static final Route DME_JOB_STATUS = Route.get("/dme/data-jobs/{}/status");
    public static final Route DME_JOB_DELETE = Route.delete("/dme/data-jobs/{}");
    public static final Route DME_RECORDS = Route.get("/dme/data-jobs/{}/records", "limit");
    public static final Route DME_ACTIONS = Route.post("/dme/actions", Set.of("requestedBy", "changes"));

    // AIMgF's model registry (AI models)
    public static final Route AIMGF_MODELS = Route.get("/aimgf/models", "limit", "offset");
    public static final Route AIMGF_MODEL_GET = Route.get("/aimgf/models/{}");

    // SME (platform)
    public static final Route SME_DISCOVER = Route.get("/sme/service-apis/v1/allServiceAPIs",
            "api_invoker_id", "api_name", "api_version", "aef_id", "protocol", "data_format", "comm_type");

    // rApp Management (the instance's own routes)
    public static final Route INSTANCE_GET = Route.get("/rapp-mgmt/instances/{}");
    public static final Route INSTANCE_CONFIG = Route.get("/rapp-mgmt/instances/{}/config");
    public static final Route INSTANCE_PERFORMANCE = Route.post("/rapp-mgmt/instances/{}/performance", Set.of());
    public static final Route INSTANCE_FAULT = Route.post("/rapp-mgmt/instances/{}/fault", Set.of(), "severity", "description");
    public static final Route INSTANCE_OPERATOR_API_PUT = Route.put("/rapp-mgmt/instances/{}/operator-api", Set.of("operatorApiBase"));
    public static final Route INSTANCE_OPERATOR_API_DELETE = Route.delete("/rapp-mgmt/instances/{}/operator-api");
    public static final Route INSTANCE_BOOTSTRAP_COMPLETE = Route.post("/rapp-mgmt/instances/{}/bootstrap-complete", Set.of());
    public static final Route INSTANCE_TERMINATE = Route.post("/rapp-mgmt/instances/{}/terminate", Set.of());

    public static final List<Route> ALL = List.of(
            DME_TYPES, DME_JOBS_LIST, DME_JOB_CREATE, DME_JOB_GET, DME_JOB_STATUS, DME_JOB_DELETE, DME_RECORDS, DME_ACTIONS,
            AIMGF_MODELS, AIMGF_MODEL_GET, SME_DISCOVER,
            INSTANCE_GET, INSTANCE_CONFIG, INSTANCE_PERFORMANCE, INSTANCE_FAULT, INSTANCE_OPERATOR_API_PUT,
            INSTANCE_OPERATOR_API_DELETE, INSTANCE_BOOTSTRAP_COMPLETE, INSTANCE_TERMINATE);
}
