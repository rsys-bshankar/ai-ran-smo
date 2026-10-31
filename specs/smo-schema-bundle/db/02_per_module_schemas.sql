-- =============================================================================
-- Per-module schemas of the converged SMO (reference DDL; the real schema is the Alembic history in smo/migrations, ADR 0001). Each schema is
-- named after its module (D13) and is written only by that module's role (03_roles_and_grants.sql); no SQL crosses schemas (ADR 0008 section 3.4).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS so_smos;
CREATE SCHEMA IF NOT EXISTS sa_smos;
CREATE SCHEMA IF NOT EXISTS ran_nf_oam;
CREATE SCHEMA IF NOT EXISTS mdaf;
CREATE SCHEMA IF NOT EXISTS aimgf;
CREATE SCHEMA IF NOT EXISTS rapp_mgmt;
CREATE SCHEMA IF NOT EXISTS sme;

-- 1. SO SMOS: intents (TS 28.312 v20.0.0 as a ProvMnS envelope), snapshots, cell locks, the write ledger (ADR 0009).
CREATE TABLE IF NOT EXISTS so_smos.intent (
    intent_id               VARCHAR(128) PRIMARY KEY,
    dn                      TEXT UNIQUE NOT NULL,
    intent_level            VARCHAR(8) NOT NULL CHECK (intent_level IN ('LEVEL_1', 'LEVEL_2')),  -- [D] derived from the targets, never sent on the wire
    parent_intent_dn        TEXT,                                -- from the generic context parentIntent ([D] D-17) of a Level-2 intent
    intent_priority         INTEGER NOT NULL DEFAULT 1 CHECK (intent_priority BETWEEN 1 AND 100),
    prov_mns_envelope       JSONB NOT NULL,                      -- {id, objectClass: Intent, objectInstance, attributes}
    playbook_state          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((intent_level = 'LEVEL_2') = (parent_intent_dn IS NOT NULL))  -- loop prevention: a Level-2 intent has a parent, a Level-1 has none
);

CREATE TABLE IF NOT EXISTS so_smos.o1_snapshot (
    snapshot_id             VARCHAR(128) PRIMARY KEY,
    intent_id               VARCHAR(128) NOT NULL REFERENCES so_smos.intent(intent_id),
    target_dn_list          TEXT[] NOT NULL,
    pre_change_attributes   JSONB NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ROLLED_BACK', 'SUPERSEDED')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cell locks (D6): written only by SO SMOS; others read them as the CellLock class of SO SMOS's contract ([D] D-23). lock_expires_at is the lease.
CREATE TABLE IF NOT EXISTS so_smos.cell_lock (
    cell_dn                 TEXT PRIMARY KEY,
    lock_state              VARCHAR(32) NOT NULL DEFAULT 'FREE' CHECK (lock_state IN ('FREE', 'NES_SLEEP_LOCKED', 'MAINTENANCE_LOCKED')),
    lock_owner_intent       VARCHAR(128) REFERENCES so_smos.intent(intent_id),
    lock_expires_at         TIMESTAMPTZ,
    CHECK ((lock_state = 'FREE') = (lock_owner_intent IS NULL))
);

-- Write ledger (D17): every enacted and pending change, the input of conflict detection before and after the write.
CREATE TABLE IF NOT EXISTS so_smos.write_ledger (
    entry_id                UUID PRIMARY KEY,
    intent_id               VARCHAR(128) NOT NULL REFERENCES so_smos.intent(intent_id),
    object_dn               TEXT NOT NULL,
    attribute_name          VARCHAR(128) NOT NULL,
    new_value               JSONB NOT NULL,
    state                   VARCHAR(16) NOT NULL CHECK (state IN ('PENDING', 'ENACTED', 'ROLLED_BACK', 'REFUSED')),
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ledger_object ON so_smos.write_ledger(object_dn, attribute_name, recorded_at DESC);

-- 2. SA SMOS: TS 28.536 assurance closed loops, created from an assurance intent of SO SMOS (ADR 0009 section 4.11).
CREATE TABLE IF NOT EXISTS sa_smos.accl (
    accl_id                 VARCHAR(128) PRIMARY KEY,
    dn                      TEXT UNIQUE NOT NULL,
    assurance_intent_dn     TEXT NOT NULL,
    operational_state       VARCHAR(16) NOT NULL DEFAULT 'ENABLED' CHECK (operational_state IN ('ENABLED', 'DISABLED')),
    attributes              JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS sa_smos.evaluation_window (
    eval_id                 UUID PRIMARY KEY,
    accl_id                 VARCHAR(128) NOT NULL REFERENCES sa_smos.accl(accl_id),
    goal_id                 VARCHAR(128) NOT NULL,
    window_start            TIMESTAMPTZ NOT NULL,
    window_end              TIMESTAMPTZ NOT NULL,
    measured_value          DOUBLE PRECISION NOT NULL,
    target_value            DOUBLE PRECISION NOT NULL,
    fulfilment_status       VARCHAR(16) NOT NULL CHECK (fulfilment_status IN ('FULFILLED', 'NOT_FULFILLED')),   -- TS 28.312 values
    not_fulfilled_state     VARCHAR(16) CHECK (not_fulfilled_state IN ('RECEIVED', 'DEGRADED', 'SUSPENDED', 'TERMINATED'))
);

-- 3. RAN NF OAM: NETCONF sessions any replica can resume (S3.6), the O1 transaction log, and the TS 28.111 RAN alarm list (R17).
CREATE TABLE IF NOT EXISTS ran_nf_oam.netconf_session (
    managed_element_dn      TEXT PRIMARY KEY,
    o1_address              INET NOT NULL,
    o1_port                 INTEGER NOT NULL DEFAULT 830,
    connection_state        VARCHAR(32) NOT NULL DEFAULT 'CONNECTED',
    yang_capabilities       TEXT[] NOT NULL,
    owning_pod              VARCHAR(128),
    last_heartbeat_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ran_nf_oam.o1_tx_log (
    rpc_message_id          VARCHAR(128) PRIMARY KEY,
    intent_id               VARCHAR(128) NOT NULL,                -- the Level-2 intent SO SMOS enacted (no cross-schema foreign key)
    target_dn               TEXT NOT NULL,
    netconf_op              VARCHAR(32) NOT NULL CHECK (netconf_op IN ('edit-config', 'get-config', 'get')),
    xml_payload             TEXT NOT NULL,
    rpc_reply_status        VARCHAR(32) NOT NULL CHECK (rpc_reply_status IN ('OK', 'RPC_ERROR_ROLLED_BACK')),
    executed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ran_nf_oam.alarm_record (
    alarm_id                VARCHAR(128) PRIMARY KEY,
    object_instance         TEXT NOT NULL,
    notification_id         BIGINT NOT NULL,
    alarm_type              VARCHAR(64) NOT NULL,
    probable_cause          VARCHAR(128) NOT NULL,
    specific_problem        VARCHAR(256),
    perceived_severity      VARCHAR(16) NOT NULL CHECK (perceived_severity IN ('CRITICAL', 'MAJOR', 'MINOR', 'WARNING', 'INDETERMINATE', 'CLEARED')),
    alarm_raised_time       TIMESTAMPTZ NOT NULL,
    alarm_changed_time      TIMESTAMPTZ,
    alarm_cleared_time      TIMESTAMPTZ,
    ack_state               VARCHAR(16) NOT NULL DEFAULT 'UNACKNOWLEDGED' CHECK (ack_state IN ('ACKNOWLEDGED', 'UNACKNOWLEDGED')),
    additional_text         TEXT,
    additional_information  JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 4. MDAF: TS 28.104 MDA reports.
CREATE TABLE IF NOT EXISTS mdaf.mda_report (
    report_id               VARCHAR(128) PRIMARY KEY,
    mda_type                VARCHAR(64) NOT NULL,                 -- a TS 28.104 MDAType value (S7.10)
    target_dn               TEXT NOT NULL,
    horizon_sec             INTEGER NOT NULL,
    outputs                 JSONB NOT NULL,
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. AIMgF (absorbs MLMR and MLLF, ADR 0010): the 14-state lifecycle, the runtime state, artifacts in the object store with their digest (D10),
--    training-data lineage (G8). servingState is derived, never stored (R3).
CREATE TABLE IF NOT EXISTS aimgf.ml_model (
    model_id                VARCHAR(128) PRIMARY KEY,
    model_name              VARCHAR(128) NOT NULL,
    model_version           VARCHAR(32) NOT NULL,
    artifact_version        INTEGER NOT NULL DEFAULT 1 CHECK (artifact_version >= 1),
    lifecycle_state         VARCHAR(16) NOT NULL DEFAULT 'REGISTERED' CHECK (lifecycle_state IN (
                                'REGISTERED', 'TRAINING', 'TRAINED', 'VALIDATING', 'VALIDATED', 'EMULATING', 'EMULATED', 'PENDING_APPROVAL',
                                'APPROVED', 'CERTIFIED', 'PROMOTED', 'DEPRECATED', 'RETIRED', 'FAILED')),
    runtime_state           VARCHAR(24) NOT NULL DEFAULT 'NOT_DEPLOYED' CHECK (runtime_state IN (
                                'NOT_DEPLOYED', 'DEPLOYMENT_REQUESTED', 'DEPLOYED', 'ACTIVATING', 'ACTIVE', 'SCALING', 'TERMINATING', 'TERMINATED')),
    artifact_object_key     TEXT,
    artifact_sha256         CHAR(64),
    artifact_format         VARCHAR(16) CHECK (artifact_format IN ('ONNX', 'SAFETENSORS')),   -- pickle refused (Part 2 section 1)
    training_data_statistics JSONB,                               -- TS 28.105 DataStatisticalProperties
    source_data_jobs        TEXT[] NOT NULL DEFAULT '{}',
    owner                   VARCHAR(128),                         -- [D] D-08
    UNIQUE (model_name, model_version, artifact_version)          -- R1GAP v14 section 5.6: name + version + artifact version
);

-- The bundle's servingState from the 14 states and the runtime state (ADR 0010 section 3.4).
CREATE OR REPLACE FUNCTION aimgf.serving_state(p_lifecycle TEXT, p_runtime TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_lifecycle IN ('CERTIFIED', 'PROMOTED') AND p_runtime = 'ACTIVE' THEN 'SERVING_ACTIVE'
        WHEN p_lifecycle IN ('VALIDATED', 'EMULATING', 'EMULATED', 'PENDING_APPROVAL', 'APPROVED', 'CERTIFIED', 'PROMOTED') THEN 'VALIDATED'
        WHEN p_lifecycle IN ('DEPRECATED', 'RETIRED') THEN 'DEPRECATED'
        ELSE 'REGISTERED'
    END
$$;

CREATE OR REPLACE VIEW aimgf.ml_model_serving AS
    SELECT model_id, lifecycle_state, runtime_state, aimgf.serving_state(lifecycle_state, runtime_state) AS serving_state FROM aimgf.ml_model;

-- 6. rApp Management: RappPackage, RappInstance and RappRegistration ([D] D-19 classes).
CREATE TABLE IF NOT EXISTS rapp_mgmt.rapp_package (
    package_id              VARCHAR(128) PRIMARY KEY,
    package_name            VARCHAR(128) NOT NULL,
    package_version         VARCHAR(32) NOT NULL,
    csar_object_key         TEXT NOT NULL,
    csar_sha256             CHAR(64) NOT NULL,
    package_state           VARCHAR(16) NOT NULL DEFAULT 'ONBOARDED' CHECK (package_state IN ('ONBOARDED', 'PRIMED', 'DEPRECATED')),
    intent_capability_profile JSONB NOT NULL,
    aiml_model_dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_r1_services    JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (package_name, package_version)
);

CREATE TABLE IF NOT EXISTS rapp_mgmt.rapp_instance (
    rapp_instance_id        VARCHAR(128) PRIMARY KEY,
    package_id              VARCHAR(128) NOT NULL REFERENCES rapp_mgmt.rapp_package(package_id),
    instance_state          VARCHAR(16) NOT NULL DEFAULT 'CREATE_INSTANCE' CHECK (instance_state IN (
                                'CREATE_INSTANCE', 'DEPLOYING', 'RUNNING', 'UPGRADING', 'TERMINATING', 'FAULTED')),
    o_cloud_deployment_ref  VARCHAR(128),
    capif_invoker_id        VARCHAR(128),
    operator_api_base       TEXT                                  -- [D] D-09
);

CREATE TABLE IF NOT EXISTS rapp_mgmt.rapp_registration (
    rapp_id                 UUID PRIMARY KEY,                     -- random (R1GAP v14 section 5.7.2, S4.8)
    rapp_instance_id        VARCHAR(128) NOT NULL REFERENCES rapp_mgmt.rapp_instance(rapp_instance_id),
    registration_state      VARCHAR(16) NOT NULL DEFAULT 'REGISTERED' CHECK (registration_state IN ('REGISTERED', 'DEREGISTERED'))
);

-- 7. SME: CAPIF published APIs (TS 29.222) and the TS 28.622 MnS registry (MnsInfo, S1.12; replaces the bundle's custom mns_producers).
CREATE TABLE IF NOT EXISTS sme.published_api (
    api_id                  VARCHAR(128) PRIMARY KEY,
    apf_id                  VARCHAR(128) NOT NULL,
    api_name                VARCHAR(128) NOT NULL,
    service_api_description JSONB NOT NULL                        -- TS 29.222 ServiceAPIDescription
);

CREATE TABLE IF NOT EXISTS sme.mns_info (
    mns_info_id             VARCHAR(128) PRIMARY KEY,
    mns_label               VARCHAR(128),
    mns_type                VARCHAR(40) NOT NULL CHECK (mns_type IN ('PROV_MNS', 'FAULT_SUPERVISION_MNS', 'STREAMING_DATA_REPORTING_MNS',
                                                                     'FILE_DATA_REPORTING_MNS')),
    mns_version             VARCHAR(32) NOT NULL,
    mns_address             TEXT NOT NULL,
    mns_scope               TEXT[] NOT NULL                       -- DNs the MnS instance covers
);
