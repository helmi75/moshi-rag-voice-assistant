"""Les répliques du client, dites par une voix de synthèse, au format du téléphone.

Rendues une fois par la voix Mistral (pas Marie : une autre voix du catalogue, pour que
l'enregistrement se lise), puis gardées sur disque — la clé du fichier est le texte et la
voix. Le rendu se paie au caractère (`COST_VOIX_PAR_MILLE_CARACTERES`) : il est compté au
passage qui l'a fait.
"""
import hashlib
import os
from pathlib import Path

from ..voice import ulaw, voices

def dossier() -> Path:
    return Path(os.getenv("RECETTE_REPLIQUES_DIR", "/app/data/recette/repliques"))


def voix_du_client(langue: str) -> str:
    """Une voix du catalogue pour cette langue, autre que Marie : à la réécoute, on doit
    distinguer le client de l'assistante. `RECETTE_VOIX_FR` / `RECETTE_VOIX_EN` la
    choisissent ; sinon la première du catalogue dans cette langue."""
    choisie = os.getenv(f"RECETTE_VOIX_{langue.upper()}", "").strip()
    if choisie and voices.get(choisie) is not None:
        return choisie
    candidates = [v for v in voices.voix_voxtral() if (v.langue or "").startswith(langue)]
    autres = [v for v in candidates if "marie" not in (v.locuteur or "").lower()]
    if not (autres or candidates):
        raise RuntimeError(f"aucune voix du catalogue en « {langue} » pour le client d'essai")
    return (autres or candidates)[0].id


def _chemin(texte: str, voix_id: str) -> Path:
    cle = hashlib.sha256(f"{voix_id}\n{texte}".encode()).hexdigest()[:24]
    return dossier() / f"{cle}.ulaw"


def deja_rendue(texte: str, langue: str) -> bytes:
    """Le son d'une réplique déjà rendue. Lève `OSError` si le fichier manque : sur le
    chemin d'un appel d'essai en cours, on ne va pas chez Mistral."""
    return _chemin(texte, voix_du_client(langue)).read_bytes()


async def replique(texte: str, langue: str) -> tuple[bytes, int]:
    """(son en µ-law 8 kHz, caractères rendus à l'instant — 0 si le fichier existait)."""
    voix_id = voix_du_client(langue)
    chemin = _chemin(texte, voix_id)
    if chemin.exists() and chemin.stat().st_size > 800:
        return chemin.read_bytes(), 0
    from ..voice.greeting import _render_pcm, _to_twilio_int16

    pcm = await _render_pcm(texte, voix_id)
    if pcm is None or not len(pcm):
        raise RuntimeError(f"réplique non rendue par la voix « {voix_id} »")
    son = ulaw.encoder(await _to_twilio_int16(pcm))
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(son)
    return son, len(texte)
