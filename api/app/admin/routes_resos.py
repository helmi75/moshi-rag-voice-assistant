"""Page « Carnet resOS » : suivre en direct ce que l'assistante fait dans resOS (SCRUM-93).

Pour tout établissement : l'état de resOS, les réservations à venir (rafraîchies toutes
les 5 s pendant qu'on appelle), et le journal de ce que l'assistante a demandé.

En bac à sable (`resos_demo`), le super-admin joue aussi le RESTAURANT : valider ou
refuser une demande, régler la capacité, les services et les jours fermés, simuler une
panne ou un resOS lent. C'est ce qui permet de tester au téléphone, sans clé ni
abonnement, tout ce qu'un vrai resOS ferait vivre à l'assistante.
"""
import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import connecteurs, db, horloge
from ..connecteurs import Injoignable, Refus, bac_a_sable, resos, sante
from ..users import User
from . import deps

router = APIRouter()

# Un resOS « lent » : au-delà du délai de 4 s du connecteur, pour voir le mode dégradé.
RETARD_SIMULE_SECONDES = 6.0

STATUTS = {
    "request": ("À valider", "chip-warn"),
    "approved": ("Validée", "chip-good"),
    "declined": ("Refusée", "chip-bad"),
    "canceled": ("Annulée", "chip-bad"),
    "waitlist": ("Liste d'attente", "chip-warn"),
    "arrived": ("Arrivée", "chip-good"),
    "seated": ("Installée", "chip-good"),
    "left": ("Partie", ""),
    "no_show": ("Absente", "chip-bad"),
}

_PLAGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _mode(tenant) -> str:
    return connecteurs.fournisseur(tenant)


def _bac(tenant) -> Optional[bac_a_sable.Etat]:
    return bac_a_sable.etat_pour(tenant.id) if _mode(tenant) == connecteurs.RESOS_DEMO else None


async def _contexte_direct(tenant) -> dict:
    """Ce qui se rafraîchit pendant l'appel : réservations, journal, état."""
    reservations, erreur = [], None
    if connecteurs.est_resos(tenant):
        try:
            reservations = await connecteurs.pour(tenant).a_venir()
        except (Injoignable, Refus) as exc:
            erreur = str(exc)
    coupure = sante.en_coupure(tenant.id)
    return {
        "tenant": tenant, "mode": _mode(tenant), "bac": _bac(tenant),
        "reservations": reservations, "erreur_lecture": erreur, "statuts": STATUTS,
        "journal": _journal(tenant),
        "coupure": None if coupure is None else int(sante.COUPE_CIRCUIT_SECONDES - coupure),
        "maintenant": horloge.maintenant().strftime("%H:%M:%S"),
    }


def _journal(tenant) -> list[dict]:
    lignes = []
    for entree in sante.journal(tenant.id)[:15]:
        instant = horloge.lire_utc(entree["instant"])
        heure = instant.astimezone(horloge.FUSEAU).strftime("%H:%M:%S") if instant else "?"
        lignes.append({**entree, "heure": heure})
    return lignes


def _exiger_bac(tenant) -> bac_a_sable.Etat:
    bac = _bac(tenant)
    if bac is None:
        raise HTTPException(404, "Réservé au bac à sable resOS.")
    return bac


@router.get("/admin/tenants/{tenant_id}/resos")
async def carnet_page(request: Request, tenant_id: int,
                      user: User = Depends(deps.current_user)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    deps.ensure_csrf(request)
    contexte = await _contexte_direct(tenant)
    bac = contexte["bac"]
    contexte.update({
        "cle_posee": bool(resos.cle_pour(tenant.id)),
        "horaires": connecteurs.horaires_en_cache(tenant),
        "lettres_horaires": _lettres(connecteurs.horaires_en_cache(tenant)),
        "services_texte": ", ".join(f"{d}-{f}" for d, f in bac.services) if bac else "",
        "jours": list(enumerate(horloge.JOURS, start=1)),
        "retard_simule": RETARD_SIMULE_SECONDES,
    })
    return deps.templates.TemplateResponse(request, "tenants/resos.html", contexte)


def _lettres(brut) -> str:
    from .. import disponibilite

    return disponibilite.en_toutes_lettres(disponibilite.charger(brut))


@router.get("/admin/tenants/{tenant_id}/resos/direct")
async def carnet_direct(request: Request, tenant_id: int,
                        user: User = Depends(deps.current_user)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    return deps.templates.TemplateResponse(request, "tenants/_resos_direct.html",
                                           await _contexte_direct(tenant))


@router.post("/admin/tenants/{tenant_id}/resos/tester",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_tester(request: Request, tenant_id: int,
                        user: User = Depends(deps.current_user)):
    """« Tester maintenant » : un vrai aller-retour vers resOS, coupe-circuit ignoré."""
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    contexte = await _contexte_direct(tenant)
    if connecteurs.est_resos(tenant):
        contexte["test"] = await connecteurs.pour(tenant).verifier()
        contexte["journal"] = _journal(tenant)
    return deps.templates.TemplateResponse(request, "tenants/_resos_direct.html", contexte)


# --- Bac à sable : jouer le restaurant (super-admin) --------------------------------

@router.post("/admin/tenants/{tenant_id}/resos/demandes/{booking_id}/{action}",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_decider(request: Request, tenant_id: int, booking_id: str, action: str,
                         user: User = Depends(deps.require_superadmin)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    bac = _exiger_bac(tenant)
    statut = {"valider": "approved", "refuser": "declined"}.get(action)
    booking = bac.reservations.get(booking_id)
    if statut is None or booking is None:
        raise HTTPException(404, "Demande introuvable.")
    booking["status"] = statut
    bac.sauver()
    return deps.templates.TemplateResponse(request, "tenants/_resos_direct.html",
                                           await _contexte_direct(tenant))


def _services(texte: str) -> tuple[list, Optional[str]]:
    services = []
    for morceau in filter(None, (m.strip() for m in texte.split(","))):
        trouve = _PLAGE.match(morceau)
        if not trouve:
            return [], f"Service illisible : « {morceau} » (format 12:00-14:00)."
        h1, m1, h2, m2 = (int(x) for x in trouve.groups())
        if not (0 <= h1 < 24 and 0 <= h2 < 24 and m1 < 60 and m2 < 60) or (h1, m1) >= (h2, m2):
            return [], f"Service impossible : « {morceau} »."
        services.append([f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"])
    return (services, None) if services else ([], "Il faut au moins un service.")


@router.post("/admin/tenants/{tenant_id}/resos/bac",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_regler(request: Request, tenant_id: int,
                        user: User = Depends(deps.require_superadmin)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    bac = _exiger_bac(tenant)
    form = await request.form()
    services, erreur = _services(str(form.get("services", "")))
    fermes = set()
    for ligne in str(form.get("fermes", "")).split():
        try:
            fermes.add(date.fromisoformat(ligne.strip()).isoformat())
        except ValueError:
            erreur = erreur or f"Date illisible : « {ligne} » (format AAAA-MM-JJ)."
    try:
        capacite = max(0, min(50, int(form.get("capacite", bac.capacite))))
    except (TypeError, ValueError):
        erreur = erreur or "Capacité illisible."
        capacite = bac.capacite
    if erreur:
        raise HTTPException(422, erreur)
    etait_en_panne = bac.en_panne or bac.retard > 0
    bac.capacite, bac.services, bac.fermes = capacite, services, fermes
    bac.jours_fermes = {n for n in range(1, 8) if form.get(f"jour_{n}")}
    bac.en_panne = bool(form.get("en_panne"))
    bac.retard = RETARD_SIMULE_SECONDES if form.get("lent") else 0.0
    bac.sauver()
    if etait_en_panne and not (bac.en_panne or bac.retard):
        # Fin de la panne simulée : on veut rejouer tout de suite, pas attendre la fin
        # du coupe-circuit.
        sante.rouvrir(tenant.id)
    if not (bac.en_panne or bac.retard):
        # Les nouveaux horaires servent dès l'appel suivant, sans attendre 10 minutes.
        await connecteurs.rafraichir_horaires(tenant)
    return RedirectResponse(f"/admin/tenants/{tenant.id}/resos", status_code=303)


@router.post("/admin/tenants/{tenant_id}/resos/vider",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_vider(request: Request, tenant_id: int,
                       user: User = Depends(deps.require_superadmin)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    bac = _exiger_bac(tenant)
    bac.reservations.clear()
    bac.notes.clear()
    bac.sauver()
    return RedirectResponse(f"/admin/tenants/{tenant.id}/resos", status_code=303)
