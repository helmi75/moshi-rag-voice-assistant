"""Tableau de bord : cartes de synthèse + graphiques (fragment htmx)."""
from typing import Optional

from fastapi import APIRouter, Depends, Request

from .. import calls
from ..users import User
from . import charts, deps

router = APIRouter()


def _scope(user: User, tenant_id: Optional[int]) -> Optional[int]:
    """Périmètre des stats : restaurateur = SON tenant, super-admin = tous ou filtré."""
    if not user.is_superadmin:
        return user.tenant_id
    return tenant_id


@router.get("/admin/")
async def dashboard(request: Request, tenant_id: Optional[int] = None,
                    user: User = Depends(deps.current_user)):
    deps.ensure_csrf(request)
    scope = _scope(user, tenant_id)
    stats = calls.stats_daily(scope, days=30)
    totals = {
        "calls": sum(s["n_calls"] for s in stats),
        "reservations": sum(s["n_reservations"] for s in stats),
        "cost": sum(s["total_cost"] for s in stats),
    }
    with_resa = sum(s["n_with_reservation"] for s in stats)
    totals["conversion"] = round(100 * with_resa / totals["calls"]) if totals["calls"] else 0
    return deps.templates.TemplateResponse(
        request, "dashboard.html",
        {"totals": totals, "stats": stats, "tenant_id": scope},
    )


@router.get("/admin/stats/charts")
async def stats_charts(request: Request, tenant_id: Optional[int] = None, days: int = 30,
                       user: User = Depends(deps.current_user)):
    scope = _scope(user, tenant_id)
    stats = calls.stats_daily(scope, days=min(days, 90))
    calls_svg = charts.bar_chart(
        [(s["day"][5:], s["n_calls"]) for s in stats], title="Appels par jour"
    )
    resas_svg = charts.bar_chart(
        [(s["day"][5:], s["n_reservations"]) for s in stats],
        title="Réservations par jour", series=2,
    )
    return deps.templates.TemplateResponse(
        request, "_charts.html", {"calls_svg": calls_svg, "resas_svg": resas_svg}
    )
