"""Notifications au restaurateur (app/notifications.py) : prévenu, sans jamais gêner l'appel.

SMTP entièrement doublé : aucun réseau. Ce qu'on vérifie : l'envoi et ses destinataires,
qu'une panne SMTP ne remonte jamais, que les outils déclenchent bien la notification (et
seulement quand ils ont écrit), et le contenu lisible d'un e-mail."""
import asyncio
import json
import smtplib
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db, llm, notifications, reservations, tenants, users
from app.main import app

APPELANT = "+33612345678"
DEMAIN = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture()
def smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.exemple.fr")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "assistante")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-de-test")
    monkeypatch.setenv("SMTP_FROM", "assistante@exemple.fr")
    monkeypatch.setenv("SMTP_SSL", "0")
    monkeypatch.setenv("PUBLIC_URL", "https://app.exemple.fr")


@pytest.fixture()
def resto(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "notif.db"))
    db.init_db()
    quinze_heures = datetime.combine(date.today(), time(15, 0), tzinfo=llm.FUSEAU)
    monkeypatch.setattr(llm, "maintenant", lambda: quinze_heures)
    tenant = tenants.create_tenant("Chez Notif", "+33199000666")
    tenant = tenants.update_tenant(tenant.id, notify_email="Salle@ChezNotif.fr")
    users.create_user("gerant@cheznotif.fr", "mot-de-passe-long", users.ROLE_RESTAURATEUR, tenant.id)
    users.create_user("salle@cheznotif.fr", "mot-de-passe-long", users.ROLE_RESTAURATEUR, tenant.id)
    return tenant


def _serveur_double(mock_smtp) -> MagicMock:
    return mock_smtp.return_value.__enter__.return_value


class TestDestinataires:
    def test_comptes_puis_adresse_de_notification_sans_doublon(self, resto):
        # salle@ est à la fois un compte et l'adresse de notification : une seule fois.
        assert notifications.destinataires(resto) == ["gerant@cheznotif.fr", "salle@cheznotif.fr"]

    def test_le_super_admin_n_est_jamais_prevenu(self, resto):
        assert "admin@test.local" not in notifications.destinataires(resto)


class TestEnvoi:
    def test_un_e_mail_part_avec_starttls_et_les_destinataires(self, smtp, resto):
        donnees = {"reservation": {"customer_name": "Durand", "date": "2026-08-14", "time": "20:00",
                                   "party_size": 4, "customer_phone": APPELANT, "notes": "terrasse"},
                   "appel_id": 42}
        with patch("smtplib.SMTP") as mock_smtp:
            serveur = _serveur_double(mock_smtp)
            assert asyncio.run(notifications.notifier(resto, "reservation_creee", donnees)) is True
        mock_smtp.assert_called_once()
        assert mock_smtp.call_args.args[:2] == ("smtp.exemple.fr", 587)
        serveur.starttls.assert_called_once()
        serveur.login.assert_called_once_with("assistante", "secret-de-test")
        message = serveur.send_message.call_args.args[0]
        assert message["From"] == "assistante@exemple.fr"
        assert message["To"] == "gerant@cheznotif.fr, salle@cheznotif.fr"
        assert "Nouvelle réservation" in message["Subject"] and "Durand" in message["Subject"]
        corps = message.get_content()
        assert "vendredi 14 août 2026 à 20h00" in corps
        assert "Couverts : 4" in corps and APPELANT in corps and "terrasse" in corps
        assert "https://app.exemple.fr/admin/calls/42" in corps

    def test_tls_implicite_sur_465(self, smtp, resto, monkeypatch):
        monkeypatch.setenv("SMTP_SSL", "1")
        monkeypatch.setenv("SMTP_PORT", "465")
        with patch("smtplib.SMTP_SSL") as mock_ssl, patch("smtplib.SMTP") as mock_plain:
            assert asyncio.run(notifications.notifier(
                resto, "message_pris", {"subject": "Candidature"})) is True
        mock_ssl.assert_called_once()
        mock_plain.assert_not_called()
        _serveur_double(mock_ssl).starttls.assert_not_called()

    def test_sans_destinataire_rien_ne_part(self, smtp, resto):
        vide = tenants.create_tenant("Personne", "+33199000667")
        with patch("smtplib.SMTP") as mock_smtp:
            assert asyncio.run(notifications.notifier(vide, "message_pris", {"subject": "x"})) is False
        mock_smtp.assert_not_called()


class TestNeCasseJamaisLAppel:
    def test_un_smtp_en_panne_ne_leve_pas(self, smtp, resto):
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("connexion refusée")):
            assert asyncio.run(notifications.notifier(
                resto, "message_pris", {"subject": "x"})) is False

    def test_une_erreur_reseau_ne_leve_pas_non_plus(self, smtp, resto):
        with patch("smtplib.SMTP", side_effect=OSError("réseau injoignable")):
            assert asyncio.run(notifications.notifier(
                resto, "message_pris", {"subject": "x"})) is False

    def test_sans_smtp_configure_rien_ne_casse_ni_ne_part(self, resto, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        assert notifications.actif() is False
        with patch("smtplib.SMTP") as mock_smtp:
            assert asyncio.run(notifications.notifier(resto, "message_pris", {"subject": "x"})) is False

            async def scenario():
                return notifications.planifier(resto, "message_pris", {"subject": "x"})

            assert asyncio.run(scenario()) is None
        mock_smtp.assert_not_called()

    def test_planifier_rend_la_main_sans_attendre_le_smtp(self, smtp, resto):
        """Le résultat de l'outil revient au modèle avant que l'e-mail soit parti."""
        with patch("smtplib.SMTP") as mock_smtp:
            async def scenario():
                tache = notifications.planifier(resto, "message_pris", {"subject": "x"})
                assert tache is not None and not tache.done()  # rendu la main, envoi en cours
                await tache
                return tache

            asyncio.run(scenario())
        mock_smtp.assert_called_once()


def _outil(tenant, nom, args, appelant=APPELANT):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, appelant, 7)))


class TestDeclencheurs:
    def test_une_reservation_creee_previent(self, resto):
        with patch.object(notifications, "planifier") as planifier:
            reponse = _outil(resto, "create_reservation",
                             {"customer_name": "Durand", "date": DEMAIN, "time": "20:00", "party_size": 2})
        assert reponse["status"] == "confirmed"
        planifier.assert_called_once()
        tenant, evenement, donnees = planifier.call_args.args
        assert (tenant.id, evenement) == (resto.id, "reservation_creee")
        assert donnees["reservation"]["customer_name"] == "Durand" and donnees["appel_id"] == 7

    def test_une_reservation_refusee_ne_previent_pas(self, resto):
        with patch.object(notifications, "planifier") as planifier:
            _outil(resto, "create_reservation",
                   {"customer_name": "Durand", "date": date.today().isoformat(),
                    "time": "13:00", "party_size": 2})  # déjà passé (il est 15 h)
            _outil(resto, "create_reservation",
                   {"customer_name": "", "date": DEMAIN, "time": "20:00", "party_size": 2})  # sans nom
        planifier.assert_not_called()

    def test_modification_et_annulation_previennent(self, resto):
        mienne = reservations.create_reservation(
            tenant_id=resto.id, customer_name="Dupont", date=DEMAIN, time="20:00",
            party_size=2, customer_phone=APPELANT)
        with patch.object(notifications, "planifier") as planifier:
            _outil(resto, "modify_reservation", {"reservation_id": mienne["id"], "time": "21:00"})
            _outil(resto, "cancel_reservation", {"reservation_id": mienne["id"]})
        evenements = [appel.args[1] for appel in planifier.call_args_list]
        assert evenements == ["reservation_modifiee", "reservation_annulee"]
        modif = planifier.call_args_list[0].args[2]
        assert modif["avant"]["time"] == "20:00" and modif["reservation"]["time"] == "21:00"

    def test_un_message_pris_previent_meme_en_numero_masque(self, resto):
        with patch.object(notifications, "planifier") as planifier:
            reponse = _outil(resto, "take_message",
                             {"subject": "Candidature plongeur", "details": "Disponible le soir"},
                             appelant=None)
        assert reponse["status"] == "recorded"
        _, evenement, donnees = planifier.call_args.args
        assert evenement == "message_pris"
        assert donnees["subject"] == "Candidature plongeur" and donnees["caller_number"] is None


class TestContenu:
    def test_le_message_pris_dit_qui_rappeler(self, resto):
        sujet, corps = notifications.sujet_et_corps("message_pris", resto, {
            "subject": "Demande de groupe", "details": "Vingt personnes samedi",
            "customer_name": "Mme Lopez", "caller_number": APPELANT, "appel_id": 3})
        assert sujet == "Rappel promis — Demande de groupe"
        assert "Vingt personnes samedi" in corps and "Mme Lopez" in corps and APPELANT in corps

    def test_un_numero_masque_est_dit_tel_quel(self, resto):
        _, corps = notifications.sujet_et_corps("message_pris", resto, {"subject": "x", "caller_number": None})
        assert "numéro masqué" in corps

    def test_l_annulation_dit_que_la_table_est_liberee(self, resto):
        sujet, corps = notifications.sujet_et_corps("reservation_annulee", resto, {
            "reservation": {"customer_name": "Dupont", "date": DEMAIN, "time": "20:00", "party_size": 2}})
        assert "annulée" in sujet and "libérée" in corps

    def test_sans_public_url_pas_de_lien(self, resto, monkeypatch):
        monkeypatch.delenv("PUBLIC_URL", raising=False)
        _, corps = notifications.sujet_et_corps("message_pris", resto, {"subject": "x"})
        assert "/admin" not in corps

    def test_un_evenement_inconnu_est_une_erreur_de_programmation(self, resto):
        with pytest.raises(ValueError):
            notifications.sujet_et_corps("autre_chose", resto, {})


def _login(client, email="admin@test.local", password="test-admin-pass"):
    resp = client.post("/admin/login", data={"email": email, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def _csrf(client) -> str:
    client.get("/admin/")
    import base64

    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.b64decode(raw))["csrf"]


class TestAdminAdresseDeNotification:
    def test_enregistree_et_normalisee(self):
        tenant = tenants.create_tenant("Notif Admin", f"+3365{id(object()) % 10_000_000:07d}")
        try:
            client = _login(TestClient(app))
            token = _csrf(client)
            resp = client.post(
                f"/admin/tenants/{tenant.id}",
                data={"name": tenant.name, "phone_number": tenant.phone_number,
                      "business_type": "restaurant", "language": "fr-FR", "greeting": "",
                      "knowledge_base": "", "notify_email": "  Salle@Resto.FR ", "csrf_token": token},
                follow_redirects=False)
            assert resp.status_code == 303
            assert tenants.get_by_id(tenant.id).notify_email == "salle@resto.fr"
        finally:
            tenants.delete_tenant(tenant.id)

    def test_une_adresse_fausse_est_refusee(self):
        tenant = tenants.create_tenant("Notif Admin 2", f"+3366{id(object()) % 10_000_000:07d}")
        try:
            client = _login(TestClient(app))
            token = _csrf(client)
            resp = client.post(
                f"/admin/tenants/{tenant.id}",
                data={"name": tenant.name, "phone_number": tenant.phone_number,
                      "business_type": "restaurant", "language": "fr-FR", "greeting": "",
                      "knowledge_base": "", "notify_email": "salle-sans-arobase", "csrf_token": token})
            assert resp.status_code == 422
            assert "adresse valide" in resp.text
            assert tenants.get_by_id(tenant.id).notify_email is None
        finally:
            tenants.delete_tenant(tenant.id)
