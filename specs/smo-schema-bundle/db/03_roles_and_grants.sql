-- =============================================================================
-- One role per module, each able to use its own schema and nothing else (R4, D6). Run after 01 and 02. The roles are NOLOGIN here; the deployed
-- login roles are smo_<module> made by smo/scripts/db_roles.py from smo/migrations/db_roles.json, which follows the same rule.
-- The S0.7 "Contracts" job checks the result: every role has USAGE on its own schema and on no other (smo/scripts/check_contract_bundle.py).
-- =============================================================================

DO $$
DECLARE
    pair   TEXT[];
    pairs  TEXT[][] := ARRAY[
        ARRAY['smo_teiv',       'common_teiv_nrm'],
        ARRAY['smo_dme',        'common_dme_telemetry'],
        ARRAY['smo_so_smos',    'so_smos'],
        ARRAY['smo_sa_smos',    'sa_smos'],
        ARRAY['smo_ran_nf_oam', 'ran_nf_oam'],
        ARRAY['smo_mdaf',       'mdaf'],
        ARRAY['smo_aimgf',      'aimgf'],
        ARRAY['smo_rapp_mgmt',  'rapp_mgmt'],
        ARRAY['smo_sme',        'sme']];
BEGIN
    FOREACH pair SLICE 1 IN ARRAY pairs LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = pair[1]) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', pair[1]);
        END IF;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', pair[2]);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', pair[2], pair[1]);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', pair[2], pair[1]);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', pair[2], pair[1]);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', pair[2]);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', pair[2], pair[1]);
    END LOOP;
END
$$;

-- No module role may create objects in public.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
