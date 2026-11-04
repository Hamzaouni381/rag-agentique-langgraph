-- Execute une seule fois a la creation du volume Postgres. Mots de passe a changer hors developpement local.
-- Les schemas et roles de chaque utilisateur, et la Row-Level Security, sont crees par l'application
-- (app/tenancy.py).
CREATE EXTENSION IF NOT EXISTS vector;

-- rag_app : lit et ecrit les passages, soumis a la Row-Level Security.
CREATE ROLE rag_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'rag_app';
GRANT CONNECT ON DATABASE rag TO rag_app;

-- rag_reader : outil SQL. Aucun droit propre sur les donnees : il prend le role de l'utilisateur.
-- Meme si le LLM genere un DROP, Postgres le refuse.
CREATE ROLE rag_reader LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT PASSWORD 'rag_reader';
GRANT CONNECT ON DATABASE rag TO rag_reader;
