"""Une réservation annoncée au client, et rien d'enregistré : le reconnaître.

Le 08/10/2026 (appel 265, moteur GPT-Live), l'assistante a dit « Très bien, j'enregistre
ça. C'est réservé. On vous attend… » alors que l'outil de création n'avait pas tourné :
aucune table en base, et un résumé d'appel qui disait lui aussi « a réservé ». La consigne
(« n'annonce jamais un résultat avant le retour ») existait déjà : elle n'a pas suffi, et
aucune consigne ne suffira jamais tout à fait. Ce module ne compte donc pas sur le modèle :
il compare ce qui s'est DIT à ce que les outils ont FAIT.

Pur : aucune lecture, aucune écriture. Qui l'appelle décide quoi faire du constat.
"""
import json
import re
from typing import Optional

# Le motif rangé au journal de l'appel (`journal["a_verifier"]["motif"]`).
MOTIF = "annonce_sans_trace"

# Les outils qui écrivent ou qui retrouvent une réservation. Après l'un d'eux, « c'est
# enregistré » peut être vrai (une table créée, modifiée, un message pris) ou porter sur
# une réservation retrouvée (« celle de vingt heures est bien enregistrée ») : on ne
# signale pas. On préfère manquer un cas douteux que crier au loup à chaque appel.
_EXPLIQUENT = ("create_reservation", "modify_reservation", "cancel_reservation",
               "take_message", "find_reservation")

_ANNONCE = re.compile(
    r"\bc'est (?:bien |bon,? c'est )?(?:réservé|enregistré|confirmé)\b"
    r"|\b(?:réservation|table) (?:est|a été|a bien été) (?:bien |désormais )?(?:réservée|enregistrée|confirmée)\b"
    r"|\bj'ai (?:bien )?(?:réservé|enregistré)\b",
    re.IGNORECASE)
# « La réservation n'a pas été enregistrée », « rien n'est confirmé » : le contraire d'une annonce.
_NEGATION = re.compile(r"\b(?:pas|rien|aucune?|jamais)\b|\bn'", re.IGNORECASE)
_PHRASES = re.compile(r"[^.!?…]+[.!?…]*")


def phrase_d_annonce(texte: Optional[str]) -> Optional[str]:
    """La phrase qui annonce un enregistrement comme fait, ou None. Une question (« C'est
    bien enregistré ? ») et une négation n'en sont pas."""
    propre = " ".join((texte or "").replace("’", "'").split())
    for phrase in _PHRASES.findall(propre):
        phrase = phrase.strip()
        if (_ANNONCE.search(phrase) and not phrase.endswith("?")
                and not _NEGATION.search(phrase)):
            return phrase
    return None


def a_abouti(resultat) -> bool:
    """Ce que `llm.run_tool` a rendu est-il un travail fait ? Un refus du serveur porte
    `error` ; une exception rend un texte qui n'est pas du JSON."""
    try:
        rendu = json.loads(resultat) if isinstance(resultat, str) else resultat
    except ValueError:
        return False
    return isinstance(rendu, dict) and not rendu.get("error")


def expliquee(outils_appeles) -> bool:
    """Un outil de la liste a-t-il abouti pendant l'appel ?"""
    return any(o.get("nom") in _EXPLIQUENT and a_abouti(o.get("resultat"))
               for o in outils_appeles or [] if isinstance(o, dict))


def sans_trace(transcription, outils_appeles) -> Optional[str]:
    """La phrase par laquelle l'assistante a annoncé un enregistrement que rien n'explique
    dans ce que les outils ont fait pendant l'appel. None quand tout concorde."""
    if expliquee(outils_appeles):
        return None
    for tour in transcription or []:
        if isinstance(tour, dict) and tour.get("role") == "assistant":
            phrase = phrase_d_annonce(tour.get("content"))
            if phrase:
                return phrase
    return None
