"""« Ce que l'IA sait » : la base de connaissances ET les horaires, modifiables sur place.

SCRUM-111 (28/09/2026) : la base se modifiait dans un grand champ de texte de la fiche
« Établissement », jugé « pas très pro » par Helmi, pendant que cette page l'affichait
joliment sans permettre d'y toucher. Désormais chaque fiche s'édite dans sa carte, avec le
même rendu. Aucun stockage nouveau : le texte reste découpé sur les titres `##`, et c'est
ce même texte qui part dans le prompt.

SCRUM-110 : les horaires d'ouverture (la grille qui REFUSE un créneau fermé) vivent sur
la même page, en tête — plus de menu à part.

Chaque formulaire porte l'empreinte de la base qu'il a lue : si elle a changé entre-temps
(un autre onglet, un collègue), l'enregistrement est refusé au lieu d'écraser en silence.
"""
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .. import connecteurs, db, disponibilite, tenants
from ..users import User
from . import deps

router = APIRouter()

_TITRE_MAX = 80


def _version(texte: Optional[str]) -> str:
    return hashlib.sha1((texte or "").encode("utf-8")).hexdigest()[:12]


def _fiches(tenant) -> list[dict]:
    return tenants.parse_knowledge_sections(tenant.knowledge_base)


def _contexte_fiches(tenant, message: Optional[str] = None,
                     erreur: Optional[str] = None) -> dict:
    from .routes_tenants import KB_MAX

    return {"tenant": tenant, "sections": _fiches(tenant),
            "version": _version(tenant.knowledge_base),
            "taille": len(tenant.knowledge_base or ""), "KB_MAX": KB_MAX,
            "message": message, "erreur": erreur}


async def contexte_horaires(tenant, grille=None, fermetures: Optional[str] = None,
                            erreurs: Optional[list[str]] = None) -> dict:
    """La carte « Horaires » : la grille (saisie, ou lue dans resOS pour un carnet
    resOS — SCRUM-86), les horaires en toutes lettres, et les erreurs de saisie."""
    from .routes_horaires import _horaires_resos

    source_resos = connecteurs.est_resos(tenant)
    brut = await _horaires_resos(tenant) if source_resos else tenant.opening_hours
    horaires = disponibilite.charger(brut)
    return {
        "grille": grille if grille is not None else disponibilite.grille(horaires),
        "fermetures": (fermetures if fermetures is not None
                       else disponibilite.fermetures_en_texte(horaires)),
        "erreurs_horaires": erreurs or [],
        "lettres": disponibilite.en_toutes_lettres(horaires),
        "source_resos": source_resos,
    }


async def rendre_page(request: Request, tenant, status_code: int = 200, **horaires):
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/knowledge.html",
        {**_contexte_fiches(tenant), **await contexte_horaires(tenant, **horaires)},
        status_code=status_code,
    )


def _grille_html(request: Request, tenant, status_code: int = 200, **contexte):
    return deps.templates.TemplateResponse(
        request, "tenants/_fiches.html", {**_contexte_fiches(tenant, **contexte)},
        status_code=status_code)


@router.get("/admin/tenants/{tenant_id}/knowledge")
async def page_connaissances(request: Request, tenant_id: int,
                             user: User = Depends(deps.current_user)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    return await rendre_page(request, tenant)


@router.get("/admin/tenants/{tenant_id}/knowledge/fiches/nouvelle")
def fiche_nouvelle(request: Request, tenant_id: int, user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    return deps.templates.TemplateResponse(
        request, "tenants/_fiche_edition.html",
        {"tenant": tenant, "n": None, "fiche": {"title": "", "body": ""},
         "version": _version(tenant.knowledge_base)})


@router.get("/admin/tenants/{tenant_id}/knowledge/fiches/annuler")
def fiche_nouvelle_annulee(tenant_id: int, user: User = Depends(deps.current_user)):
    """« Annuler » sur une fiche pas encore créée : elle disparaît, rien n'est écrit."""
    deps.resolve_tenant(tenant_id, user)
    return HTMLResponse("")


@router.get("/admin/tenants/{tenant_id}/knowledge/fiches/{n}")
def fiche_vue(request: Request, tenant_id: int, n: int, user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    fiches = _fiches(tenant)
    if not 0 <= n < len(fiches):
        return _grille_html(request, tenant)
    return deps.templates.TemplateResponse(
        request, "tenants/_fiche.html",
        {"tenant": tenant, "n": n, "s": fiches[n], "version": _version(tenant.knowledge_base)})


@router.get("/admin/tenants/{tenant_id}/knowledge/fiches/{n}/modifier")
def fiche_edition(request: Request, tenant_id: int, n: int,
                  user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    fiches = _fiches(tenant)
    if not 0 <= n < len(fiches):
        return _grille_html(request, tenant, erreur="Cette fiche n'existe plus : la page a été rechargée.")
    return deps.templates.TemplateResponse(
        request, "tenants/_fiche_edition.html",
        {"tenant": tenant, "n": n, "fiche": fiches[n], "version": _version(tenant.knowledge_base)})


def _valider(titre: str, corps: str) -> Optional[str]:
    if not titre:
        return "Le titre de la fiche est vide."
    if len(titre) > _TITRE_MAX or "\n" in titre:
        return f"Le titre tient sur une ligne de {_TITRE_MAX} caractères au plus."
    if any(ligne.startswith("## ") for ligne in corps.splitlines()):
        return ("Une ligne du texte commence par « ## » : c'est ainsi que commence une "
                "nouvelle fiche. Utilisez « Ajouter une fiche », ou retirez ces caractères.")
    return None


async def _enregistrer(request: Request, tenant, version: str, fiches: list[dict],
                       message: str) -> HTMLResponse:
    from .routes_tenants import _base_trop_longue

    if version != _version(tenant.knowledge_base):
        return _grille_html(request, tenant, status_code=409, erreur=(
            "La base a été modifiée entre-temps (autre onglet, autre compte) : rien n'a été "
            "écrit. La voici à jour, refaites la modification."))
    texte = tenants.assembler_fiches(fiches)
    erreur = _base_trop_longue(texte)
    if erreur:
        return _grille_html(request, tenant, status_code=422, erreur=erreur)
    await db.hors_boucle(tenants.update_tenant, tenant.id, knowledge_base=texte)
    tenant = await db.hors_boucle(tenants.get_by_id, tenant.id)
    return _grille_html(request, tenant, message=message)


@router.post("/admin/tenants/{tenant_id}/knowledge/fiches",
             dependencies=[Depends(deps.verify_csrf)])
async def fiche_ajouter(request: Request, tenant_id: int,
                        user: User = Depends(deps.current_user),
                        titre: str = Form(""), corps: str = Form(""), version: str = Form("")):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    titre, corps = titre.strip(), corps.strip()
    erreur = _valider(titre, corps)
    if erreur:
        return _grille_html(request, tenant, status_code=422, erreur=erreur)
    fiches = _fiches(tenant) + [{"title": titre, "body": corps}]
    return await _enregistrer(request, tenant, version, fiches, f"Fiche « {titre} » ajoutée.")


@router.post("/admin/tenants/{tenant_id}/knowledge/fiches/{n}",
             dependencies=[Depends(deps.verify_csrf)])
async def fiche_modifier(request: Request, tenant_id: int, n: int,
                         user: User = Depends(deps.current_user),
                         titre: str = Form(""), corps: str = Form(""), version: str = Form("")):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    fiches = _fiches(tenant)
    if not 0 <= n < len(fiches):
        return _grille_html(request, tenant, status_code=409,
                            erreur="Cette fiche n'existe plus : rien n'a été écrit.")
    titre, corps = titre.strip(), corps.strip()
    erreur = _valider(titre, corps)
    if erreur:
        return _grille_html(request, tenant, status_code=422, erreur=erreur)
    fiches[n] = {"title": titre, "body": corps}
    return await _enregistrer(request, tenant, version, fiches, f"Fiche « {titre} » enregistrée.")


@router.post("/admin/tenants/{tenant_id}/knowledge/fiches/{n}/supprimer",
             dependencies=[Depends(deps.verify_csrf)])
async def fiche_supprimer(request: Request, tenant_id: int, n: int,
                          user: User = Depends(deps.current_user), version: str = Form("")):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    fiches = _fiches(tenant)
    if not 0 <= n < len(fiches):
        return _grille_html(request, tenant, status_code=409,
                            erreur="Cette fiche n'existe plus : rien n'a été supprimé.")
    titre = fiches.pop(n)["title"]
    return await _enregistrer(request, tenant, version, fiches, f"Fiche « {titre} » supprimée.")
