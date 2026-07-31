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
"""
import os
from dataclasses import dataclass
from typing import Optional

# Voix historique du projet, servie tant qu'aucun établissement n'a choisi la sienne.
DEFAULT_VOICE = "unmute-prod-website/developpeuse-3.wav"

# Dossiers de voix réellement embarqués dans l'image du serveur GPU. À garder synchronisé
# avec VOICE_FOLDERS (deploy/modal_moshi_server.py) : une voix venant d'un autre dossier
# n'existe tout simplement pas sur le serveur, qui la remplacerait sans le dire.
EMBEDDED_FOLDERS = ("unmute-prod-website/", "cml-tts/fr/")


@dataclass(frozen=True)
class Voice:
    id: str  # chemin exact envoyé au serveur (?voice=...)
    label: str  # nom affiché dans l'admin
    note: str  # une ligne pour aider à choisir, à l'oreille


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


def catalogue() -> tuple[Voice, ...]:
    return CATALOGUE


def get(voice_id: Optional[str]) -> Optional[Voice]:
    """La voix du catalogue, ou None si l'identifiant n'y figure pas."""
    return next((v for v in CATALOGUE if v.id == voice_id), None)


def default_id() -> str:
    """Voix par défaut du parc : MOSHI_TTS_VOICE si elle est au catalogue, sinon la
    voix historique. Une variable d'environnement mal saisie ne doit pas rendre tout
    le parc muet ni le faire répondre avec la voix de repli du serveur."""
    configured = os.getenv("MOSHI_TTS_VOICE", "").strip()
    return configured if get(configured) else DEFAULT_VOICE


def resolve(tenant=None) -> str:
    """Identifiant de voix RÉELLEMENT envoyé au serveur pour cet établissement.

    Hors catalogue (voix retirée depuis, base éditée à la main) -> défaut du parc.
    C'est le seul endroit qui décide : le TTS live et l'accueil pré-rendu passent
    tous les deux par ici, sinon un appel pourrait mélanger deux voix."""
    chosen = getattr(tenant, "voice", None)
    return chosen if get(chosen) else default_id()


def label_for(tenant=None) -> str:
    """Nom lisible de la voix effectivement utilisée (jamais un identifiant brut)."""
    voice = get(resolve(tenant))
    return voice.label if voice else "Voix par défaut du serveur"
