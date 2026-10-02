"""« Rappelez-moi » : Marie appelle le numéro laissé sur le site (ASSISTANTE-119).

C'est la seule route du produit qui fait composer un numéro à la demande d'un inconnu.
On vérifie donc surtout ce qu'elle REFUSE : les numéros qu'on ne compose pas, les
plafonds par numéro, par adresse et par jour, les heures, une demande venue d'un autre
site. Puis ce qui se passe quand la personne décroche : un flux marqué « démonstration »,
l'accueil du rappel, un appel chiffré au tarif sortant.

Twilio n'est jamais appelé : `rappel._composer` est remplacé.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import calls, db, horloge, llm, rappel, rgpd, tenants, twilio_signature
from app.main import app

DEMO_NUMBER = "+33100000000"
MIDI = datetime(2026, 10, 2, 12, 0, tzinfo=horloge.FUSEAU)
JSON = {"Content-Type": "application/json"}


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _rappel_pret(monkeypatch):
    """Le rappel est configuré, la table est vide, il est midi, et Twilio est un bouchon."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("PUBLIC_URL", "http://testserver")
    for nom in ("RAPPEL_ACTIF", "RAPPEL_ETABLISSEMENT", "RAPPEL_MAX_PAR_JOUR", "RAPPEL_HEURES",
                "RAPPEL_PAR_NUMERO_PAR_JOUR", "RAPPEL_PAR_ADRESSE_PAR_HEURE", "SMTP_HOST"):
        monkeypatch.delenv(nom, raising=False)
    monkeypatch.setattr(horloge, "maintenant", lambda: MIDI)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rappels")
    rappel.reinitialiser()
    yield
    rappel.reinitialiser()


@pytest.fixture()
def composer():
    with patch("app.rappel._composer", new=AsyncMock(return_value="CA" + "1" * 32)) as bouchon:
        yield bouchon


def _demandes() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM rappels ORDER BY id")]


def _appel(call_sid: str) -> dict:
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone())


def _demander(numero: str, adresse: str = "203.0.113.7") -> rappel.Reponse:
    return asyncio.run(rappel.demander(numero, adresse))


class TestLesNumerosQuOnCompose:
    @pytest.mark.parametrize("brut,attendu", [
        ("06 12 34 56 78", "+33612345678"),
        ("0612345678", "+33612345678"),
        ("06.12.34.56.78", "+33612345678"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("0033612345678", "+33612345678"),
        ("01 42 68 53 00", "+33142685300"),
        ("09 72 10 10 10", "+33972101010"),
        ("07 81 23 45 67", "+33781234567"),
    ])
    def test_un_numero_de_metropole_passe(self, brut, attendu):
        assert rappel.normaliser(brut) == attendu

    @pytest.mark.parametrize("brut", [
        "08 99 12 34 56",      # surtaxé
        "0 800 123 456",       # numéro vert : 08 aussi
        "3631", "15", "112",   # numéros courts
        "06 12 34 56",         # incomplet
        "06 12 34 56 78 9",    # trop long
        "+44 20 7946 0958",    # étranger
        "+1 415 555 0100",
        "02 62 12 34 56",      # La Réunion : un autre indicatif
        "06 92 12 34 56",      # portable de La Réunion
        "05 90 12 34 56",      # Guadeloupe
        "06 39 12 34 56",      # Mayotte
        "07 00 12 34 56",      # plage outre-mer / machines
        "", None, "abc", "06 12 34 56 7a", "²6 12 34 56 78",
    ])
    def test_tout_autre_numero_est_refuse(self, brut):
        assert rappel.normaliser(brut) is None

    def test_un_numero_refuse_ne_declenche_aucun_appel(self, composer):
        reponse = _demander("08 99 12 34 56")
        assert reponse is rappel.NUMERO and reponse.code == 422
        composer.assert_not_awaited()
        assert _demandes() == []

    def test_lisible(self):
        assert rappel.lisible("+33612345678") == "06 12 34 56 78"


class TestUneDemandeAcceptee:
    def test_twilio_compose_le_numero_en_presentant_la_ligne_de_demonstration(self, composer):
        reponse = _demander("06 12 34 56 78")
        assert reponse is rappel.OK and reponse.code == 200
        composer.assert_awaited_once_with("+33612345678", DEMO_NUMBER)
        (demande,) = _demandes()
        assert demande["numero"] == "+33612345678"
        assert demande["statut"] == rappel.LANCE
        assert demande["call_sid"] == "CA" + "1" * 32

    def test_la_commande_envoyee_a_twilio(self, monkeypatch):
        """L'adresse de la suite, la sonnerie et la durée maximale : c'est Twilio qui
        coupe un appel trop long, pas nous."""
        monkeypatch.setenv("RAPPEL_DUREE_MAX_SECONDES", "180")
        envoye = {}

        class Reponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"sid": "CA" + "2" * 32}

        class Client:
            def __init__(self, **options):
                envoye["auth"] = options.get("auth")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, adresse, data=None):
                envoye.update(adresse=adresse, data=data)
                return Reponse()

        monkeypatch.setattr(rappel.httpx, "AsyncClient", Client)
        sid = asyncio.run(rappel._composer("+33612345678", DEMO_NUMBER))
        assert sid == "CA" + "2" * 32
        assert envoye["adresse"].endswith("/Accounts/AC" + "0" * 32 + "/Calls.json")
        assert envoye["data"] == {
            "To": "+33612345678", "From": DEMO_NUMBER, "Method": "POST",
            "Url": "http://testserver/twilio/rappel",
            "Timeout": str(rappel.SONNERIE_SECONDES), "TimeLimit": "180"}

    def test_l_etablissement_de_demonstration_peut_etre_choisi(self, composer, monkeypatch):
        autre = tenants.create_tenant("Le Bouchon Doré", "+33111111119",
                                      greeting="Bonjour, Le Bouchon Doré.")
        try:
            monkeypatch.setenv("RAPPEL_ETABLISSEMENT", str(autre.id))
            assert _demander("06 12 34 56 78") is rappel.OK
            composer.assert_awaited_once_with("+33612345678", "+33111111119")
        finally:
            tenants.delete_tenant(autre.id)

    def test_helmane_est_prevenu_par_e_mail(self, composer, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        monkeypatch.setenv("SMTP_FROM", "marie@test.local")
        # `envoyer` rend ses arguments au lieu d'une coroutine : on lit ce qui serait parti.
        with patch("app.rappel.taches.lancer") as lancer, \
                patch("app.rappel.notifications.envoyer", new=lambda *a: a):
            _demander("06 12 34 56 78")
        adresses, sujet, corps = lancer.call_args.args[0]
        assert adresses == ["admin@test.local"]
        assert "rappel" in sujet.lower() and "06 12 34 56 78" in corps

    def test_sans_smtp_personne_n_est_prevenu_et_l_appel_part(self, composer):
        with patch("app.rappel.taches.lancer") as lancer:
            assert _demander("06 12 34 56 78") is rappel.OK
        lancer.assert_not_called()


class TestLesPlafonds:
    def test_un_numero_n_est_pas_rappele_plus_de_deux_fois_par_jour(self, composer):
        assert _demander("06 12 34 56 78", "203.0.113.1") is rappel.OK
        assert _demander("06 12 34 56 78", "203.0.113.2") is rappel.OK
        troisieme = _demander("+33612345678", "203.0.113.3")
        assert troisieme is rappel.DEJA and troisieme.code == 429
        assert composer.await_count == 2
        # Un autre numéro, lui, passe toujours.
        assert _demander("06 98 76 54 32", "203.0.113.4") is rappel.OK

    def test_le_plafond_par_numero_se_leve_au_bout_de_24_heures(self, composer):
        _demander("06 12 34 56 78", "203.0.113.1")
        _demander("06 12 34 56 78", "203.0.113.2")
        with db.get_conn() as conn:
            conn.execute("UPDATE rappels SET created_at = ?", (horloge.il_y_a(1.1),))
        assert _demander("06 12 34 56 78", "203.0.113.3") is rappel.OK

    def test_une_adresse_ne_demande_pas_plus_de_trois_rappels_par_heure(self, composer):
        for i in range(3):
            assert _demander(f"06 12 34 56 7{i}") is rappel.OK
        quatrieme = _demander("06 12 34 56 79")
        assert quatrieme is rappel.TROP and quatrieme.code == 429
        assert composer.await_count == 3
        assert _demander("06 12 34 56 79", "198.51.100.9") is rappel.OK

    def test_le_compteur_par_adresse_ne_grossit_pas_sans_fin(self, monkeypatch):
        monkeypatch.setattr(rappel, "_ADRESSES_MAX", 50)
        for i in range(60):
            rappel.adresse_bloquee(f"198.51.100.{i}")
        # Les soixante adresses datent de moins d'une heure : elles restent comptées.
        assert len(rappel._par_adresse) == 60
        for file in rappel._par_adresse.values():
            file[0] -= 4000        # elles vieillissent d'une heure passée
        rappel.adresse_bloquee("203.0.113.200")
        assert list(rappel._par_adresse) == ["203.0.113.200"]

    def test_le_plafond_du_jour_borne_la_facture(self, composer, monkeypatch):
        monkeypatch.setenv("RAPPEL_MAX_PAR_JOUR", "4")
        for i in range(4):
            assert _demander(f"06 12 34 56 7{i}", f"203.0.113.{i}") is rappel.OK
        cinquieme = _demander("06 12 34 56 79", "203.0.113.99")
        assert cinquieme is rappel.PLAFOND and cinquieme.code == 503
        assert composer.await_count == 4
        assert rappel.compte_du_jour() == 4

    def test_le_plafond_par_defaut_est_de_quinze(self):
        assert rappel.max_par_jour() == 15 and rappel.par_numero() == 2
        assert rappel.par_adresse() == 3 and rappel.duree_max() == 240

    def test_un_appel_que_twilio_n_a_pas_compose_ne_compte_pas(self, monkeypatch):
        """Une panne chez Twilio ne doit pas brûler les deux essais du restaurateur."""
        with patch("app.rappel._composer", new=AsyncMock(side_effect=RuntimeError("503"))):
            for i in range(2):
                reponse = _demander("06 12 34 56 78", f"203.0.113.{i}")
                assert reponse is rappel.PANNE and reponse.code == 502
        assert [d["statut"] for d in _demandes()] == [rappel.ECHEC, rappel.ECHEC]
        with patch("app.rappel._composer", new=AsyncMock(return_value="CA9")):
            assert _demander("06 12 34 56 78", "203.0.113.9") is rappel.OK


class TestLesHeures:
    @pytest.mark.parametrize("heure,ouvert", [(7, False), (8, True), (12, True), (21, True),
                                              (22, False), (3, False)])
    def test_marie_ne_rappelle_qu_entre_8_h_et_22_h(self, composer, monkeypatch, heure, ouvert):
        instant = MIDI.replace(hour=heure, minute=59)
        monkeypatch.setattr(horloge, "maintenant", lambda: instant)
        reponse = _demander("06 12 34 56 78")
        assert (reponse is rappel.OK) is ouvert
        assert composer.await_count == (1 if ouvert else 0)
        if not ouvert:
            assert reponse.etat == "ferme" and "8 h et 22 h" in reponse.message
            assert _demandes() == []

    def test_la_plage_se_regle(self, monkeypatch):
        monkeypatch.setenv("RAPPEL_HEURES", "10-19")
        assert rappel.heures() == (10, 19)
        assert rappel.ferme(MIDI.replace(hour=9)) is not None
        assert rappel.ferme(MIDI.replace(hour=10)) is None

    @pytest.mark.parametrize("brut", ["", "22-8", "abc", "8", "8-25"])
    def test_une_plage_illisible_retombe_sur_8_h_22_h(self, monkeypatch, brut):
        monkeypatch.setenv("RAPPEL_HEURES", brut)
        assert rappel.heures() == (8, 22)


class TestQuandLeRappelEstCoupe:
    def test_rappel_actif_a_zero(self, composer, monkeypatch):
        monkeypatch.setenv("RAPPEL_ACTIF", "0")
        reponse = _demander("06 12 34 56 78")
        assert reponse is rappel.INACTIF and reponse.code == 503
        composer.assert_not_awaited()

    @pytest.mark.parametrize("variable", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "PUBLIC_URL"])
    def test_sans_identifiants_ni_adresse_publique(self, composer, monkeypatch, variable):
        monkeypatch.delenv(variable)
        assert rappel.actif() is False
        assert _demander("06 12 34 56 78") is rappel.INACTIF
        composer.assert_not_awaited()

    def test_sans_etablissement_de_demonstration(self, composer, monkeypatch):
        monkeypatch.setenv("RAPPEL_ETABLISSEMENT", "999999")
        assert _demander("06 12 34 56 78") is rappel.INACTIF
        composer.assert_not_awaited()
        assert _demandes() == []


class TestLaRoute:
    def test_un_numero_valide_fait_appeler_marie(self, client, composer):
        r = client.post("/rappel", json={"numero": "06 12 34 56 78", "site": ""})
        assert r.status_code == 200
        assert r.json() == {"etat": "appel", "message": rappel.OK.message}
        composer.assert_awaited_once()

    def test_un_numero_refuse_rend_422_et_sa_raison(self, client, composer):
        r = client.post("/rappel", json={"numero": "08 99 12 34 56"})
        assert r.status_code == 422 and r.json()["etat"] == "numero"
        composer.assert_not_awaited()

    def test_un_formulaire_classique_est_refuse(self, client, composer):
        """Ce qu'un formulaire posé sur un autre site peut envoyer sans permission."""
        r = client.post("/rappel", data={"numero": "0612345678"})
        assert r.status_code == 415
        r = client.post("/rappel", content='{"numero": "0612345678"}',
                        headers={"Content-Type": "text/plain"})
        assert r.status_code == 415
        composer.assert_not_awaited()

    @pytest.mark.parametrize("en_tetes", [
        {"Origin": "https://autre-site.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"},
    ])
    def test_une_demande_venue_d_un_autre_site_est_refusee(self, client, composer, en_tetes):
        r = client.post("/rappel", json={"numero": "0612345678"}, headers=en_tetes)
        assert r.status_code == 403
        composer.assert_not_awaited()

    def test_une_demande_de_notre_page_passe(self, client, composer):
        r = client.post("/rappel", json={"numero": "0612345678"},
                        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

    def test_le_champ_cache_rempli_par_un_robot_n_appelle_personne(self, client, composer):
        r = client.post("/rappel", json={"numero": "0612345678", "site": "http://spam.example"})
        assert r.status_code == 200 and r.json()["etat"] == "appel"
        composer.assert_not_awaited()
        assert _demandes() == []

    @pytest.mark.parametrize("corps", ["pas du json", "[1, 2]", '"0612345678"',
                                       '{"numero": "' + "0" * 3000 + '"}'])
    def test_un_corps_illisible_ou_trop_gros(self, client, composer, corps):
        r = client.post("/rappel", content=corps, headers=JSON)
        assert r.status_code == 400
        composer.assert_not_awaited()

    def test_l_adresse_comptee_est_celle_que_caddy_ajoute(self, client, composer):
        """La dernière entrée de X-Forwarded-For : la seule que le visiteur ne choisit pas."""
        for i in range(3):
            r = client.post("/rappel", json={"numero": f"06123456{i}0"},
                            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.50"})
            assert r.status_code == 200
        r = client.post("/rappel", json={"numero": "0612345699"},
                        headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.50"})
        assert r.status_code == 429 and r.json()["etat"] == "trop"


def _signer(url: str, data: dict) -> str:
    charge = url + "".join(k + str(v) for k, v in sorted(data.items()))
    return base64.b64encode(
        hmac.new(b"test-auth-token", charge.encode("utf-8"), hashlib.sha1).digest()).decode()


class TestQuandLaPersonneDecroche:
    DONNEES = {"CallSid": "CA" + "1" * 32, "To": "+33612345678", "From": DEMO_NUMBER,
               "Direction": "outbound-api"}

    def test_le_flux_est_marque_demonstration_et_ne_renvoie_pas_vers_le_restaurant(self, client):
        r = client.post("/twilio/rappel", data=self.DONNEES)
        assert r.status_code == 200
        twiml = r.text
        assert '<Parameter name="Rappel" value="1"/>' in twiml
        # Le flux reçoit l'établissement de démonstration et le numéro de la personne rappelée.
        assert f'<Parameter name="To" value="{DEMO_NUMBER}"/>' in twiml
        assert '<Parameter name="From" value="+33612345678"/>' in twiml
        assert "<Redirect" not in twiml and "<Hangup/>" in twiml

    def test_un_appel_recu_n_est_jamais_marque_demonstration(self, client):
        r = client.post("/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER,
                                               "From": "+33612345678"})
        assert "Rappel" not in r.text and "<Redirect" in r.text

    def test_sans_signature_la_route_refuse(self, client, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "enforce")
        twilio_signature.remettre_a_zero()
        assert client.post("/twilio/rappel", data=self.DONNEES).status_code == 403
        signature = _signer("http://testserver/twilio/rappel", self.DONNEES)
        r = client.post("/twilio/rappel", data=self.DONNEES,
                        headers={"X-Twilio-Signature": signature})
        assert r.status_code == 200 and "Rappel" in r.text

    def test_sans_etablissement_de_demonstration_on_raccroche(self, client, monkeypatch):
        monkeypatch.setenv("RAPPEL_ETABLISSEMENT", "999999")
        r = client.post("/twilio/rappel", data=self.DONNEES)
        assert "<Hangup/>" in r.text and "<Stream" not in r.text

    def _ouvrir_le_flux(self, client, call_sid: str, rappel_demande: bool):
        custom = {"To": DEMO_NUMBER, "From": "+33612345678", "CallSid": call_sid}
        if rappel_demande:
            custom["Rappel"] = "1"
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(json.dumps({
                    "event": "start", "streamSid": "MZ-rappel",
                    "start": {"streamSid": "MZ-rappel", "callSid": call_sid,
                              "customParameters": custom}}))
                fin = time.monotonic() + 2.0
                while run_bot.await_count == 0 and time.monotonic() < fin:
                    time.sleep(0.01)
        return run_bot

    def test_marie_se_presente_et_le_bot_sait_que_c_est_une_demonstration(self, client, composer):
        _demander("06 12 34 56 78")
        with db.get_conn() as conn:
            conn.execute("UPDATE rappels SET call_sid = 'CA-demo-1'")
        run_bot = self._ouvrir_le_flux(client, "CA-demo-1", rappel_demande=True)
        tenant = run_bot.await_args.args[3]
        assert run_bot.await_args.kwargs["demonstration"] is True
        assert run_bot.await_args.kwargs["caller_number"] == "+33612345678"
        assert "c'est Marie" in tenant.greeting and "Helmane" in tenant.greeting
        assert tenant.name in tenant.greeting and tenant.phone_number == DEMO_NUMBER
        # La demande passe à « décroché », et l'appel est noté sortant.
        assert _demandes()[0]["statut"] == rappel.DECROCHE
        assert _appel("CA-demo-1")["sortant"] == 1

    def test_un_appel_recu_garde_l_accueil_du_restaurant(self, client):
        run_bot = self._ouvrir_le_flux(client, "CA-recu-1", rappel_demande=False)
        tenant = run_bot.await_args.args[3]
        assert run_bot.await_args.kwargs["demonstration"] is False
        assert "Marie" not in tenant.greeting
        assert _appel("CA-recu-1")["sortant"] is None


class TestLePrompt:
    def _tenant(self):
        return tenants.get_by_phone(DEMO_NUMBER)

    def test_la_demonstration_ajoute_sa_section(self):
        prompt = llm.build_system_prompt(self._tenant(), demonstration=True)
        assert "# Démonstration" in prompt and "C'est TOI qui as appelé" in prompt
        assert "helmane.fr" in prompt

    def test_un_appel_recu_n_en_sait_rien(self):
        normal = llm.build_system_prompt(self._tenant())
        assert "Démonstration" not in normal and "Helmane" not in normal
        demo = llm.build_system_prompt(self._tenant(), demonstration=True)
        assert demo.replace(llm._section_demonstration(self._tenant()), "") == normal


class TestLeCoutDUnAppelSortant:
    def _appel(self, sid: str, numero: str, sortant: bool) -> dict:
        calls.start_call(sid, tenants.get_by_phone(DEMO_NUMBER).id, numero, sortant)
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = ? WHERE call_sid = ?",
                         (horloge.il_y_a(125 / 86400), sid))
        calls.finish_call(sid)
        return _appel(sid)

    def test_vers_un_portable_il_est_chiffre_au_tarif_sortant(self):
        """125 s = trois minutes entamées. Reçu : 0,010 $/min ; passé vers un portable :
        0,0404 $/min (grille Twilio relevée le 01/10/2026)."""
        recu = self._appel("CA-cout-recu", "+33612345678", sortant=False)
        passe = self._appel("CA-cout-passe", "+33612345678", sortant=True)
        assert recu["cout_telephonie"] == pytest.approx(3 * 0.01)
        assert passe["cout_telephonie"] == pytest.approx(3 * 0.0404)
        assert passe["estimated_cost"] == pytest.approx(
            passe["cout_telephonie"] + passe["cout_transcription"]
            + passe["cout_comprehension"] + passe["cout_voix"])

    def test_vers_un_fixe(self):
        passe = self._appel("CA-cout-fixe", "+33142685300", sortant=True)
        assert passe["cout_telephonie"] == pytest.approx(3 * 0.0187)


class TestLaConservation:
    def test_la_purge_efface_les_demandes_de_plus_d_un_mois(self, composer):
        _demander("06 12 34 56 78", "203.0.113.1")
        _demander("06 98 76 54 32", "203.0.113.2")
        with db.get_conn() as conn:
            conn.execute("UPDATE rappels SET created_at = ? WHERE numero = '+33612345678'",
                         (horloge.il_y_a(31),))
        assert rgpd.jours_rappel() == 30
        rgpd.purger()
        assert [d["numero"] for d in _demandes()] == ["+33698765432"]

    def test_l_effacement_a_la_demande_retire_aussi_la_demande_de_rappel(self, composer):
        _demander("06 12 34 56 78", "203.0.113.1")
        _demander("06 98 76 54 32", "203.0.113.2")
        rgpd.effacer_appelant("+33612345678")
        assert [d["numero"] for d in _demandes()] == ["+33698765432"]


class TestDansLAdmin:
    def test_un_rappel_du_site_est_etiquete_dans_le_journal(self):
        """Sans étiquette, l'appel passe pour un appel reçu de ce numéro."""
        from app.admin import presenters

        tenant = tenants.get_by_phone(DEMO_NUMBER)
        calls.start_call("CA-admin-passe", tenant.id, "+33612345678", True)
        calls.start_call("CA-admin-recu", tenant.id, "+33612345678")
        assert presenters.call_view(_appel("CA-admin-passe"))["sortant"] is True
        assert presenters.call_view(_appel("CA-admin-recu"))["sortant"] is False
        client = TestClient(app)
        client.post("/admin/login", data={"email": "admin@test.local",
                                          "password": "test-admin-pass"})
        page = client.get("/admin/calls").text
        with db.get_conn() as conn:
            passes = conn.execute("SELECT COUNT(*) FROM calls WHERE sortant = 1").fetchone()[0]
        # Une étiquette par appel passé, aucune sur les appels reçus.
        assert passes >= 1 and page.count("Rappel du site · ") == passes
        passe = _appel("CA-admin-passe")["id"]
        fiche = client.get(f"/admin/calls/{passe}").text.replace("&#39;", "'")
        assert "Rappel demandé sur le site, appel passé par l'assistante" in fiche
