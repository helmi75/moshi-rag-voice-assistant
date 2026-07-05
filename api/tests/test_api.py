"""Tests de l'API vocale multi-tenant.

Exécuter depuis api/ avec : pytest tests/ -v
Le LLM est mocké partout — aucun appel réseau.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import llm, reservations, tenants
from app.main import app

client = TestClient(app)

DEMO_NUMBER = "+33100000000"


def _text_response(text):
    """Fabrique une réponse Anthropic minimale (fin de tour, texte seul)."""
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
    )


def _tool_response(name, tool_input, tool_id="toolu_01"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name=name, input=tool_input, id=tool_id)],
    )


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


class TestLLMToolLoop:
    def test_tool_use_then_final_answer(self):
        """Le modèle demande create_reservation, l'outil s'exécute, puis il conclut."""
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        fake_client = SimpleNamespace(
            messages=SimpleNamespace(
                create=AsyncMock(
                    side_effect=[
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
                    ]
                )
            )
        )
        with patch.object(llm, "get_client", return_value=fake_client):
            import asyncio

            text, messages = asyncio.run(
                llm.respond(tenant, [], "Je voudrais réserver pour 4 ce soir")
            )

        assert text == "C'est réservé pour quatre personnes."
        assert fake_client.messages.create.await_count == 2
        # le tool_result a bien été renvoyé au modèle (dans l'historique retourné :
        # user → assistant(tool_use) → user(tool_result) → assistant(final))
        assert messages[2]["content"][0]["type"] == "tool_result"
        # et la réservation est en base
        rows = reservations.list_reservations(tenant.id)
        assert any(r["customer_name"] == "Durand" and r["party_size"] == 4 for r in rows)


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
