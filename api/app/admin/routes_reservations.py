"""Réservations : tableau filtré/paginé + édition inline htmx (_row ⇄ _row_edit)."""
from datetime import date as _date, time as _time
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import reservations, tenants
from ..users import User
from . import deps

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


@router.get("/admin/reservations")
async def reservations_list(
    request: Request,
    user: User = Depends(deps.current_user),
    tenant_id: Optional[int] = None,
    date_from: Optional[str] = None,
    page: int = 1,
):
    deps.ensure_csrf(request)
    if not user.is_superadmin:
        tenant_id = user.tenant_id
    page = max(1, page)
    # Cet écran-ci MONTRE les annulées : un restaurateur qui voit « annulée à 15h32 »
    # peut reproposer le créneau, et retrouver la preuve si le client conteste. La liste
    # « à venir » de la salle de contrôle, elle, garde le défaut qui les masque — y
    # laisser des annulées ferait préparer des couverts pour personne.
    rows = reservations.list_filtered(
        tenant_id=tenant_id, date_from=date_from or None,
        limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE,
        inclure_annulees=True,
    )
    has_next = len(rows) > PAGE_SIZE
    tenant_names = _tenant_names(user)
    return deps.templates.TemplateResponse(
        request, "reservations/list.html",
        {
            "reservations": rows[:PAGE_SIZE],
            "tenant_names": tenant_names,
            "tenants": tenants.list_all() if user.is_superadmin else [],
            "tenant_id": tenant_id,
            "date_from": date_from or "",
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/admin/reservations/{reservation_id}/edit")
async def reservation_edit(request: Request, reservation_id: int,
                           user: User = Depends(deps.current_user)):
    resa = _load_scoped(reservation_id, user)
    return deps.templates.TemplateResponse(request, "reservations/_row_edit.html", {"r": resa})


@router.get("/admin/reservations/{reservation_id}/row")
async def reservation_row(request: Request, reservation_id: int,
                          user: User = Depends(deps.current_user)):
    resa = _load_scoped(reservation_id, user)
    return deps.templates.TemplateResponse(
        request, "reservations/_row.html",
        {"r": resa, "tenant_names": _tenant_names(user)},
    )


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
async def reservation_update(
    request: Request,
    reservation_id: int,
    user: User = Depends(deps.current_user),
    customer_name: str = Form(...),
    customer_phone: str = Form(""),
    date: str = Form(...),
    time: str = Form(...),
    party_size: int = Form(...),
    notes: str = Form(""),
):
    actuelle = _load_scoped(reservation_id, user)
    erreur = _saisie_invalide(customer_name, date, time, party_size)
    if erreur:
        # La ligne d'édition revient avec la saisie, pour corriger sans tout retaper.
        saisie = {**actuelle, "customer_name": customer_name, "customer_phone": customer_phone,
                  "date": date, "time": time, "party_size": party_size, "notes": notes}
        return deps.templates.TemplateResponse(
            request, "reservations/_row_edit.html", {"r": saisie, "error": erreur},
            status_code=422,
        )
    resa = reservations.update_reservation(
        reservation_id,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip() or None,
        date=date, time=time, party_size=party_size,
        notes=notes.strip() or None,
    )
    return deps.templates.TemplateResponse(
        request, "reservations/_row.html",
        {"r": resa, "tenant_names": _tenant_names(user)},
    )


@router.post("/admin/reservations/{reservation_id}/cancel",
             dependencies=[Depends(deps.verify_csrf)])
async def reservation_cancel(request: Request, reservation_id: int,
                             user: User = Depends(deps.current_user)):
    """« Annuler » annule : la ligne reste, barrée et horodatée — comme une annulation
    faite au téléphone. Le lien s'appelait déjà « Annuler » mais EFFAÇAIT la ligne : le
    restaurateur perdait la preuve en cas de litige, et le client qui rappelait pour
    reprendre sa table n'était plus retrouvé. L'effacement d'un appelant passe par le
    droit à l'effacement (rgpd.effacer_appelant), pas par ce bouton."""
    _load_scoped(reservation_id, user)
    resa = reservations.cancel_reservation(reservation_id)
    return deps.templates.TemplateResponse(
        request, "reservations/_row.html",
        {"r": resa, "tenant_names": _tenant_names(user)},
    )
