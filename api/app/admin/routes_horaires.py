"""Horaires d'ouverture par établissement : la grille que l'assistante APPLIQUE.

Affichée et modifiée dans « Ce que l'IA sait » depuis le 28/09/2026 (SCRUM-110,
routes_connaissances.py) ; ce module garde l'enregistrement.

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
def horaires_page(tenant_id: int, user: User = Depends(deps.current_user)):
    """Les horaires se règlent dans « Ce que l'IA sait » (SCRUM-110) : l'ancienne adresse
    y mène, après la vérification d'accès habituelle."""
    tenant = deps.resolve_tenant(tenant_id, user)
    return RedirectResponse(f"/admin/tenants/{tenant.id}/knowledge#horaires", status_code=303)


@router.post("/admin/tenants/{tenant_id}/horaires", dependencies=[Depends(deps.verify_csrf)])
async def horaires_update(request: Request, tenant_id: int,
                          user: User = Depends(deps.current_user)):
    from .routes_connaissances import rendre_page

    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    if connecteurs.est_resos(tenant):
        return await rendre_page(request, tenant, status_code=409, erreurs=[
            "Ces horaires viennent de resOS : ils se changent dans resOS, rien n'a été "
            "enregistré ici."])
    form = await request.form()
    horaires, erreurs = disponibilite.depuis_formulaire(form)
    if erreurs:
        return await rendre_page(request, tenant, status_code=422, erreurs=erreurs,
                                 grille=_grille_saisie(form),
                                 fermetures=str(form.get("fermetures", "") or ""))
    await db.hors_boucle(tenants.update_tenant, tenant.id,
                         opening_hours=json.dumps(horaires, ensure_ascii=False))
    return RedirectResponse(f"/admin/tenants/{tenant.id}/knowledge#horaires", status_code=303)
