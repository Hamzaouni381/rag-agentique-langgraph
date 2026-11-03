"""Identite et droits : en-tete Easy Auth, roles, compte de service, cloisonnement de la memoire."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import audit
from app import auth as auth_module
from app.auth import User, parse_principal, thread_key


def principal(name="Hamza@exemple.fr", roles=("admin",), role_typ="roles"):
    claims = [{"typ": "preferred_username", "val": name}] + [{"typ": role_typ, "val": r} for r in roles]
    return base64.b64encode(json.dumps({"role_typ": role_typ, "claims": claims}).encode()).decode()


class FakeGraph:
    def __init__(self):
        self.configs = []

    def invoke(self, _input, config):
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
    test_client = TestClient(api_module.app)
    test_client.audit_calls = calls
    test_client.graph = graph
    return test_client


def as_user(role):
    return {"X-MS-CLIENT-PRINCIPAL": principal(roles=(role,))}


def test_parse_principal_reads_name_and_roles():
    user = parse_principal(principal())
    assert user == User("hamza@exemple.fr", frozenset({"admin"}))


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


def test_forged_principal_is_ignored_without_easy_auth(client, monkeypatch):
    settings = api_module.get_settings().model_copy(update={"auth_mode": "none", "api_key": "cle"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    assert client.get("/api/documents", headers=as_user("admin")).status_code == 401


def test_user_without_role_is_forbidden(client):
    headers = {"X-MS-CLIENT-PRINCIPAL": principal(roles=())}
    assert client.post("/api/ask", json={"question": "Quel indice ?"}, headers=headers).status_code == 403


def test_simple_user_can_ask_but_not_administer(client):
    headers = as_user("user")
    assert client.post("/api/ask", json={"question": "Quel indice ?"}, headers=headers).status_code == 200
    assert client.audit_calls[-1][1]["user_id"] == "hamza@exemple.fr"
    assert client.get("/api/me", headers=headers).json()["is_admin"] is False
    assert client.get("/api/audit", headers=headers).status_code == 403
    assert client.post("/api/ingest", headers=headers).status_code == 403
    assert client.delete("/api/documents/a.pdf", headers=headers).status_code == 403
    assert client.post("/api/upload", headers=headers, files={"files": ("a.pdf", b"x")}).status_code == 403


def test_admin_can_read_audit(client):
    assert client.get("/api/audit", headers=as_user("admin")).status_code == 200


def test_two_users_with_the_same_conversation_id_do_not_share_memory(client):
    body = {"question": "Quel indice ?", "thread_id": "conv-1"}
    client.post("/api/ask", json=body, headers={"X-MS-CLIENT-PRINCIPAL": principal("a@exemple.fr", ("user",))})
    client.post("/api/ask", json=body, headers={"X-MS-CLIENT-PRINCIPAL": principal("b@exemple.fr", ("user",))})
    first, second = (c["configurable"]["thread_id"] for c in client.graph.configs)
    assert first == "a@exemple.fr:conv-1"
    assert second == "b@exemple.fr:conv-1"
    assert thread_key(User("a@exemple.fr", frozenset()), "conv-1") == first
