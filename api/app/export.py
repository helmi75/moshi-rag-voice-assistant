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
import re
import tempfile
import time
import zipfile
from pathlib import Path

from . import db, horloge
from .voice import enregistrement, ulaw

# Séparateur « ; » et BOM UTF-8 : c'est ce qu'Excel français ouvre correctement.
_BOM = "﻿"

# Colonnes du compte exportées : jamais le hachage du mot de passe, qui n'est pas une
# donnée du restaurateur et faciliterait une attaque s'il circulait.
_COMPTES = "SELECT email, role, created_at FROM users WHERE tenant_id = ? ORDER BY id"


# Ce que l'admin cache au restaurateur ne sort pas non plus par l'archive (relecture) : le
# journal technique (SCRUM-108), le détail des coûts par poste, le moteur et le fournisseur
# de la voix — un restaurateur ne lit jamais le nom d'un fournisseur (décision du 09/10).
_APPELS_MASQUES = {"journal", "voix_fournisseur", "cout_telephonie", "cout_transcription",
                   "cout_comprehension", "cout_voix"}
_FICHE_MASQUEE = {"moteur_voix"}

# Un nom ou une note se dicte par un inconnu (téléphone, SMS) : « =HYPERLINK(…) » deviendrait
# une formule à l'ouverture dans Excel. Un numéro (« +33 6… ») reste un numéro.
_NOMBRE = re.compile(r"[+-][\d .]*")

# Les archives vivent ici, le temps de l'envoi. Starlette ne lance pas la suppression quand
# le client coupe en cours de route : chaque nouvel export fait le ménage des orphelins.
_DOSSIER = Path(tempfile.gettempdir()) / "helmane-export"
_ORPHELIN_SECONDES = 3600


class DisquePlein(Exception):
    """L'archive mangerait l'espace dont la base a besoin pour écrire les réservations."""


def _cellule(valeur) -> str:
    texte = "" if valeur is None else str(valeur)
    if texte[:1] in ("=", "@", "\t", "\r") or (
            texte[:1] in ("+", "-") and not _NOMBRE.fullmatch(texte)):
        return "'" + texte
    return texte


def _csv(lignes: list[dict], colonnes: list[str]) -> bytes:
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow(colonnes)
    for ligne in lignes:
        ecrivain.writerow([_cellule(ligne[c]) for c in colonnes])
    return (_BOM + tampon.getvalue()).encode("utf-8")


def _menage() -> None:
    limite = time.time() - _ORPHELIN_SECONDES
    for vieux in _DOSSIER.glob("helmane-export-*.zip"):
        try:
            if vieux.stat().st_mtime < limite:
                vieux.unlink()
        except OSError:
            pass


def _verifier_la_place(tenant_id: int, appels: list[dict]) -> None:
    """Le WAV 16 bits pèse le double du µ-law du disque. Ce qui reste après l'archive ne
    descend pas sous le plancher de l'enregistreur : sous lui, SQLite cesserait d'écrire."""
    octets = 0
    for appel in appels:
        for piste in enregistrement.PISTES:
            chemin = enregistrement.chemin(tenant_id, appel["id"], piste)
            if chemin.exists():
                octets += chemin.stat().st_size
    libre = enregistrement.espace_libre_mo(_DOSSIER)
    if libre is not None and libre - (2 * octets) // 1_000_000 < enregistrement.disque_minimal_mo():
        raise DisquePlein(f"{libre} Mo libres, archive estimée à {2 * octets // 1_000_000} Mo")


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
        "- appels.csv : tous les appels, avec leur résumé et leur transcription.",
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
        fiche = {k: v for k, v in dict(fiche).items() if k not in _FICHE_MASQUEE}
        resas, c_resas = _table(
            conn, "SELECT * FROM reservations WHERE tenant_id = ? ORDER BY id", tenant_id)
        appels, c_appels = _table(
            conn, "SELECT * FROM calls WHERE tenant_id = ? ORDER BY id", tenant_id)
        c_appels = [c for c in c_appels if c not in _APPELS_MASQUES]
        msgs, c_msgs = _table(
            conn, "SELECT * FROM messages WHERE tenant_id = ? ORDER BY id", tenant_id)
        comptes, c_comptes = _table(conn, _COMPTES, tenant_id)

    _DOSSIER.mkdir(parents=True, exist_ok=True)
    _menage()
    _verifier_la_place(tenant_id, appels)
    descripteur, nom = tempfile.mkstemp(prefix="helmane-export-", suffix=".zip", dir=_DOSSIER)
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
