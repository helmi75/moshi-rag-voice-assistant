"""Horaires d'ouverture par établissement : la grille que l'assistante APPLIQUE.

La fiche « Horaires » de la base de connaissances reste un texte libre, lu par le modèle
pour renseigner les clients ; cette grille (app/disponibilite.py) est ce qui refuse un
créneau côté serveur. Les deux doivent dire la même chose — la page le rappelle."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import connecteurs, db, disponibilite, tenants
from ..users import User
from . import deps

router = APIRouter()


def _contexte(tenant, grille, fermetures: str, erreurs: list[str],
              brut: str | None = None) -> dict:
    horaires = disponibilite.charger(tenant.opening_hours if brut is None else brut)
    return {"tenant": tenant, "grille": grille, "fermetures": fermetures, "erreurs": erreurs,
            "lettres": disponibilite.en_toutes_lettres(horaires),
            "source_resos": connecteurs.est_resos(tenant)}


async def _horaires_resos(tenant) -> str | None:
    """Pour un carnet resOS, les horaires sont ceux de resOS (SCRUM-86) : on les montre,
    on ne les saisit pas. Un champ modifiable qui n'a aucun effet est pire qu'un champ
    absent."""
    return connecteurs.horaires_en_cache(tenant) or await connecteurs.rafraichir_horaires(tenant)


def _grille_saisie(form) -> list[dict]:
    """La grille telle que l'utilisateur l'a remplie, pour la lui rendre en cas d'erreur :
    corriger une heure ne doit pas faire retaper les six autres jours."""
    return [{"nom": jour, "label": jour.capitalize(),
             "ferme": str(form.get(f"{jour}_ferme", "")).lower() in ("on", "1", "true"),
             "plages": [(str(form.get(f"{jour}_{n}_debut", "") or ""),
                         str(form.get(f"{jour}_{n}_fin", "") or "")) for n in (1, 2)]}
            for jour in disponibilite.JOURS]


@router.get("/admin/tenants/{tenant_id}/horaires")
async def horaires_page(request: Request, tenant_id: int,
                        user: User = Depends(deps.current_user)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    deps.ensure_csrf(request)
    brut = await _horaires_resos(tenant) if connecteurs.est_resos(tenant) else tenant.opening_hours
    horaires = disponibilite.charger(brut)
    return deps.templates.TemplateResponse(
        request, "tenants/horaires.html",
        _contexte(tenant, disponibilite.grille(horaires),
                  disponibilite.fermetures_en_texte(horaires), [], brut=brut or ""),
    )


@router.post("/admin/tenants/{tenant_id}/horaires", dependencies=[Depends(deps.verify_csrf)])
async def horaires_update(request: Request, tenant_id: int,
                          user: User = Depends(deps.current_user)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    if connecteurs.est_resos(tenant):
        brut = await _horaires_resos(tenant)
        horaires = disponibilite.charger(brut)
        return deps.templates.TemplateResponse(
            request, "tenants/horaires.html",
            _contexte(tenant, disponibilite.grille(horaires),
                      disponibilite.fermetures_en_texte(horaires),
                      ["Ces horaires viennent de resOS : ils se changent dans resOS, "
                       "rien n'a été enregistré ici."], brut=brut or ""),
            status_code=409,
        )
    form = await request.form()
    horaires, erreurs = disponibilite.depuis_formulaire(form)
    if erreurs:
        return deps.templates.TemplateResponse(
            request, "tenants/horaires.html",
            _contexte(tenant, _grille_saisie(form), str(form.get("fermetures", "") or ""), erreurs),
            status_code=422,
        )
    await db.hors_boucle(tenants.update_tenant, tenant.id,
                         opening_hours=json.dumps(horaires, ensure_ascii=False))
    return RedirectResponse(f"/admin/tenants/{tenant.id}/horaires", status_code=303)
