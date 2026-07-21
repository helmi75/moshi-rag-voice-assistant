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
