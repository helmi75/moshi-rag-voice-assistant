"""Tenants (les commerces clients du SaaS) et routage par numéro appelé."""
import os
from dataclasses import dataclass
from typing import Optional

from . import db

DEMO_TENANT_NUMBER = os.getenv("TWILIO_NUMBER", "+33100000000")

# Accueil du tenant démo : finit par « un instant s'il vous plaît » pour enchaîner sur
# la musique d'attente pendant le réveil du GPU (flux standardiste, voir voice/greeting.py).
_DEMO_GREETING = "Bonjour, restaurant Le Fouquet's Paris. Un instant s'il vous plaît."

_DEMO_KNOWLEDGE_BASE = """\
## Restaurant
Le Fouquet's Paris, 99 avenue des Champs-Élysées, au sein de l'hôtel Barrière.
Téléphone : 01 40 69 60 50.

## Horaires
Ouvert tous les jours. Petit-déjeuner dès 7h30, service continu jusqu'à 23h30.
Brunch le week-end de 11h30 à 15h.

## Menus
Formule déjeuner à partir de 78 €. Options sans gluten et végétariennes disponibles.
Terrasse chauffée toute l'année.

## Réservations
Tables de 1 à 12 personnes. Au-delà de 12 personnes, orienter vers le service groupes
au 01 40 69 60 50.
"""


@dataclass
class Tenant:
    id: int
    name: str
    business_type: str
    phone_number: str
    language: str
    greeting: str
    knowledge_base: str
    # Identifiant de voix choisi dans l'admin. None = la voix par défaut du parc ;
    # c'est voice/voices.py qui tranche (et ignore une valeur hors catalogue).
    voice: Optional[str] = None
    # Formule commerciale (#31). None = formule par défaut ; c'est app/plans.py:resolve
    # qui tranche, et qui ignore une valeur hors catalogue.
    plan: Optional[str] = None
    # Horaires d'ouverture structurés (JSON, app/disponibilite.py). None = non renseigné :
    # l'assistante ne refuse aucun créneau, comme avant.
    opening_hours: Optional[str] = None
    # Adresse de notification en plus des comptes restaurateurs (app/notifications.py).
    notify_email: Optional[str] = None


def _row_to_tenant(row) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        business_type=row["business_type"],
        phone_number=row["phone_number"],
        language=row["language"],
        greeting=row["greeting"] or f"Bonjour, {row['name']}, que puis-je faire pour vous ?",
        knowledge_base=row["knowledge_base"],
        voice=row["voice"],
        plan=row["plan"],
        opening_hours=row["opening_hours"],
        notify_email=row["notify_email"],
    )


def parse_knowledge_sections(knowledge_base: str) -> list[dict]:
    """Découpe la base de connaissances en fiches sur les titres Markdown `##`.

    Le format est déjà celui-là (cf. _DEMO_KNOWLEDGE_BASE) : l'admin l'affiche en
    fiches sans rien changer au stockage ni au prompt. Le texte écrit AVANT le premier
    `##` n'est pas perdu : il devient une fiche « Général ».

    `filled` = la fiche a un contenu exploitable (≥ 15 caractères) ; c'est un fait
    vérifiable, pas un score de qualité.
    """
    sections: list[dict] = []
    title, body, is_preamble = "Général", [], True

    def flush() -> None:
        text = "\n".join(body).strip()
        # Le préambule ne devient une fiche que s'il contient vraiment quelque chose ;
        # une base qui commence directement par « ## » ne gagne pas de fiche vide.
        if text or not is_preamble:
            sections.append({"title": title, "body": text, "filled": len(text) >= 15})

    for line in (knowledge_base or "").splitlines():
        if line.startswith("## "):
            flush()
            title, body, is_preamble = line[3:].strip() or "Sans titre", [], False
        else:
            body.append(line)
    flush()
    return sections


def get_by_phone(phone_number: Optional[str]) -> Optional[Tenant]:
    """Résout le tenant à partir du numéro Twilio appelé (champ Twilio `To`)."""
    if not phone_number:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE phone_number = ?", (phone_number,)
        ).fetchone()
    return _row_to_tenant(row) if row else None


def get_by_id(tenant_id: int) -> Optional[Tenant]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _row_to_tenant(row) if row else None


def list_all() -> list[Tenant]:
    """Tous les tenants (utilisé au démarrage pour pré-rendre les greetings)."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    return [_row_to_tenant(row) for row in rows]


def create_tenant(
    name: str,
    phone_number: str,
    business_type: str = "restaurant",
    language: str = "fr-FR",
    greeting: Optional[str] = None,
    knowledge_base: str = "",
) -> Tenant:
    """Crée un tenant. Lève sqlite3.IntegrityError si le numéro est déjà pris."""
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tenants (name, business_type, phone_number, language, greeting, knowledge_base)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, business_type, phone_number, language, greeting, knowledge_base),
        )
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_tenant(row)


def update_tenant(tenant_id: int, **fields) -> Optional[Tenant]:
    """Met à jour les champs fournis (name, business_type, phone_number, language,
    greeting, knowledge_base, voice, plan). Lève sqlite3.IntegrityError si numéro en
    conflit."""
    allowed = {"name", "business_type", "phone_number", "language", "greeting",
               "knowledge_base", "greeting_customized", "voice", "plan", "opening_hours",
               "notify_email"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_by_id(tenant_id)
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE tenants SET {assignments} WHERE id = ?",
            (*updates.values(), tenant_id),
        )
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _row_to_tenant(row) if row else None


def delete_tenant(tenant_id: int) -> None:
    """Supprime le tenant ET ses données (réservations, appels, messages, comptes) en une
    transaction — la FK reservations.tenant_id n'a pas de CASCADE (table historique), et
    `messages` n'a pas de FK du tout : oubliés, les numéros et noms des appelants
    survivaient à l'établissement, hors de toute durée de conservation.

    Les enregistrements audio partent aussi : ils sont rangés par `tenant<id>`, et un
    établissement créé plus tard pourrait hériter du même identifiant."""
    import shutil

    from .voice import enregistrement

    with db.get_conn() as conn:
        conn.execute("DELETE FROM reservations WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM calls WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM messages WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM users WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
    shutil.rmtree(enregistrement.dossier() / f"tenant{int(tenant_id)}", ignore_errors=True)


def seed_demo_tenant() -> None:
    """Sème le restaurant de démonstration dans une base VIDE, et seulement là.

    Jusqu'au 19/09/2026, il réalignait aussi le numéro (sur TWILIO_NUMBER) et l'accueil
    du tenant démo à chaque démarrage. Or en production le tenant réel EST le tenant
    semé : un numéro changé dans l'admin — l'achat du numéro FR, par exemple — revenait
    à l'ancien au redémarrage suivant, sans un mot, et les appels vers le nouveau numéro
    tombaient sur « numéro non configuré ». L'admin est désormais la SEULE source du
    numéro et de l'accueil d'un établissement existant.

    SEED_DEMO=0 : ne rien semer du tout, même dans une base vide (installation neuve
    pour un vrai client, qui n'a que faire d'un restaurant fictif)."""
    if os.getenv("SEED_DEMO", "1").strip() == "0":
        return
    with db.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]:
            return
        conn.execute(
            """INSERT INTO tenants (name, business_type, phone_number, language, greeting, knowledge_base)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "Le Fouquet's Paris",
                "restaurant",
                DEMO_TENANT_NUMBER,
                "fr-FR",
                _DEMO_GREETING,
                _DEMO_KNOWLEDGE_BASE,
            ),
        )
