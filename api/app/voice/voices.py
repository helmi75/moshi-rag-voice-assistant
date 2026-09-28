"""Catalogue des voix proposables par établissement.

Liste FERMÉE, et ce n'est pas de la prudence de principe : moshi-server ne renvoie
AUCUNE erreur pour une voix qu'il ne connaît pas — il rend la phrase avec
`default_voice`, que Kyutai a délibérément choisie étrange « pour qu'on voie bien
que quelque chose ne va pas ». Vérifié contre le serveur déployé le 31/07/2026 :
une voix inventée renvoie de l'audio normal, sans un mot dans les journaux. Un
identifiant erroné traverserait donc les tests, le déploiement et la supervision,
et seul l'appelant entendrait que ce n'est pas la bonne voix.

D'où deux règles :
  1. `resolve()` ne renvoie JAMAIS un identifiant hors catalogue (repli sur le défaut) ;
  2. tout identifiant ajouté ici doit exister dans l'image Modal — c'est-à-dire vivre
     dans un dossier listé par VOICE_FOLDERS (deploy/modal_moshi_server.py). Ajouter
     une voix d'un autre dossier suppose d'élargir VOICE_FOLDERS ET de redéployer.

**Depuis le 28/09/2026, toutes les voix viennent de Mistral (Voxtral)** — décision de
Helmi après un test d'écoute à l'aveugle et trois appels réels (SCRUM-94). Le catalogue
est celui que rend l'API de Mistral (`GET /v1/audio/voices` : 30 voix prédéfinies au
28/09), relu au démarrage puis toutes les heures ; une copie (`voxtral_voix.json`)
sert tant que Mistral n'a pas répondu. La liste reste FERMÉE : on n'enregistre et on
n'envoie que ce que Mistral a listé.

Les voix Moshi ne se choisissent plus. Elles restent le SECOURS si la clé Mistral
manque : une voix connue plutôt que le silence. Leur code partira avec l'arrêt du GPU
(abandon de Moshi, deuxième étape).
"""
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

# Voix historique du projet, servie tant qu'aucun établissement n'a choisi la sienne.
DEFAULT_VOICE = "unmute-prod-website/developpeuse-3.wav"

# Dossiers de voix réellement embarqués dans l'image du serveur GPU. À garder synchronisé
# avec VOICE_FOLDERS (deploy/modal_moshi_server.py) : une voix venant d'un autre dossier
# n'existe tout simplement pas sur le serveur, qui la remplacerait sans le dire.
EMBEDDED_FOLDERS = ("unmute-prod-website/", "cml-tts/fr/")


MOSHI = "moshi"
VOXTRAL = "voxtral"


@dataclass(frozen=True)
class Voice:
    id: str  # Moshi : chemin exact envoyé au serveur (?voice=...) ; Voxtral : « voxtral/… »
    label: str  # nom affiché dans l'admin
    note: str  # une ligne pour aider à choisir, à l'oreille
    fournisseur: str = MOSHI
    voxtral_id: Optional[str] = None  # identifiant de la voix chez Mistral
    langue: Optional[str] = None  # « fr_fr », « en_us », « en_gb » (Mistral)
    locuteur: Optional[str] = None  # « Marie », « Paul »… : regroupe les tons dans l'admin


# Voix retenues par Helmi à l'écoute des extraits réels, le 31/07/2026 : les noms sont
# les siens. Les identifiants `cml-tts/fr/*` viennent du dataset CML-TTS (CC BY 4.0,
# usage commercial autorisé) ; `unmute-prod-website/*` du dossier du site Unmute.
#
# Les mentions de timbre sont MESURÉES (fondamentale médiane du clip source, contrôlée
# contre les erreurs d'octave), pas jugées à l'oreille : grave < 150 Hz, médium jusqu'à
# 200 Hz, clair au-delà. Classées du plus grave au plus clair.
CATALOGUE: tuple[Voice, ...] = (
    Voice(
        id=DEFAULT_VOICE,
        label="Développeuse",
        note="Timbre clair — la voix historique du standard.",
    ),
    Voice(
        id="cml-tts/fr/4937_3731_000004-0001_enhanced.wav",
        label="Clark",
        note="Timbre grave.",
    ),
    Voice(
        id="cml-tts/fr/2154_2576_000020-0003_enhanced.wav",
        label="Claire",
        note="Timbre médium.",
    ),
    Voice(
        id="cml-tts/fr/5476_3103_000072-0001_enhanced.wav",
        label="Catrine",
        note="Timbre médium.",
    ),
    Voice(
        id="cml-tts/fr/6318_7016_000027-0002_enhanced.wav",
        label="Pierre",
        note="Timbre médium.",
    ),
    Voice(
        id="cml-tts/fr/7591_6742_000149-0002_enhanced.wav",
        label="Marine",
        note="Timbre médium.",
    ),
    Voice(
        id="cml-tts/fr/12080_11650_000047-0001_enhanced.wav",
        label="Mathilde",
        note="Timbre clair.",
    ),
)


# --- Les voix de Mistral -----------------------------------------------------------

VOIX_PAR_DEFAUT = "voxtral/fr_marie_neutral"
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


def voix_moshi() -> tuple[Voice, ...]:
    """Les voix Moshi : plus proposées, gardées comme secours sans clé Mistral."""
    return CATALOGUE


def voxtral_disponible() -> bool:
    """La clé Mistral est-elle posée ? Sans elle, rien ne se dit chez Mistral : on
    retombe sur la voix Moshi de secours plutôt que sur le silence."""
    return bool(os.getenv("MISTRAL_API_KEY", "").strip())


def get(voice_id: Optional[str]) -> Optional[Voice]:
    """La voix du catalogue (Mistral, puis Moshi de secours), ou None."""
    voice_id = _ANCIENS.get(voice_id, voice_id)
    return (next((v for v in _catalogue_mistral if v.id == voice_id), None)
            or next((v for v in CATALOGUE if v.id == voice_id), None))


def est_voxtral(voice_id: Optional[str]) -> bool:
    voix = get(voice_id)
    return voix is not None and voix.fournisseur == VOXTRAL


def _secours_moshi() -> str:
    """La voix Moshi de secours : MOSHI_TTS_VOICE si elle est au catalogue Moshi, sinon la
    voix historique. Une variable mal saisie ne doit pas faire répondre le serveur avec
    sa voix de repli."""
    configured = os.getenv("MOSHI_TTS_VOICE", "").strip()
    voix = next((v for v in CATALOGUE if v.id == configured), None)
    return voix.id if voix is not None else DEFAULT_VOICE


def default_id() -> str:
    """Voix des établissements qui n'ont rien choisi : VOIX_PAR_DEFAUT si c'est une voix
    Mistral du catalogue, sinon Marie neutre. Sans clé Mistral : le secours Moshi."""
    if not voxtral_disponible():
        return _secours_moshi()
    configured = os.getenv("VOIX_PAR_DEFAUT", "").strip()
    return get(configured).id if est_voxtral(configured) else VOIX_PAR_DEFAUT


def resolve(tenant=None) -> str:
    """Identifiant de voix RÉELLEMENT utilisé pour cet établissement.

    C'est le seul endroit qui décide : la voix en appel et l'accueil pré-rendu passent
    tous les deux par ici, sinon un appel pourrait mélanger deux voix. Jamais un
    identifiant hors catalogue : une voix inconnue, retirée par Mistral ou ancienne voix
    Moshi donne la voix par défaut."""
    if not voxtral_disponible():
        # Clé absente : la voix de secours plutôt que le silence. La supervision
        # (contrôle « Voix Mistral ») le signale en panne.
        return _secours_moshi()
    voix = get(getattr(tenant, "voice", None))
    if voix is not None and voix.fournisseur == VOXTRAL:
        return voix.id
    return default_id()


def label_for(tenant=None) -> str:
    """Nom lisible de la voix effectivement utilisée (jamais un identifiant brut)."""
    voice = get(resolve(tenant))
    if voice is None:
        return "Voix par défaut du serveur"
    return voice.label if voice.fournisseur == VOXTRAL else f"{voice.label} (secours Moshi)"
