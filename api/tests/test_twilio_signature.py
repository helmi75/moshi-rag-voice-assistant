"""Signature des requêtes Twilio : sans elle, n'importe qui fait parler l'assistante.

Le numéro d'un restaurant est public. Sans vérification, un POST forgé sur /twilio/sms
paie un appel LLM et crée réservations ou messages fantômes ; une connexion forgée sur
/ws/voice réveille un GPU. Ici on vérifie l'algorithme (contre le vecteur de la
documentation Twilio), les trois webhooks, la poignée de main du flux, les modes, et ce
que la supervision en dit. Le reste de la suite tourne en mode `log` (conftest) : ces
tests-ci passent en `enforce`."""
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import llm, supervision, twilio_signature
from app.main import app

JETON = "test-auth-token"
DEMO_NUMBER = "+33100000000"


def signer(url: str, data: dict | None = None, jeton: str = JETON) -> str:
    """Le calcul de référence, réécrit ici indépendamment du module testé."""
    charge = url + "".join(k + str(v) for k, v in sorted((data or {}).items()))
    return base64.b64encode(
        hmac.new(jeton.encode(), charge.encode("utf-8"), hashlib.sha1).digest()).decode()


def _start(to=DEMO_NUMBER) -> str:
    return json.dumps({
        "event": "start", "sequenceNumber": "1", "streamSid": "MZ-sig",
        "start": {"accountSid": "AC000", "streamSid": "MZ-sig", "callSid": "CA-sig",
                  "tracks": ["inbound"],
                  "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                  "customParameters": {"To": to, "CallSid": "CA-sig"}},
    })


def _attendre(condition, delai: float = 2.0) -> None:
    fin = time.monotonic() + delai
    while not condition() and time.monotonic() < fin:
        time.sleep(0.01)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setenv("TWILIO_SIGNATURE", "enforce")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", JETON)
    monkeypatch.setenv("PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("PUBLIC_WS_URL", "wss://testserver/ws/voice")
    twilio_signature.remettre_a_zero()
    yield
    twilio_signature.remettre_a_zero()


class TestAlgorithme:
    def test_vecteur_de_la_documentation_twilio(self):
        """L'exemple de « Validating requests » (jeton 12345) : si ce test rougit, ce n'est
        pas notre code qui a raison."""
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
                  "From": "+12349013030", "To": "+18005551212"}
        assert twilio_signature.signature_attendue("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
        assert twilio_signature.valide("12345", url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")

    def test_les_cles_sont_triees_et_les_valeurs_brutes(self):
        params = {"To": "+33 1 00", "CallSid": "CA1", "SpeechResult": "une table à 20h"}
        attendue = signer("https://x.fr/twilio/voice", params)
        assert twilio_signature.signature_attendue(JETON, "https://x.fr/twilio/voice", params) == attendue

    def test_le_port_par_defaut_est_optionnel(self):
        sans_port = "https://app.exemple.fr/twilio/voice"
        avec_port = "https://app.exemple.fr:443/twilio/voice"
        assert twilio_signature.valide(JETON, avec_port, {}, signer(sans_port))
        assert twilio_signature.valide(JETON, sans_port, {}, signer(avec_port))
        # Un autre port n'est pas « par défaut » : il fait partie de ce qui est signé.
        assert not twilio_signature.valide(JETON, "https://app.exemple.fr:8443/x", {}, signer(sans_port))

    def test_refuse_une_signature_forgee_ou_absente(self):
        url = "https://app.exemple.fr/twilio/voice"
        assert not twilio_signature.valide(JETON, url, {}, signer(url, jeton="autre"))
        assert not twilio_signature.valide(JETON, url, {}, "")
        assert not twilio_signature.valide("", url, {}, signer(url))


class TestWebhooksRefusentSansSignature:
    @pytest.mark.parametrize("chemin,data", [
        ("/twilio/voice", {"CallSid": "CA1", "To": DEMO_NUMBER}),
        ("/twilio/sms", {"Body": "Bonjour", "From": "+33600000000", "To": DEMO_NUMBER}),
        ("/twilio/webhook", {"CallSid": "CA1", "To": DEMO_NUMBER}),
    ])
    def test_sans_signature_403(self, client, chemin, data):
        with patch.object(llm, "respond", new=AsyncMock(return_value=("jamais", []))) as reponse:
            assert client.post(chemin, data=data).status_code == 403
        reponse.assert_not_awaited()

    def test_signature_forgee_403(self, client):
        data = {"CallSid": "CA1", "To": DEMO_NUMBER}
        fausse = signer("http://testserver/twilio/voice", data, jeton="pas-le-bon")
        assert client.post("/twilio/voice", data=data,
                           headers={"X-Twilio-Signature": fausse}).status_code == 403

    def test_signature_valide_200(self, client):
        data = {"CallSid": "CA1", "To": DEMO_NUMBER}
        bonne = signer("http://testserver/twilio/voice", data)
        reponse = client.post("/twilio/voice", data=data, headers={"X-Twilio-Signature": bonne})
        assert reponse.status_code == 200
        assert "<Connect>" in reponse.text

    def test_l_url_publique_prime_sur_l_hote_vu(self, client, monkeypatch):
        """Derrière Caddy, l'app voit http://api:8000 ; Twilio a signé le domaine public."""
        monkeypatch.setenv("PUBLIC_URL", "https://app.exemple.fr")
        data = {"CallSid": "CA1", "To": DEMO_NUMBER}
        bonne = signer("https://app.exemple.fr/twilio/voice", data)
        assert client.post("/twilio/voice", data=data,
                           headers={"X-Twilio-Signature": bonne}).status_code == 200

    def test_sans_public_url_le_flux_donne_la_base(self, client, monkeypatch):
        monkeypatch.delenv("PUBLIC_URL", raising=False)
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://app.exemple.fr/ws/voice")
        data = {"CallSid": "CA1", "To": DEMO_NUMBER}
        bonne = signer("https://app.exemple.fr/twilio/voice", data)
        assert client.post("/twilio/voice", data=data,
                           headers={"X-Twilio-Signature": bonne}).status_code == 200

    def test_le_webhook_sms_signe_passe(self, client):
        data = {"Body": "Bonjour", "From": "+33600000000", "To": DEMO_NUMBER}
        bonne = signer("http://testserver/twilio/sms", data)
        with patch.object(llm, "respond", new=AsyncMock(return_value=("Bien reçu", []))):
            reponse = client.post("/twilio/sms", data=data, headers={"X-Twilio-Signature": bonne})
        assert reponse.status_code == 200 and "Bien reçu" in reponse.text


class TestPoigneeDeMainWs:
    def test_sans_signature_le_flux_est_refuse(self, client):
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with pytest.raises(Exception) as exc:
                with client.websocket_connect("/ws/voice") as ws:
                    ws.send_text(_start())
                    ws.receive_text()
        assert type(exc.value).__name__ in ("WebSocketDisconnect", "WebSocketDenialResponse"), exc.value
        run_bot.assert_not_awaited()

    def test_signee_sur_l_url_publique_le_bot_demarre(self, client):
        run_bot = AsyncMock()
        bonne = signer("wss://testserver/ws/voice")
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice", headers={"X-Twilio-Signature": bonne}) as ws:
                ws.send_text(_start())
                _attendre(lambda: run_bot.await_count == 1)
        assert run_bot.await_count == 1


class TestModes:
    def test_log_laisse_passer_et_compte(self, client, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "log")
        reponse = client.post("/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER})
        assert reponse.status_code == 200
        assert twilio_signature.compteurs()["refusees"] == 1
        assert twilio_signature.compteurs()["derniere_refusee"]["chemin"] == "/twilio/voice"

    def test_off_ne_verifie_rien(self, client, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "off")
        assert client.post("/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER}).status_code == 200
        assert twilio_signature.compteurs() == {"acceptees": 0, "refusees": 0, "derniere_refusee": None}

    def test_le_defaut_est_enforce_quand_le_jeton_existe(self, monkeypatch):
        monkeypatch.delenv("TWILIO_SIGNATURE", raising=False)
        assert twilio_signature.mode() == "enforce"

    def test_sans_jeton_le_defaut_est_log(self, monkeypatch):
        monkeypatch.delenv("TWILIO_SIGNATURE", raising=False)
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
        assert twilio_signature.mode() == "log"

    def test_une_valeur_inconnue_ferme_la_porte(self, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "bof")
        assert twilio_signature.mode() == "enforce"


class TestSupervisionSignatures:
    def _controle(self):
        return supervision._controle_signatures()

    def _refus(self, n: int, chemin="/twilio/voice"):
        for _ in range(n):
            twilio_signature._compter(False, chemin, twilio_signature.mode())

    def _acceptes(self, n: int):
        for _ in range(n):
            twilio_signature._compter(True, "/twilio/voice", twilio_signature.mode())

    def test_jeton_absent_attention(self, monkeypatch):
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
        controle = self._controle()
        assert controle.niveau == supervision.ATTENTION
        assert "jeton" in controle.resume.lower()

    def test_mode_off_attention(self, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "off")
        assert self._controle().niveau == supervision.ATTENTION

    def test_log_avec_refus_attention_et_pointe_public_url(self, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "log")
        self._refus(3)
        controle = self._controle()
        assert controle.niveau == supervision.ATTENTION
        assert "PUBLIC_URL" in controle.detail

    def test_log_sans_refus_ok(self, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "log")
        self._acceptes(2)
        assert self._controle().niveau == supervision.OK

    def test_enforce_tout_refuse_panne(self):
        self._refus(4)
        controle = self._controle()
        assert controle.niveau == supervision.PANNE
        assert controle.mesure["refusees"] == 4 and controle.mesure["acceptees"] == 0

    def test_enforce_mixte_attention(self):
        self._acceptes(5)
        self._refus(1)
        assert self._controle().niveau == supervision.ATTENTION

    def test_enforce_sans_refus_ok(self):
        self._acceptes(5)
        assert self._controle().niveau == supervision.OK

    def test_sans_aucune_requete_pas_de_mesure(self):
        controle = self._controle()
        assert controle.niveau == supervision.OK
        assert "aucune requête" in controle.resume.lower()
