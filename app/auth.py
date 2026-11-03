"""Identite et droits. En production, Container Apps gere la connexion Microsoft Entra (Easy Auth) et
transmet l'utilisateur dans l'en-tete X-MS-CLIENT-PRINCIPAL ; l'application ne stocke aucun mot de passe.

Cet en-tete n'est lu qu'avec AUTH_MODE=easyauth : sans Easy Auth devant l'application, n'importe qui
pourrait le forger. La cle API reste pour les scripts et les tests (compte de service)."""
import base64
import hmac
import json
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.config import get_settings

LOGIN_URL = "/.auth/login/aad?post_login_redirect_uri=/"
LOGOUT_URL = "/.auth/logout?post_logout_redirect_uri=/"
_NAME_CLAIMS = ("preferred_username", "email", "name")


@dataclass(frozen=True)
class User:
    id: str
    roles: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


SERVICE_USER = User("service:cle-api", frozenset({"admin"}))
LOCAL_USER = User("local", frozenset({"admin"}))  # developpement : ni Easy Auth ni cle API


def parse_principal(header: str) -> User | None:
    """Decode l'en-tete base64 d'Easy Auth : {"role_typ": ..., "claims": [{"typ": ..., "val": ...}]}."""
    try:
        principal = json.loads(base64.b64decode(header))
        claims = principal["claims"]
        role_types = {"roles", principal.get("role_typ")}
        roles = frozenset(c["val"] for c in claims if c["typ"] in role_types)
        names = {c["typ"]: c["val"] for c in claims}
    except (ValueError, KeyError, TypeError):
        return None
    name = next((names[claim] for claim in _NAME_CLAIMS if names.get(claim)), None)
    return User(name.lower(), roles) if name else None


def current_user(
    x_api_key: str | None = Header(default=None),
    x_ms_client_principal: str | None = Header(default=None),
) -> User:
    s = get_settings()
    if s.auth_mode == "easyauth" and x_ms_client_principal:
        user = parse_principal(x_ms_client_principal)
        if user:
            return user
    if s.api_key and x_api_key and hmac.compare_digest(x_api_key.encode(), s.api_key.encode()):
        return SERVICE_USER
    if s.auth_mode == "none" and not s.api_key:
        return LOCAL_USER
    detail = {"message": "Connexion requise", "login_url": LOGIN_URL if s.auth_mode == "easyauth" else None}
    raise HTTPException(status_code=401, detail=detail)


def require_user(user: User = Depends(current_user)) -> User:
    if not user.roles & {"admin", "user"}:
        raise HTTPException(status_code=403, detail="Aucun rôle ne vous a été attribué sur cette application.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Action réservée aux administrateurs.")
    return user


def thread_key(user: User, thread_id: str) -> str:
    """Cle de memoire propre a l'utilisateur : connaitre l'identifiant d'une conversation ne suffit
    pas a relire celle d'un autre."""
    return f"{user.id}:{thread_id}"
