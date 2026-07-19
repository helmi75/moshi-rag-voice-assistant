"""Réservations : tableau filtré/paginé + édition inline htmx (_row ⇄ _row_edit)."""
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import reservations, tenants
from ..users import User
from . import deps

router = APIRouter()

PAGE_SIZE = 25


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
    rows = reservations.list_filtered(
        tenant_id=tenant_id, date_from=date_from or None,
        limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE,
    )
    has_next = len(rows) > PAGE_SIZE
    tenant_names = {t.id: t.name for t in tenants.list_all()} if user.is_superadmin else {}
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
        {"r": resa, "tenant_names": {t.id: t.name for t in tenants.list_all()}},
    )


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
    _load_scoped(reservation_id, user)
    resa = reservations.update_reservation(
        reservation_id,
        customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip() or None,
        date=date, time=time, party_size=party_size,
        notes=notes.strip() or None,
    )
    return deps.templates.TemplateResponse(
        request, "reservations/_row.html",
        {"r": resa, "tenant_names": {t.id: t.name for t in tenants.list_all()}},
    )


@router.post("/admin/reservations/{reservation_id}/delete",
             dependencies=[Depends(deps.verify_csrf)])
async def reservation_delete(reservation_id: int, user: User = Depends(deps.current_user)):
    _load_scoped(reservation_id, user)
    reservations.delete_reservation(reservation_id)
    return HTMLResponse("")  # htmx hx-swap="delete" retire la ligne
