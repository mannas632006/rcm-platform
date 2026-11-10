-- The database models train against (M6, ADR-0026), beside the other two.
--
-- It holds the full 24-month synthetic corpus (`make ml-db`). Training reads
-- neither `rcm`, whose smoke corpus the operator UI shows, nor `rcm_test`,
-- which the suites truncate. Same ownership and grants as `rcm_test`.

CREATE DATABASE rcm_ml OWNER rcm_migrator;

\connect rcm_ml

ALTER SCHEMA public OWNER TO rcm_migrator;
GRANT USAGE ON SCHEMA public TO rcm_app;

ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rcm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rcm_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rcm_app;
