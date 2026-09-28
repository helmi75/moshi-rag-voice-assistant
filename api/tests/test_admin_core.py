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
        """Quatre postes, chiffrés sur ce que l'appel a consommé (SCRUM-99, 28/09/2026).

        Le test LIT les constantes du module au lieu de les recopier : un test qui répète
        les tarifs duplique la grille et casse à chaque révision de prix sans avoir rien
        protégé (vécu le 30/08/2026 avec le tarif Deepgram)."""
        forfait = calls._COST_LLM_PER_CALL
        assert calls.estimate_call_cost(0) == pytest.approx(forfait)
        # Une minute : une minute Twilio, une minute de transcription, le forfait modèle.
        une = calls._COST_TWILIO_PER_MIN + calls._COST_DEEPGRAM_PER_MIN + forfait
        assert calls.estimate_call_cost(60) == pytest.approx(une)
        # Twilio facture la minute ENTAMÉE : 61 s = deux minutes de téléphone.
        assert calls.estimate_call_cost(61) - calls.estimate_call_cost(60) == pytest.approx(
            calls._COST_TWILIO_PER_MIN + calls._COST_DEEPGRAM_PER_MIN / 60, abs=1e-6)
        # Une durée négative (horloge qui recule pendant l'appel) ne crée pas d'avoir.
        assert calls.estimate_call_cost(-30) == pytest.approx(forfait)

    def test_la_voix_mistral_se_paie_aux_caracteres_et_le_modele_aux_jetons(self):
        """Appel 204 (28/09/2026) : 199 s, 991 caractères dits. L'ancienne formule y
        comptait 6,6 c de GPU qui n'avait pas tourné."""
        journal = {"voix": {"fournisseur": "voxtral"},
                   "consommation": {"caracteres_voix": 991, "jetons_entree": 40000,
                                    "jetons_cache": 30000, "jetons_sortie": 500,
                                    "generations": 12}}
        c = calls.couts_appel(199, journal)
        assert c["voix"] == pytest.approx(991 * calls._COST_VOIX_PAR_CARACTERE, abs=1e-6)
        assert c["comprehension"] == pytest.approx(
            10000 * calls._COST_LLM_ENTREE + 30000 * calls._COST_LLM_CACHE
            + 500 * calls._COST_LLM_SORTIE, abs=1e-6)
        assert c["telephonie"] == pytest.approx(4 * calls._COST_TWILIO_PER_MIN)
        assert c["fournisseur"] == "voxtral"

    def test_un_appel_du_banc_se_chiffre_sur_sa_duree_active_sans_telephone(self):
        """Appels 82 à 87 du 10/09/2026 : sept heures au compteur, 72 à 88 s de
        conversation au journal — 80 $ fictifs sur 97 dans l'admin."""
        journal = {"voix": {"fournisseur": "moshi"},
                   "evenements": [{"t_ms": 1000}, {"t_ms": 85000}]}
        c = calls.couts_appel(25242, journal, banc=True)
        assert c["telephonie"] == 0
        assert c["transcription"] == pytest.approx(90 / 60 * calls._COST_DEEPGRAM_PER_MIN, abs=1e-6)
        # Un vrai appel, lui, paie sa durée : c'est celle que Twilio facture.
        assert calls.couts_appel(25242, journal)["telephonie"] > 1

    def test_finish_call_range_les_postes_et_leur_somme(self, fresh_db, tenant_id):
        calls.start_call("CA-postes", tenant_id)
        calls.finish_call("CA-postes", "completed", journal={
            "voix": {"fournisseur": "voxtral"},
            "consommation": {"caracteres_voix": 500, "generations": 0}})
        with db.get_conn() as conn:
            r = conn.execute("SELECT * FROM calls WHERE call_sid = 'CA-postes'").fetchone()
        assert r["voix_fournisseur"] == "voxtral"
        assert r["cout_voix"] == pytest.approx(500 * calls._COST_VOIX_PAR_CARACTERE, abs=1e-6)
        assert r["estimated_cost"] == pytest.approx(
            r["cout_telephonie"] + r["cout_transcription"] + r["cout_comprehension"]
            + r["cout_voix"], abs=1e-9)

    def test_la_voix_de_secours_moshi_se_paie_a_la_minute(self):
        c = calls.couts_appel(120, {"voix": {"fournisseur": "moshi"}})
        assert c["voix"] == pytest.approx(2 * calls._COST_MODAL_PER_MIN)

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
