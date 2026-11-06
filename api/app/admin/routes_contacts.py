"""Les messages écrits depuis le formulaire de contact du site (ASSISTANTE-185).

Réservé au super-admin : ce sont les messages adressés à Helmane, pas ceux d'un
établissement. Le texte vient d'un inconnu : il n'est affiché que par l'échappement de
Jinja, jamais en HTML brut."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import contact, db
from . import deps

router = APIRouter()


def _page(request: Request):
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "contacts.html", {"contacts": contact.lister()})


@router.get("/admin/contacts", dependencies=[Depends(deps.require_superadmin)])
async def contacts_page(request: Request):
    return await db.hors_boucle(_page, request)


@router.post("/admin/contacts/{contact_id}/lu",
             dependencies=[Depends(deps.verify_csrf), Depends(deps.require_superadmin)])
async def contact_lu(contact_id: int):
    await db.hors_boucle(contact.marquer_lu, contact_id)
    return RedirectResponse("/admin/contacts", status_code=303)
