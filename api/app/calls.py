"""Journal des appels : persistance et statistiques pour la plateforme admin.

start_call/finish_call sont appelés depuis le chemin d'appel vocal : ils sont
enveloppés de try/except par L'APPELANT et doivent rester rapides (INSERT/UPDATE
SQLite ≈ 1 ms). finish_call est appelé via asyncio.to_thread depuis bot.py pour ne
jamais bloquer l'event loop.
"""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, horloge

# Coût d'un appel, poste par poste (SCRUM-99, 28/09/2026). Chaque tarif se surcharge
# par variable d'environnement. Le coût EXACT reste l'affaire des factures
# (scripts/cost_report.py) ; ceci chiffre ce que l'appel a RÉELLEMENT consommé.
#
# - Twilio : 0,01 $ par minute ENTAMÉE — lu sur les factures des appels 183 à 204
#   (numéro français, entrant) : 98 s → 0,02 $, 453 s → 0,08 $. L'ancien chiffre,
#   0,0085 $/min au prorata, venait du numéro américain de juillet.
# - Deepgram nova-3 en flux : 0,0077 $/min monolingue, 0,0092 $/min en `multi`. On
#   DÉCROCHE en `multi` (voice/langue.py) : on retient la borne haute. Un coût surestimé
#   d'un demi-centime se voit ; sous-estimé, il ne se voit qu'à la facture.
# - Gemini 2.5 Flash via OpenRouter (API de tarifs, 28/09/2026) : 0,30 $ le million de
#   jetons d'entrée, 0,03 $ s'ils sont lus en cache, 2,50 $ en sortie. Le forfait de
#   0,35 c par appel ne sert plus qu'aux appels sans mesure.
# - Voix Mistral (Voxtral) : 0,016 $ pour 1 000 caractères (mistral.ai, mars 2026).
# - Voix Moshi (GPU Modal, secours seulement) : ~2 c/min.
_COST_TWILIO_PER_MIN = float(os.getenv("COST_TWILIO_PER_MIN", "0.01"))
_COST_DEEPGRAM_PER_MIN = float(os.getenv("COST_DEEPGRAM_PER_MIN", "0.0092"))
_COST_LLM_ENTREE = float(os.getenv("COST_LLM_ENTREE_PAR_MILLION", "0.30")) / 1e6
_COST_LLM_CACHE = float(os.getenv("COST_LLM_CACHE_PAR_MILLION", "0.03")) / 1e6
_COST_LLM_SORTIE = float(os.getenv("COST_LLM_SORTIE_PAR_MILLION", "2.50")) / 1e6
_COST_LLM_PER_CALL = float(os.getenv("COST_LLM_PER_CALL", "0.0035"))
_COST_VOIX_PAR_CARACTERE = float(os.getenv("COST_VOIX_PAR_MILLE_CARACTERES", "0.016")) / 1000
_COST_MODAL_PER_MIN = float(os.getenv("COST_MODAL_PER_MIN", "0.02"))
# Essai GPT-Live (app/voice/live.py), tarifs d'OpenAI relevés le 04/10/2026 : 0,05 $ la
# minute de session, à la seconde, transcription comprise ; le modèle d'arrière-plan
# (gpt-6-luna) à part : 0,10 $ le million de jetons d'entrée, 0,01 $ en cache, 0,50 $
# en sortie.
_COST_GPT_LIVE_PER_MIN = float(os.getenv("COST_GPT_LIVE_PER_MIN", "0.05"))
_COST_GPT_LIVE_ENTREE = float(os.getenv("COST_GPT_LIVE_ENTREE_PAR_MILLION", "0.10")) / 1e6
_COST_GPT_LIVE_CACHE = float(os.getenv("COST_GPT_LIVE_CACHE_PAR_MILLION", "0.01")) / 1e6
_COST_GPT_LIVE_SORTIE = float(os.getenv("COST_GPT_LIVE_SORTIE_PAR_MILLION", "0.50")) / 1e6
# Renvoi vers le restaurant en cas de panne (ASSISTANTE-118) : une SECONDE communication,
# sortante, en plus de l'appel reçu. Grille Twilio du compte (API Pricing, France,
# 01/10/2026) : 0,0187 $/min vers un fixe, 0,0404 $/min vers un portable quand le numéro
# présenté est européen. Présenté hors d'Europe, le portable passe à 0,1603 $/min : on
# ne le chiffre pas ici (appelants étrangers, rares) — c'est la facture qui le dira.
_COST_RENVOI_FIXE = float(os.getenv("COST_TWILIO_RENVOI_FIXE_PER_MIN", "0.0187"))
_COST_RENVOI_MOBILE = float(os.getenv("COST_TWILIO_RENVOI_MOBILE_PER_MIN", "0.0404"))

POSTES = ("telephonie", "transcription", "comprehension", "voix")


# Ce que Twilio met dans `From` quand l'appelant masque son numéro : l'orthographe au
# clavier de ANONYMOUS, RESTRICTED, BLOCKED, UNKNOWN et UNAVAILABLE. Pris pour de vrais
# numéros, ils faisaient de TOUS les appels masqués un seul et même client : chacun
# retrouvait, modifiait ou annulait les réservations des autres (find_reservation).
_NUMEROS_MASQUES = {"+266696687", "+7378742833", "+2562533", "+8656696", "+86282452253"}
_E164 = re.compile(r"\+[1-9]\d{6,14}")


def numero_appelant(brut: Optional[str]) -> Optional[str]:
    """Le numéro de l'appelant s'il identifie vraiment quelqu'un, sinon None.

    None est la valeur que tout le chemin d'appel comprend déjà comme « appel masqué » :
    réservation sans numéro, pas de recherche par numéro, message sans rappel possible.
    Ce qui n'est pas un numéro E.164 (« anonymous » en SIP, chaîne vide) l'est aussi."""
    numero = (brut or "").strip()
    if not _E164.fullmatch(numero) or numero in _NUMEROS_MASQUES:
        return None
    return numero


# Les appels du banc d'essai (scripts/banc_conversation.py) : pas de Twilio derrière.
PREFIXE_BANC = "CABANC"


def _activite_secondes(journal: Optional[dict]) -> Optional[float]:
    """Jusqu'où l'appel a réellement vécu : le dernier événement du journal de bord."""
    instants = [e.get("t_ms") for e in ((journal or {}).get("evenements") or [])
                if isinstance(e, dict) and isinstance(e.get("t_ms"), (int, float))]
    return max(instants) / 1000.0 if instants else None


def couts_appel(duration_seconds: float, journal: Optional[dict] = None,
                banc: bool = False) -> dict:
    """Les quatre postes d'un appel, en dollars, et la voix qui l'a servi.

    `journal` : le journal de bord (voice/journal.py), qui compte ce que l'appel a
    consommé — caractères envoyés à la voix, jetons envoyés au modèle. Sans lui (appel
    coupé avant la fin, test), le modèle est chiffré au forfait et la voix à zéro.

    `banc` : un appel du banc d'essai ne passe pas par Twilio, et sa connexion peut
    rester ouverte des heures après la conversation (appels 82 à 87 du 10/09/2026 :
    sept heures au compteur pour 72 à 88 s de conversation, 80 $ fictifs sur 97). On
    le chiffre sur sa durée ACTIVE, lue au journal. Un vrai appel, lui, se chiffre sur
    sa durée : c'est celle que Twilio facture."""
    secondes = max(0.0, float(duration_seconds or 0))
    if banc:
        activite = _activite_secondes(journal)
        if activite is not None:
            secondes = min(secondes, activite + 5.0)
    minutes = secondes / 60.0
    conso = (journal or {}).get("consommation") or {}
    fournisseur = ((journal or {}).get("voix") or {}).get("fournisseur") or "voxtral"
    if fournisseur == "gpt-live":
        # Un seul service écoute, parle et transcrit : il est facturé à la durée de la
        # session (celle qu'il annonce, sinon celle de l'appel). Les jetons sont ceux du
        # cerveau : le nôtre, aux tarifs d'OpenRouter, ou un modèle hébergé par OpenAI.
        session = conso.get("secondes_voix")
        session = float(session) if isinstance(session, (int, float)) else secondes
        entree = int(conso.get("jetons_entree") or 0)
        cache = min(int(conso.get("jetons_cache") or 0), entree)
        if ((journal or {}).get("voix") or {}).get("cerveau") != "openai":
            tarifs = (_COST_LLM_ENTREE, _COST_LLM_CACHE, _COST_LLM_SORTIE)
        else:
            tarifs = (_COST_GPT_LIVE_ENTREE, _COST_GPT_LIVE_CACHE, _COST_GPT_LIVE_SORTIE)
        return {
            "telephonie": 0.0 if banc else round(math.ceil(secondes / 60.0) * _COST_TWILIO_PER_MIN, 6),
            "transcription": 0.0,
            "comprehension": round((entree - cache) * tarifs[0] + cache * tarifs[1]
                                   + int(conso.get("jetons_sortie") or 0) * tarifs[2], 6),
            "voix": round(session / 60.0 * _COST_GPT_LIVE_PER_MIN, 6),
            "fournisseur": fournisseur,
        }
    if conso.get("generations"):
        entree = int(conso.get("jetons_entree") or 0)
        cache = min(int(conso.get("jetons_cache") or 0), entree)
        comprehension = ((entree - cache) * _COST_LLM_ENTREE + cache * _COST_LLM_CACHE
                         + int(conso.get("jetons_sortie") or 0) * _COST_LLM_SORTIE)
    else:
        comprehension = _COST_LLM_PER_CALL
    if fournisseur == "moshi":
        voix = minutes * _COST_MODAL_PER_MIN
    else:
        voix = int(conso.get("caracteres_voix") or 0) * _COST_VOIX_PAR_CARACTERE
    # Chaque poste arrondi au millionième, et le total = leur somme : la répartition de
    # l'admin retombe exactement sur le coût de l'appel.
    return {
        "telephonie": 0.0 if banc else round(math.ceil(secondes / 60.0) * _COST_TWILIO_PER_MIN, 6),
        "transcription": round(minutes * _COST_DEEPGRAM_PER_MIN, 6),
        "comprehension": round(comprehension, 6),
        "voix": round(voix, 6),
        "fournisseur": fournisseur,
    }


def estimate_call_cost(duration_seconds: float, journal: Optional[dict] = None) -> float:
    couts = couts_appel(duration_seconds, journal)
    return sum(couts[p] for p in POSTES)


def start_call(call_sid: Optional[str], tenant_id: int,
               caller_number: Optional[str] = None, sortant: bool = False) -> Optional[int]:
    """Enregistre le début d'appel et renvoie son identifiant.

    `sortant` : c'est nous qui avons appelé (rappel demandé sur le site, app/rappel.py) ;
    `caller_number` est alors le numéro APPELÉ, et la téléphonie se chiffre au tarif
    sortant.

    ON CONFLICT DO NOTHING : un doublon de webhook ne doit jamais faire échouer l'appel.
    L'identifiant est relu plutôt que pris de `lastrowid`, précisément pour ce cas —
    sur un doublon, `lastrowid` ne désigne aucune insertion.

    Cet identifiant nomme les fichiers d'enregistrement (#88). On n'utilise pas le
    `call_sid` pour ça : il arrive dans le message `start` du websocket, donc de
    l'extérieur, alors qu'un entier de notre base ne peut désigner qu'un de nos fichiers.
    """
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, caller_number, sortant)
               VALUES (?, ?, ?, ?) ON CONFLICT(call_sid) DO NOTHING""",
            (call_sid, tenant_id, caller_number, 1 if sortant else None),
        )
        row = conn.execute(
            "SELECT id FROM calls WHERE call_sid = ?", (call_sid,)
        ).fetchone()
    return row[0] if row else None


# Plafond du journal de bord stocké. Un appel pathologique — une boucle d'interruptions
# sur quarante minutes — ne doit gonfler ni la base ni les sauvegardes quotidiennes.
_JOURNAL_MAX_OCTETS = 64 * 1024


def _journal_borne(journal: Optional[dict]) -> Optional[str]:
    """Sérialise le journal en restant sous le plafond, en sacrifiant par ordre d'utilité.

    On abandonne d'abord les évènements bruts (le détail image par image), puis on
    tronque les tours. Ce qui survit en dernier, ce sont les compteurs : même sur un
    appel monstrueux, on saura combien de fois elle a été coupée. Et `tronque` est posé
    à chaque fois — un journal amputé qui ne le dirait pas ferait conclure à tort.
    """
    if not journal:
        return None
    charge = json.dumps(journal, ensure_ascii=False, separators=(",", ":"))
    if len(charge.encode("utf-8")) <= _JOURNAL_MAX_OCTETS:
        return charge

    reduit = {**journal, "tronque": True}
    reduit.pop("evenements", None)
    charge = json.dumps(reduit, ensure_ascii=False, separators=(",", ":"))
    while len(charge.encode("utf-8")) > _JOURNAL_MAX_OCTETS and reduit.get("tours"):
        reduit["tours"] = reduit["tours"][: len(reduit["tours"]) // 2]
        charge = json.dumps(reduit, ensure_ascii=False, separators=(",", ":"))
    return charge


def finish_call(
    call_sid: str,
    status: str = "completed",
    transcript: Optional[list[dict]] = None,
    reservation_id: Optional[int | str] = None,
    turn_latencies: Optional[list[int]] = None,
    journal: Optional[dict] = None,
    recording_bytes: Optional[int] = None,
) -> Optional[int]:
    """Clôt l'appel : durée depuis started_at, statut, transcript JSON, coût estimé.

    `reservation_id` : entier = une ligne de notre table `reservations` ; texte =
    l'identifiant d'un carnet externe (resOS), rangé dans `reservation_externe` — la clé
    étrangère refuserait un identifiant qui n'existe pas chez nous.

    `turn_latencies` = les blancs ressentis tour par tour, en millisecondes (cf.
    voice/latency.py). Stockés tels quels : c'est la matière première du diagnostic.

    Renvoie l'identifiant de l'appel clôturé — c'est lui qui permet d'enchaîner sur le
    résumé (`resume.planifier`) sans relire la base — ou None si l'appel est inconnu."""
    externe = None
    if isinstance(reservation_id, str):
        externe, reservation_id = reservation_id, None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, started_at, sortant, caller_number FROM calls WHERE call_sid = ?",
            (call_sid,)
        ).fetchone()
        if row is None:
            return  # start_call a échoué/absent : ne rien inventer
        debut = horloge.lire_utc(row["started_at"])
        duration = (datetime.now(timezone.utc) - debut).total_seconds() if debut else 0.0
        duration = max(0.0, duration)
        # Chiffré au tarif du jour, poste par poste, et figé : la répartition de l'admin
        # est la somme de ces colonnes, pas une seconde estimation.
        couts = couts_appel(duration, journal, banc=call_sid.startswith(PREFIXE_BANC))
        if row["sortant"]:
            # Un appel que NOUS avons passé (rappel du site) : Twilio le facture au tarif
            # sortant, selon que le numéro appelé est un fixe ou un portable.
            couts["telephonie"] = cout_renvoi(duration, row["caller_number"])
        conn.execute(
            """UPDATE calls SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                   duration_seconds = ?,
                   status = CASE WHEN secours_motif IS NOT NULL THEN status ELSE ? END,
                   transcript = ?,
                   reservation_id = ?, reservation_externe = ?, estimated_cost = ?,
                   turn_latencies = ?, journal = ?, recording_bytes = ?,
                   cout_telephonie = ?, cout_transcription = ?, cout_comprehension = ?,
                   cout_voix = ?, voix_fournisseur = ?
               WHERE id = ?""",
            (
                duration,
                status,
                json.dumps(transcript, ensure_ascii=False) if transcript else None,
                reservation_id,
                externe,
                sum(couts[p] for p in POSTES),
                json.dumps(turn_latencies) if turn_latencies else None,
                _journal_borne(journal),
                recording_bytes,
                couts["telephonie"], couts["transcription"], couts["comprehension"],
                couts["voix"], couts["fournisseur"],
                row["id"],
            ),
        )
    return row["id"]


# ---- Secours (ASSISTANTE-118) : l'assistante en panne passe la main -------------------
# Ce que devient un appel qu'elle n'a pas pu servir. `unserved` est l'état de départ,
# posé dès que le secours s'ouvre : il ne reste que si le client a raccroché avant que
# le restaurant décroche ou sans laisser de message.
SECOURS_NON_SERVI, SECOURS_RENVOYE, SECOURS_MESSAGE = "unserved", "forwarded", "voicemail"
STATUTS_SECOURS = (SECOURS_NON_SERVI, SECOURS_RENVOYE, SECOURS_MESSAGE)


def cout_renvoi(secondes: float, numero: Optional[str]) -> float:
    """La communication vers le restaurant, à la minute entamée. Un numéro français en
    06 ou 07 est un portable ; un numéro étranger est chiffré au tarif du portable, la
    borne haute des deux."""
    if not secondes or secondes <= 0:
        return 0.0
    numero = numero or ""
    fixe = numero.startswith("+33") and numero[3:4] not in ("6", "7")
    tarif = _COST_RENVOI_FIXE if fixe else _COST_RENVOI_MOBILE
    return round(math.ceil(secondes / 60.0) * tarif, 6)


def ouvrir_secours(call_sid: Optional[str], tenant_id: int, caller_number: Optional[str],
                   motif: str) -> Optional[int]:
    """L'assistante passe la main sur cet appel. Crée sa ligne s'il n'a jamais atteint le
    pipeline (renvoyé dès le décroché), note le motif, et le clôt à l'instant : s'il n'y
    a pas de suite (le client raccroche pendant la sonnerie), il ne reste pas « en
    cours »."""
    if not call_sid:
        return None
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, caller_number) VALUES (?, ?, ?)
               ON CONFLICT(call_sid) DO NOTHING""",
            (call_sid, tenant_id, caller_number),
        )
        row = conn.execute("SELECT id, started_at, ended_at FROM calls WHERE call_sid = ?",
                           (call_sid,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE calls SET secours_motif = COALESCE(secours_motif, ?), status = ? "
                     "WHERE id = ?", (motif, SECOURS_NON_SERVI, row["id"]))
    conclure_secours(call_sid, SECOURS_NON_SERVI)
    return row["id"]


def conclure_secours(call_sid: Optional[str], statut: str, *,
                     secondes_renvoi: Optional[int] = None,
                     numero: Optional[str] = None) -> Optional[int]:
    """Ce qu'est devenu l'appel passé en secours, sa durée à cet instant, et son coût :
    l'appel reçu court toujours pendant le renvoi, et le renvoi s'y ajoute. Les autres
    postes (transcription, modèle, voix) restent ceux que le pipeline a chiffrés."""
    if not call_sid or statut not in STATUTS_SECOURS:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT id, started_at, secours_secondes, cout_transcription,
                      cout_comprehension, cout_voix FROM calls WHERE call_sid = ?""",
            (call_sid,)).fetchone()
        if row is None:
            return None
        debut = horloge.lire_utc(row["started_at"])
        duree = max(0.0, (datetime.now(timezone.utc) - debut).total_seconds()) if debut else 0.0
        renvoi = secondes_renvoi if secondes_renvoi is not None else row["secours_secondes"]
        telephonie = (round(math.ceil(duree / 60.0) * _COST_TWILIO_PER_MIN, 6)
                      + cout_renvoi(renvoi or 0, numero))
        autres = [row["cout_transcription"] or 0.0, row["cout_comprehension"] or 0.0,
                  row["cout_voix"] or 0.0]
        conn.execute(
            """UPDATE calls SET status = ?, secours_secondes = ?,
                   ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), duration_seconds = ?,
                   cout_telephonie = ?, cout_transcription = ?, cout_comprehension = ?,
                   cout_voix = ?, estimated_cost = ?
               WHERE id = ?""",
            (statut, renvoi, duree, telephonie, *autres, telephonie + sum(autres), row["id"]),
        )
    return row["id"]


def par_sid(call_sid: Optional[str]) -> Optional[dict]:
    """L'appel désigné par l'identifiant de Twilio (ses rappels ne connaissent que lui)."""
    if not call_sid:
        return None
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, tenant_id FROM calls WHERE call_sid = ?",
                           (call_sid,)).fetchone()
    return dict(row) if row else None


def secours_recents(heures: int = 24) -> list[dict]:
    """Les appels passés en secours depuis `heures` heures : la matière du contrôle de
    supervision. Un seul suffit à dire que des clients n'ont pas eu l'assistante. Les
    essais lancés depuis l'admin n'en sont pas : personne n'est tombé en panne."""
    depuis = horloge.utc_iso(datetime.now(timezone.utc) - timedelta(hours=heures))
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT id, tenant_id, started_at, status, secours_motif FROM calls
               WHERE secours_motif IS NOT NULL AND secours_motif != 'essai'
                 AND started_at >= ?
               ORDER BY started_at DESC""", (depuis,)).fetchall()
    return [dict(r) for r in rows]


# Issues filtrables depuis l'admin. Elles décrivent l'état RÉEL des colonnes ; il n'y
# a pas de catégorie « à rappeler », rien ne la matérialise en base.
# Une réservation prise pendant l'appel : dans notre carnet (reservation_id, clé
# étrangère) ou dans un carnet externe comme resOS (reservation_externe, son identifiant
# à lui — il n'a pas de ligne chez nous). Une seule définition, pour que le filtre de
# l'admin et les statistiques comptent la même chose.
A_RESERVE = "(reservation_id IS NOT NULL OR reservation_externe IS NOT NULL)"

OUTCOME_FILTERS = {
    "reservation": A_RESERVE,
    "failed": "status = 'failed'",
    "info": f"NOT {A_RESERVE} AND status = 'completed'",
    # Un rappel a été promis à l'appelant. Sans ce filtre, ces appels se noyaient dans
    # « info », au même titre qu'une question d'horaires — et le restaurateur n'avait
    # aucun moyen de savoir qu'on s'était engagé en son nom.
    "message": "EXISTS (SELECT 1 FROM messages m WHERE m.call_id = calls.id)",
    # L'assistante n'a pas pu servir l'appel : renvoyé au restaurant, message vocal, ou
    # client reparti sans personne (ASSISTANTE-118).
    "secours": "secours_motif IS NOT NULL",
}


def list_calls(tenant_id: Optional[int] = None, limit: int = 50, offset: int = 0,
               outcome: Optional[str] = None) -> list[dict]:
    query = "SELECT * FROM calls"
    clauses: list[str] = []
    params: list = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if outcome in OUTCOME_FILTERS:
        clauses.append(OUTCOME_FILTERS[outcome])
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_calls(tenant_id: Optional[int] = None) -> int:
    with db.get_conn() as conn:
        if tenant_id is None:
            return conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM calls WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()[0]


def get_call(call_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return dict(row) if row else None


def stats_daily(tenant_id: Optional[int] = None, days: int = 30) -> list[dict]:
    """Agrégats par jour (appels, appels avec résa, coût) + résas/jour, sur `days` jours.
    Renvoie une ligne par jour AYANT de l'activité (les jours vides sont comblés par l'UI)."""
    # Le JOUR est celui du restaurant, pas celui d'UTC : un appel à 00 h 30 à Paris le 1er
    # appartient au 1er. SQLite ne connaît pas le fuseau, donc le regroupement se fait ici,
    # sur des lignes déjà bornées (quelques centaines par mois au plus).
    borne = horloge.il_y_a_jours(days)
    where_calls, where_resas = "WHERE started_at >= ?", "WHERE created_at >= ?"
    params_calls: list = [borne]
    params_resas: list = [borne]
    if tenant_id is not None:
        where_calls += " AND tenant_id = ?"
        where_resas += " AND tenant_id = ?"
        params_calls.append(tenant_id)
        params_resas.append(tenant_id)
    with db.get_conn() as conn:
        calls_rows = conn.execute(
            f"SELECT started_at, {A_RESERVE} AS a_reserve, estimated_cost FROM calls {where_calls}",
            params_calls,
        ).fetchall()
        resa_rows = conn.execute(
            f"SELECT created_at FROM reservations {where_resas}", params_resas,
        ).fetchall()
    merged: dict[str, dict] = {}

    def _entree(jour: str) -> dict:
        return merged.setdefault(jour, {"day": jour, "n_calls": 0, "n_with_reservation": 0,
                                        "total_cost": 0.0, "n_reservations": 0})

    for row in calls_rows:
        jour = jour_local(row["started_at"])
        if jour is None:
            continue
        entree = _entree(jour)
        entree["n_calls"] += 1
        entree["n_with_reservation"] += 1 if row["a_reserve"] else 0
        entree["total_cost"] += row["estimated_cost"] or 0.0
    for row in resa_rows:
        jour = jour_local(row["created_at"])
        if jour is not None:
            _entree(jour)["n_reservations"] += 1
    return sorted(merged.values(), key=lambda e: e["day"])


def jour_local(brut) -> Optional[str]:
    """La date (AAAA-MM-JJ) au fuseau du restaurant d'un horodatage SQLite (UTC)."""
    instant = horloge.lire_utc(brut)
    return instant.astimezone(horloge.FUSEAU).date().isoformat() if instant else None


def _window(days: int, offset_days: int) -> tuple[str, list]:
    """Fragment SQL d'une fenêtre glissante de `days` jours, décalée de `offset_days`
    vers le passé. Les bornes sont des MINUITS du restaurant (horloge.il_y_a_jours),
    convertis en UTC : « les 30 derniers jours » commence à minuit à Paris, pas à minuit
    UTC. Sans décalage il n'y a PAS de borne haute : sinon la journée en cours tomberait
    hors de la fenêtre."""
    clause = "{col} >= ?"
    params: list = [horloge.il_y_a_jours(int(days) + int(offset_days))]
    if offset_days > 0:
        clause += " AND {col} < ?"
        params.append(horloge.il_y_a_jours(int(offset_days)))
    return clause, params


def totals(tenant_id: Optional[int] = None, days: int = 30,
           offset_days: int = 0) -> dict:
    """Totaux d'une fenêtre glissante : appels, captation, échecs, durée, coût, résas.

    `offset_days` recule la fenêtre : appelée deux fois (0 puis `days`), elle donne
    la période courante ET la précédente, donc une évolution MESURÉE — jamais un
    pourcentage décoratif.
    """
    clause, params = _window(days, offset_days)
    calls_where = clause.format(col="started_at")
    resas_where = clause.format(col="created_at")
    calls_params = list(params)
    resas_params = list(params)
    if tenant_id is not None:
        calls_where += " AND tenant_id = ?"
        resas_where += " AND tenant_id = ?"
        calls_params.append(tenant_id)
        resas_params.append(tenant_id)
    with db.get_conn() as conn:
        c = conn.execute(
            f"""SELECT COUNT(*) AS n_calls,
                       COALESCE(SUM(CASE WHEN {A_RESERVE} THEN 1 ELSE 0 END), 0)
                           AS n_with_reservation,
                       COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS n_failed,
                       COALESCE(SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END), 0) AS n_unfinished,
                       COALESCE(SUM(duration_seconds), 0) AS total_duration,
                       COALESCE(SUM(estimated_cost), 0) AS total_cost
                FROM calls WHERE {calls_where}""",
            calls_params,
        ).fetchone()
        r = conn.execute(
            f"""SELECT COUNT(*) AS n_reservations,
                       COALESCE(SUM(party_size), 0) AS n_covers
                FROM reservations WHERE {resas_where}""",
            resas_params,
        ).fetchone()
    n_calls = c["n_calls"]
    return {
        "n_calls": n_calls,
        "n_with_reservation": c["n_with_reservation"],
        "n_failed": c["n_failed"],
        "n_unfinished": c["n_unfinished"],
        "total_duration": c["total_duration"],
        "total_cost": c["total_cost"],
        "n_reservations": r["n_reservations"],
        "n_covers": r["n_covers"],
        "capture_rate": round(100 * c["n_with_reservation"] / n_calls) if n_calls else 0,
        "avg_cost": (c["total_cost"] / n_calls) if n_calls else 0.0,
        "avg_duration": (c["total_duration"] / n_calls) if n_calls else 0.0,
    }


def stats_by_tenant(days: int = 30) -> dict[int, dict]:
    """Agrégats par établissement sur `days` jours, indexés par tenant_id.

    Un seul GROUP BY pour les appels + un pour les réservations : la vue du parc
    affiche N établissements sans faire N requêtes."""
    clause, params = _window(days, 0)
    with db.get_conn() as conn:
        call_rows = conn.execute(
            f"""SELECT tenant_id, COUNT(*) AS n_calls,
                       COALESCE(SUM(CASE WHEN {A_RESERVE} THEN 1 ELSE 0 END), 0)
                           AS n_with_reservation,
                       COALESCE(SUM(estimated_cost), 0) AS total_cost
                FROM calls WHERE {clause.format(col='started_at')} GROUP BY tenant_id""",
            params,
        ).fetchall()
        resa_rows = conn.execute(
            f"""SELECT tenant_id, COUNT(*) AS n_reservations
                FROM reservations WHERE {clause.format(col='created_at')} GROUP BY tenant_id""",
            params,
        ).fetchall()
    stats: dict[int, dict] = {}

    def _entry(tenant_id: int) -> dict:
        return stats.setdefault(tenant_id, {
            "n_calls": 0, "n_with_reservation": 0, "total_cost": 0.0,
            "n_reservations": 0, "capture_rate": 0,
        })

    for row in call_rows:
        entry = _entry(row["tenant_id"])
        entry["n_calls"] = row["n_calls"]
        entry["n_with_reservation"] = row["n_with_reservation"]
        entry["total_cost"] = row["total_cost"]
        entry["capture_rate"] = (
            round(100 * row["n_with_reservation"] / row["n_calls"]) if row["n_calls"] else 0
        )
    for row in resa_rows:
        _entry(row["tenant_id"])["n_reservations"] = row["n_reservations"]
    return stats


def latency_stats(tenant_id: Optional[int] = None, days: int = 30) -> Optional[dict]:
    """Blancs ressentis par les appelants sur la période : médiane et p90, en ms.

    Renvoie None tant qu'aucun appel n'a été mesuré — les appels antérieurs à la
    migration v4 n'ont rien enregistré, et une latence inventée serait pire que pas
    de latence du tout."""
    clause, params = _window(days, 0)
    where = clause.format(col="started_at") + " AND turn_latencies IS NOT NULL"
    if tenant_id is not None:
        where += " AND tenant_id = ?"
        params = [*params, tenant_id]
    with db.get_conn() as conn:
        rows = conn.execute(f"SELECT turn_latencies FROM calls WHERE {where}", params).fetchall()
    mesures: list[int] = []
    for row in rows:
        try:
            valeurs = json.loads(row["turn_latencies"])
        except (TypeError, ValueError):
            continue
        mesures += [int(v) for v in valeurs if isinstance(v, (int, float))]
    if not mesures:
        return None
    mesures.sort()
    milieu = len(mesures) // 2
    mediane = (mesures[milieu] if len(mesures) % 2
               else (mesures[milieu - 1] + mesures[milieu]) // 2)
    return {
        "median_ms": mediane,
        "p90_ms": mesures[min(len(mesures) - 1, int(0.9 * len(mesures)))],
        "n_turns": len(mesures),
    }


def cost_breakdown(tenant_id: Optional[int] = None, days: int = 30) -> list[dict]:
    """Répartition du coût par poste : la SOMME des postes rangés à la clôture de chaque
    appel (finish_call), sur la même fenêtre que `totals`. Le total des lignes est donc
    exactement le coût affiché ailleurs, sans règle de trois.

    La voix est séparée par fournisseur : l'historique en GPU Moshi et les appels en
    voix Mistral ne se paient pas pareil, et c'est cette comparaison qui a décidé la
    bascule du 28/09/2026. GPT-Live a sa ligne aussi : jusqu'au 05/10/2026 ses minutes
    étaient rangées sous « Voix Mistral », qui n'y était pour rien. Une voix qui n'a
    servi aucun appel de la fenêtre n'est pas affichée."""
    clause, params = _window(days, 0)
    where = clause.format(col="started_at")
    if tenant_id is not None:
        where += " AND tenant_id = ?"
        params.append(tenant_id)
    with db.get_conn() as conn:
        r = conn.execute(
            f"""SELECT COALESCE(SUM(cout_telephonie), 0) AS telephonie,
                       COALESCE(SUM(cout_transcription), 0) AS transcription,
                       COALESCE(SUM(cout_comprehension), 0) AS comprehension,
                       COALESCE(SUM(CASE WHEN voix_fournisseur = 'moshi' THEN cout_voix END), 0)
                           AS voix_moshi,
                       COALESCE(SUM(CASE WHEN voix_fournisseur = 'gpt-live' THEN cout_voix END), 0)
                           AS voix_gpt_live,
                       COALESCE(SUM(CASE WHEN voix_fournisseur IS NOT 'moshi'
                                          AND voix_fournisseur IS NOT 'gpt-live'
                                         THEN cout_voix END), 0) AS voix_mistral
                FROM calls WHERE {where}""",
            params,
        ).fetchone()
    rows = [
        ("Téléphonie (Twilio)", r["telephonie"]),
        ("Transcription (Deepgram)", r["transcription"]),
        ("Compréhension (Gemini)", r["comprehension"]),
        ("Voix Mistral (Voxtral)", r["voix_mistral"]),
    ]
    if r["voix_gpt_live"]:
        rows.append(("Voix GPT-Live (OpenAI)", r["voix_gpt_live"]))
    if r["voix_moshi"]:
        rows.append(("Voix Moshi (GPU, historique)", r["voix_moshi"]))
    total = sum(montant for _, montant in rows)
    return [{"label": label, "amount": montant,
             "share": round(100 * montant / total) if total else 0}
            for label, montant in rows]


# Le moteur d'un appel se lit à la voix qui l'a servi : `voix_fournisseur`, rangé à sa
# clôture. Les appels en GPU Moshi (avant le 28/09/2026) ne sont d'aucun des deux.
MOTEURS_PAR_VOIX = {"gpt-live": "gpt_live", "voxtral": "classique"}


def par_moteur(tenant_id: Optional[int] = None, days: int = 30) -> dict[str, dict]:
    """Ce que chaque moteur a RÉELLEMENT coûté sur la fenêtre : appels, minutes, coût, et
    le coût à la minute — la somme des postes rangés à la clôture de chaque appel, pas
    un tarif recopié. Un moteur qui n'a servi aucun appel est absent du résultat.

    `moteur_par_minute` laisse la téléphonie de côté : Twilio facture la même chose
    quel que soit le moteur, mais deux à quatre fois plus pour un rappel passé depuis le
    site, et c'est l'établissement de démonstration qui les reçoit tous. C'est donc ce
    chiffre-là qui compare les deux moteurs.

    Les appels du banc d'essai n'y sont pas : leur connexion peut rester ouverte des
    heures après la conversation (voir `couts_appel`)."""
    clause, params = _window(days, 0)
    where = clause.format(col="started_at")
    if tenant_id is not None:
        where += " AND tenant_id = ?"
        params.append(tenant_id)
    with db.get_conn() as conn:
        lignes = conn.execute(
            f"""SELECT voix_fournisseur AS voix, COUNT(*) AS n,
                       COALESCE(SUM(duration_seconds), 0) AS secondes,
                       COALESCE(SUM(cout_telephonie), 0) AS telephonie,
                       COALESCE(SUM(cout_transcription), 0) AS transcription,
                       COALESCE(SUM(cout_comprehension), 0) AS comprehension,
                       COALESCE(SUM(cout_voix), 0) AS cout_voix
                FROM calls
                WHERE {where} AND ended_at IS NOT NULL AND duration_seconds > 0
                  AND voix_fournisseur IN ('gpt-live', 'voxtral')
                  AND COALESCE(call_sid, '') NOT LIKE '{PREFIXE_BANC}%'
                GROUP BY voix_fournisseur""",
            params,
        ).fetchall()
    resultat = {}
    for r in lignes:
        minutes = r["secondes"] / 60.0
        hors_telephonie = r["transcription"] + r["comprehension"] + r["cout_voix"]
        total = hors_telephonie + r["telephonie"]
        resultat[MOTEURS_PAR_VOIX[r["voix"]]] = {
            "n_calls": r["n"], "minutes": minutes, "total": total,
            "par_minute": total / minutes, "moteur_par_minute": hors_telephonie / minutes,
        }
    return resultat


def tarifs_gpt_live(cerveau_openai: bool = False) -> dict:
    """Les tarifs avec lesquels un appel GPT-Live est chiffré (`couts_appel`), pour que
    l'admin explique son calcul avec les chiffres réellement appliqués. Le cerveau est
    le nôtre (tarifs d'OpenRouter), sauf si `GPT_LIVE_MODELE` le confie à OpenAI."""
    entree, cache, sortie = ((_COST_GPT_LIVE_ENTREE, _COST_GPT_LIVE_CACHE, _COST_GPT_LIVE_SORTIE)
                             if cerveau_openai else
                             (_COST_LLM_ENTREE, _COST_LLM_CACHE, _COST_LLM_SORTIE))
    return {"voix_par_minute": _COST_GPT_LIVE_PER_MIN,
            "telephonie_par_minute": _COST_TWILIO_PER_MIN,
            "entree_par_million": round(entree * 1e6, 4),
            "cache_par_million": round(cache * 1e6, 4),
            "sortie_par_million": round(sortie * 1e6, 4)}


def enregistrements_stats(tenant_id: Optional[int] = None, days: int = 7) -> dict:
    """Combien d'appels clos ont réellement produit un enregistrement (#88).

    Sert au contrôle de supervision. Le cas qu'on veut voir est « 0 sur 12 » : la
    fonctionnalité activée mais cassée en silence — dossier non inscriptible, disque
    plein, régression. Un simple « actif : oui » ne le montrerait jamais.
    """
    clause, params = _window(days, 0)
    where = clause.format(col="started_at") + " AND ended_at IS NOT NULL"
    if tenant_id is not None:
        where += " AND tenant_id = ?"
        params = [*params, tenant_id]
    with db.get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS clos,
                       COALESCE(SUM(CASE WHEN recording_bytes IS NOT NULL THEN 1 ELSE 0 END), 0)
                           AS enregistres,
                       COALESCE(SUM(recording_bytes), 0) AS octets
                FROM calls WHERE {where}""",
            params,
        ).fetchone()
    return {"clos": row["clos"], "enregistres": row["enregistres"], "octets": row["octets"]}


def appels_avec_enregistrement(avant_jours: int) -> list[dict]:
    """Appels dont l'enregistrement a dépassé sa durée de conservation (#88).

    Rend `id` et `tenant_id`, de quoi reconstruire les chemins côté serveur. La purge est
    ainsi PILOTÉE PAR LA BASE et non par un balayage de répertoire : elle n'efface que
    des fichiers qu'on sait nôtres, et jamais ce qu'un tiers aurait déposé là.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT id, tenant_id FROM calls
               WHERE recording_bytes IS NOT NULL AND started_at < ?""",
            (horloge.il_y_a(int(avant_jours)),),
        ).fetchall()
    return [dict(r) for r in rows]


def appel_d_une_reservation(tenant_id: int, reservation_id, *, externe: bool) -> Optional[dict]:
    """L'appel pendant lequel l'assistante a pris cette réservation (ASSISTANTE-116) :
    notre carnet la désigne par sa clé (`reservation_id`), resOS par son identifiant
    (`reservation_externe`). None pour une réservation saisie à la main."""
    colonne = "reservation_externe" if externe else "reservation_id"
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM calls WHERE tenant_id = ? AND {colonne} = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (tenant_id, str(reservation_id) if externe else reservation_id),
        ).fetchone()
    return dict(row) if row else None


def du_numero(tenant_id: int, numero: Optional[str], limite: int = 10) -> list[dict]:
    """Les derniers appels de ce numéro chez cet établissement — la fiche d'un client."""
    if not numero:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE tenant_id = ? AND caller_number = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (tenant_id, numero, limite),
        ).fetchall()
    return [dict(r) for r in rows]


def appels_d_un_numero(numero: str) -> list[dict]:
    """Appels rattachés à ce numéro, pour le droit à l'effacement (#88).

    À appeler AVANT d'anonymiser `caller_number` : après, le lien est rompu et les
    fichiers deviennent des orphelins que plus rien ne désigne.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, tenant_id FROM calls WHERE caller_number = ?", (numero,)
        ).fetchall()
    return [dict(r) for r in rows]


def oublier_enregistrements(call_ids: list[int]) -> None:
    """Remet `recording_bytes` à NULL : les fichiers ont été supprimés, la base doit
    cesser de prétendre qu'ils existent."""
    if not call_ids:
        return
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE calls SET recording_bytes = NULL WHERE id IN "
            f"({','.join('?' * len(call_ids))})",
            [int(i) for i in call_ids],
        )
