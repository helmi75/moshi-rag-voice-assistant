"""Étape 0 de la plateforme admin : migrations, users (bcrypt), calls (journal/stats)."""
import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from app import calls, db, users


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
class TestMigrations:
    def test_fresh_db_reaches_latest_version(self, tmp_path):
        with patch.object(db, "DB_PATH", str(tmp_path / "fresh.db")):
            db.init_db()
            with db.get_conn() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        assert version == len(db._MIGRATIONS)
        assert {"tenants", "reservations", "users", "calls"} <= tables

    def test_legacy_db_is_migrated(self, tmp_path):
        """Une base d'AVANT les migrations (user_version=0, sans users/calls) migre."""
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(db._SCHEMA)  # ancienne base : tables historiques seulement
        conn.close()
        with patch.object(db, "DB_PATH", path):
            db.init_db()
            with db.get_conn() as conn:
                tables = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        assert {"users", "calls"} <= tables

    def test_init_db_idempotent(self, tmp_path):
        with patch.object(db, "DB_PATH", str(tmp_path / "twice.db")):
            db.init_db()
            db.init_db()  # ne doit pas lever
            with db.get_conn() as conn:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
                    db._MIGRATIONS
                )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@pytest.fixture()
def fresh_db(tmp_path):
    """Base isolée par test (patch DB_PATH partout où il est lu)."""
    path = str(tmp_path / "test.db")
    with patch.object(db, "DB_PATH", path):
        db.init_db()
        yield path


@pytest.fixture()
def tenant_id(fresh_db):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, business_type, phone_number) VALUES (?, ?, ?)",
            ("Resto Test", "restaurant", "+33199999999"),
        )
        return cur.lastrowid


class TestUsers:
    def test_password_hash_roundtrip(self):
        hashed = users.hash_password("s3cret!")
        assert hashed != "s3cret!"
        assert users.verify_password("s3cret!", hashed)
        assert not users.verify_password("wrong", hashed)
        assert not users.verify_password("s3cret!", "pas-un-hash")

    def test_create_and_get(self, fresh_db):
        user = users.create_user("Admin@Example.COM", "pw", users.ROLE_SUPERADMIN)
        assert user.email == "admin@example.com"  # normalisé
        assert user.is_superadmin
        assert users.get_by_email("admin@example.com").id == user.id
        assert users.get_by_id(user.id).email == user.email

    def test_restaurateur_requires_tenant(self, fresh_db):
        with pytest.raises(ValueError):
            users.create_user("r@x.fr", "pw", users.ROLE_RESTAURATEUR)

    def test_restaurateur_scoped_listing(self, fresh_db, tenant_id):
        users.create_user("sa@x.fr", "pw", users.ROLE_SUPERADMIN)
        users.create_user("r@x.fr", "pw", users.ROLE_RESTAURATEUR, tenant_id)
        assert len(users.list_users()) == 2
        scoped = users.list_users(tenant_id)
        assert [u.email for u in scoped] == ["r@x.fr"]

    def test_update_password_and_delete(self, fresh_db):
        user = users.create_user("a@x.fr", "old", users.ROLE_SUPERADMIN)
        users.update_password(user.id, "new")
        assert users.verify_password("new", users.get_by_id(user.id).password_hash)
        users.delete_user(user.id)
        assert users.get_by_id(user.id) is None

    def test_seed_superadmin_from_env(self, fresh_db):
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "boot-pw", "ADMIN_EMAIL": "boss@x.fr"}):
            users.seed_superadmin()
            users.seed_superadmin()  # idempotent
        found = users.list_users()
        assert len(found) == 1 and found[0].email == "boss@x.fr"

    def test_seed_superadmin_without_password_does_nothing(self, fresh_db):
        with patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
            users.seed_superadmin()
        assert users.list_users() == []


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------
class TestCalls:
    def test_start_and_finish_call(self, fresh_db, tenant_id):
        calls.start_call("CA123", tenant_id)
        calls.start_call("CA123", tenant_id)  # doublon webhook : silencieux
        transcript = [{"role": "user", "content": "Bonjour"}]
        calls.finish_call("CA123", "completed", transcript, reservation_id=None)

        rows = calls.list_calls(tenant_id)
        assert len(rows) == 1
        call = rows[0]
        assert call["status"] == "completed"
        assert call["duration_seconds"] >= 0
        assert call["estimated_cost"] is not None and call["estimated_cost"] > 0
        assert json.loads(call["transcript"]) == transcript

    def test_finish_unknown_call_is_noop(self, fresh_db):
        calls.finish_call("CA-inconnu")  # ne doit pas lever

    def test_list_scoping_and_pagination(self, fresh_db, tenant_id):
        with db.get_conn() as conn:
            other = conn.execute(
                "INSERT INTO tenants (name, business_type, phone_number) VALUES ('B','restaurant','+33188888888')"
            ).lastrowid
        for i in range(3):
            calls.start_call(f"CA-a-{i}", tenant_id)
        calls.start_call("CA-b-0", other)

        assert calls.count_calls() == 4
        assert calls.count_calls(tenant_id) == 3
        assert len(calls.list_calls(tenant_id, limit=2)) == 2
        assert all(c["tenant_id"] == other for c in calls.list_calls(other))

    def test_estimate_cost_formula(self):
        assert calls.estimate_call_cost(0) == pytest.approx(0.0035)
        one_min = calls.estimate_call_cost(60)
        assert one_min == pytest.approx(0.0085 + 0.0058 + 0.02 + 0.0035)

    def test_stats_daily(self, fresh_db, tenant_id):
        calls.start_call("CA-1", tenant_id)
        with db.get_conn() as conn:
            resa = conn.execute(
                """INSERT INTO reservations (tenant_id, customer_name, date, time, party_size)
                   VALUES (?, 'X', '2026-07-20', '20:00', 2)""",
                (tenant_id,),
            ).lastrowid
        calls.finish_call("CA-1", "completed", None, reservation_id=resa)
        calls.start_call("CA-2", tenant_id)
        calls.finish_call("CA-2", "completed")

        stats = calls.stats_daily(tenant_id, days=7)
        assert len(stats) == 1  # tout aujourd'hui
        today = stats[0]
        assert today["n_calls"] == 2
        assert today["n_with_reservation"] == 1
        assert today["n_reservations"] == 1
        assert today["total_cost"] > 0

    def test_stats_daily_scoped(self, fresh_db, tenant_id):
        with db.get_conn() as conn:
            other = conn.execute(
                "INSERT INTO tenants (name, business_type, phone_number) VALUES ('B','restaurant','+33177777777')"
            ).lastrowid
        calls.start_call("CA-x", other)
        assert calls.stats_daily(tenant_id, days=7) == []
        assert len(calls.stats_daily(other, days=7)) == 1
