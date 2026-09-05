"""Messages pris au téléphone pour l'équipe du restaurant (#32).

**Ce module existe parce qu'une promesse était faite et jamais tenue.** Le prompt
demandait déjà à l'assistante de « prendre le message et annoncer un rappel », et elle le
disait — relevé dans 5 appels réels sur 10 le 04/09/2026 : une candidature, un client qui
cherchait un cuisinier, une question de recrutement, une inquiétude sur un paiement.
Aucun outil ne l'enregistrait. Le message n'existait que dans la transcription de
l'appel, et rien, dans l'admin, ne disait au restaurateur qu'un rappel était attendu :
ces appels tombaient dans le filtre « info », au même titre qu'une question d'horaires.

Un blanc de deux secondes agace. Un rappel promis qui ne vient jamais fait perdre le
client ET le décrédibilise auprès de lui — c'est le restaurateur qui passe pour
quelqu'un qui ne rappelle pas.

**Le numéro vient du réseau, jamais du modèle.** Même règle que pour les réservations
(#33) : l'appelant ne décide pas de qui il est. Le modèle fournit le sujet et le détail,
le numéro est injecté côté serveur.
"""
from datetime import datetime, timezone
from typing import Optional

from . import db

# Un message est un pense-bête, pas un dossier. Ces bornes évitent qu'un modèle bavard
# ou un appel qui déraille ne remplisse la base — et gardent la liste lisible pour le
# restaurateur, qui la consulte entre deux services.
_MAX_SUJET = 120
_MAX_DETAILS = 1000


def _maintenant() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _borner(texte: Optional[str], maximum: int) -> Optional[str]:
    if texte is None:
        return None
    texte = " ".join(str(texte).split())
    return texte[:maximum] or None


def create_message(tenant_id: int, subject: str, details: Optional[str] = None,
                   caller_number: Optional[str] = None, call_id: Optional[int] = None,
                   customer_name: Optional[str] = None) -> Optional[int]:
    """Enregistre un message. Renvoie son identifiant, ou None si le sujet est vide.

    Un sujet vide n'est pas une erreur à faire remonter au modèle : c'est un message
    qui n'apprend rien au restaurateur, et une ligne vide dans sa liste lui coûterait
    plus d'attention qu'elle ne lui en fait gagner."""
    sujet = _borner(subject, _MAX_SUJET)
    if not sujet:
        return None
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO messages (tenant_id, call_id, caller_number, customer_name,
                                     subject, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(tenant_id), call_id, caller_number, _borner(customer_name, 80),
             sujet, _borner(details, _MAX_DETAILS), _maintenant()),
        )
        return cur.lastrowid


def list_messages(tenant_id: Optional[int] = None, only_pending: bool = False,
                  limit: int = 50, offset: int = 0) -> list[dict]:
    query = "SELECT * FROM messages"
    clauses: list[str] = []
    params: list = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if only_pending:
        clauses.append("handled_at IS NULL")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    with db.get_conn() as conn:
        rows = conn.execute(query, params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


def messages_d_un_appel(call_id: int) -> list[dict]:
    """Les messages pris pendant cet appel — affichés dans son panneau de détail."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE call_id = ? ORDER BY id", (call_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_message(message_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (int(message_id),)
        ).fetchone()
    return dict(row) if row else None


def count_pending(tenant_id: Optional[int] = None) -> int:
    """Combien de rappels sont encore dus. C'est ce chiffre qui doit sauter aux yeux :
    un message pris et jamais traité vaut moins qu'un message jamais pris, parce qu'il
    a en plus engagé le restaurateur auprès de l'appelant."""
    with db.get_conn() as conn:
        if tenant_id is None:
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE handled_at IS NULL"
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE handled_at IS NULL AND tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]


def marquer_traite(message_id: int, tenant_id: int) -> bool:
    """Marque le message comme traité. Idempotent, et SCOPÉ par établissement : le
    `tenant_id` est dans la clause WHERE, pas seulement vérifié avant — un identifiant
    deviné ne suffit donc pas à toucher le message d'un autre restaurant."""
    with db.get_conn() as conn:
        cur = conn.execute(
            """UPDATE messages SET handled_at = ?
               WHERE id = ? AND tenant_id = ? AND handled_at IS NULL""",
            (_maintenant(), int(message_id), int(tenant_id)),
        )
        return cur.rowcount > 0


def messages_d_un_numero(numero: str) -> list[dict]:
    """Les messages laissés par ce numéro — pour le droit à l'effacement (#22)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, tenant_id FROM messages WHERE caller_number = ?", (numero,)
        ).fetchall()
    return [dict(r) for r in rows]
