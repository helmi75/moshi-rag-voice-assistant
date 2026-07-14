"""Tenants (les commerces clients du SaaS) et routage par numéro appelé."""
import os
from dataclasses import dataclass
from typing import Optional

from . import db

DEMO_TENANT_NUMBER = os.getenv("TWILIO_NUMBER", "+33100000000")

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


def _row_to_tenant(row) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        business_type=row["business_type"],
        phone_number=row["phone_number"],
        language=row["language"],
        greeting=row["greeting"] or f"Bonjour, {row['name']}, que puis-je faire pour vous ?",
        knowledge_base=row["knowledge_base"],
    )


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


def seed_demo_tenant() -> None:
    """Crée le restaurant de démonstration si absent, et garde son numéro aligné
    sur TWILIO_NUMBER.

    Le numéro est réaligné à chaque démarrage : sans ça, un premier démarrage avec
    un mauvais TWILIO_NUMBER (ou le défaut) fige le numéro dans le volume Docker et
    tous les appels tombent sur « numéro non configuré ». Pour ne pas écraser une
    vraie prod, on ne sème rien si d'autres tenants existent déjà."""
    with db.get_conn() as conn:
        demo = conn.execute(
            "SELECT id, phone_number FROM tenants WHERE name = ? AND business_type = ?",
            ("Le Fouquet's Paris", "restaurant"),
        ).fetchone()
        if demo is not None:
            if DEMO_TENANT_NUMBER and demo["phone_number"] != DEMO_TENANT_NUMBER:
                conn.execute(
                    "UPDATE tenants SET phone_number = ? WHERE id = ?",
                    (DEMO_TENANT_NUMBER, demo["id"]),
                )
            return
        # Pas de tenant démo : ne semer que si la base est vide (jamais en prod).
        count = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
        if count:
            return
        conn.execute(
            """INSERT INTO tenants (name, business_type, phone_number, language, greeting, knowledge_base)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "Le Fouquet's Paris",
                "restaurant",
                DEMO_TENANT_NUMBER,
                "fr-FR",
                "Bonjour, restaurant Le Fouquet's Paris, que puis-je faire pour vous ?",
                _DEMO_KNOWLEDGE_BASE,
            ),
        )
