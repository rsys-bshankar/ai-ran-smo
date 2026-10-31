-- =============================================================================
-- SMO COMMON SHARED DATABASES (Single Source of Truth Across Modules)
-- 1. common_teiv_nrm: Canonical 3GPP TS 28.622/28.541 MOI Store & O-RAN TE&IV Graph
-- 2. common_dme_telemetry: High-Scale Wide ROP Vector Store for 3GPP TS 28.552 PM,
--    TS 28.554 KPIs, Scoped R1 DataJobs & TS 28.111 Active Alarm Store
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS common_teiv_nrm;
CREATE SCHEMA IF NOT EXISTS common_dme_telemetry;

-- 1. Canonical 3GPP TS 28.532 ProvMnS MOI Store (Shared by TE&IV, SO SMOS, SA SMOS, RAN NF OAM)
CREATE TABLE IF NOT EXISTS common_teiv_nrm.moi_store (
    dn                  TEXT PRIMARY KEY,
    parent_dn           TEXT REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    object_class        VARCHAR(64) NOT NULL,
    moi_id              VARCHAR(128) NOT NULL,
    moi_version         BIGINT NOT NULL DEFAULT 1,
    attributes          JSONB NOT NULL,
    lock_owner_intent   VARCHAR(128),
    lock_state          VARCHAR(32) NOT NULL DEFAULT 'FREE', -- FREE | NES_SLEEP_LOCKED | MAINTENANCE_LOCKED
    lock_expires_at     TIMESTAMPTZ,                         -- Fail-safe lease TTL ("Dead-Man's Switch")
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_moi_parent_dn ON common_teiv_nrm.moi_store(parent_dn);
CREATE INDEX IF NOT EXISTS idx_moi_class ON common_teiv_nrm.moi_store(object_class);
CREATE INDEX IF NOT EXISTS idx_moi_attrs_gin ON common_teiv_nrm.moi_store USING GIN (attributes jsonb_path_ops);

-- 2. O-RAN TE&IV Topology Graph Edges (Shared by TE&IV, rApps via R1, SO SMOS, SA SMOS)
CREATE TABLE IF NOT EXISTS common_teiv_nrm.topology_edges (
    edge_id             UUID PRIMARY KEY,
    source_dn           TEXT NOT NULL REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    target_dn           TEXT NOT NULL REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    relationship_type   VARCHAR(64) NOT NULL, -- CO_COVERAGE_OVERLAY | NEIGHBOR_CELL | HOSTED_ON_OCLOUD
    edge_metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teiv_rel_type ON common_teiv_nrm.topology_edges(relationship_type, source_dn);

-- 3. Common 3GPP TS 28.111 / TS 28.545 Active & Historical Alarm Store
CREATE TABLE IF NOT EXISTS common_dme_telemetry.ts28111_alarm_list (
    alarm_id            VARCHAR(128) PRIMARY KEY,
    object_instance_dn  TEXT NOT NULL,
    notification_id     BIGINT NOT NULL,
    alarm_type          VARCHAR(64) NOT NULL,
    probable_cause      VARCHAR(128) NOT NULL,
    specific_problem    VARCHAR(256),
    perceived_severity  VARCHAR(32) NOT NULL, -- CRITICAL | MAJOR | MINOR | WARNING | INDETERMINATE | CLEARED
    alarm_raised_time   TIMESTAMPTZ NOT NULL,
    alarm_changed_time  TIMESTAMPTZ,
    alarm_cleared_time  TIMESTAMPTZ,
    ack_state           VARCHAR(32) NOT NULL DEFAULT 'UNACKNOWLEDGED',
    additional_text     TEXT,
    additional_info     JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- =============================================================================
-- 4. HIGH-SCALE WIDE ROP VECTOR TELEMETRY SCHEMA (1,000x Row Reduction vs. EAV)
--    Instead of creating 1,000 rows per cell per 15-min ROP (which explodes to
--    960M rows/day for 10,000 cells), DME stores:
--      (a) dim_cell: Maps long 90-byte cell DNs to a 4-byte integer cell_id ONCE.
--      (b) pm_rop_ts28552: 1 row per (ts, cell_id) per ROP holding all normalized
--          TS 28.552 counters in a single binary JSONB object.
--      (c) kpi_rop_ts28554: 1 row per (ts, cell_id) per ROP holding all computed
--          TS 28.554 KPIs in a single binary JSONB object.
-- =============================================================================

CREATE TABLE IF NOT EXISTS common_dme_telemetry.dim_cell (
    cell_id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cell_dn              TEXT UNIQUE NOT NULL,
    managed_elem_dn      TEXT NOT NULL,
    subnetwork_dn        TEXT NOT NULL,
    vendor_name          VARCHAR(64),
    southbound_protocol  VARCHAR(32) NOT NULL DEFAULT 'O1_VES_7_2', -- O1_VES_7_2 | O1_SFTP_XML | NON_O1_LEGACY_EMS
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dim_cell_subnetwork ON common_dme_telemetry.dim_cell(subnetwork_dn);
CREATE INDEX IF NOT EXISTS idx_dim_cell_me ON common_dme_telemetry.dim_cell(managed_elem_dn);

-- Wide TS 28.552 PM ROP Table (1 Row per Cell per 15-min ROP)
CREATE TABLE IF NOT EXISTS common_dme_telemetry.pm_rop_ts28552 (
    ts                   TIMESTAMPTZ NOT NULL,
    cell_id              INTEGER NOT NULL REFERENCES common_dme_telemetry.dim_cell(cell_id) ON DELETE CASCADE,
    gran_period_sec      SMALLINT NOT NULL DEFAULT 900,
    suspect_flag         BOOLEAN NOT NULL DEFAULT FALSE,
    quality_status       VARCHAR(16) NOT NULL DEFAULT 'VALID', -- VALID | SUSPECT | IMPUTED
    -- Binary JSONB map of all TS 28.552 counters for this cell in this ROP:
    -- e.g. {"RRU.PrbTotDl": 24.8, "RRC.ConnMean": 42.0, "DRB.UEThpDl": 185.4, "PEE.AvgPower": 710.5}
    counters             JSONB NOT NULL,
    PRIMARY KEY (ts, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_rop_cell_ts ON common_dme_telemetry.pm_rop_ts28552(cell_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pm_rop_counters_gin ON common_dme_telemetry.pm_rop_ts28552 USING GIN (counters jsonb_path_ops);

-- Wide TS 28.554 KPI ROP Table (1 Row per Cell per 15-min ROP)
CREATE TABLE IF NOT EXISTS common_dme_telemetry.kpi_rop_ts28554 (
    ts                   TIMESTAMPTZ NOT NULL,
    cell_id              INTEGER NOT NULL REFERENCES common_dme_telemetry.dim_cell(cell_id) ON DELETE CASCADE,
    subnetwork_dn        TEXT NOT NULL,
    -- Binary JSONB map of all TS 28.554 KPIs for this cell in this ROP:
    -- e.g. {"PEE.EnergyEfficiency": 3.82, "DRB.RlcSduDropRateDl": 0.12, "MM.HoSuccessRate": 99.4}
    kpis                 JSONB NOT NULL,
    PRIMARY KEY (ts, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_kpi_rop_subnet_ts ON common_dme_telemetry.kpi_rop_ts28554(subnetwork_dn, ts DESC);
CREATE INDEX IF NOT EXISTS idx_kpi_rop_kpis_gin ON common_dme_telemetry.kpi_rop_ts28554 USING GIN (kpis jsonb_path_ops);

-- 5. Scoped DME DataJob Subscription Registry (For rApps & SA SMOS)
CREATE TABLE IF NOT EXISTS common_dme_telemetry.dme_data_jobs (
    data_job_id          VARCHAR(128) PRIMARY KEY,
    consumer_id          VARCHAR(128) NOT NULL, -- e.g. rapp-nes-instance-01 or sa-smos
    subnetwork_dn        TEXT NOT NULL,
    cell_dn_filter       TEXT[],
    gran_period_sec      SMALLINT NOT NULL DEFAULT 900,
    selected_counters    TEXT[] NOT NULL,
    selected_kpis        TEXT[] NOT NULL,
    delivery_transport   VARCHAR(32) NOT NULL DEFAULT 'R1_WEBHOOK', -- R1_WEBHOOK | KAFKA_TOPIC | SSE_STREAM
    callback_or_topic    TEXT NOT NULL,
    status               VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. Stored Procedure: Atomic Ingestion of RAN NF OAM Per-ManagedElement ROP Batch
CREATE OR REPLACE FUNCTION common_dme_telemetry.fn_upsert_oam_rop_batch(p_batch JSONB)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    v_cell       JSONB;
    v_cell_id    INTEGER;
    v_me_dn      TEXT := p_batch->>'managedElementDn';
    v_subnet_dn  TEXT := p_batch->>'subNetworkDn';
    v_vendor     TEXT := COALESCE(p_batch->>'sourceVendor', 'Generic-O1');
    v_proto      TEXT := COALESCE(p_batch->>'southboundProtocol', 'O1_VES_7_2');
    v_rop_end    TIMESTAMPTZ := (p_batch->>'ropEndTime')::TIMESTAMPTZ;
    v_gran_sec   SMALLINT := COALESCE((p_batch->>'granularityPeriodSec')::SMALLINT, 900);
    v_count      INTEGER := 0;
BEGIN
    FOR v_cell IN SELECT * FROM jsonb_array_elements(p_batch->'cells')
    LOOP
        INSERT INTO common_dme_telemetry.dim_cell (cell_dn, managed_elem_dn, subnetwork_dn, vendor_name, southbound_protocol)
        VALUES (v_cell->>'cellDn', v_me_dn, v_subnet_dn, v_vendor, v_proto)
        ON CONFLICT (cell_dn) DO UPDATE SET vendor_name = EXCLUDED.vendor_name
        RETURNING cell_id INTO v_cell_id;

        INSERT INTO common_dme_telemetry.pm_rop_ts28552 (ts, cell_id, gran_period_sec, suspect_flag, quality_status, counters)
        VALUES (
            v_rop_end,
            v_cell_id,
            v_gran_sec,
            COALESCE((v_cell->>'suspectFlag')::BOOLEAN, FALSE),
            CASE WHEN COALESCE((v_cell->>'suspectFlag')::BOOLEAN, FALSE) THEN 'SUSPECT' ELSE 'VALID' END,
            v_cell->'counters'
        )
        ON CONFLICT (ts, cell_id) DO UPDATE
        SET counters = common_dme_telemetry.pm_rop_ts28552.counters || EXCLUDED.counters,
            suspect_flag = EXCLUDED.suspect_flag;

        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$$;
