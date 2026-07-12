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


def count_for_slot(tenant_id: int, date: str, time: str) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(party_size), 0) FROM reservations"
            " WHERE tenant_id = ? AND date = ? AND time = ?",
            (tenant_id, date, time),
        ).fetchone()
    return row[0]
