"""Config voix par tenant : accueil (re-rendu auto), aperçu WAV, musique d'attente."""
import io
import os
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .. import db, taches, tenants
from ..users import User
from ..voice import greeting as greeting_mod
from ..voice import voices
from . import deps, presenters

router = APIRouter()

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 Mo
_TWILIO_RATE = 8000


@router.get("/admin/tenants/{tenant_id}/voice")
def voice_settings(request: Request, tenant_id: int,
                         user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    deps.ensure_csrf(request)
    return _page(request, tenant)


def _groupes(catalogue) -> list[dict]:
    """Les voix regroupées par locuteur (« Marie — Français »), dans l'ordre du catalogue :
    le français d'abord."""
    groupes: dict = {}
    for v in catalogue:
        cle = (v.locuteur, v.note)
        groupes.setdefault(cle, {"titre": f"{v.locuteur} — {v.note}", "voix": []})["voix"].append(v)
    return list(groupes.values())


def _etat_accueil(tenant) -> dict:
    """Ce que montre le bloc « accueil » : prêt ou non, l'adresse VERSIONNÉE de l'aperçu,
    et le texte réellement prononcé.

    L'adresse de l'aperçu porte le nom du fichier rendu, qui change avec la voix et le
    texte (SCRUM-106) : à adresse fixe, le navigateur rejouait sa copie — Helmi
    entendait encore la voix Moshi alors que le serveur avait rendu l'accueil en voix
    Mistral. Le texte affiché est celui de `rgpd.accueil` : accueil PUIS mention."""
    from .. import rgpd

    chemin = greeting_mod.cached_greeting_path(tenant)
    return {
        "greeting_ready": chemin is not None,
        "greeting_version": chemin.stem if chemin is not None else "",
        "texte_prononce": rgpd.accueil(tenant),
        "voix_sans_gpu": greeting_mod.voix_sans_gpu(tenant),
    }


def _page(request: Request, tenant, error: Optional[str] = None, status_code: int = 200):
    return deps.templates.TemplateResponse(
        request, "voice/settings.html",
        {
            "tenant": tenant,
            "voice_name": presenters.voice_label(tenant),
            "groupes": _groupes(voices.voix_voxtral()),
            "voxtral_disponible": voices.voxtral_disponible(),
            "voice_id": voices.resolve(tenant),
            **_etat_accueil(tenant),
            "has_custom_music": greeting_mod.hold_music_path(tenant.id)
            != greeting_mod.hold_music_path(None),
            "error": error,
        },
        status_code=status_code,
    )


@router.post("/admin/tenants/{tenant_id}/voice", dependencies=[Depends(deps.verify_csrf)])
async def voice_update(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.current_user),
    greeting: Optional[str] = Form(None),
    voice: Optional[str] = Form(None),
):
    """Enregistre l'accueil et/ou la voix. Les deux cartes de l'écran postent ici,
    chacune avec son seul champ : un champ absent n'écrase pas le réglage en place."""
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    fields: dict = {}
    if greeting is not None:
        # Un accueil non vide saisi par le restaurateur est marqué « personnalisé »
        # (greeting_customized) ; le vider rend la main à l'accueil par défaut.
        g = greeting.strip() or None
        fields.update(greeting=g, greeting_customized=1 if g else 0)
    # Voix : on n'enregistre QUE ce qui est au catalogue. Une valeur forgée (formulaire
    # bricolé, catalogue réduit depuis) serait acceptée telle quelle par moshi-server,
    # qui la remplacerait en silence par sa voix de repli — l'appelant serait le seul
    # à s'en apercevoir. Hors catalogue -> on ne touche pas au réglage existant.
    chosen = voices.get((voice or "").strip())
    if chosen is not None and chosen.fournisseur == voices.VOXTRAL \
            and not voices.voxtral_disponible():
        # Voix Mistral sans clé : l'appel retomberait sur la voix de secours sans que
        # personne ne l'ait voulu. On refuse ici, avec la raison.
        return _page(request, tenant, status_code=422, error=(
            "Les voix Mistral ne sont pas disponibles : la clé Mistral n'est pas posée sur "
            "le serveur. La voix n'a pas été changée."))
    if chosen is not None:
        fields["voice"] = chosen.id
    if fields:
        await db.hors_boucle(tenants.update_tenant, tenant.id, **fields)
    refreshed = await db.hors_boucle(tenants.get_by_id, tenant.id)
    if refreshed is not None and greeting_mod.pre_rendu_possible(refreshed):
        # Re-rendu en tâche de fond (60-90 s si GPU froid, 2 s avec Voxtral) : jamais bloquant ici,
        # l'UI polle /greeting/status jusqu'à ce que le WAV soit prêt.
        taches.lancer(greeting_mod.ensure_greeting_wav(refreshed),
                      nom=f"accueil de l'établissement {tenant.id}")
    return RedirectResponse(f"/admin/tenants/{tenant.id}/voice", status_code=303)


# Un extrait par voix, pour choisir à l'oreille. Rendu à la première écoute puis gardé :
# ~90 caractères, soit 0,15 c, une fois par voix. En qualité téléphone (8 kHz, µ-law) :
# c'est ce qu'entendront les clients, pas ce que rend un casque de studio.
_EXTRAIT = {
    "fr": "Bonjour, vous êtes bien au restaurant. C'est pour combien de personnes, et à quelle heure ?",
    "en": "Hello, you've reached the restaurant. How many people will it be, and at what time?",
}


def _dossier_extraits() -> Path:
    return Path(os.getenv("VOIX_EXTRAITS_DIR", "/app/data/voix_extraits"))


@router.get("/admin/voix/{slug}/extrait.wav")
async def extrait_de_voix(slug: str, user: User = Depends(deps.current_user)):
    voix = voices.get(f"voxtral/{slug}")
    # Liste fermée : seul ce que Mistral a listé se rend — jamais un identifiant saisi.
    if voix is None or voix.fournisseur != voices.VOXTRAL:
        raise HTTPException(status_code=404, detail="Voix inconnue.")
    chemin = _dossier_extraits() / f"{slug}.wav"
    if not chemin.exists():
        if not voices.voxtral_disponible():
            raise HTTPException(status_code=503, detail="Clé Mistral absente : extrait impossible.")
        from ..voice import ulaw
        from ..voice.voxtral_tts import rendre_pcm

        try:
            texte = _EXTRAIT["fr" if (voix.langue or "").startswith("fr") else "en"]
            pcm = await rendre_pcm(texte, voix.voxtral_id)
            huit = await greeting_mod._to_twilio_int16(pcm)
            chemin.parent.mkdir(parents=True, exist_ok=True)
            greeting_mod._write_wav(chemin, ulaw.decoder(ulaw.encoder(huit)))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Mistral n'a pas rendu l'extrait ({exc}).")
    return FileResponse(chemin, media_type="audio/wav")


@router.get("/admin/tenants/{tenant_id}/greeting.wav")
def greeting_wav(tenant_id: int, user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    path = greeting_mod.cached_greeting_path(tenant)
    if path is None:
        raise HTTPException(status_code=404, detail="Accueil pas encore rendu.")
    # L'adresse est versionnée ; « no-cache » protège en plus les anciens liens.
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@router.get("/admin/tenants/{tenant_id}/greeting/status")
def greeting_status(request: Request, tenant_id: int,
                          user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    return deps.templates.TemplateResponse(
        request, "voice/_greeting_status.html",
        {"tenant": tenant, **_etat_accueil(tenant)},
    )


@router.post("/admin/tenants/{tenant_id}/hold-music", dependencies=[Depends(deps.verify_csrf)])
async def hold_music_upload(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.current_user),
    file: UploadFile = File(...),
):
    tenant = await db.hors_boucle(deps.resolve_tenant, tenant_id, user)
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop grand (5 Mo max).")
    try:
        pcm_8k = await _to_twilio_wav(data)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Format non reconnu : fournir un WAV PCM 16 bits (mono ou stéréo).",
        )
    # Écriture atomique (un WAV partiel jouerait du bruit pendant un appel).
    dest = greeting_mod.hold_music_dir() / f"tenant{tenant.id}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_TWILIO_RATE)
        w.writeframes(pcm_8k)
    tmp.replace(dest)
    return RedirectResponse(f"/admin/tenants/{tenant.id}/voice", status_code=303)


@router.post("/admin/tenants/{tenant_id}/hold-music/delete",
             dependencies=[Depends(deps.verify_csrf)])
def hold_music_delete(tenant_id: int, user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    path = greeting_mod.hold_music_dir() / f"tenant{tenant.id}.wav"
    path.unlink(missing_ok=True)
    return RedirectResponse(f"/admin/tenants/{tenant.id}/voice", status_code=303)


async def _to_twilio_wav(data: bytes) -> bytes:
    """WAV PCM 16 bits (mono/stéréo, tout débit) → PCM mono 8 kHz int16 (débit Twilio).
    Lève si le fichier n'est pas un WAV PCM 16 bits (un MP3 renommé est rejeté)."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("PCM 16 bits requis")
        channels = w.getnchannels()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
    elif channels != 1:
        raise ValueError("mono ou stéréo uniquement")
    if rate != _TWILIO_RATE:
        # Rééchantillonnage anti-repliement de Pipecat (audioop disparaît en 3.13).
        from pipecat.audio.utils import create_stream_resampler

        resampler = create_stream_resampler()
        return await resampler.resample(samples.tobytes(), rate, _TWILIO_RATE)
    return samples.tobytes()
