"""Réservations : le calendrier mois / semaine / jour (SCRUM-112), la liste paginée, et
l'édition sur place htmx — en ligne de tableau (_row ⇄ _row_edit) ou en carte du
calendrier (_carte ⇄ _carte_edition)."""
import asyncio
from datetime import date as _date, time as _time
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import calls, connecteurs, db, disponibilite, horloge, reservations, tenants
from ..users import User
from . import calendrier, deps, presenters

router = APIRouter()

PAGE_SIZE = 25


def _tenant_names(user: User) -> dict[int, str]:
    """La colonne « Établissement » n'existe que pour le super-admin.

    La renvoyer à un restaurateur décalait sa ligne d'une cellule après une édition
    inline ET lui montrait le nom d'un autre établissement : la liste et les fragments
    doivent donc décider pareil."""
    return {t.id: t.name for t in tenants.list_all()} if user.is_superadmin else {}


def _load_scoped(reservation_id: int, user: User) -> dict:
    resa = reservations.get_reservation(reservation_id)
    if resa is None:
        raise HTTPException(status_code=404)
    deps.check_tenant_access(user, resa["tenant_id"])
    return resa


def _portee(user: User, tenant_id: Optional[int]) -> list:
    """Les établissements affichés : le sien pour un restaurateur, un seul ou tout le
    parc pour le super-admin."""
    if user.is_superadmin and tenant_id is None:
        return tenants.list_all()
    return [deps.resolve_tenant(tenant_id if user.is_superadmin else user.tenant_id, user)]


async def _lire(portee: list, debut: str, fin: str) -> tuple[list[dict], list[str]]:
    """Les réservations de la période, dans le carnet de CHAQUE établissement : le nôtre
    en base, resOS par son API (lus en parallèle). Un resOS injoignable n'empêche pas
    d'afficher les autres : il est signalé, jamais montré comme une journée vide."""
    internes = [t.id for t in portee if not connecteurs.est_resos(t)]
    lignes = await db.hors_boucle(reservations.entre, debut, fin, internes)
    resas = [calendrier.interne(r, r["tenant_id"]) for r in lignes]

    async def depuis_resos(tenant):
        try:
            lot = await connecteurs.pour(tenant).entre(debut, fin)
        except (connecteurs.Injoignable, connecteurs.Refus) as exc:
            return [], f"{tenant.name} : réservations resOS illisibles ({exc})."
        return [calendrier.resos(r, tenant.id) for r in lot], None

    echecs = []
    for lot, echec in await asyncio.gather(
            *(depuis_resos(t) for t in portee if connecteurs.est_resos(t))):
        resas += lot
        if echec:
            echecs.append(echec)
    return resas, echecs


async def _horaires(portee: list) -> Optional[dict]:
    """Les jours fermés se grisent pour UN établissement ; sur tout le parc, un jour
    fermé ici est ouvert ailleurs."""
    if len(portee) != 1:
        return None
    tenant = portee[0]
    if connecteurs.est_resos(tenant):
        from .routes_horaires import _horaires_resos

        return disponibilite.charger(await _horaires_resos(tenant))
    return disponibilite.charger(tenant.opening_hours)


_JOURS_COURTS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


@router.get("/admin/reservations")
async def reservations_page(
    request: Request,
    user: User = Depends(deps.current_user),
    vue: str = "",
    jour: str = "",
    tenant_id: Optional[int] = None,
    date_from: Optional[str] = None,
    page: int = 1,
):
    """Le calendrier (SCRUM-112) : mois, semaine, jour — et la liste d'avant."""
    deps.ensure_csrf(request)
    if not user.is_superadmin:
        tenant_id = user.tenant_id
    if not vue and date_from:
        # Anciens liens (« Voir la réservation » d'une fiche d'appel, e-mails) : le jour.
        vue, jour = "jour", date_from
    vue_implicite = vue not in calendrier.VUES
    vue = "mois" if vue_implicite else vue

    def lien(**changes) -> str:
        params = {"vue": vue, "jour": jour, "tenant_id": tenant_id, **changes}
        return "/admin/reservations?" + urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})

    portee = await db.hors_boucle(_portee, user, tenant_id)
    communs = {
        "vue": vue, "vue_implicite": vue_implicite, "lien": lien, "tenant_id": tenant_id,
        "tenants": await db.hors_boucle(tenants.list_all) if user.is_superadmin else [],
        # Le nom de l'établissement n'est utile que sur tout le parc.
        "tenant_names": ({t.id: t.name for t in portee}
                         if user.is_superadmin and tenant_id is None else {}),
    }
    if vue == "liste":
        return await _liste(request, user, tenant_id, date_from, page, communs)

    aujourd_hui = horloge.aujourd_hui()
    ancre = calendrier.ancre(jour, aujourd_hui)
    debut, fin = calendrier.bornes(vue, ancre)
    resas, echecs = await _lire(portee, debut.isoformat(), fin.isoformat())
    horaires = await _horaires(portee)
    groupes = calendrier.par_jour(resas)

    def case(j: _date) -> dict:
        du_jour = groupes.get(j.isoformat(), [])
        return {"iso": j.isoformat(), "num": j.day, "court": f"{_JOURS_COURTS[j.weekday()]} {j.day}",
                "lettres": horloge.en_toutes_lettres(j), "hors_mois": j.month != ancre.month,
                "ferme": disponibilite.ferme_le(horaires, j), "aujourdhui": j == aujourd_hui,
                "resume": calendrier.resume(du_jour), "resas": du_jour}

    cases = [case(j) for j in calendrier.jours(debut, fin)]
    periode = [c for c in cases if not (vue == "mois" and c["hors_mois"])]
    precedent, suivant = calendrier.voisins(vue, ancre)
    contexte = {
        **communs, "titre": calendrier.titre(vue, ancre),
        "precedent": precedent.isoformat(), "suivant": suivant.isoformat(),
        "aujourd_hui": aujourd_hui.isoformat(), "ancre_iso": ancre.isoformat(),
        "echecs": echecs,
        "resume": calendrier.resume([r for c in periode for r in c["resas"]]),
        "entetes": _JOURS_COURTS,
        "semaines": [cases[i:i + 7] for i in range(0, len(cases), 7)],
        "colonnes": cases,
        "heures": calendrier.heures(resas),
        "par_heure": {c["iso"]: calendrier.par_heure(c["resas"]) for c in cases},
        "lu_dans_resos": any(connecteurs.est_resos(t) for t in portee),
    }
    return deps.templates.TemplateResponse(request, "reservations/calendrier.html", contexte)


async def _liste(request: Request, user: User, tenant_id: Optional[int],
                 date_from: Optional[str], page: int, communs: dict):
    page = max(1, page)
    # Cet écran-ci MONTRE les annulées : un restaurateur qui voit « annulée à 15h32 »
    # peut reproposer le créneau, et retrouver la preuve si le client conteste. La liste
    # « à venir » de la salle de contrôle, elle, garde le défaut qui les masque — y
    # laisser des annulées ferait préparer des couverts pour personne.
    rows = await db.hors_boucle(
        reservations.list_filtered, tenant_id=tenant_id, date_from=date_from or None,
        limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE, inclure_annulees=True)
    return deps.templates.TemplateResponse(
        request, "reservations/list.html",
        {
            **communs,
            "reservations": rows[:PAGE_SIZE],
            "tenant_names": await db.hors_boucle(_tenant_names, user),
            "date_from": date_from or "",
            "page": page,
            "has_next": len(rows) > PAGE_SIZE,
        },
    )


@router.get("/admin/tenants/{tenant_id}/reservations/{ref}")
async def reservation_fiche(request: Request, tenant_id: int, ref: str,
                            user: User = Depends(deps.current_user)):
    """La fiche d'une réservation (ASSISTANTE-116) : la réservation, son client, et la
    conversation pendant laquelle l'assistante l'a prise. Un clic sur une carte de la
    vue du jour y mène.

    L'établissement vient de l'URL et passe par `resolve_tenant` ; la réservation doit en
    plus lui APPARTENIR — sinon un restaurateur lirait celle d'un autre en mettant son
    propre établissement devant l'identifiant d'une réservation qui n'est pas la sienne."""
    deps.ensure_csrf(request)
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    if connecteurs.est_resos(tenant):
        try:
            lue = await connecteurs.pour(tenant).lire(ref)
        except connecteurs.Refus:
            # resOS a LU la demande et l'a rejetée (identifiant mal formé ou périmé) :
            # réessayer ne changera rien, c'est une réservation introuvable, pas une panne.
            lue = None
        except connecteurs.Injoignable as exc:
            return deps.templates.TemplateResponse(
                request, "reservations/fiche.html",
                {"resa": None, "erreur": f"Réservation resOS illisible ({exc})."},
                status_code=502)
        resa = calendrier.resos(lue, tenant.id) if lue else None
    else:
        cle = _cle_interne(ref)
        ligne = await db.hors_boucle(reservations.get_reservation, cle) if cle else None
        resa = (calendrier.interne(ligne, tenant.id)
                if ligne and ligne["tenant_id"] == tenant.id else None)
    if resa is None:
        raise HTTPException(status_code=404, detail="Réservation introuvable.")
    contexte = await db.hors_boucle(_contexte_fiche, request, tenant, resa)
    return deps.templates.TemplateResponse(request, "reservations/fiche.html", contexte)


def _cle_interne(ref: str) -> Optional[int]:
    """La clé d'une réservation de notre carnet, ou None si `ref` n'en est pas une.

    `str.isdigit()` dit oui à « ² », que `int()` refuse ; et un entier de vingt-cinq
    chiffres déborde l'INTEGER de SQLite. Les deux donnaient une erreur 500 au lieu
    d'une réservation introuvable."""
    if ref.isascii() and ref.isdigit() and len(ref) <= 18:
        return int(ref)
    return None


def _contexte_fiche(request: Request, tenant, resa: dict) -> dict:
    from .routes_calls import _pane_context

    externe = resa["source"] == "resos"
    appel = calls.appel_d_une_reservation(tenant.id, resa["id"], externe=externe)
    # Le panneau de la fiche d'appel, tel quel — à l'heure du restaurant, comme le reste
    # de cette page.
    panneau = _pane_context(request, appel) if appel else {}
    if panneau:
        panneau["call"] = presenters.a_l_heure_du_restaurant(panneau["call"])
    # Le numéro du client : celui de la réservation s'il est au format international,
    # sinon celui de l'appel qui l'a prise (resOS garde le numéro tel qu'il a été saisi).
    numero = (calls.numero_appelant(resa.get("customer_phone"))
              or (appel or {}).get("caller_number"))
    try:
        quand = horloge.en_toutes_lettres(_date.fromisoformat(resa["date"])).capitalize()
    except (TypeError, ValueError):
        quand = resa.get("date") or "Date inconnue"
    # Le super-admin revient au jour de CET établissement : sans `tenant_id`, il
    # retombait sur tout le parc — et la page relisait le resOS de chaque établissement.
    cible = {"vue": "jour", "jour": resa.get("date") or ""}
    if request.state.user.is_superadmin:
        cible["tenant_id"] = tenant.id
    return {
        "tenant": tenant, "resa": resa, "quand": quand, "numero": numero,
        "retour": f"/admin/reservations?{urlencode(cible)}#resa-{resa['id']}",
        # « Marquer traité », dans le panneau de l'appel, revient ICI et non à la fiche
        # de l'appel.
        "retour_message": request.url.path,
        # resOS ne rend que les réservations À VENIR d'un numéro : son historique reste
        # dans resOS, la fiche le dit plutôt que d'afficher une liste trompeuse.
        "autres": [] if externe else [
            calendrier.interne(r, tenant.id)
            for r in reservations.du_client(tenant.id, resa.get("customer_phone"))
            if r["id"] != resa["id"]],
        "appels": [presenters.a_l_heure_du_restaurant(presenters.call_view(c))
                   for c in calls.du_numero(tenant.id, numero)],
        **panneau,
        "fiche": True,
    }


def _fragment(request: Request, user: User, resa: dict, rendu: str, *, edition=False,
              erreur: Optional[str] = None, status_code: int = 200):
    """La même réservation, en ligne de tableau (liste) ou en carte (calendrier)."""
    if rendu == "carte":
        gabarit = "reservations/_carte_edition.html" if edition else "reservations/_carte.html"
        contexte = {"r": calendrier.interne(resa, resa["tenant_id"])}
    else:
        gabarit = "reservations/_row_edit.html" if edition else "reservations/_row.html"
        contexte = {"r": resa}
    contexte.update(tenant_names={} if edition else _tenant_names(user), error=erreur)
    return deps.templates.TemplateResponse(request, gabarit, contexte, status_code=status_code)


@router.get("/admin/reservations/{reservation_id}/edit")
def reservation_edit(request: Request, reservation_id: int, rendu: str = "",
                     user: User = Depends(deps.current_user)):
    return _fragment(request, user, _load_scoped(reservation_id, user), rendu, edition=True)


@router.get("/admin/reservations/{reservation_id}/row")
def reservation_row(request: Request, reservation_id: int, rendu: str = "",
                    user: User = Depends(deps.current_user)):
    return _fragment(request, user, _load_scoped(reservation_id, user), rendu)


def _saisie_invalide(customer_name: str, date: str, time: str, party_size: int) -> Optional[str]:
    """Les champs du formulaire portent déjà `required`, `min` et leurs types : ceci
    protège de ce que le navigateur ne vérifie pas (requête forgée, vieux navigateur).
    Une date « 2026-13-45 » enregistrée casserait le tri, les rappels et les fenêtres de
    la salle de contrôle, qui la comparent comme texte."""
    if not customer_name.strip():
        return "Le nom du client est obligatoire."
    if party_size < 1:
        return "Le nombre de couverts doit être d'au moins 1."
    try:
        _date.fromisoformat(date)
        if len(date) != 10:
            raise ValueError
    except ValueError:
        return f"La date « {date} » n'est pas une date valide (AAAA-MM-JJ)."
    try:
        _time.fromisoformat(time)
        if len(time) != 5:
            raise ValueError
    except ValueError:
        return f"L'heure « {time} » n'est pas une heure valide (HH:MM)."
    return None


@router.post("/admin/reservations/{reservation_id}", dependencies=[Depends(deps.verify_csrf)])
def reservation_update(
    request: Request,
    reservation_id: int,
    user: User = Depends(deps.current_user),
    customer_name: str = Form(...),
    customer_phone: str = Form(""),
    date: str = Form(...),
    time: str = Form(...),
    party_size: int = Form(...),
    notes: str = Form(""),
    rendu: str = Form(""),
):
    actuelle = _load_scoped(reservation_id, user)
    erreur = _saisie_invalide(customer_name, date, time, party_size)
    if erreur:
        # Le formulaire revient avec la saisie, pour corriger sans tout retaper.
        saisie = {**actuelle, "customer_name": customer_name, "customer_phone": customer_phone,
                  "date": date, "time": time, "party_size": party_size, "notes": notes}
        return _fragment(request, user, saisie, rendu, edition=True, erreur=erreur,
                         status_code=422)
    resa = reservations.update_reservation(
        reservation_id,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip() or None,
        date=date, time=time, party_size=party_size,
        notes=notes.strip() or None,
    )
    reponse = _fragment(request, user, resa, rendu)
    if rendu == "carte" and (date, time) != (actuelle["date"], actuelle["time"]):
        # Déplacée : sa carte n'a plus sa place ici, ni les compteurs du jour. La page
        # se recharge et la montre à sa nouvelle heure.
        reponse.headers["HX-Refresh"] = "true"
    return reponse


@router.post("/admin/reservations/{reservation_id}/cancel",
             dependencies=[Depends(deps.verify_csrf)])
def reservation_cancel(request: Request, reservation_id: int,
                       user: User = Depends(deps.current_user), rendu: str = Form("")):
    """« Annuler » annule : la ligne reste, barrée et horodatée — comme une annulation
    faite au téléphone. Le lien s'appelait déjà « Annuler » mais EFFAÇAIT la ligne : le
    restaurateur perdait la preuve en cas de litige, et le client qui rappelait pour
    reprendre sa table n'était plus retrouvé. L'effacement d'un appelant passe par le
    droit à l'effacement (rgpd.effacer_appelant), pas par ce bouton."""
    _load_scoped(reservation_id, user)
    reponse = _fragment(request, user, reservations.cancel_reservation(reservation_id), rendu)
    if rendu == "carte":
        reponse.headers["HX-Refresh"] = "true"  # les couverts du jour ont changé
    return reponse
