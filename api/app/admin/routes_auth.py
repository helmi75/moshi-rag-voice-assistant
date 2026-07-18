"""Login / logout de la plateforme admin."""
import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import users
from . import deps

router = APIRouter()


@router.get("/admin/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/admin/", status_code=303)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/admin/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = users.get_by_email(email)
    # bcrypt ≈ 100-200 ms : en thread pour ne pas geler l'event loop (appels vocaux).
    ok = user is not None and await asyncio.to_thread(
        users.verify_password, password, user.password_hash
    )
    if not ok:
        # Message générique : ne révèle pas si l'email existe.
        return deps.templates.TemplateResponse(
            request, "login.html", {"error": "Identifiants invalides."}, status_code=401
        )
    request.session.clear()
    request.session["user_id"] = user.id
    deps.ensure_csrf(request)
    return RedirectResponse("/admin/", status_code=303)


@router.post("/admin/logout", dependencies=[Depends(deps.verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)
