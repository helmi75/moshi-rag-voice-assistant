"""Démonstration personnalisée (ASSISTANTE-139) : préparer, relire, valider, effacer.

Réservé au super-admin et à `DEMOS_ACTIVES=1` : sans l'interrupteur, chaque route répond 404,
comme si la fonction n'existait pas (c'est le cas en production). La lecture du site et
l'appel au modèle sont longs : ils partent en tâche de fond, la page se rafraîchit seule."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db, demos, taches
from . import deps

router = APIRouter()

IdDemo = Path(ge=1, le=10**12)

deps.templates.env.globals["demos_actives"] = demos.actives


def _actives() -> None:
    # Dernière dépendance : un visiteur sans droit ou sans jeton CSRF est refusé comme
    # partout ailleurs (test_admin_security), seul le super-admin apprend que la fonction
    # est éteinte.
    if not demos.actives():
        raise HTTPException(status_code=404)


def _acces():
    return [Depends(deps.require_superadmin), Depends(_actives)]


def _acces_post():
    return [Depends(deps.verify_csrf), Depends(deps.require_superadmin), Depends(_actives)]


def _liste(request: Request, erreur: str = "", valeurs: Optional[dict] = None):
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "demos.html", {"demos": demos.lister(), "erreur": erreur, "valeurs": valeurs or {}})


def _page(request: Request, demo_id: int):
    deps.ensure_csrf(request)
    demo = demos.obtenir(demo_id)
    if demo is None:
        raise HTTPException(status_code=404)
    return deps.templates.TemplateResponse(
        request, "demo.html",
        {"demo": demo, "champs": demos.CHAMPS, "en_cours": demos.lecture_en_cours(demo)})


def _suivi(request: Request, demo_id: int):
    demo = demos.obtenir(demo_id)
    if demo is None:
        raise HTTPException(status_code=404)
    en_cours = demos.lecture_en_cours(demo)
    reponse = deps.templates.TemplateResponse(
        request, "_demo_suivi.html", {"demo": demo, "en_cours": en_cours})
    if not en_cours:
        reponse.headers["HX-Refresh"] = "true"   # la lecture est finie : on recharge la page
    return reponse


@router.get("/admin/demos", dependencies=_acces())
async def demos_liste(request: Request):
    return await db.hors_boucle(_liste, request)


@router.post("/admin/demos", dependencies=_acces_post())
async def demos_creer(request: Request):
    form = await request.form()
    valeurs = {k: str(form.get(k) or "") for k in ("nom", "ville", "site_url")}
    try:
        identifiant = await db.hors_boucle(demos.creer, **valeurs)
    except demos.Refus as exc:
        reponse = await db.hors_boucle(_liste, request, str(exc), valeurs)
        reponse.status_code = 422
        return reponse
    taches.lancer(demos.preparer(identifiant), nom=f"démonstration {identifiant}")
    return RedirectResponse(f"/admin/demos/{identifiant}", status_code=303)


@router.get("/admin/demos/{demo_id}", dependencies=_acces())
async def demo_page(request: Request, demo_id: int = IdDemo):
    return await db.hors_boucle(_page, request, demo_id)


@router.get("/admin/demos/{demo_id}/suivi", dependencies=_acces(), response_class=HTMLResponse)
async def demo_suivi(request: Request, demo_id: int = IdDemo):
    return await db.hors_boucle(_suivi, request, demo_id)


@router.post("/admin/demos/{demo_id}/relecture", dependencies=_acces_post())
async def demo_relecture(request: Request, demo_id: int = IdDemo):
    form = await request.form()
    valeurs = {k.removeprefix("valeur_"): str(v) for k, v in form.items() if k.startswith("valeur_")}
    try:
        await db.hors_boucle(demos.enregistrer_relecture, demo_id, valeurs)
    except demos.DemoIntrouvable:
        raise HTTPException(status_code=404) from None
    except demos.Refus as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse(f"/admin/demos/{demo_id}", status_code=303)


@router.post("/admin/demos/{demo_id}/valider", dependencies=_acces_post())
async def demo_valider(demo_id: int = IdDemo):
    try:
        await db.hors_boucle(demos.valider, demo_id)
    except demos.DemoIntrouvable:
        raise HTTPException(status_code=404) from None
    except demos.PasRelue:
        raise HTTPException(status_code=409, detail="Enregistrez d'abord la relecture.") from None
    except demos.Refus as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse(f"/admin/demos/{demo_id}", status_code=303)


@router.post("/admin/demos/{demo_id}/effacer", dependencies=_acces_post())
async def demo_effacer(request: Request, demo_id: int = IdDemo):
    if not await db.hors_boucle(demos.effacer, demo_id):
        raise HTTPException(status_code=404)
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": "/admin/demos"})   # la confirmation passe par htmx
    return RedirectResponse("/admin/demos", status_code=303)
