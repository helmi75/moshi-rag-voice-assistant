"""Page « Carnet resOS » : suivre en direct ce que l'assistante fait dans resOS (SCRUM-93).

Pour tout établissement : l'état de resOS, les réservations à venir (rafraîchies toutes
les 5 s pendant qu'on appelle), et le journal de ce que l'assistante a demandé.

En bac à sable (`resos_demo`), le super-admin joue aussi le RESTAURANT : valider ou
refuser une demande, régler la capacité, les services et les jours fermés, simuler une
panne ou un resOS lent. C'est ce qui permet de tester au téléphone, sans clé ni
abonnement, tout ce qu'un vrai resOS ferait vivre à l'assistante.
"""
import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import connecteurs, db, horloge
from ..connecteurs import Injoignable, Refus, bac_a_sable, resos, sante
from ..users import User
from . import deps

router = APIRouter()

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

SOURCES = {"phone": "Téléphone (assistante)", "website": "Site web", "google": "Google",
           "walkin": "Sans réservation", "email": "E-mail", "other": "Autre"}


def _date_courte(iso: str) -> str:
    """« 2026-10-02 » → « ven. 2 oct. » : lisible d'un coup d'œil, sur une ligne."""
    try:
        jour = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "—"
    mois = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
            "oct.", "nov.", "déc.")[jour.month - 1]
    return f"{horloge.JOURS[jour.weekday()][:3]}. {jour.day} {mois}"


_PLAGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _mode(tenant) -> str:
    return connecteurs.fournisseur(tenant)


def _bac(tenant) -> Optional[bac_a_sable.Etat]:
    return bac_a_sable.etat_pour(tenant.id) if _mode(tenant) == connecteurs.RESOS_DEMO else None


async def _contexte_direct(tenant) -> dict:
    """Ce qui se rafraîchit pendant l'appel : réservations, journal, état."""
    reservations, erreur = [], None
    bac = _bac(tenant)
    if bac is not None and bac.simulation():
        # Pendant une panne simulée, la page lit le carnet fictif en direct : sinon chaque
        # rafraîchissement attendrait 4 s la panne qu'on est justement en train de simuler.
        reservations = _a_venir_du_bac(bac)
    elif connecteurs.est_resos(tenant):
        try:
            reservations = await connecteurs.pour(tenant).a_venir()
        except (Injoignable, Refus) as exc:
            erreur = str(exc)
    coupure = sante.en_coupure(tenant.id)
    for reservation in reservations:
        reservation["date_courte"] = _date_courte(reservation.get("date"))
    return {
        "tenant": tenant, "mode": _mode(tenant), "bac": bac,
        # Montrée en tête de page ET dans le bloc rafraîchi : une panne simulée qu'on ne
        # voit pas a fait échouer deux jours de tests (25-27/09/2026).
        "simulation": bac.simulation() if bac else None,
        "reservations": reservations, "erreur_lecture": erreur, "statuts": STATUTS,
        "sources": SOURCES,
        "journal": _journal(tenant),
        "coupure": None if coupure is None else int(sante.COUPE_CIRCUIT_SECONDES - coupure),
        "maintenant": horloge.maintenant().strftime("%H:%M:%S"),
    }


def _a_venir_du_bac(bac: bac_a_sable.Etat, jours: int = 14) -> list[dict]:
    debut = horloge.aujourd_hui()
    fin = (debut + timedelta(days=jours)).isoformat()
    return sorted((resos._en_reservation(b) for b in bac.reservations.values()
                   if debut.isoformat() <= b["date"] <= fin),
                  key=lambda r: (r["date"], r["time"]))


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
        "jours_fermes_texte": ", ".join(horloge.JOURS[n - 1] for n in sorted(bac.jours_fermes))
                              if bac else "",
        "retard_simule": bac_a_sable.RETARD_SIMULE_SECONDES,
        "simulation_minutes": bac_a_sable.SIMULATION_MINUTES,
        "etat_resos": etat_de_resos(contexte["simulation"]),
    })
    contexte["confirmation"] = _confirmation(request.query_params.get("ok"), bac, contexte)
    return deps.templates.TemplateResponse(request, "tenants/resos.html", contexte)


def etat_de_resos(simulation: Optional[dict]) -> str:
    """L'état du bac à sable en une phrase, tel qu'on l'écrit partout sur la page."""
    if not simulation:
        return "resOS fonctionne normalement"
    quoi = ("resOS en panne (répond 503)" if simulation["quoi"] == "panne"
            else f"resOS lent (plus de {int(bac_a_sable.RETARD_SIMULE_SECONDES)} s)")
    reste = simulation.get("reste_minutes")
    return f"{quoi}{f', encore {reste} min' if reste else ''}"


def _confirmation(quoi: Optional[str], bac, contexte: dict) -> Optional[str]:
    """Après un enregistrement, ce qui a VRAIMENT été enregistré — relu dans l'état, pas
    recopié du formulaire : un clic qui n'aurait rien changé se verrait ici."""
    if bac is None or quoi not in ("restaurant", "simulation", "vide"):
        return None
    heure = horloge.maintenant().strftime("%H:%M")
    if quoi == "restaurant":
        fermes = contexte["jours_fermes_texte"]
        return (f"Restaurant enregistré à {heure} : {bac.capacite} table(s) par créneau · "
                f"services {contexte['services_texte']} · "
                + (f"fermé le {fermes}." if fermes else "ouvert tous les jours."))
    if quoi == "simulation":
        return f"État de resOS enregistré à {heure} : {etat_de_resos(bac.simulation())}."
    return f"Carnet du bac à sable vidé à {heure}."


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
    bac.capacite, bac.services, bac.fermes = capacite, services, fermes
    bac.jours_fermes = {n for n in range(1, 8) if form.get(f"jour_{n}")}
    bac.sauver()
    # Les réglages ne touchent JAMAIS à la simulation de panne : c'est en les mêlant au
    # même formulaire qu'une case « lent » invisible est restée cochée deux jours.
    if not bac.simulation():
        # Les nouveaux horaires servent dès l'appel suivant, sans attendre 10 minutes.
        await connecteurs.rafraichir_horaires(tenant)
    return RedirectResponse(f"/admin/tenants/{tenant.id}/resos?ok=restaurant", status_code=303)


@router.post("/admin/tenants/{tenant_id}/resos/simulation",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_simuler(request: Request, tenant_id: int,
                         user: User = Depends(deps.require_superadmin)):
    """Démarrer ou arrêter une panne simulée. Elle s'arrête d'elle-même au bout de
    `SIMULATION_MINUTES` : oubliée, elle sabote les tests suivants."""
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    bac = _exiger_bac(tenant)
    action = str((await request.form()).get("action", ""))
    if action in ("panne", "lent"):
        bac.simuler(action)
    elif action == "arreter":
        bac.arreter_simulation()
        # On veut rejouer tout de suite, pas attendre la fin du coupe-circuit.
        sante.rouvrir(tenant.id)
        await connecteurs.rafraichir_horaires(tenant)
    else:
        raise HTTPException(422, "Action inconnue.")
    return RedirectResponse(f"/admin/tenants/{tenant.id}/resos?ok=simulation", status_code=303)


@router.post("/admin/tenants/{tenant_id}/resos/vider",
             dependencies=[Depends(deps.verify_csrf)])
async def carnet_vider(request: Request, tenant_id: int,
                       user: User = Depends(deps.require_superadmin)):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    bac = _exiger_bac(tenant)
    bac.reservations.clear()
    bac.notes.clear()
    bac.sauver()
    return RedirectResponse(f"/admin/tenants/{tenant.id}/resos?ok=vide", status_code=303)
