"""Accès SQLite partagé (tenants + réservations)."""
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


def get_conn() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
