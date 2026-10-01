"""Le message vocal laissé pendant une panne, rapatrié chez nous (ASSISTANTE-118).

Quand l'assistante est en panne et que personne ne peut décrocher, le client parle au
répondeur de Twilio (app/renvoi.py). C'est Twilio qui enregistre : à cet instant, notre
pipeline est justement ce qui ne marche pas. Mais un message vocal n'a pas à rester
chez lui indéfiniment — nos enregistrements ont une durée de conservation, une purge,
et une page pour les écouter. Dès que Twilio annonce le fichier prêt, on le télécharge,
on le range avec les enregistrements d'appels (piste « appelant », à la suite de ce que
l'appel avait déjà enregistré), et on l'efface de chez Twilio.

Si le rapatriement échoue, le message reste chez Twilio et le restaurant garde
l'essentiel : il a été prévenu par e-mail, avec le numéro à rappeler.
"""
import asyncio
import io
import os
import re
import wave
from typing import Optional

import httpx
import numpy as np
from loguru import logger

from . import calls, db
from .voice import enregistrement, ulaw

# L'identifiant vient d'une requête signée par Twilio, mais c'est lui qui entre dans
# l'adresse appelée avec nos identifiants : seule sa forme exacte passe.
_SID = re.compile(r"RE[0-9a-f]{32}")
_DELAI = httpx.Timeout(30.0, connect=5.0)
_SILENCE = b"\xff"  # un échantillon de silence en µ-law
_TAUX = 8000


def _identifiants() -> Optional[tuple[str, str]]:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    jeton = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return (sid, jeton) if sid and jeton else None


def en_ulaw(wav: bytes) -> bytes:
    """Un WAV de Twilio → µ-law 8 kHz mono, le format de nos enregistrements."""
    with wave.open(io.BytesIO(wav), "rb") as fichier:
        canaux, largeur, taux = (fichier.getnchannels(), fichier.getsampwidth(),
                                 fichier.getframerate())
        brut = fichier.readframes(fichier.getnframes())
    if largeur != 2:
        raise ValueError(f"WAV en {largeur * 8} bits : seul le 16 bits est attendu")
    pcm = np.frombuffer(brut, dtype="<i2")
    if canaux > 1:
        pcm = pcm.reshape(-1, canaux).astype(np.int32).mean(axis=1).astype("<i2")
    if taux != _TAUX:
        import soxr

        pcm = soxr.resample(pcm, taux, _TAUX).astype("<i2")
    return ulaw.encoder(pcm.astype("<i2").tobytes())


def _ranger(tenant_id: int, call_id: int, message: bytes) -> int:
    """Écrit le message à la suite de la piste « appelant ». Renvoie le nombre d'octets
    enregistrés pour cet appel, toutes pistes confondues."""
    piste = enregistrement.chemin(tenant_id, call_id, "appelant")
    autre = enregistrement.chemin(tenant_id, call_id, "assistante")
    piste.parent.mkdir(parents=True, exist_ok=True)
    deja = piste.stat().st_size if piste.exists() else 0
    en_face = autre.stat().st_size if autre.exists() else 0
    with open(piste, "ab") as sortie:
        # Les deux pistes sont alignées dans le temps : le message commence APRÈS la fin
        # de ce que l'assistante a dit, pas par-dessus.
        sortie.write(_SILENCE * max(0, en_face - deja))
        sortie.write(message)
    return piste.stat().st_size + en_face


def _noter(call_id: int, octets: int) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET recording_bytes = ? WHERE id = ?", (octets, call_id))


async def rapatrier(call_sid: Optional[str], recording_sid: Optional[str]) -> bool:
    """Vrai si le message est chez nous et n'est plus chez Twilio. NE LÈVE JAMAIS."""
    try:
        if not _SID.fullmatch(recording_sid or ""):
            logger.warning(f"[répondeur] identifiant d'enregistrement inattendu : {recording_sid!r}")
            return False
        identifiants = _identifiants()
        if identifiants is None:
            logger.warning("[répondeur] identifiants Twilio absents : le message reste chez Twilio")
            return False
        appel = await db.hors_boucle(calls.par_sid, call_sid)
        if appel is None:
            logger.warning(f"[répondeur] appel {call_sid} inconnu : le message reste chez Twilio")
            return False
        libre = enregistrement.espace_libre_mo()
        if libre is not None and libre < enregistrement.disque_minimal_mo():
            logger.warning("[répondeur] disque presque plein : le message reste chez Twilio")
            return False
        adresse = (f"https://api.twilio.com/2010-04-01/Accounts/{identifiants[0]}"
                   f"/Recordings/{recording_sid}")
        async with httpx.AsyncClient(timeout=_DELAI, auth=identifiants) as client:
            reponse = await client.get(adresse + ".wav")
            reponse.raise_for_status()
            message = await asyncio.to_thread(en_ulaw, reponse.content)
            octets = await asyncio.to_thread(_ranger, appel["tenant_id"], appel["id"], message)
            await db.hors_boucle(_noter, appel["id"], octets)
            # Effacé de chez Twilio seulement une fois écrit chez nous.
            efface = await client.delete(adresse + ".json")
            if efface.status_code >= 400:
                logger.warning(f"[répondeur] message rangé, mais Twilio garde sa copie "
                               f"(HTTP {efface.status_code}) : {recording_sid}")
                return False
        logger.info(f"[répondeur] message de l'appel {appel['id']} rapatrié "
                    f"({len(message) / _TAUX:.0f} s)")
        return True
    except Exception as exc:
        logger.warning(f"[répondeur] rapatriement impossible ({type(exc).__name__}: {exc}) "
                       "— le message reste chez Twilio")
        return False
