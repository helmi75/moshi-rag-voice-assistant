"""Accès SQLite partagé (tenants, réservations, comptes admin, journal des appels).

Migrations : `PRAGMA user_version` sert de compteur ; chaque script de _MIGRATIONS est
appliqué une seule fois, dans l'ordre, et reste idempotent (IF NOT EXISTS) en double
sécurité — une base déjà migrée à la main ne casse pas.
"""
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "./data/app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL DEFAULT 'restaurant',
    phone_number TEXT UNIQUE NOT NULL,
    language TEXT NOT NULL DEFAULT 'fr-FR',
    greeting TEXT,
    knowledge_base TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    customer_name TEXT NOT NULL,
    customer_phone TEXT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

# Migrations versionnées (v1 = plateforme admin : comptes + journal des appels).
_MIGRATIONS: list[str] = [
    # v1 — users (super-admin + restaurateurs) et calls (journal des appels).
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('superadmin', 'restaurateur')),
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    call_sid TEXT UNIQUE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    transcript TEXT,
    summary TEXT,
    reservation_id INTEGER REFERENCES reservations(id) ON DELETE SET NULL,
    estimated_cost REAL
);

CREATE INDEX IF NOT EXISTS idx_calls_tenant_started ON calls(tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_reservations_tenant ON reservations(tenant_id);
""",
    # v2 — greeting_customized : marque un accueil personnalisé par le restaurateur.
    # seed_demo_tenant réaligne l'accueil du tenant démo sur le défaut à chaque démarrage ;
    # sans ce flag, cela ÉCRASE l'accueil qu'un client a personnalisé dans l'admin.
    """
ALTER TABLE tenants ADD COLUMN greeting_customized INTEGER NOT NULL DEFAULT 0;
""",
    # v3 — caller_number : le numéro de l'appelant, connu dès l'ouverture du stream
    # (customParameters.From) mais jusqu'ici jeté. L'admin ne pouvait donc pas dire QUI
    # avait appelé, sauf si l'appel avait débouché sur une réservation.
    """
ALTER TABLE calls ADD COLUMN caller_number TEXT;
""",
    # v4 — turn_latencies : les blancs ressentis par l'appelant (JSON, en ms), mesurés
    # tour par tour. Sans ça, diagnostiquer une lenteur imposait de lire les journaux
    # du conteneur — qui repartent de zéro à CHAQUE déploiement, donc l'appel qu'on
    # veut analyser a déjà disparu. C'est aussi la seule source honnête pour afficher
    # une latence dans l'admin.
    """
ALTER TABLE calls ADD COLUMN turn_latencies TEXT;
""",
    # v5 — voice : la voix de l'assistante, par établissement. Elle n'existait que comme
    # variable d'environnement globale, donc tout le parc parlait de la même voix. NULL
    # = « la voix par défaut du parc », ce qui laisse MOSHI_TTS_VOICE piloter les
    # établissements qui n'ont rien choisi (cf. voice/voices.py:resolve).
    """
ALTER TABLE tenants ADD COLUMN voice TEXT;
""",
    # v6 — supervision : ardoise clé/valeur du module de supervision. Elle porte les
    # constats qui ne peuvent pas être calculés dans la sonde elle-même (la relève des
    # alertes Twilio, qui exige un appel réseau) et sert accessoirement de test
    # d'écriture réel : lire prouve que la base répond, pas qu'elle accepte encore une
    # réservation — un disque plein laisse passer les SELECT.
    """
CREATE TABLE IF NOT EXISTS supervision (
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL,
    maj_le TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
""",
    # v7 — plan : la formule commerciale de l'établissement (#31). NULL = la formule par
    # défaut du catalogue, ce qui laisse le parc existant fonctionner sans qu'on ait à
    # lui attribuer un plan avant qu'une facturation existe. Comme pour `voice`, une
    # valeur hors catalogue est ignorée par app/plans.py:resolve — un plafond doit
    # toujours venir d'une formule que quelqu'un a réellement vendue.
    """
ALTER TABLE tenants ADD COLUMN plan TEXT;
""",
    # v8 — cancelled_at : une annulation par téléphone (#33) n'efface pas la ligne, elle
    # l'horodate. Supprimer ferait disparaître la preuve le jour où un client affirme
    # avoir annulé et où le restaurant a gardé la table — c'est précisément le litige
    # qu'une annulation automatisée rend possible. NULL = réservation active ; toute
    # requête qui COMPTE des couverts doit exclure les annulées, sinon la salle paraît
    # pleine alors qu'elle ne l'est pas.
    """
ALTER TABLE reservations ADD COLUMN cancelled_at TEXT;
""",
]


def get_conn() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + busy_timeout : le dashboard admin lit pendant qu'un appel écrit — sans
    # ça, SQLite renvoie « database is locked » sous accès concurrent.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {i}")
