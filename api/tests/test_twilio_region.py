"""Deux régions Twilio, deux jetons (ASSISTANTE-123).

Un numéro français traité en Irlande plutôt qu'aux États-Unis épargne à la voix deux
traversées de l'Atlantique par réplique (83 ms d'aller-retour contre 19, mesuré le
04/10/2026 depuis le serveur). Mais l'Irlande signe avec son propre jeton et range les
appels chez elle : l'application doit savoir à quelle porte frapper, et se replier sans
casser quand on lui demande une région qu'elle ne peut pas servir.

La signature des deux régions est dans test_twilio_signature.py, le message vocal dans
test_repondeur.py, la relève des alertes dans test_supervision.py."""
import asyncio

import pytest

from app import rappel, supervision, twilio_region
from app.voice import bot

SID = "AC" + "0" * 32
AMERICAIN, IRLANDAIS = "jeton-americain", "jeton-irlandais"


@pytest.fixture(autouse=True)
def _compte(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AMERICAIN)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN_IE1", raising=False)
    monkeypatch.delenv("TWILIO_REGION", raising=False)


@pytest.fixture()
def irlande(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN_IE1", IRLANDAIS)
    monkeypatch.setenv("TWILIO_REGION", "ie1")


class TestLaRegionChoisie:
    def test_sans_reglage_c_est_les_etats_unis(self):
        assert twilio_region.choisie() == "us1" and twilio_region.anomalie() is None
        assert twilio_region.hote() == "https://api.twilio.com"
        assert twilio_region.identifiants() == (SID, AMERICAIN)
        assert twilio_region.acces() is None

    def test_l_irlande_quand_son_jeton_est_pose(self, irlande):
        assert twilio_region.choisie() == "ie1" and twilio_region.anomalie() is None
        assert twilio_region.hote() == "https://api.dublin.ie1.twilio.com"
        assert twilio_region.hote(produit="monitor") == "https://monitor.dublin.ie1.twilio.com"
        assert twilio_region.identifiants() == (SID, IRLANDAIS)
        assert twilio_region.acces() == "dublin"

    def test_le_jeton_d_une_region_ne_sert_jamais_a_l_autre(self, irlande):
        """Twilio répond 401 au jeton américain présenté en Irlande (vérifié le 04/10/2026)."""
        assert twilio_region.identifiants("us1") == (SID, AMERICAIN)
        assert twilio_region.hote("us1") == "https://api.twilio.com"

    def test_l_irlande_sans_son_jeton_se_replie_et_le_dit(self, monkeypatch):
        """Un appel qui passe par les États-Unis vaut mieux qu'un appel qui ne part pas."""
        monkeypatch.setenv("TWILIO_REGION", "ie1")
        assert twilio_region.choisie() == "us1"
        assert twilio_region.identifiants() == (SID, AMERICAIN)
        assert "TWILIO_AUTH_TOKEN_IE1" in twilio_region.anomalie()

    def test_une_region_inconnue_se_replie_et_le_dit(self, monkeypatch):
        monkeypatch.setenv("TWILIO_REGION", "paris")
        assert twilio_region.choisie() == "us1" and twilio_region.hote() == "https://api.twilio.com"
        assert "paris" in twilio_region.anomalie()

    def test_on_cherche_d_abord_dans_la_region_choisie(self, monkeypatch, irlande):
        assert twilio_region.ordre() == ["ie1", "us1"]
        monkeypatch.delenv("TWILIO_REGION")
        assert twilio_region.ordre() == ["us1", "ie1"]

    def test_sans_jeton_irlandais_on_ne_cherche_qu_aux_etats_unis(self):
        assert twilio_region.ordre() == ["us1"]

    def test_sans_compte_pas_d_identifiants(self, monkeypatch):
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        assert twilio_region.identifiants() is None


class TestLaSupervisionLeDit:
    @pytest.fixture(autouse=True)
    def _chemin_d_appel(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
        monkeypatch.setenv("MISTRAL_API_KEY", "x")
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://app.exemple.fr/ws/voice")

    def test_une_region_sans_jeton_est_une_attention_pas_un_feu_vert(self, monkeypatch):
        assert supervision._controle_configuration().niveau == supervision.OK
        monkeypatch.setenv("TWILIO_REGION", "ie1")
        controle = supervision._controle_configuration()
        assert controle.niveau == supervision.ATTENTION
        assert "TWILIO_AUTH_TOKEN_IE1" in controle.resume

    def test_l_irlande_bien_reglee_est_verte(self, irlande):
        assert supervision._controle_configuration().niveau == supervision.OK


class TestOnParleALaBonneRegion:
    def test_le_raccrochage_reste_americain_par_defaut(self):
        serialiseur = bot.serialiseur_twilio("MZ1", "CA1")
        assert (serialiseur._region, serialiseur._edge) == (None, None)
        assert serialiseur._auth_token == AMERICAIN

    def test_le_raccrochage_s_adresse_a_l_irlande(self, irlande):
        serialiseur = bot.serialiseur_twilio("MZ1", "CA1")
        assert (serialiseur._region, serialiseur._edge) == ("ie1", "dublin")
        assert (serialiseur._account_sid, serialiseur._auth_token) == (SID, IRLANDAIS)

    @pytest.fixture()
    def twilio(self, monkeypatch):
        demandes: list = []

        class _Reponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"sid": "CA" + "1" * 32}

        class _Client:
            def __init__(self, **options):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, adresse, data=None):
                demandes.append((adresse, self.options.get("auth")))
                return _Reponse()

        monkeypatch.setattr(rappel.httpx, "AsyncClient", _Client)
        return demandes

    def test_le_rappel_du_site_part_des_etats_unis_par_defaut(self, twilio):
        asyncio.run(rappel._composer("+33612345678", "+33100000000"))
        assert twilio == [(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json",
                           (SID, AMERICAIN))]

    def test_le_rappel_du_site_part_d_irlande(self, twilio, irlande):
        """C'est la région à qui l'on demande l'appel qui ouvre le flux média : la
        démonstration faite à un restaurateur est le premier appel à en profiter."""
        asyncio.run(rappel._composer("+33612345678", "+33100000000"))
        assert twilio == [(f"https://api.dublin.ie1.twilio.com/2010-04-01/Accounts/{SID}/Calls.json",
                           (SID, IRLANDAIS))]
