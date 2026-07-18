"""Comptes de la plateforme admin : super-admin (opérateur SaaS) et restaurateurs.

Un restaurateur est rattaché à UN tenant (tenant_id) et ne voit que ses données ;
le super-admin (tenant_id NULL) voit tout. Mots de passe hachés bcrypt.

⚠️ bcrypt est volontairement lent (~100-200 ms) : depuis une route FastAPI, toujours
appeler verify_password/hash_password via asyncio.to_thread pour ne pas geler
l'event loop qui sert les appels vocaux en parallèle.
"""
import os
from dataclasses import dataclass
from typing import Optional

import bcrypt

from . import db

ROLE_SUPERADMIN = "superadmin"
ROLE_RESTAURATEUR = "restaurateur"


@dataclass
class User:
    id: int
    email: str
    password_hash: str
    role: str
    tenant_id: Optional[int]
    created_at: str

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False  # hash corrompu/illisible : refuser sans lever


def _row_to_user(row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        role=row["role"],
        tenant_id=row["tenant_id"],
        created_at=row["created_at"],
    )


def get_by_email(email: str) -> Optional[User]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    return _row_to_user(row) if row else None


def get_by_id(user_id: int) -> Optional[User]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def create_user(email: str, password: str, role: str, tenant_id: Optional[int] = None) -> User:
    if role not in (ROLE_SUPERADMIN, ROLE_RESTAURATEUR):
        raise ValueError(f"rôle inconnu : {role!r}")
    if role == ROLE_RESTAURATEUR and tenant_id is None:
        raise ValueError("un restaurateur doit être rattaché à un tenant")
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, tenant_id) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), hash_password(password), role, tenant_id),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_user(row)


def list_users(tenant_id: Optional[int] = None) -> list[User]:
    with db.get_conn() as conn:
        if tenant_id is None:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY id", (tenant_id,)
            ).fetchall()
    return [_row_to_user(row) for row in rows]


def update_password(user_id: int, new_password: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )


def delete_user(user_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def seed_superadmin() -> None:
    """Crée le compte super-admin au démarrage s'il n'en existe aucun.

    Jamais de mot de passe par défaut : sans ADMIN_PASSWORD, aucun compte n'est créé
    (l'admin est simplement inaccessible, l'app vocale fonctionne normalement)."""
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = ?", (ROLE_SUPERADMIN,)
        ).fetchone()[0]
    if existing:
        return
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        print(
            "[admin] Pas de super-admin et ADMIN_PASSWORD absent : "
            "la plateforme admin restera inaccessible (l'app vocale n'est pas affectée)."
        )
        return
    email = os.getenv("ADMIN_EMAIL", "admin@local")
    create_user(email, password, ROLE_SUPERADMIN)
    print(f"[admin] Super-admin créé : {email} (mot de passe : ADMIN_PASSWORD).")
