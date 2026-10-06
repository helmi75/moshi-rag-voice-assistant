"""Réservations rattachées à un tenant, stockées en SQLite.

**Une réservation annulée reste en base**, horodatée par `cancelled_at` (#33). Deux
conséquences à ne jamais perdre de vue :

1. tout ce qui COMPTE des couverts doit exclure les annulées, sinon la salle paraît
   pleine alors qu'elle est libre — et l'assistante refuserait une table disponible ;
2. l'admin, lui, les montre : un restaurateur qui voit « annulée à 15h32 » peut
   reproposer le créneau, et retrouver la preuve si le client conteste.
"""
import re
from typing import Optional

from . import db, horloge

# Fragment SQL partagé par tout ce qui compte des couverts. Écrit UNE fois : une
# annulation oubliée dans une requête de comptage est invisible à la lecture et se
# manifeste par un refus de réservation inexplicable.
ACTIVES = "cancelled_at IS NULL"


def nom_lisible(nom: str) -> str:
    """Un nom écrit TOUT en capitales remis en casse de titre.

    Une réservation prise sur un nom épelé arrive en capitales — c'est ce que produit
    la consigne de reconstitution du prompt (« H comme Henri, E comme Émilie… »). Ça
    reste ensuite en base, et l'assistante RE-ÉPELLE ce nom aux appels suivants :
    « Is it H E L M I, like last time? », entendu sur l'appel 140 du 20/09/2026. Le
    restaurateur, lui, voyait « HELMI » crié dans toutes ses listes.

    On ne touche qu'aux noms entièrement en capitales : une casse mixte a été voulue
    par quelqu'un (« van der Berg », « McDonald ») et lui passer `.title()` dessus
    l'abîmerait."""
    nom = " ".join((nom or "").split())
    return nom.title() if nom.isupper() else nom


def create_reservation(
    tenant_id: int,
    customer_name: str,
    date: str,
    time: str,
    party_size: int,
    customer_phone: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    customer_name = nom_lisible(customer_name)
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
    if "customer_name" in updates:
        updates["customer_name"] = nom_lisible(updates["customer_name"])
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


def list_filtered(
    tenant_id: Optional[int] = None,
    date_from: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    inclure_annulees: bool = False,
    plus_proches_d_abord: bool = False,
    heure_from: Optional[str] = None,
) -> list[dict]:
    """Liste paginée/filtrée pour l'admin (tenant_id None = tous, super-admin).

    `plus_proches_d_abord` : pour « à venir ». Sans lui le tri va de la plus tardive à
    la plus proche, et `limit` ne gardait que les six tables les plus LOINTAINES — la
    salle de contrôle n'en montrait aucune du jour (recette du 05/10/2026).

    `heure_from` (« HH:MM », avec `date_from`) : le jour de `date_from`, seulement les
    tables à partir de cette heure. Sans lui, « à venir » commençait par les tables du
    midi déjà servies : avec six déjeuners, la salle de contrôle de 19 h ne montrait
    aucune table du soir (revue du 06/10/2026).

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
    if date_from and heure_from:
        clauses.append("(date > ? OR (date = ? AND time >= ?))")
        params += [date_from, date_from, heure_from]
    elif date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    sens = "ASC" if plus_proches_d_abord else "DESC"
    query += f" ORDER BY date {sens}, time {sens}, id {sens} LIMIT ? OFFSET ?"
    params += [limit, offset]
    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def entre(debut: str, fin: str, tenant_ids: Optional[list[int]] = None) -> list[dict]:
    """Du `debut` au `fin` inclus, annulées COMPRISES : le calendrier de l'admin les
    montre barrées, sans les compter (SCRUM-112). `tenant_ids` None = tout le parc
    (super-admin) ; une liste vide = rien."""
    if tenant_ids is not None and not tenant_ids:
        return []
    query = "SELECT * FROM reservations WHERE date >= ? AND date <= ?"
    params: list = [debut, fin]
    if tenant_ids is not None:
        query += f" AND tenant_id IN ({', '.join('?' * len(tenant_ids))})"
        params += list(tenant_ids)
    with db.get_conn() as conn:
        rows = conn.execute(query + " ORDER BY date, time, id", params).fetchall()
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
                  AND date >= ?
                ORDER BY date, time""",
            (tenant_id, phone, a_partir_de or horloge.aujourd_hui().isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def du_client(tenant_id: int, phone: Optional[str], limite: int = 10) -> list[dict]:
    """Toutes les réservations de ce numéro chez cet établissement, passées, à venir et
    annulées, la plus récente d'abord : l'historique d'un client (ASSISTANTE-116).
    `find_by_phone` ne rend que les actives à venir — c'est ce que l'assistante peut
    modifier, pas ce que le restaurateur veut savoir de son client."""
    phone = (phone or "").strip()
    if not phone:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM reservations WHERE tenant_id = ? AND customer_phone = ?
               ORDER BY date DESC, time DESC LIMIT ?""",
            (tenant_id, phone, limite),
        ).fetchall()
    return [dict(r) for r in rows]


# Le nom relu dans le prompt système vient d'une transcription : seul ce qui RESSEMBLE à
# un nom passe (lettres, au plus quatre mots séparés par espace, apostrophe ou tiret).
# Sans ce filtre, une phrase dictée comme « nom » lors d'un appel deviendrait du texte
# libre dans les consignes de l'appel suivant.
_NOM_PLAUSIBLE = re.compile(r"[^\W\d_]+(?:[ '’-][^\W\d_]+){0,3}")


def dernier_nom(tenant_id: int, phone: Optional[str]) -> Optional[str]:
    """Le nom de la dernière réservation faite depuis ce numéro chez cet établissement.

    Sert à PROPOSER le nom au lieu de le redemander — c'est l'étape qui échoue le plus
    au téléphone (nom mal entendu sur du 8 kHz, épellation). Annulées comprises : le nom
    reste celui de l'appelant. Rien pour un appel masqué, ni après la purge RGPD du
    numéro : la recherche se fait par numéro, elle suit donc la même durée de conservation.
    """
    phone = (phone or "").strip()
    if not phone:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT customer_name FROM reservations
               WHERE tenant_id = ? AND customer_phone = ?
               ORDER BY id DESC LIMIT 1""",
            (tenant_id, phone),
        ).fetchone()
    nom = nom_lisible(row["customer_name"] or "") if row else ""
    if not nom or len(nom) > 40 or not _NOM_PLAUSIBLE.fullmatch(nom):
        return None
    return nom


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
