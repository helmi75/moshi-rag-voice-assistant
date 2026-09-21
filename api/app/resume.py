"""Résumé d'appel : une phrase qui dit ce que l'appel a donné.

La colonne `calls.summary` existait depuis le premier jour et **rien ne l'écrivait** :
les 141 premiers appels l'ont toute laissée à NULL. Faute de mieux, la liste des appels
et le tableau de bord affichaient les premiers mots bruts de la transcription — presque
toujours « Bonjour, restaurant… ». Pour savoir ce qu'un appel avait donné, le
restaurateur devait l'ouvrir et le lire en entier.

Le résumé est produit APRÈS la clôture de l'appel, en tâche de fond : l'appelant a
raccroché, personne n'attend, et le coût (un appel au modèle rapide, quelques centaines
de jetons) ne pèse sur aucune latence. Un échec — modèle indisponible, quota, réponse
vide — laisse `summary` à NULL, et l'affichage retombe sur l'ancien extrait. C'est la
règle de tout ce qui est accessoire ici : ça n'a pas le droit de casser un appel.
"""
import json
import os
from typing import Optional

from loguru import logger

from . import db, llm, taches

# Le même modèle que la conversation par défaut : une seule facture à lire, et il est
# déjà réputé rapide. RESUME_MODEL permet d'en choisir un autre sans redéploiement.
MODELE = os.getenv("RESUME_MODEL", "").strip() or llm.MODEL
# Deux phrases au plus : au-delà, la liste des appels redevient un pavé illisible.
MAX_JETONS = 120

CONSIGNE = (
    "Tu résumes en UNE phrase, deux au maximum, ce qu'un appel téléphonique reçu par un "
    "restaurant a donné : ce que voulait l'appelant, et ce qui a été fait. Sois factuel, "
    "n'invente rien, n'ajoute ni salutation ni commentaire, ne cite aucun numéro de "
    "téléphone. Réponds en français, même si l'appel s'est tenu dans une autre langue."
)


def actif() -> bool:
    """`RESUME_APPELS=0` coupe la fonction — et sa dépense — sans redéploiement."""
    return os.getenv("RESUME_APPELS", "1").strip().lower() not in ("0", "false", "off")


def en_dialogue(transcript: Optional[list]) -> str:
    """La transcription rendue lisible par le modèle. Vide s'il n'y a rien à résumer.

    Un appel coupé net (worker mort, appelant qui raccroche à l'accueil) n'a pas de
    tour de l'appelant : le résumer produirait « l'appelant n'a rien dit », une ligne
    qui occupe la place sans rien apprendre. On préfère alors ne rien écrire."""
    lignes = []
    client = False
    for message in transcript or []:
        if not isinstance(message, dict):
            continue
        texte = " ".join((message.get("content") or "").split())
        if not texte:
            continue
        if message.get("role") == "user":
            client = True
            lignes.append(f"Client : {texte}")
        elif message.get("role") == "assistant":
            lignes.append(f"Assistante : {texte}")
    return "\n".join(lignes) if client else ""


def _transcription(call_id: int) -> Optional[list]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT transcript FROM calls WHERE id = ?", (call_id,)).fetchone()
    if row is None or not row["transcript"]:
        return None
    try:
        return json.loads(row["transcript"])
    except (TypeError, ValueError):
        return None


def _ecrire(call_id: int, texte: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET summary = ? WHERE id = ?", (texte, call_id))


async def resumer(call_id: int) -> Optional[str]:
    """Écrit le résumé de l'appel et le renvoie. **Ne lève jamais** : l'appelant a
    raccroché, et rien de ce qui se passe ici ne doit remonter dans le chemin d'appel."""
    try:
        dialogue = en_dialogue(await db.hors_boucle(_transcription, call_id))
        if not dialogue:
            return None
        reponse = await llm.get_client().chat.completions.create(
            model=MODELE,
            max_tokens=MAX_JETONS,
            messages=[{"role": "system", "content": CONSIGNE},
                      {"role": "user", "content": dialogue}],
        )
        texte = " ".join((reponse.choices[0].message.content or "").split())
        if not texte:
            return None
        await db.hors_boucle(_ecrire, call_id, texte)
        logger.info(f"[resume] appel {call_id} : {texte}")
        return texte
    except Exception as exc:
        logger.warning(f"[resume] appel {call_id} non résumé (sans conséquence) : {exc}")
        return None


def planifier(call_id: Optional[int]) -> None:
    """Lance le résumé en tâche de fond. Appelé depuis la clôture de l'appel, en
    synchrone : le résultat d'un résumé n'intéresse personne tout de suite."""
    if call_id and actif():
        taches.lancer(resumer(call_id), nom=f"résumé de l'appel {call_id}")
