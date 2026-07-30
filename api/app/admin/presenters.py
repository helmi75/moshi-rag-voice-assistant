"""Mise en forme des lignes d'appel pour l'admin.

Aucune requête ici : ces fonctions ne font que traduire ce qui est DÉJÀ en base en
libellés lisibles. Rien n'est déduit ni estimé — un appel dont le numéro n'a pas été
enregistré affiche « Numéro inconnu », pas un numéro plausible.
"""
import json
import os
from typing import Optional

# Issues d'un appel, telles qu'elles existent réellement en base :
# `status` ∈ {in_progress, completed, failed} et `reservation_id` renseigné ou non.
OUTCOMES = {
    "reservation": ("Réservation", "chip-good", "dot-good"),
    "failed": ("Échec", "chip-bad", "dot-bad"),
    "unfinished": ("Inachevé", "chip-warn", "dot-warn"),
    "info": ("Renseignement", "chip", "dot"),
}


def voice_label() -> str:
    """Nom lisible de la voix RÉELLEMENT configurée (variable d'environnement globale).

    Affichée en lecture seule partout : tant que la voix n'est pas un champ du tenant,
    un sélecteur laisserait croire à un réglage par établissement qui n'existe pas."""
    provider = os.getenv("TTS_PROVIDER", "moshi_server")
    if provider != "moshi_server":
        return f"Moteur « {provider} »"
    raw = os.getenv("MOSHI_TTS_VOICE", "")
    stem = raw.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ").strip()
    return stem.capitalize() if stem else "Voix par défaut du serveur"


def outcome_key(call: dict) -> str:
    if call.get("status") == "failed":
        return "failed"
    if call.get("reservation_id"):
        return "reservation"
    if not call.get("ended_at"):
        # Le worker n'a jamais clôturé l'appel (arrêt brutal) : l'appel n'est pas
        # « en cours » pour autant — on ne prétend pas suivre un état live.
        return "unfinished"
    return "info"


def parse_transcript(raw: Optional[str]) -> list[dict]:
    """Transcript JSON → liste de tours ; défensif (une ligne corrompue n'échoue pas)."""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def first_customer_line(transcript: list[dict], limit: int = 90) -> str:
    """Première phrase de l'appelant : c'est l'extrait le plus parlant d'un appel."""
    for message in transcript:
        if isinstance(message, dict) and message.get("role") == "user":
            text = (message.get("content") or "").strip()
            if text:
                return f"« {text[:limit]}… »" if len(text) > limit else f"« {text} »"
    return ""


def format_duration(seconds) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def call_view(call: dict) -> dict:
    """Ligne d'appel prête à afficher (liste, panneau de détail, salle de contrôle)."""
    key = outcome_key(call)
    label, chip, dot = OUTCOMES[key]
    transcript = parse_transcript(call.get("transcript"))
    started = call.get("started_at") or ""
    return {
        **call,
        "outcome": key,
        "outcome_label": label,
        "chip_class": chip,
        "dot_class": dot,
        "caller": call.get("caller_number") or "Numéro inconnu",
        "snippet": first_customer_line(transcript),
        "transcript": transcript,
        "duration_label": format_duration(call.get("duration_seconds")),
        "date_label": started[:10],
        "time_label": started[11:16],
    }
