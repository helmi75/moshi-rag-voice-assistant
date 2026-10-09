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

# Les outils qui écrivent. Après l'un d'eux, « c'est enregistré » peut être vrai (une table
# créée, modifiée, un message pris) : on ne signale pas. On préfère manquer un cas douteux
# que crier au loup à chaque appel.
_ECRIVENT = ("create_reservation", "modify_reservation", "cancel_reservation", "take_message")
# Une réservation RETROUVÉE explique aussi « celle de vingt heures est bien enregistrée ».
# Une recherche qui ne trouve rien, non : elle rend une liste vide, sans erreur.
_RETROUVE = "find_reservation"

# « C'est confirmé » seul n'y est pas : elle le dit aussi d'un horaire (« oui, c'est bien
# confirmé, nous sommes ouverts le dimanche »). En français seulement : une annonce faite
# dans une autre langue n'est pas reconnue.
_ANNONCE = re.compile(
    r"\bc'est (?:donc |bien |bon,? c'est )*(?:réservé|enregistré)\b"
    r"|\b(?:réservation|table) (?:est|a été|a bien été) (?:donc |bien |désormais )*"
    r"(?:réservée|enregistrée|confirmée|faite)\b"
    r"|\bj'ai (?:donc |bien )*(?:réservé|enregistré)\b",
    re.IGNORECASE)
# « Rien n'est réservé », « ce n'est pas enregistré » : le contraire d'une annonce. La
# négation ne compte que DANS l'annonce ou juste avant elle : « c'est réservé, n'hésitez
# pas à rappeler » reste une annonce.
_NEGATION = re.compile(r"\b(?:pas|rien|aucune?|jamais)\b|\bn'", re.IGNORECASE)
_PHRASES = re.compile(r"[^.!?…]+[.!?…]*")


def phrase_d_annonce(texte: Optional[str]) -> Optional[str]:
    """La phrase qui annonce un enregistrement comme fait, ou None. Une question (« C'est
    bien enregistré ? ») et une négation n'en sont pas."""
    propre = " ".join((texte or "").replace("’", "'").split())
    for phrase in _PHRASES.findall(propre):
        phrase = phrase.strip()
        trouve = _ANNONCE.search(phrase)
        if (trouve and not phrase.endswith("?")
                and not _NEGATION.search(phrase[:trouve.end()])):
            return phrase
    return None


def _rendu(resultat) -> Optional[dict]:
    """Ce que `llm.run_tool` a rendu, s'il a fait son travail. Un refus du serveur porte
    `error` ; une exception rend un texte qui n'est pas du JSON."""
    try:
        rendu = json.loads(resultat) if isinstance(resultat, str) else resultat
    except ValueError:
        return None
    return rendu if isinstance(rendu, dict) and not rendu.get("error") else None


def expliquee(outils_appeles) -> bool:
    """Un outil a-t-il, pendant l'appel, écrit quelque chose ou retrouvé une réservation ?"""
    for outil in outils_appeles or []:
        if not isinstance(outil, dict):
            continue
        rendu = _rendu(outil.get("resultat"))
        if rendu is None:
            continue
        if outil.get("nom") in _ECRIVENT or (outil.get("nom") == _RETROUVE and rendu.get("reservations")):
            return True
    return False


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


_PROMESSE = re.compile(r"\bj(?:e l)?'enregistre\b|\bje (?:l'|vous l')?enregistre\b", re.IGNORECASE)


def promesse_sans_suite(transcription, outils_appeles) -> Optional[str]:
    """« Je l'enregistre maintenant » et plus rien : la dernière phrase par laquelle
    l'assistante a promis d'enregistrer, quand aucun outil n'a rien écrit de tout l'appel.
    Ne se regarde qu'après un démenti (`voice/live.py`) : dite avant chaque création, cette
    phrase est ordinaire, et un client qui raccroche en cours de route n'est pas une alerte."""
    if expliquee(outils_appeles):
        return None
    for tour in reversed(transcription or []):
        if isinstance(tour, dict) and tour.get("role") == "assistant":
            propre = " ".join((tour.get("content") or "").replace("’", "'").split())
            for phrase in _PHRASES.findall(propre):
                if _PROMESSE.search(phrase) and not _NEGATION.search(phrase):
                    return phrase.strip()
    return None
