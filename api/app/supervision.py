"""Supervision : l'état RÉEL de la pile vocale, calculé à un seul endroit.

Pourquoi ce module existe. Jusqu'ici une panne se découvrait **en écoutant un appel** :
c'est comme ça qu'a été trouvée la panne des appels muets du 30/07/2026, où le pipeline
répondait `HTTP 200` à Twilio, marquait l'appel `completed`, et ne disait pas un mot.
`/health` répondait « ok » pendant toute la durée de la panne, parce qu'il ne regardait
rien — il renvoyait une constante.

Trois principes tenus ici :

1. **Une seule source.** `etat()` sert à la fois la page « Santé & coûts » et la sonde
   `/supervision` interrogée de l'extérieur. Un tableau de bord qui pourrait afficher
   « tout va bien » pendant que l'alerte crie serait pire que pas de tableau de bord.

2. **Aucun appel réseau.** La sonde est interrogeable en boucle : si elle réveillait le
   GPU Modal, superviser coûterait plus cher que servir. Ce qui exige du réseau (les
   alertes Twilio) est rafraîchi hors sonde, à un rythme choisi, et seulement LU ici.

3. **Pas de mesure ≠ tout va bien.** Zéro appel sur la fenêtre ne donne pas 0 % d'échec :
   ça donne « pas de mesure ». Le projet s'interdit les chiffres décoratifs ; s'interdire
   les feux verts décoratifs est la même règle appliquée à la supervision.
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import calls, db, tenants

# --- Niveaux -----------------------------------------------------------------
# Ordonnés : `pire()` prend le maximum. « attention » = dégradé mais le standard
# répond ; « panne » = un appelant qui téléphone maintenant n'est pas servi
# correctement. Seul « panne » réveille quelqu'un (HTTP 503 → alerte).
OK = "ok"
ATTENTION = "attention"
PANNE = "panne"
_RANG = {OK: 0, ATTENTION: 1, PANNE: 2}


def pire(*niveaux: str) -> str:
    return max(niveaux, key=lambda n: _RANG.get(n, 0)) if niveaux else OK


@dataclass
class Controle:
    """Un fait vérifiable, pas une opinion. `mesure` est la valeur brute qui a servi
    à décider : elle est affichée telle quelle, pour qu'on puisse contester le seuil
    sans avoir à refaire le calcul."""
    cle: str
    titre: str
    niveau: str
    resume: str
    detail: str = ""
    mesure: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "cle": self.cle, "titre": self.titre, "niveau": self.niveau,
            "resume": self.resume, "detail": self.detail, "mesure": self.mesure,
        }


# --- Seuils ------------------------------------------------------------------
# Tous surchargeables : un seuil qui ne peut pas bouger finit par être ignoré.
def _entier(nom: str, defaut: int) -> int:
    try:
        return int(os.getenv(nom, "").strip() or defaut)
    except ValueError:
        return defaut


# 7 jours et non 30 : une alerte doit parler du présent. Un incident réglé il y a
# trois semaines n'a pas à maintenir le voyant au rouge — il est dans l'historique,
# pas dans la supervision.
def fenetre_jours() -> int:
    return _entier("SUPERVISION_FENETRE_JOURS", 7)


# Un appel « muet » : l'assistante n'a pas produit UN tour, alors que l'appelant est
# resté en ligne. En dessous de ce seuil c'est un raccroché immédiat, pas une panne.
def muet_secondes() -> int:
    return _entier("SUPERVISION_MUET_SECONDES", 15)


# Un appel sans `ended_at` passé ce délai n'est pas « en cours » : le worker est mort
# avec lui. Confortablement au-dessus de la durée d'un appel réel (~1 à 3 min).
def inacheve_minutes() -> int:
    return _entier("SUPERVISION_INACHEVE_MINUTES", 30)


def latence_seuils_ms() -> tuple[int, int]:
    """(attention, panne). Le blanc médian mesuré en production est de 1,16 s ;
    au-delà de 2,5 s l'appelant dit « allô ? », au-delà de 4 s il raccroche."""
    return (_entier("SUPERVISION_LATENCE_ATTENTION_MS", 2500),
            _entier("SUPERVISION_LATENCE_PANNE_MS", 4000))


def sauvegarde_seuils_heures() -> tuple[int, int]:
    """(attention, panne). Le cron tourne à 04h00 : 36 h laisse passer un décalage
    d'exécution sans crier, 72 h veut dire que deux nuits ont été manquées."""
    return (_entier("SUPERVISION_SAUVEGARDE_ATTENTION_H", 36),
            _entier("SUPERVISION_SAUVEGARDE_PANNE_H", 72))


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _lire_horodatage(brut: Optional[str]) -> Optional[datetime]:
    """Les dates en base sont écrites par SQLite au format `%Y-%m-%dT%H:%M:%SZ` (UTC).
    Tolérant : une date illisible vaut « inconnue », jamais une exception dans la sonde."""
    if not brut:
        return None
    texte = str(brut).strip().replace(" ", "T")
    if texte.endswith("Z"):
        texte = texte[:-1] + "+00:00"
    try:
        date = datetime.fromisoformat(texte)
    except ValueError:
        return None
    return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


# --- Mémo de supervision -----------------------------------------------------
# Table clé/valeur (migration v6) : sert d'ardoise aux contrôles qui ne peuvent pas
# être calculés dans la sonde elle-même (l'état Twilio, rafraîchi en tâche de fond).
# Elle sert AUSSI de test d'écriture réel : voir `_controle_base`.

def noter(cle: str, valeur: dict) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO supervision (cle, valeur, maj_le)
               VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur,
                                              maj_le = excluded.maj_le""",
            (cle, json.dumps(valeur, ensure_ascii=False)),
        )


def relire(cle: str) -> Optional[tuple[dict, Optional[datetime]]]:
    """(valeur, date de mise à jour) ou None si rien n'a jamais été noté."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT valeur, maj_le FROM supervision WHERE cle = ?", (cle,)
        ).fetchone()
    if row is None:
        return None
    try:
        valeur = json.loads(row["valeur"])
    except (TypeError, ValueError):
        return None
    return valeur, _lire_horodatage(row["maj_le"])


# --- Contrôles ---------------------------------------------------------------

def _controle_base() -> Controle:
    """La base répond ET accepte une écriture.

    Lire ne suffit pas : un disque plein ou un volume remonté en lecture seule laisse
    les `SELECT` passer et fait échouer la seule chose qui compte — enregistrer une
    réservation. On écrit donc vraiment, dans la table de supervision, faite pour ça.
    """
    try:
        noter("sonde", {"vivant": True})
        with db.get_conn() as conn:
            tenants_n = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    except Exception as exc:
        return Controle(
            "base", "Base de données", PANNE,
            "Écriture impossible.",
            f"La base ne peut pas être écrite ({type(exc).__name__}). Une réservation "
            "prise au téléphone serait perdue. Vérifier l'espace disque et le volume "
            "`api_data`.",
        )
    return Controle(
        "base", "Base de données", OK,
        f"Lecture et écriture confirmées · {tenants_n} établissement(s).",
        mesure={"tenants": tenants_n},
    )


# Ce dont le chemin d'appel a RÉELLEMENT besoin, selon la configuration en vigueur.
# Chaque entrée vient d'une panne possible et silencieuse : la variable manque, le
# service démarre quand même, et l'appelant tombe sur un blanc.
def _configuration_requise() -> list[tuple[str, str]]:
    mode = os.getenv("VOICE_MODE", "gather").strip().lower()
    requis = [("OPENROUTER_API_KEY", "sans clé LLM, l'assistante ne comprend rien")]
    if mode != "stream":
        return requis
    requis.append(("PUBLIC_WS_URL",
                   "Twilio ne saurait pas où brancher le flux audio : l'appel raccroche"))
    if os.getenv("STT_PROVIDER", "deepgram").strip().lower() == "deepgram":
        requis.append(("DEEPGRAM_API_KEY", "sans transcription, l'assistante n'entend rien"))
    if os.getenv("TTS_PROVIDER", "pocket").strip().lower() == "moshi_server":
        requis.append(("MOSHI_TTS_URL", "sans serveur de voix, l'assistante ne parle pas"))
    return requis


def _controle_configuration() -> Controle:
    """Les variables sans lesquelles un appel est muet.

    C'est le contrôle le plus rentable du lot : une clé absente ne lève aucune erreur
    au démarrage, l'app répond 200 partout, et seul l'appelant s'en aperçoit.
    """
    requis = _configuration_requise()
    manquantes = [(nom, pourquoi) for nom, pourquoi in requis if not os.getenv(nom, "").strip()]
    if manquantes:
        return Controle(
            "configuration", "Configuration du chemin d'appel", PANNE,
            f"{len(manquantes)} variable(s) manquante(s) : "
            + ", ".join(nom for nom, _ in manquantes) + ".",
            " · ".join(f"{nom} : {pourquoi}" for nom, pourquoi in manquantes),
            mesure={"manquantes": [nom for nom, _ in manquantes]},
        )
    # Une espace parasite dans PUBLIC_WS_URL (copier-coller depuis un navigateur) suffit
    # à empêcher Twilio de joindre le flux : l'appel raccroche sans un mot. Vécu.
    ws = os.getenv("PUBLIC_WS_URL", "")
    if ws and (ws != "".join(ws.split()) or not ws.startswith("wss://")):
        return Controle(
            "configuration", "Configuration du chemin d'appel", PANNE,
            "PUBLIC_WS_URL est malformée.",
            "Elle contient une espace ou ne commence pas par `wss://`. Twilio ne peut "
            "pas ouvrir le flux média : l'appel raccroche sans un mot.",
            mesure={"public_ws_url_valide": False},
        )
    return Controle(
        "configuration", "Configuration du chemin d'appel", OK,
        f"{len(requis)} variable(s) requise(s), toutes présentes.",
        mesure={"requises": [nom for nom, _ in requis]},
    )


# Nombre d'appels relus au maximum par la sonde. Au trafic actuel (24 appels en 30
# jours) la fenêtre entière tient largement dedans ; la borne existe pour que la sonde
# reste à coût constant le jour où le parc grossit.
_MAX_APPELS = 500


def _appels_fenetre() -> list[dict]:
    depuis = f"-{fenetre_jours()} days"
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT started_at, ended_at, duration_seconds, status, transcript
               FROM calls WHERE started_at >= datetime('now', ?)
               ORDER BY started_at DESC LIMIT ?""",
            (depuis, _MAX_APPELS),
        ).fetchall()
    return [dict(r) for r in rows]


def _a_parle(transcript: Optional[str]) -> bool:
    """L'assistante a-t-elle produit au moins un tour ? Lu depuis le JSON, pas par
    `LIKE` : le format exact de sérialisation n'a pas à devenir un contrat."""
    if not transcript:
        return False
    try:
        tours = json.loads(transcript)
    except (TypeError, ValueError):
        return False
    return any(isinstance(t, dict) and t.get("role") == "assistant" for t in tours)


def _verdict_proportion(anormaux: int, total: int) -> str:
    """Un incident isolé mérite un coup d'œil, une majorité d'appels touchés est une
    panne. Le plancher de 3 appels évite qu'un unique appel de test fasse hurler le
    système sur un parc qui reçoit deux appels par semaine."""
    if not anormaux:
        return OK
    if total >= 3 and anormaux * 2 >= total:
        return PANNE
    return ATTENTION


def _controle_appels_muets(appels: list[dict]) -> Controle:
    """Des appels qui se terminent « normalement » sans que l'assistante ait parlé.

    C'est la signature EXACTE de la panne du 30/07/2026 : `extra_body` mal formé, le
    LLM lève une exception à chaque tour, l'appel est marqué `completed`, Twilio reçoit
    ses 200, et l'appelant écoute le silence. Aucun compteur d'erreur ne bougeait.
    """
    seuil = muet_secondes()
    candidats = [a for a in appels
                 if a["status"] == "completed" and (a["duration_seconds"] or 0) >= seuil]
    muets = [a for a in candidats if not _a_parle(a["transcript"])]
    if not candidats:
        return Controle(
            "appels_muets", "Appels muets", OK,
            f"Aucun appel de plus de {seuil} s sur {fenetre_jours()} jours — pas de mesure.",
            "Ce contrôle ne dira rien tant qu'il n'y a pas d'appels : l'absence "
            "d'anomalie n'est pas une preuve de bon fonctionnement.",
            mesure={"candidats": 0, "muets": 0},
        )
    niveau = _verdict_proportion(len(muets), len(candidats))
    return Controle(
        "appels_muets", "Appels muets", niveau,
        f"{len(muets)} appel(s) sans un mot de l'assistante sur {len(candidats)} "
        f"appel(s) de plus de {seuil} s.",
        "L'appelant est resté en ligne et n'a reçu aucune réponse. Regarder les "
        "journaux du conteneur et le fournisseur LLM." if muets else
        "L'assistante a répondu sur tous les appels de la période.",
        mesure={"candidats": len(candidats), "muets": len(muets)},
    )


def _controle_appels_echoues(appels: list[dict]) -> Controle:
    """Appels que le pipeline a lui-même déclarés en échec (exception remontée)."""
    echoues = [a for a in appels if a["status"] == "failed"]
    if not appels:
        return Controle(
            "appels_echoues", "Appels en échec", OK,
            f"Aucun appel sur {fenetre_jours()} jours — pas de mesure.",
            mesure={"appels": 0, "echoues": 0},
        )
    niveau = _verdict_proportion(len(echoues), len(appels))
    return Controle(
        "appels_echoues", "Appels en échec", niveau,
        f"{len(echoues)} échec(s) sur {len(appels)} appel(s).",
        "Le pipeline vocal a levé une erreur pendant l'appel." if echoues else "",
        mesure={"appels": len(appels), "echoues": len(echoues)},
    )


def _controle_appels_inacheves(appels: list[dict]) -> Controle:
    """Appels ouverts et jamais refermés : `finish_call` n'a pas tourné.

    Ni durée, ni transcription, ni coût — et surtout : le processus qui portait
    l'appel s'est arrêté avec lui (OOM, redémarrage du conteneur en plein appel).
    On ignore les appels très récents, qui sont simplement encore en cours.
    """
    limite = _maintenant() - timedelta(minutes=inacheve_minutes())
    concernes = [a for a in appels
                 if (_lire_horodatage(a["started_at"]) or _maintenant()) < limite]
    inacheves = [a for a in concernes if not a["ended_at"]]
    if not concernes:
        return Controle(
            "appels_inacheves", "Appels jamais clôturés", OK,
            f"Aucun appel terminé depuis plus de {inacheve_minutes()} min — pas de mesure.",
            mesure={"appels": 0, "inacheves": 0},
        )
    niveau = _verdict_proportion(len(inacheves), len(concernes))
    return Controle(
        "appels_inacheves", "Appels jamais clôturés", niveau,
        f"{len(inacheves)} appel(s) non clôturé(s) sur {len(concernes)}.",
        "Le worker s'est arrêté avant la fin de l'appel : ni durée, ni transcription. "
        "Vérifier les redémarrages du conteneur et la mémoire." if inacheves else "",
        mesure={"appels": len(concernes), "inacheves": len(inacheves)},
    )


def _controle_latence() -> Controle:
    """Le blanc ressenti par l'appelant, mesuré tour par tour (migration v4).

    Une dérive de latence ne casse rien : elle rend seulement l'assistante pénible,
    puis inutilisable. C'est typiquement ce qu'on ne voit pas venir sans mesure.
    """
    attention_ms, panne_ms = latence_seuils_ms()
    stats = calls.latency_stats(None, days=fenetre_jours())
    if stats is None:
        return Controle(
            "latence", "Blanc ressenti", OK,
            f"Aucun tour mesuré sur {fenetre_jours()} jours — pas de mesure.",
            mesure={"tours": 0},
        )
    mediane = stats["median_ms"]
    niveau = PANNE if mediane >= panne_ms else ATTENTION if mediane >= attention_ms else OK
    return Controle(
        "latence", "Blanc ressenti", niveau,
        f"Médiane {mediane / 1000:.1f} s (p90 {stats['p90_ms'] / 1000:.1f} s) "
        f"sur {stats['n_turns']} tours.",
        f"Seuils : attention à {attention_ms / 1000:.1f} s, panne à {panne_ms / 1000:.1f} s. "
        "Au-delà, l'appelant relance « allô ? » puis raccroche." if niveau != OK else "",
        mesure={"mediane_ms": mediane, "p90_ms": stats["p90_ms"], "tours": stats["n_turns"]},
    )


def _controle_accueils() -> Controle:
    """Le WAV d'accueil de chaque établissement est-il en cache ?

    Sans lui, le décroché passe par un TTS live — donc par un éventuel démarrage à
    froid du GPU (55 à 70 s mesurées). L'appel n'échoue pas : il commence par un
    très long silence, ce que l'appelant lit comme « ça ne marche pas ».
    """
    # Import local : `voice.greeting` tire la pile audio (numpy, client websocket).
    # La sonde doit rester chargeable même dans un contexte qui n'en a pas besoin.
    from .voice import greeting as greeting_mod

    if not greeting_mod.is_moshi_server():
        return Controle(
            "accueils", "Voix d'accueil", OK,
            "Sans objet : le TTS n'est pas moshi-server.",
            mesure={"applicable": False},
        )
    liste = tenants.list_all()
    manquants = [t.name for t in liste if greeting_mod.cached_greeting_path(t) is None]
    return Controle(
        "accueils", "Voix d'accueil", ATTENTION if manquants else OK,
        f"{len(liste) - len(manquants)} / {len(liste)} accueil(s) pré-rendu(s).",
        "Décroché non instantané pour : " + ", ".join(manquants[:5]) + ". "
        "Le premier appel supportera un démarrage à froid du GPU." if manquants else "",
        mesure={"total": len(liste), "manquants": len(manquants)},
    )


def _controle_sauvegarde() -> Controle:
    """Fraîcheur de la dernière sauvegarde réussie.

    Le cron tourne sur l'hôte, hors du conteneur : l'app ne peut pas voir
    `/opt/backups`. `scripts/backup-db.sh` dépose donc un jeton dans le volume de
    données APRÈS le contrôle d'intégrité — un jeton frais prouve une sauvegarde
    restaurable, pas seulement un cron qui s'est exécuté.
    """
    chemin = os.getenv("SUPERVISION_BACKUP_STAMP", "/app/data/derniere-sauvegarde")
    attention_h, panne_h = sauvegarde_seuils_heures()
    try:
        with open(chemin, "r", encoding="utf-8") as fichier:
            date = _lire_horodatage(fichier.read().strip().splitlines()[0])
    except (OSError, IndexError):
        date = None
    if date is None:
        return Controle(
            "sauvegarde", "Sauvegarde de la base", ATTENTION,
            "Aucune sauvegarde observée.",
            f"Le jeton `{chemin}` est absent ou illisible. Soit le cron ne tourne pas, "
            "soit il tourne avec une version de `backup-db.sh` antérieure au jeton — "
            "dans ce second cas, une seule nuit suffit à le rétablir.",
            mesure={"jeton": False},
        )
    ages_h = (_maintenant() - date).total_seconds() / 3600
    niveau = PANNE if ages_h >= panne_h else ATTENTION if ages_h >= attention_h else OK
    return Controle(
        "sauvegarde", "Sauvegarde de la base", niveau,
        f"Dernière sauvegarde il y a {ages_h:.0f} h.",
        f"Le cron passe à 04h00 ; au-delà de {panne_h} h, deux nuits ont été manquées."
        if niveau != OK else "",
        mesure={"jeton": True, "age_heures": round(ages_h, 1)},
    )


def _controle_twilio() -> Controle:
    """Erreurs remontées par Twilio lui-même (Monitor/Alerts), relues depuis l'ardoise.

    Angle mort que rien d'autre ne couvre : quand Twilio n'arrive PAS à nous joindre —
    webhook injoignable, TwiML invalide, URL de flux média fausse — l'appel meurt avant
    que l'application n'en sache quoi que ce soit. Aucune ligne dans `calls`, aucun
    journal : de l'intérieur, la panne est parfaitement invisible.

    Le rafraîchissement est une tâche de fond (`rafraichir_twilio`), pas un appel réseau
    dans la sonde : la sonde doit rester gratuite et instantanée.
    """
    memo = relire("twilio")
    if memo is None:
        # Aucune relève : soit l'application vient de démarrer (la première a lieu
        # dans la seconde), soit SUPERVISION_TWILIO_SECONDES=0 l'a désactivée. Dans
        # les deux cas ce contrôle ne sait rien — et le dit, plutôt que de compter
        # « 0 erreur » comme une bonne nouvelle.
        return Controle(
            "twilio", "Alertes Twilio", OK,
            "Pas encore relevé — ce contrôle ne dit rien pour l'instant.",
            "La première relève a lieu au démarrage de l'application. Si ce message "
            "persiste, SUPERVISION_TWILIO_SECONDES vaut 0 : les erreurs de webhook ne "
            "sont alors surveillées par rien.",
            mesure={"releve": False},
        )
    valeur, maj = memo
    age_min = round((_maintenant() - maj).total_seconds() / 60, 1) if maj else None
    if valeur.get("erreur"):
        return Controle(
            "twilio", "Alertes Twilio", ATTENTION,
            "Relève impossible.",
            f"L'API Twilio n'a pas répondu ({valeur['erreur']}). Ce contrôle ne dit donc "
            "rien de l'état réel des webhooks.",
            mesure={"releve": False, "age_minutes": age_min},
        )
    n = int(valeur.get("erreurs", 0))
    # La relève est périodique : un mémo vieux de plusieurs heures ne décrit plus le
    # présent, et se taire serait ici pire que le dire.
    if age_min is not None and age_min > 6 * 60:
        return Controle(
            "twilio", "Alertes Twilio", ATTENTION,
            f"Relève figée depuis {age_min / 60:.0f} h.",
            "La tâche de fond ne tourne plus : les erreurs de webhook ne sont plus vues.",
            mesure={"releve": False, "age_minutes": age_min},
        )
    return Controle(
        "twilio", "Alertes Twilio", ATTENTION if n else OK,
        f"{n} erreur(s) signalée(s) par Twilio sur {fenetre_jours()} jours.",
        "Twilio n'a pas pu joindre le webhook ou a reçu un TwiML invalide : ces appels "
        "sont morts avant d'atteindre l'application. Détail dans la console Twilio → "
        "Monitor → Alerts." if n else "",
        mesure={"releve": True, "erreurs": n, "age_minutes": age_min},
    )


# --- Verdict d'ensemble ------------------------------------------------------

# La sonde est interrogeable de l'extérieur : on met le résultat en cache quelques
# secondes pour qu'une boucle serrée ne coûte rien. Court, parce qu'un état de
# supervision périmé est un mensonge poli.
_CACHE_SECONDES = 15
_cache: dict = {}


def controles() -> list[Controle]:
    """Tous les contrôles, toujours dans le même ordre : du plus grave au plus fin.

    Un contrôle qui explose ne doit jamais faire tomber la sonde — sinon la panne
    de supervision se déguise en panne d'application. Il est alors signalé comme
    tel, ce qui reste une information exacte : on ne sait pas.
    """
    # La lecture des appels est faite UNE fois pour les trois contrôles qui s'en
    # servent. Si elle échoue, on continue avec une liste vide au lieu de laisser
    # l'exception remonter : sans ça, une base injoignable ferait tomber la sonde
    # entière (HTTP 500) avant que `_controle_base` ait pu diagnostiquer la panne —
    # l'alerte dirait « supervision cassée » là où elle doit dire « base cassée ».
    try:
        appels = _appels_fenetre()
        appels_lus = True
    except Exception:
        appels, appels_lus = [], False

    def _sur_appels(fabrique):
        """Un contrôle d'appels calculé sur une liste vide dirait « pas de mesure »,
        c'est-à-dire un feu vert, alors qu'on n'a simplement pas pu lire."""
        if appels_lus:
            return lambda: fabrique(appels)
        def _inconnu():
            raise RuntimeError("appels illisibles en base")
        return _inconnu

    fabriques = [
        ("base", _controle_base),
        ("configuration", _controle_configuration),
        ("appels_muets", _sur_appels(_controle_appels_muets)),
        ("appels_echoues", _sur_appels(_controle_appels_echoues)),
        ("appels_inacheves", _sur_appels(_controle_appels_inacheves)),
        ("latence", _controle_latence),
        ("twilio", _controle_twilio),
        ("accueils", _controle_accueils),
        ("sauvegarde", _controle_sauvegarde),
    ]
    resultats = []
    for cle, fabrique in fabriques:
        try:
            resultats.append(fabrique())
        except Exception as exc:
            resultats.append(Controle(
                cle, cle.replace("_", " ").capitalize(), ATTENTION,
                "Contrôle impossible.",
                f"La vérification elle-même a échoué ({type(exc).__name__}: {exc}). "
                "L'état de ce point est INCONNU, pas bon.",
            ))
    return resultats


def etat(*, force: bool = False) -> dict:
    """État complet, mis en cache quelques secondes.

    `niveau` vaut le pire des contrôles. C'est cette valeur qui décide du code HTTP
    de la sonde, donc de l'alerte : elle ne doit dépendre d'aucun jugement humain.
    """
    maintenant = time.monotonic()
    if not force and _cache and maintenant - _cache["t"] < _CACHE_SECONDES:
        return _cache["etat"]
    liste = controles()
    resultat = {
        "niveau": pire(*(c.niveau for c in liste)),
        "mesure_le": _maintenant().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fenetre_jours": fenetre_jours(),
        "controles": [c.as_dict() for c in liste],
    }
    _cache.clear()
    _cache.update({"t": maintenant, "etat": resultat})
    return resultat


def vider_cache() -> None:
    """Utilisé par les tests et par l'admin, qui ne doit jamais afficher un état figé
    juste après une action de l'utilisateur."""
    _cache.clear()


def resume(etat_courant: dict) -> str:
    """Une ligne, pour un objet d'alerte ou un titre d'incident."""
    fautifs = [c for c in etat_courant["controles"] if c["niveau"] != OK]
    if not fautifs:
        return "Tout est vert"
    return ", ".join(f"{c['titre']} ({c['niveau']})" for c in fautifs)


# --- Relève des alertes Twilio (tâche de fond) -------------------------------

_TWILIO_ALERTES_URL = "https://monitor.twilio.com/v1/Alerts"


def twilio_intervalle_secondes() -> int:
    """0 désactive la relève. 15 min : l'API Monitor est gratuite en lecture, mais
    interroger un tiers en boucle depuis un service en ligne est une mauvaise
    habitude — et une panne de webhook n'a pas besoin d'être connue à la seconde."""
    return _entier("SUPERVISION_TWILIO_SECONDES", 900)


async def rafraichir_twilio() -> None:
    """Interroge Twilio une fois et note le résultat sur l'ardoise.

    N'échoue jamais bruyamment : une relève ratée est notée COMME telle
    (`{"erreur": ...}`), ce qui fait passer le contrôle en « attention ». Un
    fournisseur de supervision muet ne doit pas se lire comme un feu vert.
    """
    import httpx

    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        noter("twilio", {"erreur": "identifiants Twilio absents"})
        return
    depuis = (_maintenant() - timedelta(days=fenetre_jours())).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            reponse = await client.get(
                _TWILIO_ALERTES_URL,
                params={"StartDate": depuis, "PageSize": 50},
                auth=(sid, token),
            )
        reponse.raise_for_status()
        alertes = reponse.json().get("alerts", []) or []
    except Exception as exc:
        noter("twilio", {"erreur": type(exc).__name__})
        return
    # `error_code` est renseigné pour les erreurs (11200 webhook injoignable, 12100
    # TwiML invalide, 31920 flux média refusé…) ; les alertes de niveau `notice` ne
    # décrivent pas une panne et n'ont donc rien à faire dans un compteur d'alerte.
    erreurs = [a for a in alertes if str(a.get("error_code") or "").strip()]
    codes: dict[str, int] = {}
    for alerte in erreurs:
        code = str(alerte["error_code"])
        codes[code] = codes.get(code, 0) + 1
    noter("twilio", {"erreurs": len(erreurs), "codes": codes})


async def boucle_twilio() -> None:
    """Relève périodique, lancée au démarrage de l'application."""
    import asyncio

    from loguru import logger

    intervalle = twilio_intervalle_secondes()
    if intervalle <= 0:
        return
    logger.info(f"supervision : relève des alertes Twilio toutes les {intervalle} s.")
    while True:
        try:
            await rafraichir_twilio()
        except Exception as exc:  # la boucle ne doit jamais mourir
            logger.warning(f"supervision : relève Twilio échouée ({exc}).")
        await asyncio.sleep(intervalle)
