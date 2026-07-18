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
# ~0,85 ct/min, Deepgram nova-2 ~0,58 ct/min, LLM gemini-flash ~0,35 ct/appel.
# Le coût EXACT reste l'affaire de cost_report.py (APIs de facturation).
_COST_TWILIO_PER_MIN = float(os.getenv("COST_TWILIO_PER_MIN", "0.0085"))
_COST_DEEPGRAM_PER_MIN = float(os.getenv("COST_DEEPGRAM_PER_MIN", "0.0058"))
_COST_MODAL_PER_MIN = float(os.getenv("COST_MODAL_PER_MIN", "0.02"))
_COST_LLM_PER_CALL = float(os.getenv("COST_LLM_PER_CALL", "0.0035"))


def estimate_call_cost(duration_seconds: float) -> float:
    minutes = max(0.0, duration_seconds) / 60.0
    per_min = _COST_TWILIO_PER_MIN + _COST_DEEPGRAM_PER_MIN + _COST_MODAL_PER_MIN
    return round(minutes * per_min + _COST_LLM_PER_CALL, 6)


def start_call(call_sid: Optional[str], tenant_id: int) -> None:
    """Enregistre le début d'appel. ON CONFLICT DO NOTHING : un doublon de webhook
    ne doit jamais faire échouer l'appel."""
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id) VALUES (?, ?)
               ON CONFLICT(call_sid) DO NOTHING""",
            (call_sid, tenant_id),
        )


def finish_call(
    call_sid: str,
    status: str = "completed",
    transcript: Optional[list[dict]] = None,
    reservation_id: Optional[int] = None,
) -> None:
    """Clôt l'appel : durée depuis started_at, statut, transcript JSON, coût estimé."""
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
                   reservation_id = ?, estimated_cost = ?
               WHERE id = ?""",
            (
                duration,
                status,
                json.dumps(transcript, ensure_ascii=False) if transcript else None,
                reservation_id,
                estimate_call_cost(duration),
                row["id"],
            ),
        )


def list_calls(tenant_id: Optional[int] = None, limit: int = 50, offset: int = 0) -> list[dict]:
    query = "SELECT * FROM calls"
    params: list = []
    if tenant_id is not None:
        query += " WHERE tenant_id = ?"
        params.append(tenant_id)
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
