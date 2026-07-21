"""Tests de l'API vocale multi-tenant.

Exécuter depuis api/ avec : pytest tests/ -v
Le LLM est mocké partout — aucun appel réseau.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import llm, reservations, tenants
from app.main import app

client = TestClient(app)

DEMO_NUMBER = "+33100000000"


def _text_response(text):
    """Fabrique une réponse Chat Completions minimale (fin de tour, texte seul)."""
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_response(name, tool_input, tool_id="call_01"):
    """Fabrique une réponse Chat Completions demandant un appel d'outil.

    `function.arguments` est une chaîne JSON (format réel OpenAI/OpenRouter),
    pas un dict — contrairement à l'ancien format Anthropic."""
    tool_call = SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(tool_input)),
    )
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TestHealth:
    def test_health_returns_ok(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "model" in data


class TestTenantRouting:
    def test_demo_tenant_seeded(self):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        assert tenant is not None
        assert tenant.business_type == "restaurant"

    def test_seed_realigns_demo_number_on_restart(self, monkeypatch):
        # Un premier démarrage a pu figer un mauvais numéro dans le volume ;
        # au redémarrage avec le bon TWILIO_NUMBER, le tenant démo doit suivre.
        new_number = "+19998887777"
        monkeypatch.setattr(tenants, "DEMO_TENANT_NUMBER", new_number)
        tenants.seed_demo_tenant()
        assert tenants.get_by_phone(new_number) is not None
        # Restaure le numéro de démo pour ne pas perturber les autres tests.
        monkeypatch.setattr(tenants, "DEMO_TENANT_NUMBER", DEMO_NUMBER)
        tenants.seed_demo_tenant()
        assert tenants.get_by_phone(DEMO_NUMBER) is not None
        assert tenants.get_by_phone(new_number) is None

    def test_seed_preserves_customized_greeting(self):
        """Un accueil personnalisé (greeting_customized=1) ne doit JAMAIS être écrasé au
        redémarrage : sinon le restaurateur reperd son accueil à chaque restart du conteneur."""
        demo = tenants.get_by_phone(DEMO_NUMBER)
        custom = "Bonjour et bienvenue au Fouquet's, un instant s'il vous plaît."
        tenants.update_tenant(demo.id, greeting=custom, greeting_customized=1)
        tenants.seed_demo_tenant()  # simule un redémarrage
        assert tenants.get_by_id(demo.id).greeting == custom
        # Restaure l'état par défaut pour l'isolation des autres tests.
        tenants.update_tenant(demo.id, greeting=tenants._DEMO_GREETING, greeting_customized=0)

    def test_seed_realigns_non_customized_greeting(self):
        """Un accueil NON personnalisé (vieux défaut figé dans le volume) est bien réaligné
        sur le défaut courant au redémarrage."""
        demo = tenants.get_by_phone(DEMO_NUMBER)
        tenants.update_tenant(demo.id, greeting="Vieux défaut périmé.", greeting_customized=0)
        tenants.seed_demo_tenant()
        assert tenants.get_by_id(demo.id).greeting == tenants._DEMO_GREETING

    def test_unknown_number_hangs_up(self):
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA1", "To": "+19999999999"}
        )
        assert response.status_code == 200
        assert "<Hangup/>" in response.text
        assert "<Gather" not in response.text

    def test_missing_to_hangs_up(self):
        response = client.post("/twilio/voice", data={"CallSid": "CA1"})
        assert "<Hangup/>" in response.text


class TestStreamWsUrl:
    def test_strips_stray_space_in_public_ws_url(self, monkeypatch):
        # Une espace collée par erreur dans .env ne doit pas casser l'URL Twilio.
        from app import main
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://6e24.ngrok-free.app /ws/voice")
        assert main._stream_ws_url(None) == "wss://6e24.ngrok-free.app/ws/voice"

    def test_strips_non_breaking_space(self, monkeypatch):
        # Espace insécable \xa0 (copier-coller depuis un navigateur/chat).
        from app import main
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://6e24.ngrok-free.app\xa0/ws/voice")
        assert main._stream_ws_url(None) == "wss://6e24.ngrok-free.app/ws/voice"


class TestVoiceWebhook:
    def test_initial_call_greets_and_gathers(self):
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA100", "To": DEMO_NUMBER}
        )
        assert response.status_code == 200
        assert "text/xml" in response.headers["content-type"]
        assert "<Gather" in response.text
        assert "Fouquet" in response.text
        assert 'language="fr-FR"' in response.text

    def test_greeting_uses_neural_voice_by_default(self):
        # Par défaut, la voix neuronale Amazon Polly (Léa) est utilisée pour le <Say>.
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA100b", "To": DEMO_NUMBER}
        )
        assert 'voice="Polly.Lea-Neural"' in response.text

    def test_voice_is_configurable(self, monkeypatch):
        monkeypatch.setenv("TWILIO_VOICE", "Polly.Remi-Neural")
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA100c", "To": DEMO_NUMBER}
        )
        assert 'voice="Polly.Remi-Neural"' in response.text

    def test_voice_can_fallback_to_standard(self, monkeypatch):
        monkeypatch.setenv("TWILIO_VOICE", "")
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA100d", "To": DEMO_NUMBER}
        )
        assert "voice=" not in response.text
        assert 'language="fr-FR"' in response.text

    def test_speech_result_calls_llm(self):
        with patch.object(llm, "respond", new=AsyncMock(return_value=("Bien sûr !", []))) as mock:
            response = client.post(
                "/twilio/voice",
                data={
                    "CallSid": "CA101",
                    "To": DEMO_NUMBER,
                    "SpeechResult": "Quels sont vos horaires ?",
                },
            )
        assert response.status_code == 200
        assert "Bien sûr !" in response.text
        assert "<Gather" in response.text
        assert mock.await_args.args[2] == "Quels sont vos horaires ?"

    def test_conversation_history_is_kept_per_call(self):
        history_after_turn_1 = [{"role": "user", "content": "t1"},
                                {"role": "assistant", "content": "r1"}]
        with patch.object(
            llm, "respond", new=AsyncMock(return_value=("ok", history_after_turn_1))
        ):
            client.post(
                "/twilio/voice",
                data={"CallSid": "CA102", "To": DEMO_NUMBER, "SpeechResult": "t1"},
            )
        with patch.object(llm, "respond", new=AsyncMock(return_value=("ok", []))) as mock2:
            client.post(
                "/twilio/voice",
                data={"CallSid": "CA102", "To": DEMO_NUMBER, "SpeechResult": "t2"},
            )
        # le 2e tour reçoit l'historique sauvegardé au 1er tour
        assert mock2.await_args.args[1] == history_after_turn_1

    def test_llm_failure_returns_polite_error(self):
        with patch.object(llm, "respond", new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = client.post(
                "/twilio/voice",
                data={"CallSid": "CA103", "To": DEMO_NUMBER, "SpeechResult": "Bonjour"},
            )
        assert response.status_code == 200
        assert "problème technique" in response.text

    def test_xml_is_escaped(self):
        with patch.object(
            llm, "respond", new=AsyncMock(return_value=("a < b & c", []))
        ):
            response = client.post(
                "/twilio/voice",
                data={"CallSid": "CA104", "To": DEMO_NUMBER, "SpeechResult": "test"},
            )
        assert "a &lt; b &amp; c" in response.text


def _fake_openai_client(*responses):
    create = AsyncMock(side_effect=list(responses))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class TestLLMToolLoop:
    def test_tool_use_then_final_answer(self):
        """Le modèle demande create_reservation, l'outil s'exécute, puis il conclut."""
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        fake_client = _fake_openai_client(
            _tool_response(
                "create_reservation",
                {
                    "customer_name": "Durand",
                    "date": "2026-07-10",
                    "time": "20:00",
                    "party_size": 4,
                },
            ),
            _text_response("C'est réservé pour quatre personnes."),
        )
        with patch.object(llm, "get_client", return_value=fake_client):
            import asyncio

            text, messages = asyncio.run(
                llm.respond(tenant, [], "Je voudrais réserver pour 4 ce soir")
            )

        assert text == "C'est réservé pour quatre personnes."
        assert fake_client.chat.completions.create.await_count == 2
        # historique retourné : user → assistant(tool_calls) → tool(résultat) → assistant(final)
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["tool_calls"][0]["function"]["name"] == "create_reservation"
        assert messages[2]["role"] == "tool"
        assert json.loads(messages[2]["content"])["status"] == "confirmed"
        assert messages[3]["role"] == "assistant"
        # et la réservation est en base
        rows = reservations.list_reservations(tenant.id)
        assert any(r["customer_name"] == "Durand" and r["party_size"] == 4 for r in rows)

    def test_malformed_tool_arguments_do_not_crash(self):
        """Des arguments JSON invalides renvoyés par le modèle -> traités comme {} plutôt que de planter le tour."""
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        tool_call = SimpleNamespace(
            id="call_bad",
            function=SimpleNamespace(name="check_availability", arguments="{not valid json"),
        )
        bad_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
        )
        fake_client = _fake_openai_client(
            bad_response, _text_response("Un instant, je vérifie autrement.")
        )
        with patch.object(llm, "get_client", return_value=fake_client):
            import asyncio

            text, messages = asyncio.run(llm.respond(tenant, [], "Vous avez de la place ?"))

        assert text == "Un instant, je vérifie autrement."
        # l'outil a bien été appelé avec des args vides (KeyError -> message d'erreur en tool result)
        assert messages[2]["role"] == "tool"
        assert "Erreur" in messages[2]["content"]

    def test_openai_tools_schema_matches_tools(self):
        schemas = llm._openai_tools()
        assert {s["function"]["name"] for s in schemas} == {t["name"] for t in llm.TOOLS}
        for schema, tool in zip(schemas, llm.TOOLS):
            assert schema["type"] == "function"
            assert schema["function"]["parameters"] == tool["input_schema"]


class TestSMSWebhook:
    def test_sms_uses_llm(self):
        with patch.object(llm, "respond", new=AsyncMock(return_value=("Réponse SMS", []))):
            response = client.post(
                "/twilio/sms",
                data={"Body": "Bonjour", "From": "+33612345678", "To": DEMO_NUMBER},
            )
        assert response.status_code == 200
        assert "<Message>Réponse SMS</Message>" in response.text

    def test_sms_unknown_number(self):
        response = client.post(
            "/twilio/sms", data={"Body": "Bonjour", "To": "+19999999999"}
        )
        assert "pas encore configuré" in response.text


class TestGenericWebhook:
    def test_callsid_routes_to_voice(self):
        response = client.post(
            "/twilio/webhook", data={"CallSid": "CA200", "To": DEMO_NUMBER}
        )
        assert response.status_code == 200
        assert "<Gather" in response.text

    def test_body_routes_to_sms(self):
        with patch.object(llm, "respond", new=AsyncMock(return_value=("OK", []))):
            response = client.post(
                "/twilio/webhook", data={"Body": "Test", "To": DEMO_NUMBER}
            )
        assert "<Message>" in response.text

    def test_unknown_payload_returns_empty_response(self):
        response = client.post("/twilio/webhook", data={})
        assert response.status_code == 200
        assert "<Response></Response>" in response.text


class TestReservationsEndpoint:
    def test_list_reservations(self):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        reservations.create_reservation(
            tenant_id=tenant.id,
            customer_name="Martin",
            date="2026-07-12",
            time="19:30",
            party_size=2,
        )
        response = client.get(f"/tenants/{tenant.id}/reservations")
        assert response.status_code == 200
        names = [r["customer_name"] for r in response.json()["reservations"]]
        assert "Martin" in names

    def test_unknown_tenant_404(self):
        assert client.get("/tenants/9999/reservations").status_code == 404
