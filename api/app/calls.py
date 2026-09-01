"""Journal des appels : persistance et statistiques pour la plateforme admin.

start_call/finish_call sont appelés depuis le chemin d'appel vocal : ils sont
enveloppés de try/except par L'APPELANT et doivent rester rapides (INSERT/UPDATE
SQLite ≈ 1 ms). finish_call est appelé via asyncio.to_thread depuis bot.py pour ne
jamais bloquer l'event loop.
"""
import json
import os
from typing import Optional

from . import db

# Tarifs pour le coût ESTIMÉ par appel (affichage admin). Calés sur les mesures réelles
# de scripts/cost_report.py (18/07/2026) : L4 ~2 ct/min (helmi), Twilio entrant
# ~0,85 ct/min, LLM gemini-flash ~0,35 ct/appel.
# Le coût EXACT reste l'affaire de cost_report.py (APIs de facturation).
#
# Deepgram corrigé le 30/08/2026 : 0,0058 était le tarif **nova-2**, alors que la
# production tourne en **nova-3** (DEEPGRAM_MODEL) depuis le réglage de naturalité.
# Tarif nova-3 streaming, à la carte, monolingue : 0,0077 $/min (deepgram.com/pricing).
# ⚠️ Le multilingue est à 0,0092 : passer DEEPGRAM_LANGUAGE=multi change ce coût, et
# il faudra alors ajuster COST_DEEPGRAM_PER_MIN — sinon l'admin sous-estime en silence.
# L'écart n'était pas cosmétique : +33 % sur la ligne transcription, et c'est sur ces
# chiffres qu'on arrête une grille tarifaire (#29).
_COST_TWILIO_PER_MIN = float(os.getenv("COST_TWILIO_PER_MIN", "0.0085"))
_COST_DEEPGRAM_PER_MIN = float(os.getenv("COST_DEEPGRAM_PER_MIN", "0.0077"))
_COST_MODAL_PER_MIN = float(os.getenv("COST_MODAL_PER_MIN", "0.02"))
_COST_LLM_PER_CALL = float(os.getenv("COST_LLM_PER_CALL", "0.0035"))


def estimate_call_cost(duration_seconds: float) -> float:
    minutes = max(0.0, duration_seconds) / 60.0
    per_min = _COST_TWILIO_PER_MIN + _COST_DEEPGRAM_PER_MIN + _COST_MODAL_PER_MIN
    return round(minutes * per_min + _COST_LLM_PER_CALL, 6)


def start_call(call_sid: Optional[str], tenant_id: int,
               caller_number: Optional[str] = None) -> Optional[int]:
    """Enregistre le début d'appel et renvoie son identifiant.

    ON CONFLICT DO NOTHING : un doublon de webhook ne doit jamais faire échouer l'appel.
    L'identifiant est relu plutôt que pris de `lastrowid`, précisément pour ce cas —
    sur un doublon, `lastrowid` ne désigne aucune insertion.

    Cet identifiant nomme les fichiers d'enregistrement (#88). On n'utilise pas le
    `call_sid` pour ça : il arrive dans le message `start` du websocket, donc de
    l'extérieur, alors qu'un entier de notre base ne peut désigner qu'un de nos fichiers.
    """
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, caller_number) VALUES (?, ?, ?)
               ON CONFLICT(call_sid) DO NOTHING""",
            (call_sid, tenant_id, caller_number),
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
    reservation_id: Optional[int] = None,
    turn_latencies: Optional[list[int]] = None,
    journal: Optional[dict] = None,
    recording_bytes: Optional[int] = None,
) -> None:
    """Clôt l'appel : durée depuis started_at, statut, transcript JSON, coût estimé.

    `turn_latencies` = les blancs ressentis tour par tour, en millisecondes (cf.
    voice/latency.py). Stockés tels quels : c'est la matière première du diagnostic."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, started_at FROM calls WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        if row is None:
            return  # start_call a échoué/absent : ne rien inventer
        duration = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 86400.0", (row["started_at"],)
        ).fetchone()[0]
        duration = max(0.0, float(duration or 0.0))
        conn.execute(
            """UPDATE calls SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                   duration_seconds = ?, status = ?, transcript = ?,
                   reservation_id = ?, estimated_cost = ?, turn_latencies = ?,
                   journal = ?, recording_bytes = ?
               WHERE id = ?""",
            (
                duration,
                status,
                json.dumps(transcript, ensure_ascii=False) if transcript else None,
                reservation_id,
                estimate_call_cost(duration),
                json.dumps(turn_latencies) if turn_latencies else None,
                _journal_borne(journal),
                recording_bytes,
                row["id"],
            ),
        )


# Issues filtrables depuis l'admin. Elles décrivent l'état RÉEL des colonnes ; il n'y
# a pas de catégorie « à rappeler », rien ne la matérialise en base.
OUTCOME_FILTERS = {
    "reservation": "reservation_id IS NOT NULL",
    "failed": "status = 'failed'",
    "info": "reservation_id IS NULL AND status = 'completed'",
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
    where_calls = "WHERE started_at >= date('now', ?)"
    where_resas = "WHERE created_at >= date('now', ?)"
    params_calls: list = [f"-{int(days)} days"]
    params_resas: list = [f"-{int(days)} days"]
    if tenant_id is not None:
        where_calls += " AND tenant_id = ?"
        where_resas += " AND tenant_id = ?"
        params_calls.append(tenant_id)
        params_resas.append(tenant_id)
    with db.get_conn() as conn:
        calls_rows = conn.execute(
            f"""SELECT date(started_at) AS day,
                       COUNT(*) AS n_calls,
                       SUM(CASE WHEN reservation_id IS NOT NULL THEN 1 ELSE 0 END) AS n_with_reservation,
                       COALESCE(SUM(estimated_cost), 0) AS total_cost
                FROM calls {where_calls} GROUP BY day""",
            params_calls,
        ).fetchall()
        resa_rows = conn.execute(
            f"""SELECT date(created_at) AS day, COUNT(*) AS n_reservations
                FROM reservations {where_resas} GROUP BY day""",
            params_resas,
        ).fetchall()
    merged: dict[str, dict] = {}
    for row in calls_rows:
        merged[row["day"]] = {
            "day": row["day"],
            "n_calls": row["n_calls"],
            "n_with_reservation": row["n_with_reservation"] or 0,
            "total_cost": row["total_cost"] or 0.0,
            "n_reservations": 0,
        }
    for row in resa_rows:
        entry = merged.setdefault(
            row["day"],
            {"day": row["day"], "n_calls": 0, "n_with_reservation": 0,
             "total_cost": 0.0, "n_reservations": 0},
        )
        entry["n_reservations"] = row["n_reservations"]
    return sorted(merged.values(), key=lambda e: e["day"])


def _window(days: int, offset_days: int) -> tuple[str, list]:
    """Fragment SQL d'une fenêtre glissante de `days` jours, décalée de `offset_days`
    vers le passé. Sans décalage il n'y a PAS de borne haute : sinon la journée en
    cours (date('now') = minuit) tomberait hors de la fenêtre."""
    clause = "{col} >= date('now', ?)"
    params: list = [f"-{int(days) + int(offset_days)} days"]
    if offset_days > 0:
        clause += " AND {col} < date('now', ?)"
        params.append(f"-{int(offset_days)} days")
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
                       COALESCE(SUM(CASE WHEN reservation_id IS NOT NULL THEN 1 ELSE 0 END), 0)
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
                       COALESCE(SUM(CASE WHEN reservation_id IS NOT NULL THEN 1 ELSE 0 END), 0)
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
    """Ventilation du coût estimé par composant, avec les MÊMES tarifs que
    estimate_call_cost : c'est la décomposition de la somme affichée ailleurs, pas
    une seconde estimation."""
    t = totals(tenant_id, days=days)
    minutes = t["total_duration"] / 60.0
    rows = [
        ("Twilio (téléphonie)", minutes * _COST_TWILIO_PER_MIN),
        ("Deepgram (transcription)", minutes * _COST_DEEPGRAM_PER_MIN),
        ("Modal GPU (voix Moshi)", minutes * _COST_MODAL_PER_MIN),
        ("LLM (compréhension)", t["n_calls"] * _COST_LLM_PER_CALL),
    ]
    raw_total = sum(amount for _, amount in rows)
    if not raw_total:
        return [{"label": label, "amount": 0.0, "share": 0} for label, _ in rows]
    # `estimated_cost` est arrondi au millionième À CHAQUE appel : recalculer les
    # composants depuis la durée totale laisse un résidu d'arrondi. On le redistribue
    # au prorata pour que la ventilation somme EXACTEMENT au total affiché ailleurs —
    # c'est bien une décomposition, pas une seconde estimation.
    scale = t["total_cost"] / raw_total
    return [
        {"label": label, "amount": amount * scale,
         "share": round(100 * amount / raw_total)}
        for label, amount in rows
    ]


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
               WHERE recording_bytes IS NOT NULL AND started_at < datetime('now', ?)""",
            (f"-{int(avant_jours)} days",),
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
