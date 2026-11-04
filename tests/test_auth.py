"""Identite et droits : en-tete Easy Auth, roles, compte de service, cloisonnement de la memoire."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import audit
from app import auth as auth_module
from app.auth import User, parse_principal, thread_key
from app.tenancy import owner_key

OID_CLAIM = "http://schemas.microsoft.com/identity/claims/objectidentifier"


def principal(name="Hamza@exemple.fr", roles=("admin",), role_typ="roles", oid="oid-hamza"):
    claims = [{"typ": "preferred_username", "val": name}, {"typ": OID_CLAIM, "val": oid}]
    claims += [{"typ": role_typ, "val": r} for r in roles]
    return base64.b64encode(json.dumps({"role_typ": role_typ, "claims": claims}).encode()).decode()


class FakeGraph:
    def __init__(self):
        self.configs = []
        self.inputs = []

    def invoke(self, graph_input, config):
        self.inputs.append(graph_input)
        self.configs.append(config)
        return {"answer": "IP54", "route": "documents", "sources": [], "sql": ""}


@pytest.fixture
def client(monkeypatch):
    calls = []
    graph = FakeGraph()
    settings = api_module.get_settings().model_copy(update={"auth_mode": "easyauth", "api_key": "cle"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(audit, "record", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(audit, "recent", lambda limit: [])
    monkeypatch.setattr("app.graph.get_graph", lambda: graph)
    monkeypatch.setattr(api_module, "ensure_tenant", lambda owner: None)
    test_client = TestClient(api_module.app)
    test_client.audit_calls = calls
    test_client.graph = graph
    return test_client


def as_user(role):
    return {"X-MS-CLIENT-PRINCIPAL": principal(roles=(role,))}


def test_parse_principal_reads_name_and_roles():
    user = parse_principal(principal())
    assert user == User("hamza@exemple.fr", frozenset({"admin"}), owner_key("oid-hamza"))


def test_parse_principal_reads_roles_from_the_declared_claim_type():
    claim = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
    assert parse_principal(principal(role_typ=claim)).is_admin


@pytest.mark.parametrize("bad", ["", "pas du base64 !", base64.b64encode(b"[1]").decode(), base64.b64encode(b"{}").decode()])
def test_parse_principal_rejects_garbage(bad):
    assert parse_principal(bad) is None


def test_without_identity_the_answer_is_401_with_login_url(client):
    res = client.get("/api/documents")
    assert res.status_code == 401
    assert res.json()["detail"]["login_url"].startswith("/.auth/login/aad")


def test_wrong_api_key_is_refused(client):
    assert client.get("/api/documents", headers={"X-API-Key": "faux"}).status_code == 401


def test_api_key_is_a_named_service_account(client):
    res = client.post("/api/ask", json={"question": "Quel indice ?"}, headers={"X-API-Key": "cle"})
    assert res.status_code == 200
    assert client.audit_calls[-1][1]["user_id"] == "service:cle-api"


def test_only_a_signed_in_user_gets_a_logout_link(client):
    assert client.get("/api/me", headers=as_user("user")).json()["logout_url"].startswith("/.auth/logout")
    assert client.get("/api/me", headers={"X-API-Key": "cle"}).json()["logout_url"] is None


def test_forged_principal_is_ignored_without_easy_auth(client, monkeypatch):
    settings = api_module.get_settings().model_copy(update={"auth_mode": "none", "api_key": "cle"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    assert client.get("/api/documents", headers=as_user("admin")).status_code == 401


def test_user_without_role_is_forbidden(client):
    headers = {"X-MS-CLIENT-PRINCIPAL": principal(roles=())}
    assert client.post("/api/ask", json={"question": "Quel indice ?"}, headers=headers).status_code == 403


def test_simple_user_can_ask_but_not_read_the_audit(client):
    headers = as_user("user")
    assert client.post("/api/ask", json={"question": "Quel indice ?"}, headers=headers).status_code == 200
    assert client.audit_calls[-1][1]["user_id"] == "hamza@exemple.fr"
    assert client.get("/api/me", headers=headers).json()["is_admin"] is False
    assert client.get("/api/audit", headers=headers).status_code == 403


def test_email_change_keeps_the_same_owner():
    assert parse_principal(principal("ancien@exemple.fr")).owner == parse_principal(principal("nouveau@exemple.fr")).owner


def test_email_reassigned_to_someone_else_gives_another_owner():
    assert parse_principal(principal(oid="personne-1")).owner != parse_principal(principal(oid="personne-2")).owner


def test_principal_without_stable_identifier_is_rejected():
    claims = [{"typ": "preferred_username", "val": "a@exemple.fr"}, {"typ": "roles", "val": "user"}]
    assert parse_principal(base64.b64encode(json.dumps({"claims": claims}).encode()).decode()) is None


def test_question_runs_on_the_data_of_the_signed_in_user(client):
    client.post("/api/ask", json={"question": "Quel indice ?"}, headers=as_user("user"))
    assert client.graph.inputs[-1]["owner_id"] == owner_key("oid-hamza")


def test_body_cannot_choose_another_owner(client):
    body = {"question": "Quel indice ?", "owner_id": owner_key("victime"), "owner": owner_key("victime")}
    client.post("/api/ask", json=body, headers=as_user("user"))
    assert client.graph.inputs[-1]["owner_id"] == owner_key("oid-hamza")


def test_each_user_has_a_separate_document_folder(client, monkeypatch, tmp_path):
    settings = api_module.get_settings().model_copy(update={"documents_dir": str(tmp_path)})
    monkeypatch.setattr("app.tenancy.get_settings", lambda: settings)
    monkeypatch.setattr("app.ingestion.ingest_directory", lambda *a, **k: {})
    alice = {"X-MS-CLIENT-PRINCIPAL": principal("alice@exemple.fr", ("user",), oid="alice")}
    bob = {"X-MS-CLIENT-PRINCIPAL": principal("bob@exemple.fr", ("user",), oid="bob")}

    assert client.post("/api/upload", headers=alice, files={"files": ("secret.txt", b"x")}).status_code == 200
    assert [d["name"] for d in client.get("/api/documents", headers=alice).json()] == ["secret.txt"]
    assert client.get("/api/documents", headers=bob).json() == []
    assert client.delete("/api/documents/secret.txt", headers=bob).status_code == 404
    traversal = f"..%2F{owner_key('alice')}%2Fsecret.txt"
    assert client.delete(f"/api/documents/{traversal}", headers=bob).status_code in (404, 405)
    assert (tmp_path / owner_key("alice") / "secret.txt").exists()


def test_storage_quota_is_enforced_per_user(client, monkeypatch, tmp_path):
    settings = api_module.get_settings().model_copy(update={"documents_dir": str(tmp_path), "max_user_storage_mb": 1})
    monkeypatch.setattr("app.tenancy.get_settings", lambda: settings)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.ingestion.ingest_directory", lambda *a, **k: {})
    big = b"x" * (800 * 1024)
    alice = {"X-MS-CLIENT-PRINCIPAL": principal("alice@exemple.fr", ("user",), oid="alice")}
    bob = {"X-MS-CLIENT-PRINCIPAL": principal("bob@exemple.fr", ("user",), oid="bob")}

    assert client.post("/api/upload", headers=alice, files={"files": ("a.txt", big)}).status_code == 200
    assert client.post("/api/upload", headers=alice, files={"files": ("b.txt", big)}).status_code == 413
    assert client.post("/api/upload", headers=alice, files={"files": ("a.txt", big)}).status_code == 200  # remplacement
    assert client.post("/api/upload", headers=bob, files={"files": ("b.txt", big)}).status_code == 200


def test_admin_can_read_audit(client):
    assert client.get("/api/audit", headers=as_user("admin")).status_code == 200


def test_two_users_with_the_same_conversation_id_do_not_share_memory(client):
    body = {"question": "Quel indice ?", "thread_id": "conv-1"}
    client.post("/api/ask", json=body, headers={"X-MS-CLIENT-PRINCIPAL": principal("a@exemple.fr", ("user",), oid="personne-a")})
    client.post("/api/ask", json=body, headers={"X-MS-CLIENT-PRINCIPAL": principal("b@exemple.fr", ("user",), oid="personne-b")})
    first, second = (c["configurable"]["thread_id"] for c in client.graph.configs)
    assert first == f"{owner_key('personne-a')}:conv-1"
    assert second == f"{owner_key('personne-b')}:conv-1"
    assert thread_key(User("a@exemple.fr", frozenset(), owner_key("personne-a")), "conv-1") == first
