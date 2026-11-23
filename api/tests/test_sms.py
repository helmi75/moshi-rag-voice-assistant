"""SMS de confirmation au client après une réservation confirmée (ASSISTANTE-154).

Le client qui réserve au téléphone ne garde aucune trace écrite. Ce fichier tient ce qui ne
doit jamais arriver : un SMS vers un fixe, un numéro masqué ou une démonstration ; un SMS
promis au client alors qu'il ne part pas ; un envoi qui retarde l'appel ou le fait échouer ;
un numéro de client dans le journal. Aucun test ne touche le réseau.
"""
import asyncio
import base64
import json
import time
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app import calls, db, llm, sms, tenants, users
from app.main import app

MOBILE = "+33612345678"


def _demain() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Le Bouchon Doré", "+33199000881")
    tenants.update_tenant(tenant.id, sms_actif=1)
    yield tenants.get_by_id(tenant.id)
    tenants.delete_tenant(tenant.id)


@pytest.fixture()
def planifies():
    """Ce qui serait parti : (numéro, expéditeur)."""
    partis: list = []
    with patch.object(llm.sms, "planifier",
                      side_effect=lambda tenant, reservation, call_id, numero, nom:
                      partis.append((numero, nom))):
        yield partis


# Un identifiant d'appel quelconque : le SMS ne part que pour une réservation prise au
# téléphone (le canal SMS répond déjà par écrit, et son coût n'aurait pas d'appel où se noter).
APPEL = 987654


def _creer(tenant, numero=MOBILE, call_id=APPEL) -> dict:
    return json.loads(asyncio.run(llm.run_tool(
        tenant, "create_reservation",
        {"customer_name": "Martin", "date": _demain(), "time": "20:00", "party_size": 2},
        numero, call_id)))


class TestExpediteur:
    def test_accents_espaces_et_longueur(self):
        assert sms.nom_expediteur("Le Bouchon Doré") == "LeBouchonDo"
        assert sms.nom_expediteur("Café de l'Étoile du Nord") == "CafeDeLEtoi"
        assert len(sms.nom_expediteur("Brasserie des Grands Boulevards")) == 11

    def test_accents_retires_pas_supprimes(self):
        assert sms.nom_expediteur("Étoile") == "Etoile"

    def test_chiffres_ou_symboles_seuls_ne_donnent_rien(self):
        assert sms.nom_expediteur("1234") is None
        assert sms.nom_expediteur("***") is None
        assert sms.nom_expediteur("") is None
        assert sms.nom_expediteur(None) is None

    def test_la_valeur_posee_passe_avant_le_nom(self, resto):
        tenants.update_tenant(resto.id, sms_expediteur="Bouchon")
        assert sms.expediteur(tenants.get_by_id(resto.id)) == "Bouchon"
        assert sms.expediteur(resto) == "LeBouchonDo"


class TestDestinataire:
    @pytest.mark.parametrize("numero", ["+33612345678", "+33712345678"])
    def test_un_mobile_francais_est_accepte(self, numero):
        assert sms.destinataire(numero) == numero

    @pytest.mark.parametrize("numero", [
        "+33512345678", "+33112345678", "+14155550123", "+4915112345678", "", None,
        "anonymous", "+3361234567", "0612345678"])
    def test_le_reste_est_refuse(self, numero):
        assert sms.destinataire(numero) is None


class TestCorps:
    def _lignes(self, tenant, nom="Martin", numero=None):
        return sms.corps(tenant, {"customer_name": nom, "date": "2026-12-05",
                                  "time": "20:30", "party_size": 4})

    def test_le_modele(self, resto):
        texte = self._lignes(resto)
        assert texte == ("Le Bouchon Doré : table pour 4 le 05/12 à 20h30 au nom de Martin. "
                         "Pour modifier ou annuler, appelez le 01 99 00 08 81.")

    def test_que_du_gsm7_et_160_caracteres(self, resto):
        longue = tenants.create_tenant("Ô Château de la Très Longue Enseigne " * 3, "+33199000882")
        try:
            for tenant in (resto, longue):
                for nom in ("Martin", "Jean-François Œuvre-Élodie " * 10, "Zoë Ångström"):
                    texte = self._lignes(tenant, nom)
                    assert len(texte) <= 160, texte
                    assert set(texte) <= sms._GSM7, set(texte) - sms._GSM7
        finally:
            tenants.delete_tenant(longue.id)

    def test_le_nom_dicte_ne_porte_ni_chiffre_ni_lien(self, resto):
        """Le nom vient de l'appelant et part sous la marque du restaurant : pas de quoi y
        glisser un numéro surtaxé ou une adresse de site."""
        texte = self._lignes(resto, "Martin appelez le 08 99 12 34 56 www.x.fr")
        nom = texte.split("au nom de ")[1].split(". Pour")[0]
        assert not any(c.isdigit() for c in nom)
        assert "." not in nom and "www.x" not in texte
        assert len(nom) <= 30

    def test_un_nom_et_un_restaurant_de_cent_caracteres(self, resto):
        enorme = tenants.create_tenant("R" * 100, "+33199000883")
        try:
            texte = self._lignes(enorme, "N" * 100)
            assert len(texte) <= 160
            assert "Pour modifier" in texte
        finally:
            tenants.delete_tenant(enorme.id)


class TestLaCreationPlanifieLeSms:
    def test_un_mobile_chez_un_etablissement_allume(self, resto, planifies):
        resultat = _creer(resto)
        assert resultat["status"] == "confirmed"
        assert resultat["sms_confirmation"] is True
        assert planifies == [(MOBILE, "LeBouchonDo")]

    def test_etablissement_eteint(self, resto, planifies):
        tenants.update_tenant(resto.id, sms_actif=0)
        resultat = _creer(tenants.get_by_id(resto.id))
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_demonstration_envoie_sans_case_cochee(self, planifies):
        """ASSISTANTE-199 : pendant une démonstration, le gérant rappelé reçoit le SMS que
        recevront ses clients. Une démonstration n'est joignable que par `/twilio/demo`
        (route signée) : son seul appelant est le numéro que l'admin a fait rappeler."""
        demo = tenants.create_tenant("Le Procope", "demo-sms-1")
        try:
            resultat = _creer(tenants.get_by_id(demo.id))
            assert resultat["sms_confirmation"] is True and len(planifies) == 1
        finally:
            tenants.delete_tenant(demo.id)

    def test_demonstration_sans_mobile(self, planifies):
        demo = tenants.create_tenant("Le Procope", "demo-sms-2")
        try:
            resultat = _creer(tenants.get_by_id(demo.id), "+33412345678")
            assert resultat["sms_confirmation"] is False and planifies == []
        finally:
            tenants.delete_tenant(demo.id)

    def test_demonstration_ne_donne_pas_un_numero_qui_n_existe_pas(self):
        """Le numéro d'une démonstration (`demo-…`) n'est pas un numéro : le SMS n'invite
        pas à rappeler un numéro inventé."""
        demo = tenants.Tenant(id=1, name="Le Procope", business_type="restaurant",
                              phone_number="demo-3", language="fr", greeting="",
                              knowledge_base="")
        texte = sms.corps(demo, {"date": _demain(), "time": "20:00", "party_size": 2,
                                 "customer_name": "Martin"})
        assert "appelez" not in texte and "demo" not in texte

    def test_etablissement_d_essai(self, resto, planifies, monkeypatch):
        monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(resto.id))
        resultat = _creer(resto)
        assert resultat["sms_confirmation"] is False and planifies == []

    @pytest.mark.parametrize("numero", ["+33412345678", "", None, "anonymous", "+14155550123"])
    def test_numero_sans_sms(self, resto, planifies, numero):
        resultat = _creer(resto, numero)
        assert resultat["status"] == "confirmed"
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_des_couverts_qui_ne_sont_pas_un_nombre(self, resto, planifies):
        """Le modèle peut rendre une chaîne pour un entier : rien de ce texte, dicté par
        l'appelant, ne part dans un SMS signé du restaurant."""
        resultat = json.loads(asyncio.run(llm.run_tool(
            resto, "create_reservation",
            {"customer_name": "Martin", "date": _demain(), "time": "20:00",
             "party_size": "2. Acompte sur arnaque.fr"}, MOBILE, APPEL)))
        assert resultat.get("sms_confirmation") is not True and planifies == []
        assert sms.couverts({"party_size": "2. Visitez x.fr"}) is None
        assert sms.couverts({"party_size": "4"}) == 4 and sms.couverts({"party_size": 0}) is None

    def test_canal_sms_sans_appel(self, resto, planifies):
        resultat = _creer(resto, call_id=None)
        assert resultat["status"] == "confirmed"
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_rappel_de_demonstration(self, resto, planifies):
        """« Rappelez-moi » du site : Marie appelle un inconnu depuis un VRAI établissement.
        Le visiteur qui essaie une réservation ne reçoit pas un SMS payé."""
        from app import rappel
        resultat = _creer(rappel.pour_la_demonstration(resto))
        assert resultat["status"] == "confirmed"
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_nom_sans_lettre(self, resto, planifies):
        tenants.update_tenant(resto.id, name="1234")
        resultat = _creer(tenants.get_by_id(resto.id))
        assert resultat["sms_confirmation"] is False and planifies == []

    def test_une_demande_a_valider_ne_promet_rien(self, resto, planifies):
        class Carnet:
            async def retrouver(self, *a, **k):
                return []

            async def creer(self, **k):
                return {"id": "r1", "customer_name": "Martin", "date": k["date"],
                        "time": k["heure"], "party_size": 2, "a_valider": True}

        with patch.object(llm.connecteurs, "pour", return_value=Carnet()):
            resultat = _creer(resto)
        assert resultat["status"] == "pending_restaurant_approval"
        assert "sms_confirmation" not in resultat and planifies == []

    def test_une_reponse_twilio_lente_ne_retarde_pas_l_outil(self, resto):
        async def lent(*args, **kwargs):
            await asyncio.sleep(5)

        with patch.object(llm.sms, "envoyer", side_effect=lent):
            debut = time.monotonic()
            resultat = _creer(resto)
            assert time.monotonic() - debut < 2
        assert resultat["sms_confirmation"] is True

    def test_le_prompt_ne_promet_le_sms_que_sur_signal(self, resto):
        prompt = llm.build_system_prompt(resto)
        assert "sms_confirmation: true" in prompt


class _FauxClient:
    """Un httpx.AsyncClient qui ne sort pas : rend `reponse`, ou lève `erreur`."""
    appels: list = []
    reponse = None
    erreur = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        _FauxClient.appels.append((url, data))
        if _FauxClient.erreur:
            raise _FauxClient.erreur
        return _FauxClient.reponse


@pytest.fixture()
def twilio(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACsmstest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "jeton-de-test-sms")
    monkeypatch.setenv("TWILIO_REGION", "ie1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN_IE1", "jeton-ie1-de-test")
    _FauxClient.appels, _FauxClient.erreur = [], None
    _FauxClient.reponse = httpx.Response(201, json={"sid": "SM1", "num_segments": "1"})
    with patch.object(sms.httpx, "AsyncClient", _FauxClient):
        yield _FauxClient


@pytest.fixture()
def journal():
    lignes: list = []
    identifiant = logger.add(lambda m: lignes.append(str(m)), level="DEBUG")
    yield lignes
    logger.remove(identifiant)


@pytest.fixture()
def appel(resto):
    identifiant = calls.start_call("CA-sms-test", resto.id, MOBILE)
    yield identifiant
    with db.get_conn() as conn:
        conn.execute("DELETE FROM calls WHERE id = ?", (identifiant,))


RESERVATION = {"customer_name": "Martin", "date": "2026-12-05", "time": "20:30", "party_size": 2}


def _cout(identifiant):
    with db.get_conn() as conn:
        return conn.execute("SELECT cout_sms FROM calls WHERE id = ?", (identifiant,)).fetchone()[0]


class TestEnvoyer:
    def test_l_envoi_pose_le_cout_selon_les_segments(self, resto, appel, twilio, monkeypatch):
        monkeypatch.delenv("COST_SMS_PAR_SEGMENT", raising=False)
        twilio.reponse = httpx.Response(201, json={"sid": "SM1", "num_segments": "2"})
        assert asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo")) is True
        assert _cout(appel) == pytest.approx(2 * 0.0798)
        url, data = twilio.appels[0]
        # Les SMS restent traités en us1, quelle que soit la région des appels.
        assert url == "https://api.twilio.com/2010-04-01/Accounts/ACsmstest/Messages.json"
        assert data["From"] == "LeBouchonDo" and data["To"] == MOBILE

    @pytest.mark.parametrize("cas", ["refus", "delai", "exception", "illisible"])
    def test_une_panne_ne_leve_rien_et_ne_chiffre_rien(self, resto, appel, twilio, cas):
        if cas == "refus":
            twilio.reponse = httpx.Response(400, json={"code": 21614, "message": "To number"})
        elif cas == "delai":
            twilio.erreur = httpx.ReadTimeout("trop lent")
        elif cas == "exception":
            twilio.erreur = RuntimeError("boum")
        else:
            twilio.reponse = httpx.Response(500, text="<html>")
        assert asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo")) is False
        assert _cout(appel) is None

    def test_sans_identifiants_rien_ne_part(self, resto, appel, twilio, monkeypatch):
        monkeypatch.delenv("TWILIO_AUTH_TOKEN")
        assert asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo")) is False
        assert twilio.appels == []

    def test_ni_numero_ni_texte_ni_jeton_dans_le_journal(self, resto, appel, twilio, journal):
        asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo"))
        twilio.reponse = httpx.Response(400, json={"code": 21614})
        asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo"))
        twilio.erreur = RuntimeError(f"échec pour {MOBILE}")
        asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo"))
        tout = "\n".join(journal)
        assert "[sms]" in tout
        for interdit in (MOBILE, MOBILE[1:], "612345678", "Martin", "table pour", "jeton-de-test-sms"):
            assert interdit not in tout, interdit


class TestLeCoutEntreDansLAppel:
    def test_la_cloture_garde_le_cout_du_sms(self, resto, appel):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET cout_sms = 0.0798 WHERE id = ?", (appel,))
        calls.finish_call("CA-sms-test")
        with db.get_conn() as conn:
            ligne = conn.execute("SELECT estimated_cost, cout_sms, cout_telephonie, "
                                 "cout_transcription, cout_comprehension, cout_voix "
                                 "FROM calls WHERE id = ?", (appel,)).fetchone()
        assert ligne["cout_sms"] == pytest.approx(0.0798)
        hors_sms = (ligne["cout_telephonie"] + ligne["cout_transcription"]
                    + ligne["cout_comprehension"] + ligne["cout_voix"])
        assert ligne["estimated_cost"] == pytest.approx(hors_sms + 0.0798)

    def test_la_cloture_de_secours_aussi(self, resto, appel):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET cout_sms = 0.0798 WHERE id = ?", (appel,))
        calls.conclure_secours("CA-sms-test", calls.SECOURS_RENVOYE, secondes_renvoi=30,
                               numero="+33612345678")
        with db.get_conn() as conn:
            ligne = conn.execute("SELECT estimated_cost, cout_telephonie, cout_transcription, "
                                 "cout_comprehension, cout_voix FROM calls WHERE id = ?",
                                 (appel,)).fetchone()
        hors_sms = (ligne["cout_telephonie"] + ligne["cout_transcription"]
                    + ligne["cout_comprehension"] + ligne["cout_voix"])
        assert ligne["estimated_cost"] == pytest.approx(hors_sms + 0.0798)

    def test_un_sms_accepte_apres_la_cloture_entre_dans_le_total(self, resto, appel, twilio):
        """Raccrochage juste après la création, Twilio lent : le SMS arrive après
        `finish_call`. Sans cela, « Santé & coûts » ne tomberait plus sur le total."""
        calls.finish_call("CA-sms-test")
        with db.get_conn() as conn:
            avant = conn.execute("SELECT estimated_cost FROM calls WHERE id = ?",
                                 (appel,)).fetchone()[0]
        twilio.reponse = httpx.Response(201, json={"sid": "SM1", "num_segments": "1"})
        assert asyncio.run(sms.envoyer(resto, RESERVATION, appel, MOBILE, "LeBouchonDo"))
        with db.get_conn() as conn:
            apres = conn.execute("SELECT estimated_cost FROM calls WHERE id = ?",
                                 (appel,)).fetchone()[0]
        assert apres == pytest.approx(avant + sms.cout_par_segment())

    def test_la_repartition_a_un_poste_sms_et_reste_une_somme_exacte(self, resto, appel):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET cout_sms = 0.0798, cout_telephonie = 0.02, "
                         "ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?", (appel,))
        lignes = {l["label"]: l["amount"] for l in calls.cost_breakdown(resto.id, days=30)}
        assert lignes["SMS de confirmation (Twilio)"] == pytest.approx(0.0798)
        assert sum(lignes.values()) == pytest.approx(0.0798 + 0.02)


# ---- Admin ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(app)


def _csrf(client) -> str:
    charge = client.cookies.get("session").split(".")[0]
    charge += "=" * (-len(charge) % 4)
    return json.loads(base64.urlsafe_b64decode(charge))["csrf"]


def _admin(client) -> None:
    assert client.post("/admin/login", data={"email": "admin@test.local",
                                             "password": "test-admin-pass"},
                       follow_redirects=False).status_code == 303


def _restaurateur(client, tenant) -> None:
    user = users.create_user(f"gerant-sms-{tenant.id}@test.fr", "resto-pass-sms",
                             users.ROLE_RESTAURATEUR, tenant.id)
    assert client.post("/admin/login", data={"email": user.email, "password": "resto-pass-sms"},
                       follow_redirects=False).status_code == 303


def _fiche(tenant, **champs) -> dict:
    return {"name": tenant.name, "business_type": "restaurant", "language": "fr-FR",
            "phone_number": tenant.phone_number, **champs}


def _poster(client, tenant, **champs):
    return client.post(f"/admin/tenants/{tenant.id}", data=_fiche(tenant, **champs),
                       headers={"X-CSRF-Token": _csrf(client)}, follow_redirects=False)


class TestAdmin:
    def test_un_restaurateur_ne_peut_pas_allumer_les_sms(self, client, resto):
        tenants.update_tenant(resto.id, sms_actif=0)
        _restaurateur(client, resto)
        reponse = _poster(client, resto, sms_present="1", sms_actif="1", sms_expediteur="Pirate")
        assert reponse.status_code == 303
        apres = tenants.get_by_id(resto.id)
        assert apres.sms_actif is False and apres.sms_expediteur is None

    def test_un_restaurateur_ne_peut_pas_non_plus_les_eteindre(self, client, resto):
        _restaurateur(client, resto)
        _poster(client, resto, sms_present="1")
        assert tenants.get_by_id(resto.id).sms_actif is True

    def test_le_super_admin_allume_et_eteint(self, client, resto):
        tenants.update_tenant(resto.id, sms_actif=0)
        _admin(client)
        assert _poster(client, resto, sms_present="1", sms_actif="1",
                       sms_expediteur="Le Bouchon").status_code == 303
        apres = tenants.get_by_id(resto.id)
        assert apres.sms_actif is True and apres.sms_expediteur == "LeBouchon"
        _poster(client, resto, sms_present="1", sms_expediteur="")
        apres = tenants.get_by_id(resto.id)
        assert apres.sms_actif is False and apres.sms_expediteur is None

    def test_un_formulaire_sans_la_case_ne_touche_pas_au_reglage(self, client, resto):
        _admin(client)
        _poster(client, resto)
        assert tenants.get_by_id(resto.id).sms_actif is True

    def test_un_nom_sans_lettre_est_refuse_par_le_serveur(self, client, resto):
        _admin(client)
        reponse = _poster(client, resto, sms_present="1", sms_actif="1", sms_expediteur="12345")
        assert reponse.status_code == 422
        assert tenants.get_by_id(resto.id).sms_expediteur is None

    def test_cloisonnement_403_sur_la_fiche_d_un_autre(self, client, resto):
        autre = tenants.create_tenant("Chez L'Autre", "+33199000884")
        try:
            _restaurateur(client, autre)
            assert _poster(client, resto, sms_present="1").status_code == 403
            assert client.get(f"/admin/tenants/{resto.id}/edit").status_code == 403
        finally:
            tenants.delete_tenant(autre.id)

    def test_post_sans_csrf_refuse(self, client, resto):
        _admin(client)
        reponse = client.post(f"/admin/tenants/{resto.id}", data=_fiche(resto, sms_present="1"),
                              follow_redirects=False)
        assert reponse.status_code == 403

    def test_la_fiche_montre_la_case_et_le_nom_qui_partira(self, client, resto):
        _admin(client)
        page = client.get(f"/admin/tenants/{resto.id}/edit").text
        assert 'name="sms_actif"' in page and "checked" in page[page.index('name="sms_actif"'):][:80]
        assert "LeBouchonDo" in page

    def test_la_fiche_d_un_restaurateur_n_a_pas_la_case(self, client, resto):
        _restaurateur(client, resto)
        assert 'name="sms_actif"' not in client.get(f"/admin/tenants/{resto.id}/edit").text
