"""Les traces de la recette d'appels automatique (ASSISTANTE-192).

`recette/essai.purger` efface l'établissement d'essai après chaque scénario (sans cela, le
client suivant serait reconnu par son numéro) : il ne restait rien à relire dans l'admin. Ce
module garde, à part, ce qui permet de relire un passage : le tableau des scénarios, la
conversation complète de chacun et, pour les vrais appels, l'audio.

Le client est simulé et la voix de synthèse : aucune donnée personnelle (registre de
`docs/RGPD.md`). Conservé 30 jours (`rgpd.jours_recette`).

Tout le SQL de cette fonction est ici. Les fichiers audio sont des COPIES des pistes de
`voice/enregistrement.py`, sous un nom aléatoire que seule la base connaît : la route qui les
sert reconstruit le chemin côté serveur (`chemin_audio`), jamais depuis la requête.
"""
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger

from . import db, horloge
from .voice import enregistrement

_NOM = re.compile(r"^[0-9a-f]{32}$")


def dossier() -> Path:
    return Path(os.getenv("RECETTE_ARCHIVE_DIR", "/app/data/recette/archives"))


def chemin_audio(nom: str, piste: str) -> Path:
    """Chemin d'une piste archivée, construit côté serveur. `nom` doit être un hexadécimal de
    32 caractères et `piste` l'une de `enregistrement.PISTES` : rien d'autre ne sort du dossier."""
    if not isinstance(nom, str) or not _NOM.fullmatch(nom):
        raise ValueError("nom d'archive invalide")
    if piste not in enregistrement.PISTES:
        raise ValueError(f"piste inconnue : {piste!r}")
    return dossier() / f"{nom}-{piste}.ulaw"


def garder_audio(tenant_id: int, call_id: int) -> Optional[str]:
    """Copie les pistes de l'appel avant que la purge de l'établissement d'essai ne l'efface.
    Rend le nom de l'archive, ou None si l'appel n'a aucune piste. Ne lève jamais : perdre
    l'audio d'un passage ne doit pas faire échouer la recette."""
    nom = uuid.uuid4().hex
    copiees = 0
    try:
        for piste in enregistrement.PISTES:
            source = enregistrement.chemin(tenant_id, call_id, piste)
            if not source.is_file():
                continue
            cible = chemin_audio(nom, piste)
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, cible)
            copiees += 1
    except Exception as exc:
        logger.warning(f"recette : audio non archivé : {type(exc).__name__} {exc}")
    return nom if copiees else None


def archiver(lignes: list[dict], cout: float, version: str, etage2: bool) -> int:
    """Enregistre un passage et ses scénarios. Rend l'identifiant de la recette."""
    echecs = sum(l.get("etat") == "echec" for l in lignes)
    with db.get_conn() as conn:
        recette_id = conn.execute(
            "INSERT INTO recettes (version, cout, scenarios, echecs, etage2) VALUES (?, ?, ?, ?, ?)",
            (version, float(cout or 0), len(lignes), echecs, 1 if etage2 else 0)).lastrowid
        for rang, ligne in enumerate(lignes):
            conn.execute(
                """INSERT INTO recette_scenarios (recette_id, rang, etage, moteur, scenario, recette,
                       titre, etat, preuve, cout, conversation, audio)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (recette_id, rang, ligne.get("etage"), ligne.get("moteur"), ligne.get("scenario"),
                 ligne.get("recette"), ligne.get("titre"), ligne.get("etat"), ligne.get("preuve"),
                 float(ligne.get("cout") or 0),
                 json.dumps(ligne.get("conversation") or [], ensure_ascii=False),
                 ligne.get("audio") or None))
    return int(recette_id)


def lister(limite: int = 50) -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM recettes ORDER BY created_at DESC, id DESC LIMIT ?", (int(limite),))]


def obtenir(recette_id: int) -> Optional[dict]:
    """La recette et ses scénarios (`lignes`, dans l'ordre joué ; `scenarios` reste le nombre), conversation décodée."""
    with db.get_conn() as conn:
        recette = conn.execute("SELECT * FROM recettes WHERE id = ?", (recette_id,)).fetchone()
        if recette is None:
            return None
        scenarios = [dict(r) for r in conn.execute(
            "SELECT * FROM recette_scenarios WHERE recette_id = ? ORDER BY rang, id", (recette_id,))]
    for s in scenarios:
        try:
            s["conversation"] = json.loads(s["conversation"] or "[]")
        except ValueError:
            s["conversation"] = []
    return {**dict(recette), "lignes": scenarios}


def scenario_audio(scenario_id: int) -> Optional[str]:
    """Le nom de l'archive audio d'un scénario, ou None."""
    with db.get_conn() as conn:
        ligne = conn.execute("SELECT audio FROM recette_scenarios WHERE id = ?", (scenario_id,)).fetchone()
    return ligne["audio"] if ligne and ligne["audio"] else None


def purger(jours: int) -> int:
    """Efface les passages plus vieux que la durée de conservation, avec leurs scénarios et
    leurs fichiers audio. Rend le nombre de passages effacés."""
    limite = horloge.il_y_a(jours)
    with db.get_conn() as conn:
        anciennes = [r["id"] for r in conn.execute("SELECT id FROM recettes WHERE created_at < ?", (limite,))]
        noms = [r["audio"] for r in conn.execute(
            """SELECT audio FROM recette_scenarios WHERE audio IS NOT NULL AND recette_id IN
               (SELECT id FROM recettes WHERE created_at < ?)""", (limite,))]
        # Pas de dépendance au `PRAGMA foreign_keys` : les scénarios sont effacés explicitement.
        for identifiant in anciennes:
            conn.execute("DELETE FROM recette_scenarios WHERE recette_id = ?", (identifiant,))
            conn.execute("DELETE FROM recettes WHERE id = ?", (identifiant,))
    for nom in noms:
        for piste in enregistrement.PISTES:
            try:
                chemin_audio(nom, piste).unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                logger.warning(f"recette : archive audio non effacée : {type(exc).__name__} {exc}")
    return len(anciennes)
