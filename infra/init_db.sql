-- Equivalent Azure de docker/init.sql, a executer une fois avec psql (relance sans risque) :
--   psql "..." -v reader_password=... -v app_password=... -f init_db.sql
-- Les mots de passe viennent de l'exterieur : rien de sensible dans le depot.
-- Les schemas et roles de chaque utilisateur, et la Row-Level Security, sont crees par l'application
-- (app/tenancy.py).
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;

-- rag_app : lit et ecrit les passages, soumis a la Row-Level Security.
SELECT format('CREATE ROLE rag_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app')
\gexec
ALTER ROLE rag_app PASSWORD :'app_password';
GRANT CONNECT ON DATABASE rag TO rag_app;

-- rag_reader : outil SQL. Aucun droit propre sur les donnees : il prend le role de l'utilisateur.
SELECT format('CREATE ROLE rag_reader LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_reader')
\gexec
ALTER ROLE rag_reader PASSWORD :'reader_password' NOINHERIT;
GRANT CONNECT ON DATABASE rag TO rag_reader;
