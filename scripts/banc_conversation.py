#!/usr/bin/env python3
"""Banc conversationnel : un appelant qui ÉCOUTE avant de parler (#40).

**Pourquoi le premier banc ne suffisait pas.** `banc_appels.py` rejouait l'enregistrement
d'un vrai appel d'un seul tenant, à l'aveugle, sans jamais écouter l'assistante. Le
« client » parlait d'une candidature de cuisinier pendant qu'elle proposait une
réservation, lui coupait la parole huit fois sur trente tours, et enchaînait sa phrase
suivante qu'elle ait fini ou non. Ses chiffres de CHARGE restaient justes — CPU, premier
son, décomposition par étage — mais la CONVERSATION n'avait aucun sens : on ne pouvait
pas savoir si elle avait compris, ni si la réservation était la bonne.

Ce banc-ci joue un vrai scénario, tour par tour, avec la voix de Helmi :

    « Bonjour »
    « Je souhaiterais faire une réservation s'il vous plaît »
    « Nous sommes quatre et ça sera pour demain 20h »
    « Ça sera sous le nom de Helmi, H-E-L-M-I »
    « Très bien merci »

et chaque réplique n'est dite qu'après que l'assistante a FINI de parler.

**Comment « fini de parler » est décidé.** Comme le fait le téléphone de l'appelant : le
serveur envoie l'audio au rythme réel, on le rejoue dans un tampon de lecture simulé
(celui de Twilio), et l'assistante s'est tue quand plus rien d'audible n'est à jouer
depuis `--patience` secondes. Un `clear` de Twilio (elle a été interrompue) vide le
tampon, comme sur une vraie ligne. Entre deux répliques, le banc envoie du silence en
continu, 20 ms par 20 ms : une ligne téléphonique ne se tait jamais, elle transmet du
silence — un flux qui s'arrête serait un autre test.

**Trois volets, un seul passage :**
  - technique — le silence que l'appelant ENTEND, mesuré de SON côté (de la fin de sa
    phrase au premier son de la réponse), recoupé avec le journal de bord du serveur ;
  - fonctionnel — a-t-elle entendu « quatre », « demain », « Helmi » ? la réservation
    enregistrée est-elle la bonne, pour la bonne date ?
  - financier — ce que ça a coûté, relevé par API (voir `banc_couts.py`).

⚠️ Ce sont de VRAIS appels : GPU, transcription et modèle de langage sont facturés, et
chaque appel réussi crée une réservation dans l'établissement de test.

Usage :
    python3 scripts/banc_conversation.py --paliers 1,3,5,7 --je-sais-que-ca-coute
    python3 scripts/banc_conversation.py --scenario en --paliers 1,3 --je-sais-que-ca-coute
"""
import argparse
import asyncio
import base64
import datetime
import difflib
import json
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
sys.path.insert(0, str(ICI.parent / "api"))

import numpy as np  # noqa: E402

import banc_couts  # noqa: E402
from app.voice import ulaw  # noqa: E402  (le codec testé du projet, numpy seulement)

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("Il manque `websockets` : pip install websockets")

TRAME_OCTETS = 160          # 20 ms de µ-law à 8 kHz — ce qu'envoie Twilio
TRAME_S = 0.02
SILENCE = b"\xff" * TRAME_OCTETS   # 0xFF = zéro en µ-law
MARGE_TRAMES = 8            # 160 ms gardés autour de la parole au découpage des fichiers
SEUIL_AUDIBLE = 100.0       # RMS PCM16 : au-dessus, l'appelant entend quelque chose

DEFAUT_URL = os.getenv("BANC_URL", "wss://app.helmane.fr/ws/voice")
DEFAUT_TENANT = os.getenv("BANC_TENANT_TEL", "+33900000001")
NUMERO_APPELANT = "+33900000999"

# Le scénario. `faits` : ce qui DOIT avoir été entendu pour que la réplique compte comme
# comprise — plusieurs graphies acceptées, parce que Deepgram écrit « 4 » ou « quatre »,
# « 20h » ou « vingt heures », et que ce n'est pas une erreur.
SCRIPT = [
    {"fichier": "Bonjour",
     "texte": "Bonjour",
     "faits": {"bonjour": ["bonjour"]}},
    {"fichier": "Je_souhaiterais_faire_une_reservation_sil_vous_plait",
     "texte": "Je souhaiterais faire une réservation s'il vous plaît",
     "faits": {"réservation": ["reservation"]}},
    {"fichier": "Nous_sommes_quatre_et_ca_sera_pour_demain_20h",
     "texte": "Nous sommes quatre et ça sera pour demain 20h",
     "faits": {"quatre": ["quatre", "4"], "demain": ["demain"], "20 h": ["20", "vingt"]}},
    {"fichier": "Ca_sera_sous_le_nom_de_helmi_H_e_l_m_i",
     "texte": "Ça sera sous le nom de Helmi, H-E-L-M-I",
     "faits": {"Helmi": ["helmi"]}},
    {"fichier": "Tres_bien_merci",
     "texte": "Très bien merci",
     "faits": {"merci": ["merci"]}},
    # La même phrase une seconde fois, et ce n'est pas du remplissage. L'assistante
    # récapitule et demande « Est-ce bien cela ? » : il faut quelqu'un pour dire oui,
    # sinon la réservation n'est jamais enregistrée — c'est exactement ce qui est arrivé
    # à l'appel de validation. Les deux chemins sont couverts : si elle enchaîne
    # normalement, la 5e réplique confirme et la 6e conclut ; si elle se BLOQUE après
    # « je vérifie », la 5e la débloque et la 6e confirme. Le blocage est alors mesuré,
    # sans faire échouer le test fonctionnel pour une raison qui tiendrait au banc.
    {"fichier": "Tres_bien_merci",
     "texte": "Très bien merci (confirmation)",
     "faits": {}},
]

# Le même scénario en anglais (voice/langue.py). La première réplique est la VRAIE voix de
# Helmi, découpée dans l'appel 101 ; les suivantes sont synthétisées dans une voix anglaise
# du catalogue Kyutai (p329), faute d'enregistrement de Helmi en anglais.
SCRIPT_EN = [
    {"fichier": "Hello_my_name_is_Helmi_I_want_to_make_a_reservation",
     "texte": "Yes, hello, my name is Helmi. I want to make a reservation. Do you speak English?",
     "faits": {"reservation": ["reservation"]}},
    {"fichier": "Its_for_four_people_tomorrow_at_eight_pm",
     "texte": "It's for four people, tomorrow at eight p.m.",
     "faits": {"four": ["four", "4"], "tomorrow": ["tomorrow"], "8 pm": ["8", "eight", "20"]}},
    {"fichier": "The_name_is_Helmi_H_E_L_M_I",
     "texte": "The name is Helmi. H, E, L, M, I.",
     "faits": {"Helmi": ["helmi"]}},
    {"fichier": "Thats_perfect_thank_you",
     "texte": "That's perfect, thank you.",
     "faits": {"thank you": ["thank", "thanks"]}},
    {"fichier": "Thats_perfect_thank_you",
     "texte": "That's perfect, thank you (confirmation)",
     "faits": {}},
]

# Scénario -> (répliques, dossier des voix). Choisi par --scenario.
SCENARIOS = {"fr": (SCRIPT, "banc-voix"), "en": (SCRIPT_EN, "banc-voix-en")}
SCRIPT_ACTIF = SCRIPT

# Relances d'inactivité prononcées par le serveur (voice/bot.py) : ce ne sont pas des
# réponses au client, elles se comptent à part. Les deux premières sont les anciennes,
# gardées pour relire les bancs d'avant le 10/09/2026.
RELANCES = ("je vous ecoute que puis je faire pour vous", "etes vous toujours en ligne",
            "vous etes toujours la", "je ne vous entends plus",
            "are you still there", "i can t hear you anymore")

# Ce que dit l'assistante avant un appel d'outil (« Je vérifie tout de suite. »). Le
# transcript ne garde pas les outils : cette annonce suivie du résultat n'est PAS une
# double réponse, et le banc la comptait comme telle.
_ANNONCES = ("verifie", "je regarde", "un instant", "let me check", "checking", "one moment")


def _annonce_outil(norme: str) -> bool:
    return len(norme.split()) <= 10 and any(x in norme for x in _ANNONCES)


# ─── Audio ───────────────────────────────────────────────────────────────────

def _rms_par_trame(ulaw_octets: bytes) -> np.ndarray:
    pcm = np.frombuffer(ulaw.decoder(ulaw_octets), dtype="<i2").astype(np.float32)
    n = len(pcm) // TRAME_OCTETS
    if n == 0:
        return np.zeros(0)
    return np.sqrt((pcm[:n * TRAME_OCTETS].reshape(n, TRAME_OCTETS) ** 2).mean(axis=1))


def preparer_voix(dossier: Path, script: list) -> dict:
    """Charge chaque réplique et la découpe autour de la parole.

    Le découpage rend la mesure exacte : « fin de ma phrase » devient l'instant où la
    voix s'arrête, pas celui où un fichier se termine après une seconde de silence."""
    voix = {}
    for ligne in script:
        chemin = dossier / f"{ligne['fichier']}.ulaw"
        if not chemin.exists():
            sys.exit(f"Réplique introuvable : {chemin}")
        brut = chemin.read_bytes()
        r = _rms_par_trame(brut)
        seuil = max(float(np.percentile(r, 10)) * 4, 150.0)
        actives = np.flatnonzero(r > seuil)
        if not len(actives):
            sys.exit(f"Aucune parole détectée dans {chemin.name}")
        a = max(0, int(actives[0]) - MARGE_TRAMES)
        b = min(len(r), int(actives[-1]) + 1 + MARGE_TRAMES)
        voix[ligne["fichier"]] = brut[a * TRAME_OCTETS:b * TRAME_OCTETS]
    return voix


def _sid(prefixe: str) -> str:
    """`CABANCV…` : reconnaissable en base comme appel de banc (le rapport de charge le
    lit via `CABANC%`), et distinguable du banc de charge par le `V` de « voix »."""
    return prefixe + "BANCV" + "".join(random.choices("0123456789abcdef", k=27))


# ─── Un appel ────────────────────────────────────────────────────────────────

class Appel:
    """Un appelant simulé : il écoute, attend qu'elle se taise, puis parle."""

    def __init__(self, n: int, voix: dict, args, decalage: float, script=None):
        self.n = n
        self.voix = voix
        self.args = args
        self.decalage = decalage
        self.script = script if script is not None else SCRIPT_ACTIF
        self.call_sid = _sid("CA")
        self.stream_sid = _sid("MZ")
        self.t0 = 0.0
        # Émission
        self.a_dire = b""
        self.phrase_finie = asyncio.Event()
        self.t_fin_phrase = 0.0
        self.fin = False
        # Réception : tampon de lecture simulé (celui de Twilio)
        self.fin_lecture = 0.0
        self.sons: list[tuple[float, float]] = []   # intervalles AUDIBLES (début, fin)
        self.clears = 0
        self.ferme = False
        # Résultats
        self.premier_son_s: Optional[float] = None
        self.fin_accueil_s: Optional[float] = None
        self.lignes: list[dict] = []
        self.raccroche_par: Optional[str] = None
        self.erreur: Optional[str] = None
        self.duree_s = 0.0

    # -- ce que l'appelant entend --------------------------------------------

    def _fin_audible(self) -> float:
        return self.sons[-1][1] if self.sons else 0.0

    def _recevoir_audio(self, octets: bytes) -> None:
        maintenant = time.monotonic()
        debut = max(self.fin_lecture, maintenant)
        rms = _rms_par_trame(octets)
        # Chaque tranche de 20 ms est placée sur la ligne de temps de LECTURE : c'est à
        # cet instant que l'appelant l'entend, pas à celui où elle arrive sur le réseau.
        for k, valeur in enumerate(rms):
            t = debut + k * TRAME_S
            if valeur > SEUIL_AUDIBLE:
                if self.sons and t - self.sons[-1][1] <= TRAME_S * 1.5:
                    self.sons[-1] = (self.sons[-1][0], t + TRAME_S)
                else:
                    self.sons.append((t, t + TRAME_S))
                if self.premier_son_s is None:
                    self.premier_son_s = round(t - self.t0, 2)
        self.fin_lecture = debut + len(octets) / 8000

    async def _ecouter(self, ws) -> None:
        try:
            async for brut in ws:
                try:
                    m = json.loads(brut)
                except (json.JSONDecodeError, TypeError):
                    continue
                ev = m.get("event")
                if ev == "media":
                    octets = base64.b64decode(m.get("media", {}).get("payload") or "")
                    if octets:
                        self._recevoir_audio(octets)
                elif ev == "clear":
                    # Twilio vide son tampon : la voix s'arrête net, comme sur une ligne.
                    self.clears += 1
                    maintenant = time.monotonic()
                    self.fin_lecture = maintenant
                    self.sons = [(a, min(b, maintenant)) for a, b in self.sons if a < maintenant]
        except websockets.ConnectionClosed:
            pass
        finally:
            self.ferme = True

    # -- ce que l'appelant envoie --------------------------------------------

    async def _emettre(self, ws) -> None:
        depart = time.monotonic()
        trame = 0
        try:
            while not self.fin and not self.ferme:
                if self.a_dire:
                    bloc = self.a_dire[:TRAME_OCTETS]
                    self.a_dire = self.a_dire[TRAME_OCTETS:]
                    if len(bloc) < TRAME_OCTETS:
                        bloc += SILENCE[len(bloc):]
                    if not self.a_dire:
                        self.t_fin_phrase = time.monotonic()
                        self.phrase_finie.set()
                else:
                    bloc = SILENCE
                trame += 1
                await ws.send(json.dumps({
                    "event": "media", "streamSid": self.stream_sid,
                    "media": {"track": "inbound", "chunk": str(trame),
                              "timestamp": str(trame * 20),
                              "payload": base64.b64encode(bloc).decode()},
                }))
                attente = depart + trame * TRAME_S - time.monotonic()
                if attente > 0:
                    await asyncio.sleep(attente)
        except websockets.ConnectionClosed:
            self.ferme = True
            self.phrase_finie.set()

    # -- le déroulé ----------------------------------------------------------

    async def _attendre(self, condition, delai: float) -> bool:
        limite = time.monotonic() + delai
        while time.monotonic() < limite:
            if condition():
                return True
            if self.ferme:
                return False
            await asyncio.sleep(0.02)
        return False

    async def _attendre_silence(self, patience: float, delai: float) -> bool:
        """Elle s'est tue depuis `patience` secondes — du point de vue de l'oreille."""
        return await self._attendre(
            lambda: time.monotonic() >= self._fin_audible() + patience, delai)

    async def _dire(self, i: int, ligne: dict) -> dict:
        audio = self.voix[ligne["fichier"]]
        t_debut = time.monotonic()
        elle_parlait = t_debut < self._fin_audible()
        self.phrase_finie.clear()
        self.a_dire = audio
        await self.phrase_finie.wait()
        # Fin de la VOIX : la marge de découpage est retirée, pour mesurer à partir de
        # l'instant exact où l'appelant se tait.
        t_fin_voix = self.t_fin_phrase - MARGE_TRAMES * TRAME_S

        def premiere_reponse() -> Optional[float]:
            for a, _ in self.sons:
                if a >= t_fin_voix:
                    return a
            return None

        ok = await self._attendre(lambda: premiere_reponse() is not None,
                                  self.args.attente_max)
        debut_reponse = premiere_reponse() if ok else None
        mesure = {
            "ligne": i + 1, "texte": ligne["texte"],
            "t_s": round(t_debut - self.t0, 2),
            "reponse_ms": None, "duree_reponse_s": None, "trou_max_ms": None,
            "j_ai_parle_sur_elle": elle_parlait,
            "elle_a_parle_sur_moi": any(a < t_fin_voix and b > t_debut for a, b in self.sons),
        }
        if debut_reponse is None:
            return mesure  # aucune réponse dans le délai : c'est une donnée
        mesure["reponse_ms"] = round((debut_reponse - t_fin_voix) * 1000)
        await self._attendre_silence(self.args.patience, 90)
        segments = [(a, b) for a, b in self.sons if a >= debut_reponse]
        if segments:
            mesure["duree_reponse_s"] = round(segments[-1][1] - segments[0][0], 2)
            trous = [s2[0] - s1[1] for s1, s2 in zip(segments, segments[1:])]
            # Le plus long silence À L'INTÉRIEUR de sa réponse : typiquement entre
            # « je vérifie tout de suite » et le résultat. L'appelant l'entend aussi.
            mesure["trou_max_ms"] = round(max(trous) * 1000) if trous else 0
        return mesure

    async def jouer(self) -> "Appel":
        await asyncio.sleep(self.decalage)
        self.t0 = time.monotonic()
        try:
            async with websockets.connect(self.args.url, max_size=None,
                                          open_timeout=20, close_timeout=5) as ws:
                await ws.send(json.dumps({"event": "connected", "protocol": "Call",
                                          "version": "1.0.0"}))
                await ws.send(json.dumps({
                    "event": "start", "sequenceNumber": "1", "streamSid": self.stream_sid,
                    "start": {
                        "streamSid": self.stream_sid, "callSid": self.call_sid,
                        "accountSid": "ACbanc", "tracks": ["inbound"],
                        "customParameters": {"To": self.args.tenant_tel,
                                             "From": NUMERO_APPELANT},
                        "mediaFormat": {"encoding": "audio/x-mulaw",
                                        "sampleRate": 8000, "channels": 1},
                    },
                }))
                recepteur = asyncio.create_task(self._ecouter(ws))
                emetteur = asyncio.create_task(self._emettre(ws))

                # 1. L'accueil. On attend qu'il soit ENTIÈREMENT dit : le STT est muet
                #    jusqu'à la reprise « je vous écoute », parler avant, c'est parler
                #    dans le vide.
                await self._attendre(lambda: self.premier_son_s is not None, 120)
                await self._attendre_silence(self.args.patience_ouverture, 150)
                if self.sons:
                    self.fin_accueil_s = round(self._fin_audible() - self.t0, 2)

                # 2. Le scénario, réplique par réplique.
                for i, ligne in enumerate(self.script):
                    if self.ferme:
                        break
                    self.lignes.append(await self._dire(i, ligne))

                # 3. La fin : elle raccroche d'elle-même après « au revoir », ou on
                #    raccroche au bout de quelques secondes de silence.
                await self._attendre(lambda: self.ferme, self.args.attente_fin)
                self.raccroche_par = "assistante" if self.ferme else "banc"
                self.fin = True
                if not self.ferme:
                    try:
                        await ws.send(json.dumps({"event": "stop",
                                                  "streamSid": self.stream_sid}))
                    except websockets.ConnectionClosed:
                        pass
                for tache in (emetteur, recepteur):
                    tache.cancel()
        except Exception as exc:
            self.erreur = f"{type(exc).__name__}: {exc}"
        self.duree_s = round(time.monotonic() - self.t0, 1)
        return self


# ─── Côté serveur : ce que le service a entendu, dit, et enregistré ─────────

LECTURE_SERVEUR = r'''
import sqlite3, json, sys
sids = json.loads(sys.argv[1])
c = sqlite3.connect("/app/data/app.db"); c.row_factory = sqlite3.Row
sortie = {}
for sid in sids:
    r = c.execute("""SELECT id, started_at, duration_seconds, status, journal, reservation_id,
                            transcript
                     FROM calls WHERE call_sid = ?""", (sid,)).fetchone()
    if r is None:
        sortie[sid] = None
        continue
    resa = None
    if r["reservation_id"]:
        x = c.execute("""SELECT customer_name, date, time, party_size, cancelled_at
                         FROM reservations WHERE id = ?""", (r["reservation_id"],)).fetchone()
        resa = dict(x) if x else None
    sortie[sid] = {"id": r["id"], "started_at": r["started_at"],
                   "duree": r["duration_seconds"], "statut": r["status"],
                   "journal": json.loads(r["journal"]) if r["journal"] else None,
                   "transcript": json.loads(r["transcript"]) if r["transcript"] else [],
                   "reservation": resa}
tel = sys.argv[2]
sortie["_reservations_du_banc"] = c.execute(
    "SELECT COUNT(*) FROM reservations WHERE customer_phone = ?", (tel,)).fetchone()[0]
print("#JSON" + json.dumps(sortie, ensure_ascii=False))
'''


def lire_serveur(sids: list[str]) -> dict:
    commande = (f"docker exec -i moshi-rag-voice-assistant-api-1 python - "
                f"{shlex.quote(json.dumps(sids))} {shlex.quote(NUMERO_APPELANT)}")
    sortie = subprocess.run(
        ["ssh", "-i", os.path.expanduser(os.getenv("VPS_SSH_KEY", "~/.ssh/moshi-vps-deploy")),
         "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
         os.getenv("VPS_HOTE", "root@187.77.172.87"), commande],
        input=LECTURE_SERVEUR, text=True, capture_output=True, timeout=180)
    for ligne in sortie.stdout.splitlines():
        if ligne.startswith("#JSON"):
            return json.loads(ligne[5:])
    raise RuntimeError(f"lecture serveur impossible : {sortie.stderr.strip()[:400]}")


# ─── Analyse fonctionnelle ───────────────────────────────────────────────────

def _mots(texte: str) -> list[str]:
    t = unicodedata.normalize("NFD", (texte or "").lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)          # « 20h » -> « 20 h »
    return re.sub(r"[^a-z0-9 ]", " ", t).split()


def _contient(mots: list[str], variante: str) -> bool:
    if variante in mots:
        return True
    # Un nom épelé arrive en lettres isolées : « h e l m i » compte comme « helmi ».
    lettres, courant = [], ""
    for m in mots:
        if len(m) == 1 and m.isalpha():
            courant += m
        else:
            if courant:
                lettres.append(courant)
            courant = ""
    if courant:
        lettres.append(courant)
    return variante in lettres


def mesurer_dialogue(msgs: list[dict], tours: list[dict]) -> dict:
    """Ce que le transcript du modèle révèle, et que le seul chronomètre ne voit pas."""
    norme = [" ".join(_mots(m.get("content") or "")) for m in msgs]
    vu_client = False
    doubles, relances, blocages = [], 0, []
    for i, (a, b) in enumerate(zip(msgs, msgs[1:])):
        if a["role"] == "user":
            vu_client = True
        if not vu_client:
            continue  # l'accueil et la reprise se suivent par construction
        a_relance = any(norme[i].startswith(r) for r in RELANCES)
        b_relance = any(norme[i + 1].startswith(r) for r in RELANCES)
        annonce = a["role"] == "assistant" and _annonce_outil(norme[i])
        if (a["role"] == "assistant" and b["role"] == "assistant"
                and not (a_relance or b_relance or annonce)):
            doubles.append((a["content"], b["content"]))
        # « Je vérifie tout de suite. » puis… le client reprend : l'outil n'est pas parti.
        if (a["role"] == "assistant" and "verifie" in norme[i]
                and len(norme[i].split()) <= 8 and b["role"] == "user"):
            blocages.append(a["content"])
    for i, m in enumerate(msgs):
        if m["role"] == "assistant" and any(norme[i].startswith(r) for r in RELANCES) \
                and any(x["role"] == "user" for x in msgs[:i]):
            relances += 1
    minuterie = [t.get("entendu") for t in tours
                 if (t.get("attente_tour_ms") or 0) >= 2000]
    nom = None
    for t in tours:
        trouve = re.search(r"(?:nom de|name is) ((?:[a-z]\s?)+|[a-z]+)",
                           " ".join(_mots(t.get("entendu") or "")))
        if trouve:
            nom = trouve.group(1).replace(" ", "")
    return {"doubles": doubles, "relances": relances, "blocages": blocages,
            "minuterie": minuterie, "nom_entendu": nom}


def analyser_appel(appel: Appel, serveur: Optional[dict]) -> dict:
    """Ce qu'elle a entendu de chaque réplique, et si la réservation est la bonne."""
    tours = ((serveur or {}).get("journal") or {}).get("tours") or []
    entendu_total = _mots(" ".join(t.get("entendu") or "" for t in tours))
    faits = {}
    for ligne in appel.script:
        for fait, variantes in ligne["faits"].items():
            faits[fait] = any(_contient(entendu_total, v) for v in variantes)

    # Réplique -> tour du serveur le plus ressemblant (lecture seule, pour l'affichage).
    correspondances = []
    for ligne in appel.script:
        attendu = " ".join(_mots(ligne["texte"]))
        meilleur, score = None, 0.0
        for t in tours:
            s = difflib.SequenceMatcher(None, attendu, " ".join(_mots(t.get("entendu") or ""))).ratio()
            if s > score:
                meilleur, score = t, s
        correspondances.append({"texte": ligne["texte"],
                                "entendu": (meilleur or {}).get("entendu"),
                                "dit": (meilleur or {}).get("dit"),
                                "ressemblance": round(score, 2)})

    resa = (serveur or {}).get("reservation")
    verdict_resa = {"creee": bool(resa)}
    if resa and serveur.get("started_at"):
        jour_appel = datetime.date.fromisoformat(serveur["started_at"][:10])
        verdict_resa.update({
            "nom_ok": "helmi" in " ".join(_mots(resa.get("customer_name") or "")).replace(" ", ""),
            "couverts_ok": resa.get("party_size") == 4,
            "heure_ok": (resa.get("time") or "").startswith("20:00"),
            "date_ok": resa.get("date") == (jour_appel + datetime.timedelta(days=1)).isoformat(),
            "enregistre": resa,
        })
        verdict_resa["juste"] = all(verdict_resa[k] for k in
                                    ("nom_ok", "couverts_ok", "heure_ok", "date_ok"))
    else:
        verdict_resa["juste"] = False
    dialogue = mesurer_dialogue((serveur or {}).get("transcript") or [], tours)
    return {"faits": faits, "correspondances": correspondances,
            "reservation": verdict_resa, "tours_serveur": len(tours), "dialogue": dialogue}


# ─── Rapport ─────────────────────────────────────────────────────────────────

def _med(v):
    return statistics.median(v) if v else None


def _p90(v):
    return sorted(v)[min(len(v) - 1, int(len(v) * 0.9))] if v else None


def _ms(v):
    return "   —  " if v is None else f"{v:5.0f} ms"


def rapport(paliers: list[tuple[int, list[Appel]]], serveur: dict) -> dict:
    print("\n" + "=" * 96)
    print("VOLET TECHNIQUE — ce que l'appelant entend, et ce que le serveur a mesuré")
    print("=" * 96)
    print("simultanés │ silence perçu (appelant) │ blanc serveur │ attente  llm    tts  │"
          " sans rép. │ chevauch.")
    print("           │   médiane      p90       │ médiane  p90  │  (médianes, ms)      │"
          "           │")
    print("─" * 96)
    resume = []
    for n, appels in paliers:
        percu, blancs, att, llm, tts, sans, chev = [], [], [], [], [], 0, 0
        for a in appels:
            for m in a.lignes:
                if m["reponse_ms"] is None:
                    sans += 1
                elif not (m["j_ai_parle_sur_elle"] or m["elle_a_parle_sur_moi"]):
                    percu.append(m["reponse_ms"])
                if m["j_ai_parle_sur_elle"] or m["elle_a_parle_sur_moi"]:
                    chev += 1
            j = ((serveur.get(a.call_sid) or {}).get("journal") or {})
            for t in j.get("tours") or []:
                for cle, liste in (("blanc_ressenti_ms", blancs), ("attente_tour_ms", att),
                                   ("llm_ms", llm), ("tts_ms", tts)):
                    if t.get(cle):
                        liste.append(t[cle])
        print(f"{n:9}  │ {_ms(_med(percu))}  {_ms(_p90(percu))}      │"
              f" {_ms(_med(blancs))} {_ms(_p90(blancs))} │"
              f" {_med(att) or 0:5.0f} {_med(llm) or 0:5.0f} {_med(tts) or 0:5.0f}   │"
              f" {sans:6}    │ {chev:6}")
        resume.append({"simultanes": n, "percu_med": _med(percu), "percu_p90": _p90(percu),
                       "blanc_med": _med(blancs), "blanc_p90": _p90(blancs),
                       "attente_med": _med(att), "llm_med": _med(llm), "tts_med": _med(tts),
                       "sans_reponse": sans, "chevauchements": chev})
    print("\nRéférence : vrais appels, un seul à la fois — blanc serveur médian 1 904 ms.")
    print("« Silence perçu » : de la fin de la voix de l'appelant au premier son de la réponse,")
    print("réseau compris — c'est ce qu'entend un vrai client. Les répliques où l'un a parlé")
    print("sur l'autre en sont exclues et comptées à part, pour ne pas fausser la médiane.")

    trous = [m["trou_max_ms"] for _, ap in paliers for a in ap for m in a.lignes
             if m.get("trou_max_ms")]
    if trous:
        print(f"\nSilence le plus long À L'INTÉRIEUR d'une réponse (ex. après « je vérifie ») :"
              f" médiane {statistics.median(trous):.0f} ms, max {max(trous):.0f} ms")
    ouv = [a.premier_son_s for _, ap in paliers for a in ap if a.premier_son_s is not None]
    acc = [a.fin_accueil_s for _, ap in paliers for a in ap if a.fin_accueil_s is not None]
    if ouv:
        print(f"Accueil : premier son à {statistics.median(ouv):.1f} s (médiane), "
              f"l'appelant peut parler à {statistics.median(acc):.1f} s")

    print("\n" + "=" * 96)
    print("VOLET FONCTIONNEL — a-t-elle compris, et a-t-elle réservé juste ?")
    print("=" * 96)
    analyses = {}
    for n, appels in paliers:
        for a in appels:
            analyses[a.call_sid] = analyser_appel(a, serveur.get(a.call_sid))
    tous_faits = list(SCRIPT_FAITS())
    print(f"{'simultanés':>10} │ " + " │ ".join(f"{f:>11}" for f in tous_faits)
          + " │ réservation juste")
    print("─" * 96)
    for n, appels in paliers:
        cases = []
        for f in tous_faits:
            ok = sum(1 for a in appels if analyses[a.call_sid]["faits"].get(f))
            cases.append(f"{ok:>4} / {len(appels):<4}")
        justes = sum(1 for a in appels if analyses[a.call_sid]["reservation"]["juste"])
        creees = sum(1 for a in appels if analyses[a.call_sid]["reservation"]["creee"])
        print(f"{n:>10} │ " + " │ ".join(f"{c:>11}" for c in cases)
              + f" │ {justes} / {len(appels)}  (créées : {creees})")

    fautes = [(a, analyses[a.call_sid]) for _, ap in paliers for a in ap
              if not analyses[a.call_sid]["reservation"]["juste"]]
    if fautes:
        print("\nRéservations absentes ou fausses :")
        for a, an in fautes[:10]:
            r = an["reservation"]
            if not r["creee"]:
                print(f"  • appel {a.n} ({a.call_sid[:15]}) : AUCUNE réservation enregistrée")
            else:
                e = r["enregistre"]
                print(f"  • appel {a.n} : {e['customer_name']} × {e['party_size']}, "
                      f"{e['date']} {e['time']}  — nom {'✓' if r['nom_ok'] else '✗'} "
                      f"couverts {'✓' if r['couverts_ok'] else '✗'} "
                      f"date {'✓' if r['date_ok'] else '✗'} heure {'✓' if r['heure_ok'] else '✗'}")

    print("\n" + "=" * 96)
    print("QUALITÉ DU DIALOGUE — ce que le transcript du modèle révèle")
    print("=" * 96)
    print("simultanés │ bloquée après « je vérifie » │ doubles réponses │ relances │"
          " fins de tour à la minuterie │ nom entendu")
    print("─" * 96)
    for n, appels in paliers:
        d = [analyses[a.call_sid]["dialogue"] for a in appels]
        noms = {}
        for x in d:
            noms[x["nom_entendu"] or "—"] = noms.get(x["nom_entendu"] or "—", 0) + 1
        print(f"{n:>10} │ {sum(bool(x['blocages']) for x in d):>6} / {len(d):<19} │"
              f" {sum(len(x['doubles']) for x in d):>9}        │ {sum(x['relances'] for x in d):>5}    │"
              f" {sum(len(x['minuterie']) for x in d):>13}               │ "
              + ", ".join(f"{k} ×{v}" for k, v in sorted(noms.items(), key=lambda kv: -kv[1])))
    langues = {}
    for a in (a for _, ap in paliers for a in ap):
        tranchee = ((serveur.get(a.call_sid) or {}).get("journal") or {}).get("langue") or "—"
        langues[tranchee] = langues.get(tranchee, 0) + 1
    print("\nLangue tranchée par le serveur : "
          + ", ".join(f"{k} ×{v}" for k, v in sorted(langues.items())))
    minut = {}
    for a in (a for _, ap in paliers for a in ap):
        for e in analyses[a.call_sid]["dialogue"]["minuterie"]:
            minut[e or "—"] = minut.get(e or "—", 0) + 1
    if minut:
        print("\nRépliques jugées INACHEVÉES par smart-turn (attente de la minuterie de 2 s) :")
        for e, k in sorted(minut.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  ×{k:<3} « {e} »")

    # Une conversation complète par palier, telle que le MODÈLE l'a vécue : le transcript
    # fait foi, pas le journal, qui attribue mal le texte quand elle répond avant la fin
    # de tour (inférence spéculative).
    for n, appels in paliers:
        a = appels[0]
        msgs = (serveur.get(a.call_sid) or {}).get("transcript") or []
        print(f"\n── Conversation réelle, palier {n} (appel {a.n}) " + "─" * 38)
        for m in msgs:
            qui = "ELLE     " if m["role"] == "assistant" else "ENTENDU  "
            print(f"  {qui}: {m['content'][:120]}")
        percu = ", ".join("—" if m["reponse_ms"] is None else f"{m['reponse_ms']}" for m in a.lignes)
        print(f"  (silence perçu après chaque réplique, en ms : {percu})")
    return {"resume": resume, "analyses": analyses}


def SCRIPT_FAITS():
    for ligne in SCRIPT_ACTIF:
        yield from ligne["faits"].keys()


# ─── Orchestration ───────────────────────────────────────────────────────────

async def palier(n: int, voix: dict, args) -> list[Appel]:
    print(f"\n── {n} appel(s) simultané(s) ".ljust(66, "─"))
    # Départs décalés au hasard : de vrais appelants n'appellent pas à la milliseconde
    # près, et des tours parfaitement alignés mesureraient le pire cas, pas la réalité.
    appels = [Appel(k + 1, voix, args, random.uniform(0, args.decalage)) for k in range(n)]
    debut = time.monotonic()
    await asyncio.gather(*(a.jouer() for a in appels))
    ko = [a for a in appels if a.erreur]
    print(f"   terminé en {time.monotonic() - debut:.0f} s · {n - len(ko)} abouti(s), "
          f"{len(ko)} en échec"
          + "".join(f"\n     ✗ appel {a.n} : {a.erreur}" for a in ko[:3]))
    rep = [m["reponse_ms"] for a in appels for m in a.lignes if m["reponse_ms"] is not None]
    if rep:
        print(f"   silence perçu : médiane {statistics.median(rep):.0f} ms · "
              f"max {max(rep):.0f} ms · raccrochés par l'assistante : "
              f"{sum(1 for a in appels if a.raccroche_par == 'assistante')}/{n}")
    return appels


async def principal() -> int:
    global SCRIPT_ACTIF
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--paliers", default="1,3,5,7",
                   help="nombres d'appels simultanés, dans l'ordre (défaut 1,3,5,7)")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="fr",
                   help="réservation en français (voix de Helmi) ou en anglais")
    p.add_argument("--voix", type=Path, default=None,
                   help="dossier des répliques en µ-law 8 kHz (défaut : celui du scénario)")
    p.add_argument("--url", default=DEFAUT_URL)
    p.add_argument("--tenant-tel", default=DEFAUT_TENANT)
    p.add_argument("--patience", type=float, default=2.0,
                   help="silence (s) avant de reprendre la parole. 2 s : ne pas couper "
                        "une assistante qui dit « je vérifie » puis enchaîne")
    p.add_argument("--patience-ouverture", type=float, default=2.5,
                   help="idem pour l'accueil, qui enchaîne deux phrases (défaut 2,5 s)")
    p.add_argument("--attente-max", type=float, default=15.0,
                   help="au-delà, la réplique est comptée « sans réponse »")
    p.add_argument("--attente-fin", type=float, default=12.0,
                   help="après la dernière réplique, délai laissé pour qu'elle raccroche. "
                        "Le serveur raccroche 8 s après un « au revoir » (USER_IDLE_TIMEOUT) : "
                        "attendre moins ferait raccrocher le banc à sa place")
    p.add_argument("--decalage", type=float, default=6.0,
                   help="décalage aléatoire maximal des départs, en secondes")
    p.add_argument("--graine", type=int, default=20260910,
                   help="graine du hasard : un banc rejouable à l'identique")
    p.add_argument("--sans-prechauffage", action="store_true")
    p.add_argument("--je-sais-que-ca-coute", action="store_true")
    args = p.parse_args()

    random.seed(args.graine)
    paliers_n = [int(x) for x in args.paliers.split(",")]
    SCRIPT_ACTIF, dossier = SCENARIOS[args.scenario]
    if args.voix is None:
        args.voix = ICI.parent / dossier
    voix = preparer_voix(args.voix, SCRIPT_ACTIF)

    print("=" * 70)
    print(f"BANC CONVERSATIONNEL — scénario « {args.scenario} », un appelant qui écoute")
    print("=" * 70)
    for ligne in SCRIPT_ACTIF:
        print(f"  « {ligne['texte']} »  ({len(voix[ligne['fichier']]) / 8000:.1f} s)")
    print(f"Paliers : {paliers_n} — {sum(paliers_n)} appels · patience {args.patience} s · "
          f"départs décalés de 0 à {args.decalage:.0f} s")
    if not args.je_sais_que_ca_coute:
        print("\nCe sont de VRAIS appels, facturés, qui créent des réservations de test.")
        print("Relance avec --je-sais-que-ca-coute pour confirmer.")
        return 1

    print("\nRelevé de facturation (avant)…")
    # Hors de la boucle d'événements : le client Modal est bloquant, et le relevé ne
    # doit rien retarder une fois les appels lancés.
    avant = await asyncio.to_thread(banc_couts.instantane)
    print(f"   OpenRouter {avant['openrouter_usd']} $ · Modal "
          f"{'lu' if avant.get('modal_heures') is not None else 'indisponible'}")

    prechauffe: list[Appel] = []
    if not args.sans_prechauffage:
        # Un seul « Bonjour » pour réveiller le GPU. Pas mesuré, mais FACTURÉ : il fait
        # partie du coût réel d'un banc, et du coût réel d'un premier appel à froid.
        print("\n── préchauffage : un « Bonjour » pour réveiller le GPU ".ljust(66, "─"))
        a = Appel(0, voix, args, 0.0, script=SCRIPT_ACTIF[:1])
        await a.jouer()
        prechauffe.append(a)
        rep = a.lignes[0]["reponse_ms"] if a.lignes else None
        print(f"   accueil à {a.premier_son_s} s · réponse au « Bonjour » : "
              f"{'aucune' if rep is None else f'{rep} ms'}"
              + (f" · {a.erreur}" if a.erreur else ""))

    paliers: list[tuple[int, list[Appel]]] = []
    for i, n in enumerate(paliers_n):
        paliers.append((n, await palier(n, voix, args)))
        if i < len(paliers_n) - 1:
            # < 120 s : sous la fenêtre d'extinction du GPU, qui reste chaud entre deux
            # paliers — sinon chacun commencerait par un démarrage à froid.
            print("   (pause de 20 s)")
            await asyncio.sleep(20)

    tous = [a for _, ap in paliers for a in ap]
    print("\nLecture des journaux côté serveur…")
    serveur = lire_serveur([a.call_sid for a in tous + prechauffe])
    resultats = rapport(paliers, serveur)

    print("\nFacturation : on attend l'extinction du GPU pour compter aussi ses 120 s")
    print("d'inactivité, puis que la facture Modal cesse de bouger…")
    # L'heure de départ est transmise : un banc qui passe l'heure pile doit lire les deux
    # seaux horaires de Modal, sinon la consommation de la première heure disparaît.
    apres = await asyncio.to_thread(banc_couts.attendre_facture_stable,
                                    datetime.datetime.fromisoformat(avant["quand"]))
    banc_couts.enregistrer(avant, apres)
    secondes = sum(a.duree_s for a in tous + prechauffe)
    facture = banc_couts.calculer(avant, apres, secondes, len(tous) + len(prechauffe),
                                  durees_appels=[a.duree_s for a in tous + prechauffe])
    banc_couts.afficher(facture)

    horodatage = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    fichier = ICI.parent / f"banc-conversation-{args.scenario}-{horodatage}.json"
    fichier.write_text(json.dumps({
        "parametres": vars(args) | {"voix": str(args.voix)},
        "paliers": [{"simultanes": n, "appels": [
            {"n": a.n, "call_sid": a.call_sid, "decalage_s": round(a.decalage, 2),
             "premier_son_s": a.premier_son_s, "fin_accueil_s": a.fin_accueil_s,
             "lignes": a.lignes, "raccroche_par": a.raccroche_par, "clears": a.clears,
             "duree_s": a.duree_s, "erreur": a.erreur} for a in ap]} for n, ap in paliers],
        "serveur": serveur, "analyse": resultats, "facture": facture,
    }, ensure_ascii=False, indent=2, default=str))
    print(f"\nRésultats complets : {fichier.name}")
    print(f"Réservations créées par le banc en base (cumul) : "
          f"{serveur.get('_reservations_du_banc')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
