-- A second database for the test suites, beside the development one.
--
-- The integration, security and leakage suites truncate the tables they use.
-- Run against `rcm`, every `make test` wiped the corpus the operator UI was
-- showing. They run against `rcm_test` instead (the Makefile points them at
-- it); `rcm` keeps the loaded corpus. CI is unaffected: it runs each suite
-- against its own ephemeral database.

CREATE DATABASE rcm_test OWNER rcm_migrator;

\connect rcm_test

ALTER SCHEMA public OWNER TO rcm_migrator;
GRANT USAGE ON SCHEMA public TO rcm_app;

ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rcm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rcm_app;
