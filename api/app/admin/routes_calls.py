"""Journal des appels : liste filtrée + panneau de détail (fragment htmx)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import calls, messages, reservations, tenants
from ..users import User
from . import deps, presenters

router = APIRouter()

PAGE_SIZE = 25

FILTERS = [
    ("", "Tous"),
    ("reservation", "Avec réservation"),
    ("message", "Rappel promis"),
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
    """Contexte du panneau de détail : l'appel enrichi, la réservation qu'il a créée, et
    les messages pris — c'est là qu'un rappel promis doit se voir."""
    view = presenters.call_view(call)
    resa = reservations.get_reservation(call["reservation_id"]) if call["reservation_id"] else None
    return {"call": view, "reservation": resa,
            "messages": messages.messages_d_un_appel(call["id"]),
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


@router.post("/admin/messages/{message_id}/traite",
             dependencies=[Depends(deps.verify_csrf)])
async def message_traite(request: Request, message_id: int,
                         user: User = Depends(deps.current_user)):
    """Marque un rappel comme traité.

    Le `tenant_id` part dans la clause WHERE de `marquer_traite`, il n'est pas seulement
    vérifié avant : un identifiant deviné ne touche donc pas le message d'un autre
    établissement, même si la vérification préalable était contournée."""
    from fastapi.responses import RedirectResponse

    message = messages.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404)
    deps.check_tenant_access(user, message["tenant_id"])
    messages.marquer_traite(message_id, message["tenant_id"])
    retour = (await request.form()).get("retour") or "/admin/calls"
    # Jamais une URL fournie librement : seul un chemin interne est accepté, sinon on
    # offrirait une redirection ouverte depuis une page authentifiée.
    if not str(retour).startswith("/admin/"):
        retour = "/admin/calls"
    return RedirectResponse(str(retour), status_code=303)


@router.get("/admin/calls/{call_id}")
async def call_detail(request: Request, call_id: int,
                      user: User = Depends(deps.current_user)):
    """Page pleine : lien profond partageable, et repli si htmx n'est pas chargé."""
    deps.ensure_csrf(request)
    call = _load_scoped(call_id, user)
    return deps.templates.TemplateResponse(request, "calls/detail.html",
                                           _pane_context(request, call))


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic d'un appel (#88) — réécouter et disséquer
#
# Les deux routes passent par `_load_scoped`, donc par `check_tenant_access` : un
# restaurateur n'atteint jamais l'appel d'un autre établissement, ni sa transcription,
# ni sa voix.
# ─────────────────────────────────────────────────────────────────────────────

# Ce qu'on accepte comme piste. Liste FERMÉE, et c'est un paramètre de REQUÊTE typé
# `str` — pas un segment d'URL, pas un `Literal`. Raison précise : un type contraint
# ferait répondre 422 à FastAPI avant toute vérification d'autorisation, et
# `test_admin_security.py` compte un 422 comme « l'autorisation n'a PAS été atteinte »,
# c'est-à-dire comme une fuite non prouvée. On valide donc à la main, et on renvoie 404.
_PISTES_SERVIES = ("stereo", "appelant", "assistante")


def _pistes_presentes(call: dict) -> list[str]:
    """Les pistes réellement sur le disque. Le contrôle vit ICI et non dans
    `presenters.py`, qui s'interdit tout accès autre qu'à ce qui est déjà en mémoire.

    Sans cette vérification, la page proposerait un lecteur qui répondrait 404 — pire
    qu'un message honnête disant qu'il n'y a pas d'enregistrement."""
    from ..voice import enregistrement

    presentes = []
    for piste in enregistrement.PISTES:
        try:
            if enregistrement.chemin(call["tenant_id"], call["id"], piste).exists():
                presentes.append(piste)
        except (OSError, ValueError):
            continue
    return presentes


@router.get("/admin/calls/{call_id}/diagnostic")
async def call_diagnostic(request: Request, call_id: int,
                          user: User = Depends(deps.current_user)):
    """Réécouter un appel et lire sa chronologie, tour par tour."""
    call = _load_scoped(call_id, user)
    deps.ensure_csrf(request)
    contexte = _pane_context(request, call)
    contexte["journal"] = presenters.parse_journal(call.get("journal"))
    contexte["tours"] = presenters.tours_du_journal(contexte["journal"])
    contexte["pistes"] = _pistes_presentes(call)
    return deps.templates.TemplateResponse(request, "calls/diagnostic.html", contexte)


@router.get("/admin/calls/{call_id}/audio.wav")
async def call_audio(call_id: int, piste: str = "stereo",
                     user: User = Depends(deps.current_user)):
    """Sert l'enregistrement, en WAV, décodé à la volée depuis le µ-law du disque.

    L'en-tête WAV est calculé ICI, à partir de la taille réelle du fichier : c'est ce
    qui rend lisible un enregistrement interrompu par un arrêt brutal — précisément
    l'appel qu'on veut réécouter.
    """
    import asyncio

    from fastapi.responses import Response

    from ..voice import enregistrement, ulaw

    call = _load_scoped(call_id, user)
    if piste not in _PISTES_SERVIES:
        raise HTTPException(status_code=404)

    def _lire(nom: str) -> bytes:
        chemin = enregistrement.chemin(call["tenant_id"], call["id"], nom)
        return ulaw.decoder(chemin.read_bytes()) if chemin.exists() else b""

    def _assembler() -> tuple[bytes, int]:
        if piste == "stereo":
            # Appelant à gauche, assistante à droite : on ENTEND qui parle par-dessus
            # qui, ce qu'un mixage laisserait seulement deviner.
            return ulaw.entrelacer(_lire("appelant"), _lire("assistante")), 2
        return _lire(piste), 1

    # Lecture et décodage DANS UN THREAD : l'admin tourne dans le même processus que
    # les appels en cours, et Twilio attend une trame toutes les 20 ms. Un appel de dix
    # minutes en stéréo, c'est ~20 Mo à décoder — bloquer la boucle d'événements ce
    # temps-là ferait bégayer la voix d'un client au téléphone pour qu'un admin réécoute.
    try:
        pcm, canaux = await asyncio.to_thread(_assembler)
    except OSError:
        raise HTTPException(status_code=404)
    if not pcm:
        raise HTTPException(status_code=404)
    return Response(content=ulaw.entete_wav(len(pcm), canaux=canaux) + pcm,
                    media_type="audio/wav")
