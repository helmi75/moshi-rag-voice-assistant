"""Sentry (ASSISTANTE-163) : une erreur remonte avec son établissement et son appel, et
jamais avec un numéro, un corps de requête ou un secret. Aucun réseau : le transport est
une doublure qui garde les événements."""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app import erreurs, taches, tenants
from app.main import app

DSN = "https://cle@o0.ingest.sentry.io/0"
NUMERO = "+33611223344"


class TransportDeTest(Transport):
    def __init__(self):
        super().__init__()
        self.evenements: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                self.evenements.append(json.loads(item.payload.get_bytes()))


@pytest.fixture()
def sentry(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", DSN)
    transport = TransportDeTest()
    assert erreurs.initialiser(transport=transport)
    yield transport
    sentry_sdk.get_global_scope().set_client(None)
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_current_scope().clear()


def test_sans_dsn_rien_ne_s_initialise(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    with patch("sentry_sdk.init") as init:
        assert erreurs.initialiser() is False
        erreurs.contexte_appel("t1", "CA1")
        erreurs.capturer(RuntimeError("x"))
    init.assert_not_called()


def test_le_filtre_retire_numero_corps_cookies_et_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-123456")
    evenement = {
        "message": f"rappel de {NUMERO} raté",
        "request": {"url": "http://x/twilio/voice", "data": {"From": NUMERO},
                    "cookies": {"session": "abc"}, "query_string": f"From={NUMERO}",
                    "headers": {"User-Agent": "Twilio", "Cookie": "session=abc",
                                "X-Twilio-Signature": "sig"}},
        "exception": {"values": [{"type": "RuntimeError",
                                  "value": "appel 06 11 22 33 44 avec sk-secret-123456",
                                  "stacktrace": {"frames": [{"function": "f", "vars": {"a": NUMERO}}]}}]},
        "breadcrumbs": {"values": [{"message": f"vers {NUMERO}", "data": {"to": "+33 6 11 22 33 44"}}]},
    }
    sortie = erreurs.avant_envoi(evenement)
    texte = json.dumps(sortie, ensure_ascii=False)
    assert "6 11 22 33 44" not in texte and "611223344" not in texte
    assert "sk-secret" not in texte and "abc" not in texte and "sig" not in texte
    assert sortie["request"]["headers"] == {"User-Agent": "Twilio"}
    assert "data" not in sortie["request"] and "query_string" not in sortie["request"]
    assert "[numéro]" in sortie["message"]


def test_un_filtre_qui_leve_jette_l_evenement():
    # Un « message » qui n'est pas une chaîne et un « request » qui n'est pas un dict.
    assert erreurs.avant_envoi({"request": "pas un dict"}) is None


def test_la_configuration_est_la_plus_sobre(sentry):
    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] == 0
    assert options["environment"] == "production"


def test_une_tache_de_fond_en_erreur_arrive_avec_ses_tags_sans_numero(sentry):
    async def echec():
        raise RuntimeError(f"échec pour {NUMERO}")

    async def scenario():
        erreurs.contexte_appel("etab-7", "CAtache")
        taches.lancer(echec(), nom="essai")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    sentry_sdk.flush()
    assert len(sentry.evenements) == 1
    evenement = sentry.evenements[0]
    assert evenement["tags"]["etablissement"] == "etab-7"
    assert evenement["tags"]["appel"] == "CAtache"
    assert NUMERO not in json.dumps(evenement, ensure_ascii=False)


def test_une_erreur_du_flux_vocal_arrive_avec_etablissement_et_appel(sentry):
    from test_voice_stream import _attendre_le_bot, _twilio_start_message

    tenant = tenants.create_tenant("Chez Sentry", f"+3363{id(object()) % 10_000_000:07d}")
    sid = f"CA{uuid.uuid4().hex}"
    run_bot = AsyncMock(side_effect=RuntimeError(f"panne chez {NUMERO}"))
    try:
        with patch("app.main._get_bot_runner", return_value=run_bot):
            try:
                with TestClient(app).websocket_connect("/ws/voice") as ws:
                    ws.send_text(json.dumps(_twilio_start_message(
                        to=tenant.phone_number, call_sid=sid, from_number=NUMERO)))
                    _attendre_le_bot(run_bot)
                    ws.receive_text()
            except Exception:
                pass  # le serveur ferme le flux : c'est ce qu'on attend
        sentry_sdk.flush()
        evenements = [e for e in sentry.evenements if e.get("tags", {}).get("appel") == sid]
        assert len(evenements) == 1
        assert evenements[0]["tags"]["etablissement"] == str(tenant.id)
        assert NUMERO not in json.dumps(evenements[0], ensure_ascii=False)
    finally:
        tenants.delete_tenant(tenant.id)
