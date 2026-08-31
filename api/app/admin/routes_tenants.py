"""CRUD des établissements (tenants) + comptes restaurateurs. Super-admin, sauf
l'édition de SA fiche par le restaurateur."""
import asyncio
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import plans, tenants, users
from ..users import User
from . import deps

router = APIRouter()


@router.get("/admin/tenants")
async def tenants_list(request: Request, user: User = Depends(deps.require_superadmin)):
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/list.html", {"tenants": tenants.list_all()}
    )


@router.get("/admin/tenants/new")
async def tenant_new(request: Request, user: User = Depends(deps.require_superadmin)):
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/form.html",
        {"tenant": None, "error": None, "formules": plans.catalogue()},
    )


@router.post("/admin/tenants", dependencies=[Depends(deps.verify_csrf)])
async def tenant_create(
    request: Request,
    user: User = Depends(deps.require_superadmin),
    name: str = Form(...),
    phone_number: str = Form(...),
    business_type: str = Form("restaurant"),
    language: str = Form("fr-FR"),
    greeting: str = Form(""),
    knowledge_base: str = Form(""),
    plan: Optional[str] = Form(None),
):
    try:
        tenant = tenants.create_tenant(
            name.strip(), phone_number.strip(), business_type.strip(),
            language.strip(), greeting.strip() or None, knowledge_base,
        )
    except sqlite3.IntegrityError:
        return deps.templates.TemplateResponse(
            request, "tenants/form.html",
            {"tenant": None, "error": f"Le numéro {phone_number} est déjà utilisé.",
             "formules": plans.catalogue()},
            status_code=409,
        )
    # `create_tenant` ne prend pas la formule (signature historique) : on la pose juste
    # après, et seulement si elle appartient au catalogue. Sans ça, l'établissement
    # naîtrait sur la formule par défaut sans que personne ne l'ait décidé.
    if plans.get(plan) is not None:
        tenants.update_tenant(tenant.id, plan=plan)
    _prerender_greeting(tenant.id)
    return RedirectResponse("/admin/tenants", status_code=303)


@router.get("/admin/tenants/{tenant_id}/edit")
async def tenant_edit(request: Request, tenant_id: int,
                      user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/form.html",
        {"tenant": tenant, "error": None, "formules": plans.catalogue()},
    )


@router.post("/admin/tenants/{tenant_id}", dependencies=[Depends(deps.verify_csrf)])
async def tenant_update(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.current_user),
    name: str = Form(...),
    phone_number: Optional[str] = Form(None),
    business_type: str = Form("restaurant"),
    language: str = Form("fr-FR"),
    greeting: str = Form(""),
    knowledge_base: str = Form(""),
    plan: Optional[str] = Form(None),
):
    tenant = deps.resolve_tenant(tenant_id, user)
    fields = {
        "name": name.strip(),
        "business_type": business_type.strip(),
        "language": language.strip(),
        "greeting": greeting.strip() or None,
        "knowledge_base": knowledge_base,
    }
    # Le numéro de téléphone (routage Twilio) est réservé au super-admin.
    if user.is_superadmin and phone_number is not None:
        fields["phone_number"] = phone_number.strip()
    # La formule aussi : elle décide du plafond et de ce qui sera facturé. Un
    # restaurateur qui pourrait se l'attribuer choisirait son propre tarif.
    # `plans.get` rejette toute valeur hors catalogue — un formulaire forgé ne peut
    # donc pas inscrire un plafond que personne n'a vendu.
    if user.is_superadmin and plans.get(plan) is not None:
        fields["plan"] = plan
    greeting_changed = (greeting.strip() or None) != tenant.greeting
    try:
        tenants.update_tenant(tenant.id, **fields)
    except sqlite3.IntegrityError:
        return deps.templates.TemplateResponse(
            request, "tenants/form.html",
            {"tenant": tenant, "error": f"Le numéro {phone_number} est déjà utilisé.",
             "formules": plans.catalogue()},
            status_code=409,
        )
    if greeting_changed:
        _prerender_greeting(tenant.id)
    back = "/admin/tenants" if user.is_superadmin else f"/admin/tenants/{tenant.id}/edit"
    return RedirectResponse(back, status_code=303)


@router.get("/admin/tenants/{tenant_id}/knowledge")
async def tenant_knowledge(request: Request, tenant_id: int,
                           user: User = Depends(deps.current_user)):
    """« Ce que l'IA sait » : la base de connaissances en fiches (lecture).

    Aucun stockage nouveau — le texte est déjà sectionné par des titres `##`, et
    c'est ce même texte qui part dans le prompt système."""
    tenant = deps.resolve_tenant(tenant_id, user)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/knowledge.html",
        {"tenant": tenant,
         "sections": tenants.parse_knowledge_sections(tenant.knowledge_base)},
    )


@router.post("/admin/tenants/{tenant_id}/delete", dependencies=[Depends(deps.verify_csrf)])
async def tenant_delete(tenant_id: int, user: User = Depends(deps.require_superadmin)):
    if tenants.get_by_id(tenant_id) is None:
        raise HTTPException(status_code=404)
    tenants.delete_tenant(tenant_id)
    return RedirectResponse("/admin/tenants", status_code=303)


# ---------------------------------------------------------------------------
# Comptes restaurateurs (super-admin)
# ---------------------------------------------------------------------------
@router.get("/admin/tenants/{tenant_id}/users")
async def tenant_users(request: Request, tenant_id: int,
                       user: User = Depends(deps.require_superadmin)):
    tenant = deps.resolve_tenant(tenant_id, user)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "tenants/users.html",
        {"tenant": tenant, "accounts": users.list_users(tenant.id), "error": None},
    )


@router.post("/admin/tenants/{tenant_id}/users", dependencies=[Depends(deps.verify_csrf)])
async def tenant_user_create(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.require_superadmin),
    email: str = Form(...),
    password: str = Form(...),
):
    tenant = deps.resolve_tenant(tenant_id, user)
    if users.trop_court(password):
        return deps.templates.TemplateResponse(
            request, "tenants/users.html",
            {"tenant": tenant, "accounts": users.list_users(tenant.id),
             "error": f"Mot de passe trop court : {users.MIN_MOT_DE_PASSE} "
                      f"caractères minimum."},
            status_code=400,
        )
    try:
        # bcrypt en thread : ne pas geler l'event loop (appels vocaux en parallèle).
        await asyncio.to_thread(
            users.create_user, email, password, users.ROLE_RESTAURATEUR, tenant.id
        )
    except sqlite3.IntegrityError:
        return deps.templates.TemplateResponse(
            request, "tenants/users.html",
            {"tenant": tenant, "accounts": users.list_users(tenant.id),
             "error": f"L'email {email} est déjà utilisé."},
            status_code=409,
        )
    return RedirectResponse(f"/admin/tenants/{tenant.id}/users", status_code=303)


@router.post("/admin/users/{user_id}/delete", dependencies=[Depends(deps.verify_csrf)])
async def user_delete(user_id: int, user: User = Depends(deps.require_superadmin)):
    target = users.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404)
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Impossible de supprimer son propre compte.")
    users.delete_user(user_id)
    back = f"/admin/tenants/{target.tenant_id}/users" if target.tenant_id else "/admin/tenants"
    return RedirectResponse(back, status_code=303)


@router.post("/admin/users/{user_id}/password", dependencies=[Depends(deps.verify_csrf)])
async def user_password(
    user_id: int,
    user: User = Depends(deps.current_user),
    password: str = Form(...),
):
    target = users.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404)
    # Un restaurateur ne change que SON mot de passe ; le super-admin, tous.
    if not user.is_superadmin and target.id != user.id:
        raise HTTPException(status_code=403)
    if users.trop_court(password):
        raise HTTPException(
            status_code=400,
            detail=f"Mot de passe trop court : {users.MIN_MOT_DE_PASSE} caractères minimum.",
        )
    await asyncio.to_thread(users.update_password, user_id, password)
    back = (
        f"/admin/tenants/{target.tenant_id}/users"
        if user.is_superadmin and target.tenant_id
        else "/admin/"
    )
    return RedirectResponse(back, status_code=303)


def _prerender_greeting(tenant_id: int) -> None:
    """Re-rend le WAV d'accueil en tâche de fond (60-90 s si GPU froid) — jamais
    bloquant depuis une route. Best-effort : l'échec laisse le repli TTS live."""
    from ..voice import greeting as greeting_mod

    tenant = tenants.get_by_id(tenant_id)
    if tenant is not None and greeting_mod.is_moshi_server():
        asyncio.create_task(greeting_mod.ensure_greeting_wav(tenant))
