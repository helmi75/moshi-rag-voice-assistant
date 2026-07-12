"""Tests du mode streaming (Twilio Media Streams + Pipecat).

Tout est mocké : aucun appel réseau, le bot Pipecat n'est jamais réellement lancé.
Exécuter depuis api/ avec : pytest tests/ -v
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import llm, reservations, tenants
from app.main import app
from app.voice import bot

client = TestClient(app)

DEMO_NUMBER = "+33100000000"


def _twilio_start_message(to=DEMO_NUMBER, stream_sid="MZ123", call_sid="CA_stream_1"):
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": stream_sid,
        "start": {
            "accountSid": "AC000",
            "streamSid": stream_sid,
            "callSid": call_sid,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"To": to, "CallSid": call_sid},
        },
    }


class TestStreamTwiML:
    """TwiML renvoyé par /twilio/voice en mode stream."""

    def test_stream_mode_returns_connect_stream(self, monkeypatch):
        monkeypatch.setenv("VOICE_MODE", "stream")
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://assistant.example.com/ws/voice")
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER}
        )
        assert response.status_code == 200
        body = response.text
        assert "<Connect>" in body
        assert '<Stream url="wss://assistant.example.com/ws/voice">' in body
        assert f'<Parameter name="To" value="{DEMO_NUMBER}"/>' in body
        assert '<Parameter name="CallSid" value="CA1"/>' in body
        assert "<Gather" not in body

    def test_stream_mode_unknown_tenant_hangs_up(self, monkeypatch):
        monkeypatch.setenv("VOICE_MODE", "stream")
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA1", "To": "+19999999999"}
        )
        assert "<Hangup/>" in response.text
        assert "<Connect>" not in response.text

    def test_stream_url_falls_back_to_request_host(self, monkeypatch):
        monkeypatch.setenv("VOICE_MODE", "stream")
        monkeypatch.delenv("PUBLIC_WS_URL", raising=False)
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER}
        )
        assert 'url="wss://testserver/ws/voice"' in response.text

    def test_default_mode_is_still_gather(self, monkeypatch):
        monkeypatch.delenv("VOICE_MODE", raising=False)
        response = client.post(
            "/twilio/voice", data={"CallSid": "CA1", "To": DEMO_NUMBER}
        )
        assert "<Gather" in response.text
        assert "<Connect>" not in response.text

    def test_stream_mode_applies_to_generic_webhook_too(self, monkeypatch):
        monkeypatch.setenv("VOICE_MODE", "stream")
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://assistant.example.com/ws/voice")
        response = client.post(
            "/twilio/webhook", data={"CallSid": "CA1", "To": DEMO_NUMBER}
        )
        assert "<Connect>" in response.text


class TestVoiceWebSocket:
    """Poignée de main Twilio sur /ws/voice (bot mocké)."""

    def test_start_message_launches_bot_with_tenant(self):
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected", "protocol": "Call"}))
                ws.send_text(json.dumps(_twilio_start_message()))
        assert run_bot.await_count == 1
        _ws, stream_sid, call_sid, tenant = run_bot.await_args.args
        assert stream_sid == "MZ123"
        assert call_sid == "CA_stream_1"
        assert tenant.phone_number == DEMO_NUMBER

    def test_unknown_tenant_closes_without_bot(self):
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps(_twilio_start_message(to="+19999999999")))
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1008
        run_bot.assert_not_awaited()

    def test_missing_stream_sid_closes(self):
        run_bot = AsyncMock()
        message = _twilio_start_message()
        del message["streamSid"]
        del message["start"]["streamSid"]
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps(message))
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1008
        run_bot.assert_not_awaited()

    def test_malformed_json_closes_with_protocol_error(self):
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text("ceci n'est pas du JSON")
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1002
        run_bot.assert_not_awaited()

    def test_no_start_within_limit_closes(self):
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                for _ in range(11):
                    ws.send_text(json.dumps({"event": "media", "media": {}}))
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1002
        run_bot.assert_not_awaited()

    def test_bot_crash_is_contained(self):
        run_bot = AsyncMock(side_effect=RuntimeError("boom pipeline"))
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps(_twilio_start_message()))
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1011


class TestSharedTools:
    """llm.run_tool : logique métier partagée entre les deux modes vocaux."""

    def _tenant(self):
        return tenants.get_by_phone(DEMO_NUMBER)

    def test_create_reservation_persists(self):
        result = asyncio.run(
            llm.run_tool(
                self._tenant(),
                "create_reservation",
                {
                    "customer_name": "Streaming Client",
                    "date": "2026-08-01",
                    "time": "20:30",
                    "party_size": 3,
                },
            )
        )
        payload = json.loads(result)
        assert payload["status"] == "confirmed"
        rows = reservations.list_reservations(self._tenant().id)
        assert any(r["customer_name"] == "Streaming Client" for r in rows)

    def test_check_availability_counts_covers(self):
        tenant = self._tenant()
        reservations.create_reservation(
            tenant_id=tenant.id, customer_name="X", date="2026-08-02",
            time="20:00", party_size=4,
        )
        result = asyncio.run(
            llm.run_tool(
                tenant, "check_availability",
                {"date": "2026-08-02", "time": "20:00", "party_size": 2},
            )
        )
        payload = json.loads(result)
        assert payload["available"] is True
        assert payload["covers_already_booked"] >= 4

    def test_unknown_tool_returns_error_json(self):
        result = asyncio.run(llm.run_tool(self._tenant(), "explode", {}))
        assert "outil inconnu" in json.loads(result)["error"]


class TestPipecatToolBridge:
    """Le pont voice/bot.py entre Pipecat et le cerveau métier."""

    def test_handler_runs_tool_and_calls_result_callback(self):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        handler = bot.make_tool_handler(tenant)
        callback = AsyncMock()
        params = SimpleNamespace(
            function_name="create_reservation",
            arguments={
                "customer_name": "Via Pipecat",
                "date": "2026-08-03",
                "time": "19:00",
                "party_size": 2,
            },
            result_callback=callback,
        )
        asyncio.run(handler(params))
        callback.assert_awaited_once()
        payload = json.loads(callback.await_args.args[0])
        assert payload["status"] == "confirmed"
        rows = reservations.list_reservations(tenant.id)
        assert any(r["customer_name"] == "Via Pipecat" for r in rows)

    def test_handler_reports_tool_failure_via_callback(self):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        handler = bot.make_tool_handler(tenant)
        callback = AsyncMock()
        params = SimpleNamespace(
            function_name="create_reservation",
            arguments={},  # champs obligatoires manquants -> KeyError dans run_tool
            result_callback=callback,
        )
        asyncio.run(handler(params))
        callback.assert_awaited_once()
        assert "Erreur outil" in callback.await_args.args[0]

    def test_function_schemas_match_llm_tools(self):
        schemas = bot.build_function_schemas()
        assert {s.name for s in schemas} == {t["name"] for t in llm.TOOLS}
        by_name = {s.name: s for s in schemas}
        for tool in llm.TOOLS:
            schema = by_name[tool["name"]]
            assert schema.properties == tool["input_schema"]["properties"]
            assert schema.required == tool["input_schema"]["required"]
