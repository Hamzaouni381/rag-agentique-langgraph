"""Isolation entre utilisateurs, attaquee sur un vrai PostgreSQL avec des roles sans privilege special.

Ces tests essaient de lire les donnees d'un autre utilisateur par tous les chemins disponibles. Ils ne
tournent que si TEST_DATABASE_URL, TEST_APP_DATABASE_URL et TEST_READER_DATABASE_URL sont definies (CI)."""
import os

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app import config, ingestion, retriever, sql_backend, tenancy, vectorstore
from app.tenancy import owner_key, tenant_schema

pytestmark = pytest.mark.skipif("TEST_DATABASE_URL" not in os.environ, reason="PostgreSQL de test non configure")

ALICE = owner_key("alice-oid")
BOB = owner_key("bob-oid")
SECRET_ALICE = "Le code du coffre PV-300 est ROUGE-ALICE. Portique installe a Lyon."
SECRET_BOB = "Le code du coffre PV-300 est BLEU-BOB. Portique installe a Paris."


def _clear_caches():
    for cached in (
        config.get_settings, vectorstore.get_engine, vectorstore.owner_engine, vectorstore.get_vectorstore,
        vectorstore.get_record_manager, tenancy.ensure_schema, tenancy.ensure_tenant, sql_backend.get_sql_backend,
    ):
        cached.cache_clear()


def _write(owner, name, content):
    directory = tenancy.tenant_dir(owner)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    patch.setenv("APP_DATABASE_URL", os.environ["TEST_APP_DATABASE_URL"])
    patch.setenv("READER_DATABASE_URL", os.environ["TEST_READER_DATABASE_URL"])
    patch.setenv("DOCUMENTS_DIR", str(tmp_path_factory.mktemp("documents")))
    patch.setenv("COLLECTION_NAME", "test_isolation")
    patch.setenv("RERANK_ENABLED", "false")
    fake = DeterministicFakeEmbedding(size=32)
    patch.setattr("app.llm.get_embeddings", lambda: fake)
    patch.setattr("app.vectorstore.get_embeddings", lambda: fake)
    _clear_caches()

    _write(ALICE, "coffre.txt", SECRET_ALICE)
    _write(ALICE, "commun.txt", "Note commune de maintenance du portique.")
    _write(ALICE, "ventes_alice.csv", "article,total\nrouge,11\n")
    _write(BOB, "coffre.txt", SECRET_BOB)
    _write(BOB, "commun.txt", "Note commune de maintenance du portique.")
    _write(BOB, "ventes_bob.csv", "article,total\nbleu,22\n")
    ingestion.ingest_directory(ALICE, cleanup="full")
    ingestion.ingest_directory(BOB, cleanup="full")
    yield
    patch.undo()
    _clear_caches()


def _texts(docs):
    return " ".join(d.page_content for d in docs)


def test_hybrid_search_stays_in_own_documents(corpus):
    alice, bob = _texts(retriever.retrieve("code du coffre", ALICE)), _texts(retriever.retrieve("code du coffre", BOB))
    assert "ROUGE-ALICE" in alice and "BLEU-BOB" not in alice
    assert "BLEU-BOB" in bob and "ROUGE-ALICE" not in bob


def test_keyword_search_finds_references_and_inflections_in_own_documents_only(corpus):
    assert "ROUGE-ALICE" in _texts(vectorstore.keyword_search("PV-300", ALICE, 10))
    assert "ROUGE-ALICE" in _texts(vectorstore.keyword_search("portiques installes", ALICE, 10))
    assert "ROUGE-ALICE" not in _texts(vectorstore.keyword_search("ROUGE-ALICE", BOB, 10))


def test_summary_only_reads_own_documents(corpus):
    assert "BLEU-BOB" not in _texts(retriever.summary_chunks("resume le coffre", ALICE))


def test_row_level_security_blocks_a_query_without_owner_filter(corpus):
    """Le cas de l'erreur de code : une requete sans aucun filtre sur le proprietaire."""
    with vectorstore.owner_engine(ALICE).connect() as conn:
        owners = {row[0] for row in conn.execute(text("SELECT cmetadata->>'owner_id' FROM langchain_pg_embedding"))}
    assert owners == {ALICE}


def test_connection_without_owner_sees_nothing(corpus):
    engine = create_engine(os.environ["TEST_APP_DATABASE_URL"], poolclass=NullPool)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM langchain_pg_embedding")).scalar() == 0


def test_row_level_security_blocks_writing_into_another_owner(corpus):
    sql = text("UPDATE langchain_pg_embedding SET cmetadata = jsonb_set(cmetadata, '{owner_id}', to_jsonb(CAST(:o AS text)))")
    with pytest.raises(DBAPIError, match="row-level security"):
        with vectorstore.owner_engine(ALICE).begin() as conn:
            conn.execute(sql, {"o": BOB})


def test_app_role_cannot_delete_rows_of_another_owner(corpus):
    with vectorstore.owner_engine(ALICE).begin() as conn:
        deleted = conn.execute(text("DELETE FROM langchain_pg_embedding WHERE cmetadata->>'owner_id' = :o"), {"o": BOB})
        assert deleted.rowcount == 0
    assert "BLEU-BOB" in _texts(vectorstore.keyword_search("BLEU-BOB", BOB, 10))


def test_app_role_cannot_bypass_row_level_security(corpus):
    with vectorstore.owner_engine(ALICE).connect() as conn:
        assert conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar() is False
        with pytest.raises(DBAPIError):
            conn.execute(text("ALTER TABLE langchain_pg_embedding DISABLE ROW LEVEL SECURITY"))


def test_sql_tool_lists_own_tables_only(corpus):
    assert sql_backend.get_sql_backend(ALICE).list_tables() == ["ventes_alice"]
    assert sql_backend.get_sql_backend(BOB).list_tables() == ["ventes_bob"]
    assert "bleu" not in sql_backend.get_sql_backend(ALICE).table_info()


def test_generated_sql_reads_own_table(corpus):
    result = sql_backend.get_sql_backend(ALICE).run(f'SELECT total FROM "{tenant_schema(ALICE)}".ventes_alice')
    assert "11" in result


def test_generated_sql_cannot_read_another_owner_table(corpus):
    with pytest.raises(DBAPIError, match="permission denied"):
        sql_backend.get_sql_backend(ALICE).run(f'SELECT * FROM "{tenant_schema(BOB)}".ventes_bob')


@pytest.mark.parametrize("table", ["langchain_pg_embedding", "upsertion_record"])
def test_generated_sql_cannot_read_application_tables(corpus, table):
    with pytest.raises(DBAPIError, match="permission denied"):
        sql_backend.get_sql_backend(ALICE).run(f"SELECT * FROM {table}")


def test_reader_role_alone_has_no_access_to_tenant_data(corpus):
    engine = create_engine(os.environ["TEST_READER_DATABASE_URL"], poolclass=NullPool)
    with pytest.raises(DBAPIError, match="permission denied"):
        with engine.connect() as conn:
            conn.execute(text(f'SELECT * FROM "{tenant_schema(ALICE)}".ventes_alice'))


@pytest.mark.parametrize("sql", [
    "SELECT rolname FROM pg_roles",
    "SELECT nspname FROM pg_catalog.pg_namespace",
    "SELECT table_schema FROM information_schema.tables",
    "SELECT set_config('role', 'x', true)",
])
def test_generated_sql_cannot_explore_the_catalog_or_change_role(corpus, sql):
    with pytest.raises(sql_backend.UnsafeSQLError):
        sql_backend.get_sql_backend(ALICE).run(sql)


def test_same_file_for_two_owners_is_stored_for_each(corpus):
    assert "maintenance" in _texts(vectorstore.keyword_search("maintenance", ALICE, 10))
    assert "maintenance" in _texts(vectorstore.keyword_search("maintenance", BOB, 10))


def test_full_cleanup_of_one_owner_leaves_the_other_intact(corpus):
    """Doit rester le dernier test : il supprime des donnees."""
    (tenancy.tenant_dir(ALICE) / "commun.txt").unlink()
    (tenancy.tenant_dir(ALICE) / "ventes_alice.csv").unlink()
    ingestion.ingest_directory(ALICE, cleanup="full")

    assert "maintenance" not in _texts(vectorstore.keyword_search("maintenance", ALICE, 10))
    assert sql_backend.get_sql_backend(ALICE).list_tables() == []
    assert "maintenance" in _texts(vectorstore.keyword_search("maintenance", BOB, 10))
    assert sql_backend.get_sql_backend(BOB).list_tables() == ["ventes_bob"]
