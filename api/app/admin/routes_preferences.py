"""Préférences d'affichage : le thème clair ou sombre (ASSISTANTE-114).

Retenu dans un cookie, pas en base : c'est un réglage de l'APPAREIL — le téléphone de la
salle et l'ordinateur du bureau ne sont pas sous la même lumière. Le serveur le pose sur
<html> dès la première ligne de la page : aucun flash du mauvais thème au chargement,
ce qu'un script ne garantirait pas (la CSP interdit le script en ligne dans <head>).
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse

from . import deps

router = APIRouter()

THEMES = ("light", "dark")  # les valeurs de `data-theme` que Pico et admin.css lisent
COOKIE = "theme"
_UN_AN = 365 * 24 * 3600


def theme_choisi(request: Request) -> Optional[str]:
    """Le thème imposé, ou None pour suivre l'appareil. Le cookie vient du navigateur :
    seule une valeur connue atteint le gabarit."""
    choix = request.cookies.get(COOKIE)
    return choix if choix in THEMES else None


deps.templates.env.globals["theme_choisi"] = theme_choisi


@router.post("/admin/theme", dependencies=[Depends(deps.verify_csrf)])
async def choisir_theme(request: Request, theme: str = Form("auto"),
                        retour: str = Form("/admin/")):
    # admin.js a déjà basculé la page : la requête htmx n'a qu'à retenir le choix. Sans
    # script, le formulaire revient à la page d'où il est parti — une page de l'admin
    # seulement, jamais une adresse fournie de l'extérieur.
    if request.headers.get("HX-Request"):
        reponse: Response = Response(status_code=204)
    else:
        reponse = RedirectResponse(retour if retour.startswith("/admin") else "/admin/",
                                   status_code=303)
    if theme in THEMES:
        reponse.set_cookie(COOKIE, theme, max_age=_UN_AN, path="/admin", samesite="lax",
                           httponly=True,
                           secure=os.getenv("SESSION_SECURE", "").lower() in ("1", "true"))
    else:
        reponse.delete_cookie(COOKIE, path="/admin")
    return reponse
