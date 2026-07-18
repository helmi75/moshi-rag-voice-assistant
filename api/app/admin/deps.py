"""Dépendances FastAPI de la plateforme admin : auth, rôles, scoping tenant, CSRF.

L'auth se fait par DÉPENDANCES (pas de middleware global) : les webhooks Twilio,
/ws/voice et /health ne traversent aucune logique d'auth.
"""
import hmac
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .. import tenants, users
from ..tenants import Tenant
from ..users import User

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def ensure_csrf(request: Request) -> str:
    """Garantit un token CSRF en session et le retourne (posé au login/premier GET)."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        request.session["csrf"] = token
    return token


def current_user(request: Request) -> User:
    """Utilisateur connecté, sinon redirection vers /admin/login.

    htmx ne suit pas les redirects de fragments : on renvoie un 401 avec HX-Redirect
    pour que le navigateur bascule en pleine page."""
    user_id = request.session.get("user_id")
    user = users.get_by_id(user_id) if user_id else None
    if user is None:
        if request.headers.get("HX-Request"):
            raise HTTPException(status_code=401, headers={"HX-Redirect": "/admin/login"})
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    request.state.user = user  # accessible dans les templates via request.state
    return user


def require_superadmin(user: User = Depends(current_user)) -> User:
    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Réservé au super-admin.")
    return user


def resolve_tenant(tenant_id: Optional[int], user: User) -> Tenant:
    """Résout le tenant sur lequel agir, en imposant le scoping restaurateur :
    un restaurateur ne peut agir QUE sur son tenant (403 sinon)."""
    if not user.is_superadmin:
        if tenant_id is not None and tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Accès limité à votre établissement.")
        tenant_id = user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_id requis.")
    tenant = tenants.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Établissement inconnu.")
    return tenant


def check_tenant_access(user: User, tenant_id: int) -> None:
    """403 si un restaurateur touche un objet (résa, appel) d'un autre tenant."""
    if not user.is_superadmin and tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Accès limité à votre établissement.")


async def verify_csrf(request: Request, csrf_token: Optional[str] = Form(None)) -> None:
    """Protège tous les POST : token en champ de formulaire OU en-tête X-CSRF-Token
    (posé par hx-headers sur <body> pour les appels htmx)."""
    expected = request.session.get("csrf", "")
    provided = csrf_token or request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="CSRF token invalide.")
