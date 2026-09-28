-- One-time role setup for the TNT database in Neon project restless-pond-21520685.
-- Run as the database owner against the DIRECT endpoint, database tnt (and again for tnt_staging with
-- the staging login role). Create roles with SQL, not the Neon console (console roles get neon_superuser).
--
--   APP_PW=$(openssl rand -hex 24); RO_PW=$(openssl rand -hex 24)   # hex: URL-safe; store both in the password manager
--   psql "$OWNER_DIRECT_URL" -v app_role=tnt_app -v app_pw="$APP_PW" -v ro_pw="$RO_PW" -f scripts/neon/roles.sql
--   psql "$OWNER_DIRECT_URL" -f scripts/neon/grants.sql
\set ON_ERROR_STOP on

SELECT 'CREATE ROLE tnt_rw NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tnt_rw') \gexec

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE tnt_rw', :'app_role', :'app_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_role', :'app_pw') \gexec

SELECT format('CREATE ROLE tnt_readonly LOGIN PASSWORD %L', :'ro_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tnt_readonly') \gexec

-- Only this database's login role may connect to it.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, tnt_readonly', current_database(), :'app_role') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Role-level settings are applied server-side, so they also work through the transaction pooler.
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'app_role', '15s') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'app_role', '30s') \gexec
SELECT format('ALTER ROLE %I SET lock_timeout = %L', :'app_role', '5s') \gexec
