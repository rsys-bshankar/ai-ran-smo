-- =============================================================================
-- SMO common schemas (plan task S1.9 builds these as Alembic revisions; this file is the reference DDL the S0.7 "Contracts" job applies to a
-- scratch Postgres 18 with TimescaleDB).
--   common_teiv_nrm       one writer: TE&IV (D6). MOIs and topology edges; RAN NF OAM and FOCOM write through TE&IV's ProvMnS, never here.
--   common_dme_telemetry  one writer: DME (D6). ROP tables as TimescaleDB hypertables (D8). Read by others only through DME over R1 (ADR 0011).
-- Graph store: relational edges and recursive SQL (D7). Apache AGE is not used. Cell locks are not here: their only writer is SO SMOS
-- (so_smos.cell_lock in 02_per_module_schemas.sql). Roles and grants: 03_roles_and_grants.sql.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS common_teiv_nrm;
CREATE SCHEMA IF NOT EXISTS common_dme_telemetry;

-- 1. TS 28.532 ProvMnS MOI store of the NRM (TS 28.541, TS 28.622), written only by TE&IV.
CREATE TABLE IF NOT EXISTS common_teiv_nrm.moi_store (
    dn                  TEXT PRIMARY KEY,
    parent_dn           TEXT REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    object_class        VARCHAR(64) NOT NULL,
    moi_id              VARCHAR(128) NOT NULL,
    moi_version         BIGINT NOT NULL DEFAULT 1,
    attributes          JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_moi_parent_dn ON common_teiv_nrm.moi_store(parent_dn);
CREATE INDEX IF NOT EXISTS idx_moi_class ON common_teiv_nrm.moi_store(object_class);
CREATE INDEX IF NOT EXISTS idx_moi_attrs_gin ON common_teiv_nrm.moi_store USING GIN (attributes jsonb_path_ops);

-- 2. Topology edges, written only by TE&IV. NEIGHBOR_CELL mirrors NRCellRelation objects; CO_COVERAGE_OVERLAY and HOSTED_ON_OCLOUD wait for the
--    WG10 TE&IV models (S0.13).
CREATE TABLE IF NOT EXISTS common_teiv_nrm.topology_edges (
    edge_id             UUID PRIMARY KEY,
    source_dn           TEXT NOT NULL REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    target_dn           TEXT NOT NULL REFERENCES common_teiv_nrm.moi_store(dn) ON DELETE CASCADE,
    relationship_type   VARCHAR(64) NOT NULL CHECK (relationship_type IN ('CONTAINMENT', 'NEIGHBOR_CELL', 'CO_COVERAGE_OVERLAY', 'HOSTED_ON_OCLOUD')),
    edge_metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teiv_rel_type ON common_teiv_nrm.topology_edges(relationship_type, source_dn);

-- =============================================================================
-- 3. DME wide ROP store (S3.8): one row per cell per ROP, all TS 28.552 counters or computed KPIs of that ROP in one JSONB object.
--    dim_cell maps the long cell DN to a small integer once. The two ROP tables are TimescaleDB hypertables (D8): hot 14 days raw, warm 90 days
--    as continuous aggregates and cold daily Parquet in the object store are S3.11's (no retention policy here, so nothing is dropped before the
--    aggregates exist).
-- =============================================================================

CREATE TABLE IF NOT EXISTS common_dme_telemetry.dim_cell (
    cell_id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cell_dn              TEXT UNIQUE NOT NULL,
    managed_elem_dn      TEXT NOT NULL,
    subnetwork_dn        TEXT NOT NULL,
    vendor_name          VARCHAR(64),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dim_cell_subnetwork ON common_dme_telemetry.dim_cell(subnetwork_dn);
CREATE INDEX IF NOT EXISTS idx_dim_cell_me ON common_dme_telemetry.dim_cell(managed_elem_dn);

CREATE TABLE IF NOT EXISTS common_dme_telemetry.pm_rop_ts28552 (
    ts                   TIMESTAMPTZ NOT NULL,
    cell_id              INTEGER NOT NULL REFERENCES common_dme_telemetry.dim_cell(cell_id) ON DELETE CASCADE,
    gran_period_sec      SMALLINT NOT NULL DEFAULT 900,
    suspect_flag         BOOLEAN NOT NULL DEFAULT FALSE,
    quality_status       VARCHAR(16) NOT NULL DEFAULT 'VALID' CHECK (quality_status IN ('VALID', 'SUSPECT', 'IMPUTED')),
    counters             JSONB NOT NULL,
    PRIMARY KEY (ts, cell_id)
);
SELECT create_hypertable('common_dme_telemetry.pm_rop_ts28552', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_pm_rop_cell_ts ON common_dme_telemetry.pm_rop_ts28552(cell_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pm_rop_counters_gin ON common_dme_telemetry.pm_rop_ts28552 USING GIN (counters jsonb_path_ops);

CREATE TABLE IF NOT EXISTS common_dme_telemetry.kpi_rop_ts28554 (
    ts                   TIMESTAMPTZ NOT NULL,
    cell_id              INTEGER NOT NULL REFERENCES common_dme_telemetry.dim_cell(cell_id) ON DELETE CASCADE,
    subnetwork_dn        TEXT NOT NULL,
    kpis                 JSONB NOT NULL,
    PRIMARY KEY (ts, cell_id)
);
SELECT create_hypertable('common_dme_telemetry.kpi_rop_ts28554', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_kpi_rop_subnet_ts ON common_dme_telemetry.kpi_rop_ts28554(subnetwork_dn, ts DESC);
CREATE INDEX IF NOT EXISTS idx_kpi_rop_kpis_gin ON common_dme_telemetry.kpi_rop_ts28554 USING GIN (kpis jsonb_path_ops);

-- 4. KPI formula definitions ([D] ADR 0011 D-10), moved from RAN NF OAM with S3.8.
CREATE TABLE IF NOT EXISTS common_dme_telemetry.kpi_definition (
    name                 VARCHAR(128) PRIMARY KEY,
    formula              TEXT NOT NULL,
    unit                 VARCHAR(32),
    standard_ref         TEXT
);

-- 5. Data jobs of the R1GAP v14 section 5.2 data services (a consumer, a data type, the job definition and the delivery), the R1TD v05 type id.
CREATE TABLE IF NOT EXISTS common_dme_telemetry.data_job (
    data_job_id          VARCHAR(128) PRIMARY KEY,
    consumer_id          VARCHAR(128) NOT NULL,
    data_type_id         VARCHAR(256) NOT NULL,                -- namespace:name:semver, e.g. ORAN:RanOamPmData:1.0.0 or SMO:RopMatrix:1.0.0 ([D] D-02)
    job_definition       JSONB NOT NULL,
    delivery             JSONB NOT NULL,
    status               VARCHAR(32) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DELETED')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. Atomic ingestion of one ROP batch of one ManagedElement (the production schema OamPmRopBatch of openapi/modules/dme.yaml).
CREATE OR REPLACE FUNCTION common_dme_telemetry.fn_upsert_oam_rop_batch(p_batch JSONB)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    v_cell       JSONB;
    v_cell_id    INTEGER;
    v_me_dn      TEXT := p_batch->>'managedElementDn';
    v_subnet_dn  TEXT := p_batch->>'subNetworkDn';
    v_vendor     TEXT := p_batch->>'sourceVendor';
    v_rop_end    TIMESTAMPTZ := (p_batch->>'ropEndTime')::TIMESTAMPTZ;
    v_gran_sec   SMALLINT := COALESCE((p_batch->>'granularityPeriodSec')::SMALLINT, 900);
    v_count      INTEGER := 0;
BEGIN
    FOR v_cell IN SELECT * FROM jsonb_array_elements(p_batch->'cells')
    LOOP
        INSERT INTO common_dme_telemetry.dim_cell (cell_dn, managed_elem_dn, subnetwork_dn, vendor_name)
        VALUES (v_cell->>'cellDn', v_me_dn, v_subnet_dn, v_vendor)
        ON CONFLICT (cell_dn) DO UPDATE SET vendor_name = COALESCE(EXCLUDED.vendor_name, common_dme_telemetry.dim_cell.vendor_name)
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
            suspect_flag = EXCLUDED.suspect_flag,
            quality_status = EXCLUDED.quality_status;

        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$$;
