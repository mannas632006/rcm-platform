-- Two roles, created before the application ever connects.
--
-- DR-06 requires the audit tables to be append-only, and CLAUDE.md M0.4 requires a
-- test proving the application role cannot modify them. That proof is only
-- meaningful if the application actually runs as a role that lacks the grant --
-- so the split exists from the first migration, not as a later hardening step.
--
-- Passwords here are local-development only. Deployed environments inject
-- credentials from a secret manager (S-8); nothing in this file reaches them.

CREATE ROLE rcm_migrator LOGIN PASSWORD 'rcm_migrator';
CREATE ROLE rcm_app      LOGIN PASSWORD 'rcm_app';

ALTER DATABASE rcm OWNER TO rcm_migrator;

\connect rcm

-- The migrator owns the schema; the application only uses it.
ALTER SCHEMA public OWNER TO rcm_migrator;
GRANT USAGE ON SCHEMA public TO rcm_app;

-- Default privileges for tables the migrator creates later. Deliberately does
-- NOT include audit tables, which migration 0002 restricts further.
ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rcm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rcm_app;
