"""Plateforme admin (dashboard server-rendered Jinja2 + htmx).

Deux routers :
- public_router : /admin/login (sans auth).
- admin_router  : tout le reste, protégé par Depends(current_user) — l'auth est portée
  par les DÉPENDANCES, pas par un middleware : les webhooks Twilio et /ws/voice ne
  traversent aucune logique d'auth.
"""
from pathlib import Path

from fastapi import APIRouter, Depends

from . import deps, routes_auth

STATIC_DIR = Path(__file__).resolve().parent / "static"

public_router = routes_auth.router

admin_router = APIRouter(dependencies=[Depends(deps.current_user)])


def _include_protected_routes() -> None:
    """Importe les modules de routes protégées (imports locaux : le paquet reste
    importable même si un module optionnel manque pendant le développement)."""
    from . import routes_dashboard  # noqa: WPS433

    admin_router.include_router(routes_dashboard.router)
    from . import routes_tenants

    admin_router.include_router(routes_tenants.router)
    from . import routes_reservations

    admin_router.include_router(routes_reservations.router)
    from . import routes_calls

    admin_router.include_router(routes_calls.router)
    from . import routes_voice

    admin_router.include_router(routes_voice.router)


_include_protected_routes()
