"""Config voix par tenant : accueil (re-rendu auto), aperçu WAV, musique d'attente."""
import asyncio
import io
import wave

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .. import tenants
from ..users import User
from ..voice import greeting as greeting_mod
from . import deps

router = APIRouter()

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 Mo
_TWILIO_RATE = 8000


@router.get("/admin/tenants/{tenant_id}/voice")
async def voice_settings(request: Request, tenant_id: int,
                         user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    deps.ensure_csrf(request)
    return deps.templates.TemplateResponse(
        request, "voice/settings.html",
        {
            "tenant": tenant,
            "greeting_ready": greeting_mod.cached_greeting_path(tenant) is not None,
            "has_custom_music": greeting_mod.hold_music_path(tenant.id)
            != greeting_mod.hold_music_path(None),
            "error": None,
        },
    )


@router.post("/admin/tenants/{tenant_id}/voice", dependencies=[Depends(deps.verify_csrf)])
async def voice_update(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.current_user),
    greeting: str = Form(...),
):
    tenant = deps.resolve_tenant(tenant_id, user)
    g = greeting.strip() or None
    # Un accueil non vide saisi par le restaurateur est marqué « personnalisé » : ainsi
    # seed_demo_tenant ne le réécrasera jamais au redémarrage (cf. tenants.py). Le vider
    # rend la main au défaut géré (greeting_customized=0).
    tenants.update_tenant(tenant.id, greeting=g, greeting_customized=1 if g else 0)
    refreshed = tenants.get_by_id(tenant.id)
    if refreshed is not None and greeting_mod.is_moshi_server():
        # Re-rendu en tâche de fond (60-90 s si GPU froid) : jamais bloquant ici,
        # l'UI polle /greeting/status jusqu'à ce que le WAV soit prêt.
        asyncio.create_task(greeting_mod.ensure_greeting_wav(refreshed))
    return RedirectResponse(f"/admin/tenants/{tenant.id}/voice", status_code=303)


@router.get("/admin/tenants/{tenant_id}/greeting.wav")
async def greeting_wav(tenant_id: int, user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    path = greeting_mod.cached_greeting_path(tenant)
    if path is None:
        raise HTTPException(status_code=404, detail="Accueil pas encore rendu.")
    return FileResponse(path, media_type="audio/wav")


@router.get("/admin/tenants/{tenant_id}/greeting/status")
async def greeting_status(request: Request, tenant_id: int,
                          user: User = Depends(deps.current_user)):
    tenant = deps.resolve_tenant(tenant_id, user)
    return deps.templates.TemplateResponse(
        request, "voice/_greeting_status.html",
        {"tenant": tenant,
         "greeting_ready": greeting_mod.cached_greeting_path(tenant) is not None},
    )


@router.post("/admin/tenants/{tenant_id}/hold-music", dependencies=[Depends(deps.verify_csrf)])
async def hold_music_upload(
    request: Request,
    tenant_id: int,
    user: User = Depends(deps.current_user),
    file: UploadFile = File(...),
):
    tenant = deps.resolve_tenant(tenant_id, user)
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
async def hold_music_delete(tenant_id: int, user: User = Depends(deps.current_user)):
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
