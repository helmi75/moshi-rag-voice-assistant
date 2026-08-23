"""Login / logout de la plateforme admin."""
import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from .. import users
from . import deps, throttle

router = APIRouter()


@router.get("/admin/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/admin/", status_code=303)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/admin/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    # Adresse trop insistante : on refuse AVANT de hacher. Vérifier le mot de passe
    # coûterait 150 ms de bcrypt à chaque essai — autant d'énergie offerte à
    # l'attaquant, et un canal temporel de moins pour lui.
    attente = throttle.bloque(request)
    if attente:
        logger.warning(
            f"admin: connexion bloquée pour {throttle.adresse(request)} "
            f"({throttle.MAX_ECHECS} échecs récents), réessai dans {attente}s"
        )
        return deps.templates.TemplateResponse(
            request, "login.html",
            {"error": f"Trop de tentatives. Réessayez dans {attente} secondes."},
            status_code=429, headers={"Retry-After": str(attente)},
        )

    user = users.get_by_email(email)
    # bcrypt ≈ 100-200 ms : en thread pour ne pas geler l'event loop (appels vocaux).
    ok = user is not None and await asyncio.to_thread(
        users.verify_password, password, user.password_hash
    )
    if not ok:
        n = throttle.enregistrer_echec(request)
        # Tracé : après l'exposition du port 8000 en août, l'absence de ce journal a
        # rendu impossible de dire si quelqu'un avait tenté d'entrer.
        logger.warning(
            f"admin: échec de connexion depuis {throttle.adresse(request)} "
            f"({n}/{throttle.MAX_ECHECS} sur {throttle.FENETRE}s)"
        )
        # Message générique : ne révèle pas si l'email existe.
        return deps.templates.TemplateResponse(
            request, "login.html", {"error": "Identifiants invalides."}, status_code=401
        )
    throttle.reinitialiser(request)
    logger.info(f"admin: connexion de {user.email} depuis {throttle.adresse(request)}")
    request.session.clear()
    request.session["user_id"] = user.id
    deps.ensure_csrf(request)
    return RedirectResponse("/admin/", status_code=303)


@router.post("/admin/logout", dependencies=[Depends(deps.verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)
