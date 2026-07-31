"""Journal des appels : liste filtrée + panneau de détail (fragment htmx)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import calls, reservations, tenants
from ..users import User
from . import deps, presenters

router = APIRouter()

PAGE_SIZE = 25

FILTERS = [
    ("", "Tous"),
    ("reservation", "Avec réservation"),
    ("info", "Sans réservation"),
    ("failed", "Échecs"),
]


def _load_scoped(call_id: int, user: User) -> dict:
    call = calls.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    deps.check_tenant_access(user, call["tenant_id"])
    return call


def _pane_context(request: Request, call: dict) -> dict:
    """Contexte du panneau de détail : l'appel enrichi + la réservation qu'il a créée."""
    view = presenters.call_view(call)
    resa = reservations.get_reservation(call["reservation_id"]) if call["reservation_id"] else None
    return {"call": view, "reservation": resa,
            "tenant": tenants.get_by_id(call["tenant_id"])}


@router.get("/admin/calls")
async def calls_list(
    request: Request,
    user: User = Depends(deps.current_user),
    tenant_id: Optional[int] = None,
    outcome: Optional[str] = None,
    open: Optional[int] = None,
    page: int = 1,
):
    deps.ensure_csrf(request)
    if not user.is_superadmin:
        tenant_id = user.tenant_id
    outcome = outcome if outcome in calls.OUTCOME_FILTERS else None
    page = max(1, page)
    rows = calls.list_calls(
        tenant_id, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE, outcome=outcome
    )
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    views = [presenters.call_view(c) for c in rows]

    # Panneau ouvert : celui demandé (s'il est dans le périmètre) sinon le plus récent.
    opened = None
    if open is not None:
        try:
            opened = _pane_context(request, _load_scoped(open, user))
        except HTTPException:
            opened = None
    if opened is None and rows:
        opened = _pane_context(request, rows[0])

    tenant_names = {t.id: t.name for t in tenants.list_all()} if user.is_superadmin else {}
    return deps.templates.TemplateResponse(
        request, "calls/list.html",
        {
            "calls": views,
            "tenant_names": tenant_names,
            "tenants": tenants.list_all() if user.is_superadmin else [],
            "tenant_id": tenant_id,
            "outcome": outcome or "",
            "filters": FILTERS,
            "opened": opened,
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/admin/calls/{call_id}")
async def call_detail(request: Request, call_id: int,
                      user: User = Depends(deps.current_user)):
    """Page pleine : lien profond partageable, et repli si htmx n'est pas chargé."""
    deps.ensure_csrf(request)
    call = _load_scoped(call_id, user)
    return deps.templates.TemplateResponse(request, "calls/detail.html",
                                           _pane_context(request, call))
