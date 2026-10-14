package io.smo.sdk;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** {@code sdk.data}: DME types, data jobs and records (the Python SDK's {@code DataClient}, the routes a rApp needs). */
public final class DataClient extends NamespaceClient {
    DataClient(R1Client r1) {
        super(r1);
    }

    /** The data types DME knows ({@code GET /dme/dme-types}); {@code dataCategory} may be null. */
    public JsonNode listTypes(String dataCategory) {
        return call(Routes.DME_TYPES, params("data_category", dataCategory), null);
    }

    public JsonNode listDataJobs(String dmeTypeId, String consumerId) {
        return call(Routes.DME_JOBS_LIST, params("dme_type_id", dmeTypeId, "consumer_id", consumerId), null);
    }

    /**
     * {@code POST /dme/data-jobs}. {@code lifecycleStage} (TRAINING, TESTING, EMULATION, INFERENCE, CLOSED_LOOP_FEEDBACK),
     * {@code deliveryDetails} and {@code productionJobDefinition} may be null.
     */
    public JsonNode createDataJob(String dmeTypeId, String dataDeliveryMode, String dataDeliveryMethod, String consumerId,
            Map<String, ?> productionJobDefinition, Map<String, ?> deliveryDetails, String lifecycleStage) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("dmeTypeId", dmeTypeId);
        body.put("dataDeliveryMode", dataDeliveryMode);
        body.put("dataDeliveryMethod", dataDeliveryMethod);
        body.put("consumerId", consumerId);
        body.put("productionJobDefinition", productionJobDefinition == null ? Map.of() : productionJobDefinition);
        body.put("deliveryDetails", deliveryDetails == null ? Map.of() : deliveryDetails);
        body.put("lifecycleStage", lifecycleStage);
        return call(Routes.DME_JOB_CREATE, null, body);
    }

    public JsonNode getDataJob(String dataJobId) {
        return call(Routes.DME_JOB_GET, null, null, dataJobId);
    }

    public JsonNode queryDataJobStatus(String dataJobId) {
        return call(Routes.DME_JOB_STATUS, null, null, dataJobId);
    }

    public void terminateDataJob(String dataJobId) {
        call(Routes.DME_JOB_DELETE, null, null, dataJobId);
    }

    public JsonNode fetchDataRecords(String dataJobId, int limit) {
        return call(Routes.DME_RECORDS, params("limit", limit), null, dataJobId);
    }

    /** As {@link #mediateAction(String, List, String, String, Map, Map, MsacCredential)} without a credential. */
    public JsonNode mediateAction(String requestedBy, List<? extends Map<String, ?>> changes, String scope, String msacRole,
            Map<String, ?> sourceContext, Map<String, ?> decision) {
        return mediateAction(requestedBy, changes, scope, msacRole, sourceContext, decision, null);
    }

    /**
     * Submits an O1 configuration action to DME, which mediates it to RAN NF OAM ({@code POST /dme/actions}); returns the action
     * record. {@code scope} (the platform's default is {@code single-ME}), {@code msacRole}, {@code sourceContext} and
     * {@code decision} (why the rApp acts) may be null and are then left out of the body.
     *
     * <p>{@code credential} is the credential of the MSAC Identity named by {@code requestedBy} ({@code SEC-15.6b}); RAN NF OAM
     * asks an rApp for it when that Identity has one (403 {@code MSAC_ACCESS_DENIED} without it). It is sent in the
     * {@code X-MSAC-Credential} header of this call, and only when not null: never in the body, an {@link SdkException}, a log
     * or a {@code toString}, and no environment variable is read for it. Like every POST the call carries an
     * {@code Idempotency-Key}; a repeat sends the same headers.
     */
    public JsonNode mediateAction(String requestedBy, List<? extends Map<String, ?>> changes, String scope, String msacRole,
            Map<String, ?> sourceContext, Map<String, ?> decision, MsacCredential credential) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("requestedBy", requestedBy);
        body.put("changes", changes == null ? List.of() : changes);
        body.putAll(params("scope", scope, "msacRole", msacRole, "sourceContext", sourceContext, "decision", decision));
        Map<String, String> headers = credential == null ? Map.of() : Map.of(MsacCredential.HEADER, credential.value());
        return callWith(Routes.DME_ACTIONS, null, body, headers);
    }
}
