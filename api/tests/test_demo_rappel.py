"""Démo 5 (ASSISTANTE-145) : en rendez-vous, le gérant est rappelé sur son restaurant d'essai.

Aucun réseau : Twilio (`rappel._composer`) et la synthèse de l'accueil sont des doublures. On
vérifie surtout que l'appel ne peut servir QU'un établissement d'essai d'une démonstration
prête, que la route lue par Twilio est signée, et que le gérant entend son restaurant."""
import base64
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db, demos, rappel, tenants, twilio_signature, users
from app.main import app
from app.voice import greeting

LIGNE = "+33900000001"


@pytest.fixture()
def actives(monkeypatch):
    monkeypatch.setenv("DEMOS_ACTIVES", "1")
    monkeypatch.setenv("DEMO_LIGNE", LIGNE)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")
    demos.oublier_les_rappels()
    yield
    demos.oublier_les_rappels()
    with db.get_conn() as conn:
        for (t,) in conn.execute("SELECT tenant_id FROM demonstrations WHERE tenant_id IS NOT NULL").fetchall():
            tenants.delete_tenant(t)
        conn.execute("DELETE FROM demonstrations")


@pytest.fixture()
def composer(monkeypatch):
    faux = AsyncMock(return_value="CA" + "7" * 32)
    monkeypatch.setattr(rappel, "_composer", faux)
    monkeypatch.setattr(greeting, "ensure_greeting_wav", AsyncMock())
    return faux


def _demo_prete(nom: str = "Chez Marcel") -> int:
    identifiant = demos.creer(nom, "Lyon")
    demos.enregistrer_relecture(identifiant, {})
    demos.valider(identifiant)
    return identifiant


def _faire_rappeler(demo_id: int, numero: str = "06 12 34 56 78"):
    import asyncio
    return asyncio.run(demos.faire_rappeler(demo_id, numero))


class TestLeBouton:
    def test_le_gerant_est_appele_sur_la_route_de_sa_demonstration(self, actives, composer):
        d = _demo_prete()
        parti, message = _faire_rappeler(d)
        assert parti and "06 12 34 56 78" in message
        composer.assert_awaited_once_with("+33612345678", LIGNE, f"/twilio/demo?d={d}")

    def test_une_demonstration_pas_encore_validee_n_appelle_personne(self, actives, composer):
        d = demos.creer("Chez Marcel", "Lyon")
        parti, message = _faire_rappeler(d)
        assert not parti and "Créez d'abord" in message
        composer.assert_not_awaited()

    def test_un_numero_hors_metropole_est_refuse(self, actives, composer):
        parti, _ = _faire_rappeler(_demo_prete(), "08 99 00 00 00")
        assert not parti
        composer.assert_not_awaited()

    def test_sans_ligne_presentee_le_bouton_dit_ce_qui_manque(self, actives, composer, monkeypatch):
        monkeypatch.setenv("DEMO_LIGNE", "")
        parti, message = _faire_rappeler(_demo_prete())
        assert not parti and "DEMO_LIGNE" in message
        composer.assert_not_awaited()

    def test_sans_l_interrupteur_rien_ne_part(self, actives, composer, monkeypatch):
        d = _demo_prete()
        monkeypatch.setenv("DEMOS_ACTIVES", "")
        parti, _ = _faire_rappeler(d)
        assert not parti
        composer.assert_not_awaited()

    def test_le_plafond_du_jour_borne_la_facture(self, actives, composer, monkeypatch):
        monkeypatch.setenv("DEMO_RAPPELS_PAR_JOUR", "2")
        monkeypatch.setattr(demos, "ENTRE_DEUX_RAPPELS_SECONDES", 0.0)
        d = _demo_prete()
        assert _faire_rappeler(d)[0] and _faire_rappeler(d)[0]
        parti, message = _faire_rappeler(d)
        assert not parti and "Plafond" in message
        assert composer.await_count == 2

    def test_twilio_qui_refuse_ne_montre_pas_d_erreur_brute(self, actives, composer):
        composer.side_effect = RuntimeError("400 pour +33612345678")
        parti, message = _faire_rappeler(_demo_prete())
        assert not parti and "+33612345678" not in message

    def test_un_appel_que_twilio_refuse_ne_compte_pas_dans_le_plafond(self, actives, composer, monkeypatch):
        monkeypatch.setenv("DEMO_RAPPELS_PAR_JOUR", "1")
        d = _demo_prete()
        composer.side_effect = RuntimeError("503")
        assert not _faire_rappeler(d)[0]
        composer.side_effect = None
        assert _faire_rappeler(d)[0]   # la place est rendue, et pas de délai après un échec

    def test_un_double_clic_ne_fait_pas_sonner_deux_fois(self, actives, composer):
        d = _demo_prete()
        assert _faire_rappeler(d)[0]
        parti, message = _faire_rappeler(d)
        assert not parti and "vient de partir" in message
        assert composer.await_count == 1
        assert _faire_rappeler(_demo_prete("Autre"))[0]   # une autre démonstration, elle, part


def _signer(url: str, data: dict) -> str:
    charge = url + "".join(k + str(v) for k, v in sorted(data.items()))
    return base64.b64encode(
        hmac.new(b"test-auth-token", charge.encode("utf-8"), hashlib.sha1).digest()).decode()


DONNEES = {"CallSid": "CA" + "8" * 32, "To": "+33612345678", "From": LIGNE, "Direction": "outbound-api"}


@pytest.fixture()
def client():
    return TestClient(app)


class TestQuandLeGerantDecroche:
    def test_le_flux_porte_l_etablissement_d_essai_et_ne_renvoie_nulle_part(self, client, actives):
        d = _demo_prete()
        tenant = demos.etablissement_a_rappeler(d)
        r = client.post(f"/twilio/demo?d={d}", data=DONNEES)
        assert r.status_code == 200
        assert f'<Parameter name="To" value="{tenant.phone_number}"/>' in r.text
        assert '<Parameter name="RendezVous" value="1"/>' in r.text
        assert '<Parameter name="From" value="+33612345678"/>' in r.text
        assert "Rappel" not in r.text and "<Redirect" not in r.text and "<Hangup/>" in r.text

    def test_un_vrai_etablissement_n_est_jamais_servi(self, client, actives):
        """Une démonstration dont la ligne désignerait un établissement réel (base abîmée,
        reprise à la main) : l'appel raccroche, il n'écrit pas dans un vrai carnet."""
        d = _demo_prete()
        essai = demos.obtenir(d)["tenant_id"]
        vrai = tenants.create_tenant("Vrai Restaurant", "+33111111188")
        try:
            with db.get_conn() as conn:
                conn.execute("UPDATE demonstrations SET tenant_id = ? WHERE id = ?", (vrai.id, d))
            assert demos.etablissement_a_rappeler(d) is None
            r = client.post(f"/twilio/demo?d={d}", data=DONNEES)
            assert "<Hangup/>" in r.text and "<Stream" not in r.text
        finally:
            tenants.delete_tenant(vrai.id)
            tenants.delete_tenant(essai)

    def test_une_demonstration_effacee_ou_inconnue_raccroche(self, client, actives):
        d = _demo_prete()
        demos.effacer(d)
        for adresse in (f"/twilio/demo?d={d}", "/twilio/demo?d=999999", "/twilio/demo"):
            r = client.post(adresse, data=DONNEES)
            assert "<Hangup/>" in r.text and "<Stream" not in r.text

    def test_sans_l_interrupteur_la_route_raccroche(self, client, actives, monkeypatch):
        d = _demo_prete()
        monkeypatch.setenv("DEMOS_ACTIVES", "")
        assert "<Stream" not in client.post(f"/twilio/demo?d={d}", data=DONNEES).text

    def test_la_route_exige_la_signature_de_l_adresse_entiere(self, client, actives, monkeypatch):
        d = _demo_prete()
        monkeypatch.setenv("TWILIO_SIGNATURE", "enforce")
        twilio_signature.remettre_a_zero()
        assert client.post(f"/twilio/demo?d={d}", data=DONNEES).status_code == 403
        # Signée pour une autre démonstration : la signature couvre le paramètre.
        autre = _signer(f"http://testserver/twilio/demo?d={d + 1}", DONNEES)
        assert client.post(f"/twilio/demo?d={d}", data=DONNEES,
                           headers={"X-Twilio-Signature": autre}).status_code == 403
        bonne = _signer(f"http://testserver/twilio/demo?d={d}", DONNEES)
        r = client.post(f"/twilio/demo?d={d}", data=DONNEES, headers={"X-Twilio-Signature": bonne})
        assert r.status_code == 200 and "RendezVous" in r.text

    def test_le_gerant_entend_son_restaurant_et_l_appel_est_note_sortant(self, client, actives):
        d = _demo_prete("Chez Marcel")
        tenant = demos.etablissement_a_rappeler(d)
        custom = {"To": tenant.phone_number, "From": "+33612345678", "CallSid": "CA-rdv-1",
                  "RendezVous": "1"}
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot), \
                patch("app.voice.live.actif", return_value=False):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(json.dumps({
                    "event": "start", "streamSid": "MZ-rdv",
                    "start": {"streamSid": "MZ-rdv", "callSid": "CA-rdv-1", "customParameters": custom}}))
                fin = time.monotonic() + 2.0
                while run_bot.await_count == 0 and time.monotonic() < fin:
                    time.sleep(0.01)
        servi = run_bot.await_args.args[3]
        assert servi.id == tenant.id and tenants.est_demo(servi)
        assert servi.greeting == "Bonjour, Chez Marcel." and "Marie" not in servi.greeting
        # Il joue un client du restaurant : pas la consigne du rappel demandé sur le site.
        assert run_bot.await_args.kwargs["demonstration"] is False
        with db.get_conn() as conn:
            ligne = conn.execute("SELECT sortant, caller_number FROM calls WHERE call_sid = 'CA-rdv-1'").fetchone()
        assert ligne["sortant"] == 1 and ligne["caller_number"] == "+33612345678"

    def test_rendez_vous_sur_un_vrai_numero_n_est_pas_cru(self, client, actives):
        """Le paramètre ne vient que d'un TwiML signé ; le serveur le recoupe quand même."""
        vrai = tenants.create_tenant("Vrai Restaurant", "+33111111187")
        try:
            custom = {"To": vrai.phone_number, "From": "+33612345678", "CallSid": "CA-rdv-2",
                      "RendezVous": "1"}
            run_bot = AsyncMock()
            with patch("app.main._get_bot_runner", return_value=run_bot), \
                    patch("app.voice.live.actif", return_value=False):
                with client.websocket_connect("/ws/voice") as ws:
                    ws.send_text(json.dumps({"event": "connected"}))
                    ws.send_text(json.dumps({
                        "event": "start", "streamSid": "MZ-rdv2",
                        "start": {"streamSid": "MZ-rdv2", "callSid": "CA-rdv-2", "customParameters": custom}}))
                    fin = time.monotonic() + 2.0
                    while run_bot.await_count == 0 and time.monotonic() < fin:
                        time.sleep(0.01)
            with db.get_conn() as conn:
                ligne = conn.execute("SELECT sortant FROM calls WHERE call_sid = 'CA-rdv-2'").fetchone()
            assert ligne["sortant"] is None
        finally:
            tenants.delete_tenant(vrai.id)


def _connecter(client, email=None, mdp=None):
    r = client.post("/admin/login", data={"email": email or os.environ["ADMIN_EMAIL"],
                                          "password": mdp or os.environ["ADMIN_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 303
    jeton = client.get("/admin/").text.split('"X-CSRF-Token": "')[1].split('"')[0]
    return {"X-CSRF-Token": jeton}


class TestLaPage:
    def test_le_super_admin_fait_rappeler_le_gerant(self, client, actives, composer):
        d = _demo_prete()
        entete = _connecter(client)
        page = client.get(f"/admin/demos/{d}").text
        assert "Faire essayer le gérant" in page and 'action="/admin/demos/' in page
        r = client.post(f"/admin/demos/{d}/rappeler", headers=entete, data={"numero": "06 12 34 56 78"})
        assert r.status_code == 200 and "Appel lancé" in r.text and "va sonner" in r.text
        composer.assert_awaited_once()

    def test_un_numero_refuse_revient_en_422_sans_appel(self, client, actives, composer):
        d = _demo_prete()
        entete = _connecter(client)
        r = client.post(f"/admin/demos/{d}/rappeler", headers=entete, data={"numero": "12"})
        assert r.status_code == 422 and "Pas d'appel" in r.text
        composer.assert_not_awaited()

    def test_sans_configuration_le_bouton_dit_ce_qui_manque(self, client, actives, monkeypatch):
        monkeypatch.setenv("DEMO_LIGNE", "")
        d = _demo_prete()
        _connecter(client)
        page = client.get(f"/admin/demos/{d}").text
        assert "Indisponible" in page and "DEMO_LIGNE" in page and "Le faire rappeler" not in page

    def test_sans_csrf_ou_sans_interrupteur_rien_ne_part(self, client, actives, composer, monkeypatch):
        d = _demo_prete()
        entete = _connecter(client)
        assert client.post(f"/admin/demos/{d}/rappeler", data={"numero": "0612345678"}).status_code == 403
        monkeypatch.setenv("DEMOS_ACTIVES", "")
        assert client.post(f"/admin/demos/{d}/rappeler", headers=entete,
                           data={"numero": "0612345678"}).status_code == 404
        composer.assert_not_awaited()

    def test_un_restaurateur_ne_peut_pas_faire_appeler(self, client, actives, composer):
        d = _demo_prete()
        resto = tenants.create_tenant("Resto Rappel", "+33111111189")
        try:
            users.create_user("resto-rappel@test.local", "mot-de-passe-1", "restaurateur", resto.id)
            entete = _connecter(client, "resto-rappel@test.local", "mot-de-passe-1")
            r = client.post(f"/admin/demos/{d}/rappeler", headers=entete, data={"numero": "0612345678"})
            assert r.status_code == 403
            composer.assert_not_awaited()
        finally:
            tenants.delete_tenant(resto.id)
