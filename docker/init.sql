-- Execute une seule fois a la creation du volume Postgres.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS tabular AUTHORIZATION rag;

-- Role en lecture seule pour l'outil Text-to-SQL : meme si le LLM genere un DROP,
-- Postgres le refuse. Mot de passe a changer hors developpement local.
CREATE ROLE rag_reader LOGIN PASSWORD 'rag_reader';
GRANT CONNECT ON DATABASE rag TO rag_reader;
GRANT USAGE ON SCHEMA tabular TO rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA tabular TO rag_reader;
-- Les tables creees plus tard par l'ingestion (role rag) seront lisibles aussi.
ALTER DEFAULT PRIVILEGES FOR ROLE rag IN SCHEMA tabular GRANT SELECT ON TABLES TO rag_reader;
