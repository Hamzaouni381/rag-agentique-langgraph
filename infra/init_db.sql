-- Equivalent Azure de docker/init.sql, a executer une fois avec psql (relance sans risque) :
--   psql "..." -v reader_password=... -f init_db.sql
-- Le mot de passe du role lecteur vient de l'exterieur : rien de sensible dans le depot.
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS tabular AUTHORIZATION rag;

SELECT format('CREATE ROLE rag_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_reader')
\gexec
ALTER ROLE rag_reader PASSWORD :'reader_password';

GRANT CONNECT ON DATABASE rag TO rag_reader;
GRANT USAGE ON SCHEMA tabular TO rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA tabular TO rag_reader;
-- Les tables creees plus tard par l'ingestion (role rag) seront lisibles aussi.
ALTER DEFAULT PRIVILEGES FOR ROLE rag IN SCHEMA tabular GRANT SELECT ON TABLES TO rag_reader;
