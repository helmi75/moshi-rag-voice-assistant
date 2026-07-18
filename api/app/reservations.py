"""Réservations rattachées à un tenant, stockées en SQLite."""
from typing import Optional

from . import db


def create_reservation(
    tenant_id: int,
    customer_name: str,
    date: str,
    time: str,
    party_size: int,
    customer_phone: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reservations
               (tenant_id, customer_name, customer_phone, date, time, party_size, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, customer_name, customer_phone, date, time, party_size, notes),
        )
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def list_reservations(tenant_id: int) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE tenant_id = ? ORDER BY date, time",
            (tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_reservation(reservation_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
    return dict(row) if row else None


def update_reservation(reservation_id: int, **fields) -> Optional[dict]:
    """Met à jour les champs fournis (customer_name, customer_phone, date, time,
    party_size, notes)."""
    allowed = {"customer_name", "customer_phone", "date", "time", "party_size", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_reservation(reservation_id)
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE reservations SET {assignments} WHERE id = ?",
            (*updates.values(), reservation_id),
        )
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_reservation(reservation_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))


def list_filtered(
    tenant_id: Optional[int] = None,
    date_from: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Liste paginée/filtrée pour l'admin (tenant_id None = tous, super-admin)."""
    query = "SELECT * FROM reservations"
    clauses: list[str] = []
    params: list = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY date DESC, time DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def count_for_slot(tenant_id: int, date: str, time: str) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(party_size), 0) FROM reservations"
            " WHERE tenant_id = ? AND date = ? AND time = ?",
            (tenant_id, date, time),
        ).fetchone()
    return row[0]
