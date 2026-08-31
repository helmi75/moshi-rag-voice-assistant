"""Réservations rattachées à un tenant, stockées en SQLite.

**Une réservation annulée reste en base**, horodatée par `cancelled_at` (#33). Deux
conséquences à ne jamais perdre de vue :

1. tout ce qui COMPTE des couverts doit exclure les annulées, sinon la salle paraît
   pleine alors qu'elle est libre — et l'assistante refuserait une table disponible ;
2. l'admin, lui, les montre : un restaurateur qui voit « annulée à 15h32 » peut
   reproposer le créneau, et retrouver la preuve si le client conteste.
"""
from typing import Optional

from . import db

# Fragment SQL partagé par tout ce qui compte des couverts. Écrit UNE fois : une
# annulation oubliée dans une requête de comptage est invisible à la lecture et se
# manifeste par un refus de réservation inexplicable.
ACTIVES = "cancelled_at IS NULL"


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
    inclure_annulees: bool = False,
) -> list[dict]:
    """Liste paginée/filtrée pour l'admin (tenant_id None = tous, super-admin).

    Les annulées sont masquées PAR DÉFAUT : la liste « à venir » de la salle de contrôle
    passe par ici, et y laisser des tables annulées ferait préparer des couverts pour des
    clients qui ne viendront pas. L'écran des réservations, lui, demande à les voir."""
    query = "SELECT * FROM reservations"
    clauses: list[str] = []
    params: list = []
    if not inclure_annulees:
        clauses.append(ACTIVES)
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


def covers_by_slot(tenant_id: int, date: str) -> list[dict]:
    """Couverts réservés par créneau horaire pour une date donnée (salle de contrôle).

    La capacité d'une salle n'existe pas en base : on renvoie les couverts réellement
    réservés, sans jauge de remplissage inventée."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT time, COALESCE(SUM(party_size), 0) AS covers, COUNT(*) AS n
               FROM reservations WHERE tenant_id = ? AND date = ? AND {actives}
               GROUP BY time ORDER BY time""".format(actives=ACTIVES),
            (tenant_id, date),
        ).fetchall()
    return [dict(r) for r in rows]


def count_for_slot(tenant_id: int, date: str, time: str) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(party_size), 0) FROM reservations"
            f" WHERE tenant_id = ? AND date = ? AND time = ? AND {ACTIVES}",
            (tenant_id, date, time),
        ).fetchone()
    return row[0]


# ─────────────────────────────────────────────────────────────────────────────
# Accès par l'appelant (#33) — modification et annulation au téléphone
#
# L'assistante ne doit JAMAIS pouvoir toucher la réservation d'un autre. Le modèle de
# langage propose un identifiant ; il ne l'a pas inventé, mais il pourrait. La porte est
# donc ici, dans la couche données, et elle a une forme délibérée : **on ne peut pas
# obtenir la réservation sans avoir fourni le tenant ET le numéro appelant**. Il n'existe
# pas de version « charger d'abord, vérifier ensuite » qu'on pourrait oublier d'appeler.
# ─────────────────────────────────────────────────────────────────────────────

def find_by_phone(tenant_id: int, phone: str, *, a_partir_de: Optional[str] = None) -> list[dict]:
    """Réservations À VENIR de ce numéro, chez cet établissement.

    Les passées ne sont pas rendues : on ne modifie ni n'annule un dîner d'hier, et les
    proposer à l'assistante l'inciterait à parler d'une réservation périmée.
    """
    phone = (phone or "").strip()
    if not phone:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM reservations
                WHERE tenant_id = ? AND customer_phone = ? AND {ACTIVES}
                  AND date >= COALESCE(?, date('now'))
                ORDER BY date, time""",
            (tenant_id, phone, a_partir_de),
        ).fetchall()
    return [dict(r) for r in rows]


def get_for_caller(reservation_id: int, tenant_id: int, phone: str) -> Optional[dict]:
    """La réservation, SEULEMENT si elle appartient à cet établissement et à ce numéro.

    Renvoie None dans tous les autres cas — y compris quand la réservation existe mais
    appartient à quelqu'un d'autre. Indistinguable d'un identifiant inexistant, à
    dessein : une réponse différenciée confirmerait à un appelant qu'une réservation
    existe à ce numéro, ce qu'il n'a pas à savoir.
    """
    phone = (phone or "").strip()
    if not phone:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            f"""SELECT * FROM reservations
                WHERE id = ? AND tenant_id = ? AND customer_phone = ? AND {ACTIVES}""",
            (reservation_id, tenant_id, phone),
        ).fetchone()
    return dict(row) if row else None


def cancel_reservation(reservation_id: int) -> Optional[dict]:
    """Annule sans effacer : la ligne reste, horodatée.

    Idempotent — une seconde annulation ne réécrit pas l'horodatage, sinon on perdrait
    l'heure réelle de l'annulation, celle qui compte en cas de litige.
    """
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE reservations
               SET cancelled_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE id = ? AND cancelled_at IS NULL""",
            (reservation_id,),
        )
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
    return dict(row) if row else None
