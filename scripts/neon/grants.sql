-- Re-runnable grants. Run after every migration that adds a table or view:
--   psql "$OWNER_DIRECT_URL" -f scripts/neon/grants.sql
-- tnt_rw (NOLOGIN group role; tnt_app / tnt_staging_app are members) gets SELECT/INSERT/UPDATE only:
-- no DELETE, no DDL. The importer and migrations run as the owner.
\set ON_ERROR_STOP on
GRANT USAGE ON SCHEMA public TO tnt_rw, tnt_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO tnt_rw, tnt_readonly;
GRANT INSERT, UPDATE ON kids, attendance, completed_sections, sync_state TO tnt_rw;
GRANT INSERT ON team_assignments TO tnt_rw;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tnt_rw;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO tnt_readonly;
