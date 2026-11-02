"""Export de toutes les données d'un établissement, en un ZIP.

Au départ d'un client, et pour la portabilité RGPD (art. 20) : remettre au restaurateur
tout ce que nous détenons pour lui, dans des formats qu'il ouvre sans nous (CSV pour
Excel, JSON, WAV). Synchrone : la route l'appelle hors de la boucle d'événements, qui
porte l'audio des appels en cours.

Chaque requête est bornée par `tenant_id = ?` : c'est ce qui empêche l'archive d'un
restaurant d'emporter les données d'un autre.
"""
import csv
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

from . import db, horloge
from .voice import enregistrement, ulaw

# Séparateur « ; » et BOM UTF-8 : c'est ce qu'Excel français ouvre correctement.
_BOM = "﻿"

# Colonnes du compte exportées : jamais le hachage du mot de passe, qui n'est pas une
# donnée du restaurateur et faciliterait une attaque s'il circulait.
_COMPTES = "SELECT email, role, created_at FROM users WHERE tenant_id = ? ORDER BY id"


def _csv(lignes: list[dict], colonnes: list[str]) -> bytes:
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow(colonnes)
    for ligne in lignes:
        ecrivain.writerow(["" if ligne[c] is None else ligne[c] for c in colonnes])
    return (_BOM + tampon.getvalue()).encode("utf-8")


def _table(conn, requete: str, tenant_id: int) -> tuple[list[dict], list[str]]:
    curseur = conn.execute(requete, (tenant_id,))
    colonnes = [d[0] for d in curseur.description]
    return [dict(zip(colonnes, r, strict=True)) for r in curseur.fetchall()], colonnes


def _wav(tenant_id: int, call_id: int) -> bytes | None:
    """Le WAV stéréo d'un appel (appelant à gauche, assistante à droite), ou None."""
    def lire(piste: str) -> bytes:
        chemin = enregistrement.chemin(tenant_id, call_id, piste)
        return ulaw.decoder(chemin.read_bytes()) if chemin.exists() else b""

    try:
        pcm = ulaw.entrelacer(lire("appelant"), lire("assistante"))
    except OSError:
        return None
    return ulaw.entete_wav(len(pcm), canaux=2) + pcm if pcm else None


def _lisez_moi(tenant: dict, nb_audio: int) -> str:
    quand = horloge.maintenant().strftime("%d/%m/%Y à %H:%M")
    resos = (tenant.get("booking_provider") or "").startswith("resos")
    lignes = [
        f"Export des données de « {tenant['name']} » — {quand} (heure de Paris)",
        "",
        "Fichiers de cette archive :",
        "- etablissement.json : la fiche de l'établissement (identité, horaires, ce que "
        "l'assistante sait, voix, réglages).",
        "- reservations.csv : les réservations de notre carnet, annulées comprises "
        "(colonne cancelled_at).",
        "- appels.csv : tous les appels, avec le résumé, la transcription et le journal.",
        "- messages.csv : les messages laissés à l'équipe.",
        "- comptes.csv : les comptes de connexion (e-mail, rôle, date de création). "
        "Les mots de passe n'y sont pas.",
        f"- enregistrements/ : {nb_audio} enregistrement(s) d'appel, un fichier WAV "
        "stéréo par appel (l'appelant à gauche, l'assistante à droite), nommé appelN.wav "
        "où N est la colonne id de appels.csv.",
        "",
        "Les fichiers CSV se séparent par « ; » et s'ouvrent directement dans Excel. Les "
        "dates sont en UTC (heure de Paris moins une ou deux heures).",
    ]
    if resos:
        lignes += ["", "Cet établissement tient ses réservations chez resOS : elles font foi "
                   "là-bas. Seules celles de notre table sont jointes ici."]
    return "\r\n".join(lignes) + "\r\n"


def construire(tenant_id: int) -> Path:
    """Écrit l'archive dans un fichier temporaire et en rend le chemin. À l'appelant de
    le supprimer."""
    with db.get_conn() as conn:
        fiche = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if fiche is None:
            raise LookupError(f"établissement {tenant_id} inconnu")
        fiche = dict(fiche)
        resas, c_resas = _table(
            conn, "SELECT * FROM reservations WHERE tenant_id = ? ORDER BY id", tenant_id)
        appels, c_appels = _table(
            conn, "SELECT * FROM calls WHERE tenant_id = ? ORDER BY id", tenant_id)
        msgs, c_msgs = _table(
            conn, "SELECT * FROM messages WHERE tenant_id = ? ORDER BY id", tenant_id)
        comptes, c_comptes = _table(conn, _COMPTES, tenant_id)

    descripteur, nom = tempfile.mkstemp(prefix="helmane-export-", suffix=".zip")
    os.close(descripteur)
    chemin = Path(nom)
    try:
        with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as archive:
            nb_audio = 0
            for appel in appels:
                wav = _wav(tenant_id, appel["id"])
                if wav is not None:
                    archive.writestr(f"enregistrements/appel{appel['id']}.wav", wav)
                    nb_audio += 1
            archive.writestr("LISEZMOI.txt", _lisez_moi(fiche, nb_audio).encode("utf-8"))
            archive.writestr("etablissement.json", json.dumps(
                fiche, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
            archive.writestr("reservations.csv", _csv(resas, c_resas))
            archive.writestr("appels.csv", _csv(appels, c_appels))
            archive.writestr("messages.csv", _csv(msgs, c_msgs))
            archive.writestr("comptes.csv", _csv(comptes, c_comptes))
    except BaseException:
        chemin.unlink(missing_ok=True)
        raise
    return chemin
