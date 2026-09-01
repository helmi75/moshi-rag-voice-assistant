"""Mise en forme des lignes d'appel pour l'admin.

Aucune requête ici : ces fonctions ne font que traduire ce qui est DÉJÀ en base en
libellés lisibles. Rien n'est déduit ni estimé — un appel dont le numéro n'a pas été
enregistré affiche « Numéro inconnu », pas un numéro plausible.
"""
import json
import os
from typing import Optional

from ..voice import voices

# Issues d'un appel, telles qu'elles existent réellement en base :
# `status` ∈ {in_progress, completed, failed} et `reservation_id` renseigné ou non.
OUTCOMES = {
    "reservation": ("Réservation", "chip-good", "dot-good"),
    "failed": ("Échec", "chip-bad", "dot-bad"),
    "unfinished": ("Inachevé", "chip-warn", "dot-warn"),
    "info": ("Renseignement", "chip", "dot"),
}


def voice_label(tenant=None) -> str:
    """Nom lisible de la voix RÉELLEMENT servie à cet établissement.

    Sans tenant (écran parc), c'est la voix par défaut du parc. Le nom vient du
    catalogue, jamais du chemin du fichier : afficher « 10087 11650 000028 0002 »
    ne dirait rien à un restaurateur. Seul moshi_server gère la voix par
    établissement ; pour les autres moteurs on nomme le moteur, sans inventer."""
    provider = os.getenv("TTS_PROVIDER", "moshi_server")
    if provider != "moshi_server":
        return f"Moteur « {provider} »"
    return voices.label_for(tenant)


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


def parse_journal(raw: Optional[str]) -> Optional[dict]:
    """Journal de bord d'un appel (#88). None si absent ou illisible.

    Défensif comme `parse_transcript` : un journal corrompu ne doit pas casser la page
    de diagnostic — c'est précisément la page qu'on ouvre quand quelque chose va mal."""
    if not raw:
        return None
    try:
        journal = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return journal if isinstance(journal, dict) else None


def format_mmss(millisecondes: Optional[int]) -> str:
    """`mm:ss` — le repère qu'on retrouve dans le lecteur audio."""
    if millisecondes is None:
        return "—"
    total = max(0, int(millisecondes)) // 1000
    return f"{total // 60}:{total % 60:02d}"


def tours_du_journal(journal: Optional[dict]) -> list[dict]:
    """Les tours, prêts à l'affichage : horodatage lisible et parts en pourcentage.

    Les parts sont calculées ICI et non dans le gabarit — un gabarit qui calcule finit
    par diverger de ce que le journal dit réellement."""
    if not journal:
        return []
    sortie = []
    for tour in journal.get("tours", []) or []:
        blanc = tour.get("blanc_ms") or 0
        etages = []
        for cle, libelle in (("stt_ms", "Transcription"), ("llm_ms", "Compréhension"),
                             ("outil_ms", "Outil"), ("tts_ms", "Voix"),
                             ("non_attribue_ms", "Non attribué")):
            valeur = tour.get(cle) or 0
            if valeur:
                etages.append({
                    "libelle": libelle, "ms": valeur,
                    "part": round(100 * valeur / blanc) if blanc else 0,
                })
        sortie.append({**tour,
                       "horodatage": format_mmss(tour.get("t_ms")),
                       "blanc_s": f"{blanc / 1000:.1f}",
                       "etages": etages})
    return sortie
