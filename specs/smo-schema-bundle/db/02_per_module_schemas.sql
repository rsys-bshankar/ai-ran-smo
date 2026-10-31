-- =============================================================================
-- SMO ISOLATED PER-MODULE OPERATIONAL SCHEMAS
-- Each SMO microservice owns its private schema; cross-module SQL joins are banned.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS so_smos;
CREATE SCHEMA IF NOT EXISTS sa_smos;
CREATE SCHEMA IF NOT EXISTS ran_oam;
CREATE SCHEMA IF NOT EXISTS mdaf;
CREATE SCHEMA IF NOT EXISTS aiml_wf;
CREATE SCHEMA IF NOT EXISTS rapp_mgmt;
CREATE SCHEMA IF NOT EXISTS sme_capif;

-- 1. SO SMOS (Centralized Playbook & TS 28.312 Intent Broker)
CREATE TABLE IF NOT EXISTS so_smos.intent_moi_doc (
    intent_id               VARCHAR(128) PRIMARY KEY,
    dn                      TEXT UNIQUE NOT NULL,
    intent_level            VARCHAR(32) NOT NULL, -- LEVEL_1_SERVICE_GOAL | LEVEL_2_RESOURCE_ACTUATION
    parent_intent_id        VARCHAR(128),
    priority                INTEGER NOT NULL DEFAULT 50,
    prov_mns_envelope_jsonb JSONB NOT NULL,
    playbook_state_jsonb    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS so_smos.o1_snapshots (
    snapshot_id             VARCHAR(128) PRIMARY KEY,
    parent_intent_id        VARCHAR(128) NOT NULL REFERENCES so_smos.intent_moi_doc(intent_id),
    target_dn_list          TEXT[] NOT NULL,
    pre_change_attrs_jsonb  JSONB NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | ROLLED_BACK | SUPERSEDED
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. SA SMOS (Service Assurance & TS 28.536 ACCL Engine)
CREATE TABLE IF NOT EXISTS sa_smos.accl_moi_doc (
    accl_id                 VARCHAR(128) PRIMARY KEY,
    dn                      TEXT UNIQUE NOT NULL,
    parent_intent_id        VARCHAR(128) NOT NULL,
    rollback_snapshot_id    VARCHAR(128) NOT NULL,
    operational_state       VARCHAR(32) NOT NULL DEFAULT 'ENABLED',
    accl_attributes_jsonb   JSONB NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sa_smos.sla_eval_windows (
    eval_id                 UUID PRIMARY KEY,
    accl_id                 VARCHAR(128) NOT NULL REFERENCES sa_smos.accl_moi_doc(accl_id),
    goal_id                 VARCHAR(128) NOT NULL,
    window_start            TIMESTAMPTZ NOT NULL,
    window_end              TIMESTAMPTZ NOT NULL,
    measured_value          DOUBLE PRECISION NOT NULL,
    target_value            DOUBLE PRECISION NOT NULL,
    compliance_state        VARCHAR(32) NOT NULL -- FULFILLED | DEGRADED
);

-- 3. RAN NF OAM (Southbound O1 NETCONF/YANG & VES Session Store)
CREATE TABLE IF NOT EXISTS ran_oam.netconf_sessions (
    managed_element_dn      TEXT PRIMARY KEY,
    o1_ip_address           INET NOT NULL,
    o1_port                 INTEGER NOT NULL DEFAULT 830,
    connection_state        VARCHAR(32) NOT NULL DEFAULT 'CONNECTED',
    yang_capabilities       TEXT[] NOT NULL,
    last_heartbeat_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ran_oam.o1_tx_audit_log (
    rpc_message_id          VARCHAR(128) PRIMARY KEY,
    prov_mns_request_id     VARCHAR(128) NOT NULL,
    target_dn               TEXT NOT NULL,
    netconf_op              VARCHAR(32) NOT NULL, -- edit-config | get-config
    xml_payload             TEXT NOT NULL,
    rpc_reply_status        VARCHAR(32) NOT NULL, -- OK | RPC_ERROR_ROLLED_BACK
    executed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. MDAF (3GPP TS 28.104 Analytics Store)
CREATE TABLE IF NOT EXISTS mdaf.analytics_ts28104 (
    analytics_id            VARCHAR(128) PRIMARY KEY,
    analytics_type          VARCHAR(64) NOT NULL,
    target_dn               TEXT NOT NULL,
    horizon_sec             INTEGER NOT NULL,
    prediction_vector_jsonb JSONB NOT NULL,
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. AI/ML Workflow Services (3GPP TS 28.105 Model Catalog & Training Jobs)
CREATE TABLE IF NOT EXISTS aiml_wf.model_moi_doc (
    model_id                VARCHAR(128) PRIMARY KEY,
    model_version           VARCHAR(32) NOT NULL,
    optimization_domain     VARCHAR(64) NOT NULL,
    artifact_s3_uri         TEXT NOT NULL,
    validation_metrics      JSONB NOT NULL,
    serving_state           VARCHAR(32) NOT NULL DEFAULT 'VALIDATED'
);

-- 6. rAppMgmt & Onboarding (O-RAN WG2/WG10 CSAR Catalog & Active Instances)
CREATE TABLE IF NOT EXISTS rapp_mgmt.csar_catalog (
    descriptor_id           VARCHAR(128) PRIMARY KEY,
    version                 VARCHAR(32) NOT NULL,
    csar_s3_uri             TEXT NOT NULL,
    asd_yaml_jsonb          JSONB NOT NULL,
    intent_profile_jsonb    JSONB NOT NULL,
    warm_start_model_ids    TEXT[] NOT NULL
);

CREATE TABLE IF NOT EXISTS rapp_mgmt.rapp_instances (
    rapp_instance_id        VARCHAR(128) PRIMARY KEY,
    descriptor_id           VARCHAR(128) NOT NULL REFERENCES rapp_mgmt.csar_catalog(descriptor_id),
    o2ims_cluster_id        VARCHAR(128) NOT NULL,
    o2dms_deployment_id     VARCHAR(128) NOT NULL,
    capif_invoker_id        VARCHAR(128),
    health_status           VARCHAR(32) NOT NULL DEFAULT 'HEALTHY',
    active_intent_count     INTEGER NOT NULL DEFAULT 0
);

-- 7. SME (3GPP TS 23.222 CAPIF Service Registry & TS 28.533 MnS Registry)
CREATE TABLE IF NOT EXISTS sme_capif.published_apis (
    api_id                  VARCHAR(128) PRIMARY KEY,
    aef_id                  VARCHAR(128) NOT NULL,
    api_name                VARCHAR(128) NOT NULL,
    r1_uri_prefix           TEXT NOT NULL,
    service_api_desc_jsonb  JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS sme_capif.mns_producers (
    producer_id             VARCHAR(128) PRIMARY KEY,
    mns_type                VARCHAR(64) NOT NULL, -- ProvMnS | IntentMnS | FaultMnS | AnalyticsMnS
    handled_class_names     TEXT[] NOT NULL,
    scope_dn_prefix         TEXT NOT NULL,
    producer_endpoint_uri   TEXT NOT NULL
);
