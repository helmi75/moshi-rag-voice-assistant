"""Catalogue des voix proposables par établissement.

**Depuis le 28/09/2026, toutes les voix viennent de Mistral (Voxtral)** — décision de
Helmi après un test d'écoute à l'aveugle et trois appels réels (SCRUM-94). Le catalogue
est celui que rend l'API de Mistral (`GET /v1/audio/voices` : 30 voix prédéfinies au
28/09), relu au démarrage puis toutes les heures ; une copie (`voxtral_voix.json`)
sert tant que Mistral n'a pas répondu. La liste reste FERMÉE : on n'enregistre et on
n'envoie que ce que Mistral a listé.

Les voix Moshi ont été retirées avec le GPU (ASSISTANTE-103, 28/11/2026). Un
établissement dont la voix enregistrée est une ancienne voix Moshi, ou une voix inconnue,
est servi par la voix par défaut (Marie) : la résolution se fait à la lecture, la base
n'est pas réécrite.
"""
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

VOXTRAL = "voxtral"


@dataclass(frozen=True)
class Voice:
    id: str  # « voxtral/… »
    label: str  # nom affiché dans l'admin
    note: str  # une ligne pour aider à choisir, à l'oreille
    fournisseur: str = VOXTRAL
    voxtral_id: Optional[str] = None  # identifiant de la voix chez Mistral
    langue: Optional[str] = None  # « fr_fr », « en_us », « en_gb » (Mistral)
    locuteur: Optional[str] = None  # « Marie », « Paul »… : regroupe les tons dans l'admin


# --- Les voix de Mistral -----------------------------------------------------------

# Marie enthousiaste depuis le 03/10/2026, à la demande de Helmi (c'était Marie neutre).
# S'applique à tout établissement qui n'a pas choisi de voix, et au rappel du site.
VOIX_PAR_DEFAUT = "voxtral/fr_marie_excited"
_INSTANTANE = Path(__file__).with_name("voxtral_voix.json")
_LANGUES = {"fr_fr": "Français", "en_us": "Anglais (États-Unis)",
            "en_gb": "Anglais (Royaume-Uni)"}
# Le ton, tel que Mistral le nomme, en français et accordé au genre de la voix.
_TONS = {
    "neutral": ("neutre", "neutre"), "happy": ("joyeux", "joyeuse"),
    "sad": ("triste", "triste"), "excited": ("enthousiaste", "enthousiaste"),
    "curious": ("curieux", "curieuse"), "angry": ("en colère", "en colère"),
    "frustrated": ("agacé", "agacée"), "confident": ("assuré", "assurée"),
    "cheerful": ("enjoué", "enjouée"), "sarcasm": ("sarcastique", "sarcastique"),
    "confused": ("perplexe", "perplexe"), "shameful": ("honteux", "honteuse"),
    "jealousy": ("jaloux", "jalouse"),
}
# Les quatre voix du 28/09 au matin, avant qu'on prenne tout le catalogue de Mistral :
# un établissement enregistré avec l'une d'elles garde sa voix (migration v16 aussi).
_ANCIENS = {
    "voxtral/marie-neutre": "voxtral/fr_marie_neutral",
    "voxtral/marie-joyeuse": "voxtral/fr_marie_happy",
    "voxtral/marie-curieuse": "voxtral/fr_marie_curious",
    "voxtral/marie-enthousiaste": "voxtral/fr_marie_excited",
}


def _voix_mistral(brute: dict) -> Optional[Voice]:
    """Une voix telle que l'API de Mistral la décrit, en voix du catalogue."""
    if brute.get("kind") not in (None, "ivc"):
        # Les voix de Voxtral 3 (20/10/2026, kind « pvc ») : notre modèle (voxtral-mini-tts-
        # 2603) les refuse, HTTP 400 « voice is not supported for speech v1 » (relevé le
        # 15/11/2026 : 30 voix « ivc » qui parlent, 36 « pvc » refusées). Liste fermée : un
        # genre inconnu reste dehors. L'instantané du 28/09 n'a pas de `kind` (None).
        return None
    try:
        locuteur, _, ton = str(brute["name"]).partition(" - ")
        mots = _TONS.get(ton.strip().lower())
        if mots:
            ton = mots[1] if brute.get("gender") == "female" else mots[0]
        langue = (brute.get("languages") or [None])[0]
        return Voice(
            id=f"voxtral/{brute['slug']}",
            label=f"{locuteur.strip()} · {ton.strip().lower()}" if ton.strip() else locuteur.strip(),
            note=_LANGUES.get(langue, langue or "langue inconnue"),
            fournisseur=VOXTRAL, voxtral_id=str(brute["id"]), langue=langue,
            locuteur=locuteur.strip())
    except (KeyError, TypeError):
        return None


def _depuis(bruts: list) -> tuple[Voice, ...]:
    voix = [v for v in (_voix_mistral(b) for b in bruts) if v is not None]
    # Le français d'abord (c'est la langue des établissements), puis par locuteur.
    return tuple(sorted(voix, key=lambda v: (v.langue != "fr_fr", v.locuteur or "", v.label)))


def _instantane() -> tuple[Voice, ...]:
    try:
        return _depuis(json.loads(_INSTANTANE.read_text(encoding="utf-8"))["voix"])
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(f"catalogue Mistral : copie illisible ({exc})")
        return ()


_catalogue_mistral: tuple[Voice, ...] = _instantane()


async def rafraichir_catalogue() -> bool:
    """Relit le catalogue chez Mistral (lecture gratuite). En cas d'échec, l'ancien reste :
    une voix déjà choisie ne doit pas disparaître parce que Mistral a hoqueté."""
    global _catalogue_mistral
    cle = os.getenv("MISTRAL_API_KEY", "").strip()
    if not cle:
        return False
    import httpx

    try:
        bruts, page = [], 1
        async with httpx.AsyncClient(timeout=10.0) as client:
            while page <= 10:
                reponse = await client.get(
                    "https://api.mistral.ai/v1/audio/voices",
                    params={"limit": 100, "page": page},
                    headers={"Authorization": f"Bearer {cle}"})
                reponse.raise_for_status()
                donnees = reponse.json()
                bruts += donnees.get("items") or []
                if page >= int(donnees.get("total_pages") or 1):
                    break
                page += 1
        nouveau = _depuis(bruts)
    except Exception as exc:
        logger.warning(f"catalogue Mistral : relecture impossible ({exc}), l'ancien reste")
        return False
    if nouveau:
        _catalogue_mistral = nouveau
    return bool(nouveau)


async def boucle_catalogue(intervalle: float = 3600.0) -> None:
    while True:
        await rafraichir_catalogue()
        await asyncio.sleep(intervalle)


def catalogue() -> tuple[Voice, ...]:
    """Ce qui se CHOISIT : les voix de Mistral."""
    return _catalogue_mistral


def voix_voxtral() -> tuple[Voice, ...]:
    return _catalogue_mistral


def voxtral_disponible() -> bool:
    """La clé Mistral est-elle posée ? Sans elle, rien ne se dit chez Mistral : la
    supervision (contrôle « Voix Mistral ») le signale en panne et la vigie rend la ligne."""
    return bool(os.getenv("MISTRAL_API_KEY", "").strip())


def get(voice_id: Optional[str]) -> Optional[Voice]:
    """La voix du catalogue Mistral, ou None."""
    voice_id = _ANCIENS.get(voice_id, voice_id)
    return next((v for v in _catalogue_mistral if v.id == voice_id), None)


def est_voxtral(voice_id: Optional[str]) -> bool:
    return get(voice_id) is not None


def default_id() -> str:
    """Voix des établissements qui n'ont rien choisi : VOIX_PAR_DEFAUT si c'est une voix
    Mistral du catalogue, sinon Marie enthousiaste."""
    configured = os.getenv("VOIX_PAR_DEFAUT", "").strip()
    return get(configured).id if est_voxtral(configured) else VOIX_PAR_DEFAUT


def resolve_id(voice_id: Optional[str]) -> str:
    """Identifiant de voix RÉELLEMENT utilisé pour une voix enregistrée : jamais un
    identifiant hors catalogue. Une voix inconnue, retirée par Mistral ou ancienne voix
    Moshi donne la voix par défaut."""
    voix = get(voice_id)
    return voix.id if voix is not None else default_id()


def resolve(tenant=None) -> str:
    """Identifiant de voix RÉELLEMENT utilisé pour cet établissement.

    C'est le seul endroit qui décide : la voix en appel et l'accueil pré-rendu passent
    tous les deux par ici, sinon un appel pourrait mélanger deux voix."""
    return resolve_id(getattr(tenant, "voice", None))


def label_for(tenant=None) -> str:
    """Nom lisible de la voix effectivement utilisée (jamais un identifiant brut)."""
    voice = get(resolve(tenant))
    return voice.label if voice is not None else "Voix par défaut"
