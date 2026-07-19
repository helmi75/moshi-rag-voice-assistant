"""Journal des appels : liste paginée + détail avec transcript."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import calls, tenants
from ..users import User
from . import deps

router = APIRouter()

PAGE_SIZE = 25


@router.get("/admin/calls")
async def calls_list(
    request: Request,
    user: User = Depends(deps.current_user),
    tenant_id: Optional[int] = None,
    page: int = 1,
):
    deps.ensure_csrf(request)
    if not user.is_superadmin:
        tenant_id = user.tenant_id
    page = max(1, page)
    rows = calls.list_calls(tenant_id, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    has_next = len(rows) > PAGE_SIZE
    tenant_names = {t.id: t.name for t in tenants.list_all()} if user.is_superadmin else {}
    return deps.templates.TemplateResponse(
        request, "calls/list.html",
        {
            "calls": rows[:PAGE_SIZE],
            "tenant_names": tenant_names,
            "tenants": tenants.list_all() if user.is_superadmin else [],
            "tenant_id": tenant_id,
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/admin/calls/{call_id}")
async def call_detail(request: Request, call_id: int,
                      user: User = Depends(deps.current_user)):
    call = calls.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    deps.check_tenant_access(user, call["tenant_id"])
    try:
        transcript = json.loads(call["transcript"]) if call["transcript"] else []
    except (TypeError, ValueError):
        transcript = []
    tenant = tenants.get_by_id(call["tenant_id"])
    return deps.templates.TemplateResponse(
        request, "calls/detail.html",
        {"call": call, "transcript": transcript, "tenant": tenant},
    )
