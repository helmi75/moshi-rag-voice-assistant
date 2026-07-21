"""Le numéro de l'appelant (Twilio From) est rattaché d'office comme customer_phone de la
réservation, quelle que soit la qualité de transcription du nom. Aucun réseau, tout mocké."""
import asyncio
import json

from app.voice import bot


class _FakeParams:
    """Imite pipecat FunctionCallParams : function_name, arguments, result_callback."""

    def __init__(self, name, arguments):
        self.function_name = name
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result):
        self.result = result


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _handler(monkeypatch, caller_number):
    captured = {}

    async def fake_run_tool(tenant, name, args):
        captured["name"], captured["args"] = name, args
        return json.dumps({"status": "confirmed", "reservation_id": 7})

    monkeypatch.setattr(bot.llm, "run_tool", fake_run_tool)
    handler = bot.make_tool_handler(object(), [], caller_number=caller_number)
    return handler, captured


def test_caller_number_injected_into_reservation(monkeypatch):
    handler, captured = _handler(monkeypatch, "+33612345678")
    _run(handler(_FakeParams("create_reservation",
         {"customer_name": "Dupont", "date": "2026-07-21", "time": "20:00", "party_size": 2})))
    assert captured["args"]["customer_phone"] == "+33612345678"


def test_caller_number_overrides_any_llm_value(monkeypatch):
    """La source de vérité est le numéro Twilio, pas ce que le modèle aurait pu inventer."""
    handler, captured = _handler(monkeypatch, "+33612345678")
    _run(handler(_FakeParams("create_reservation",
         {"customer_name": "X", "date": "2026-07-21", "time": "20:00", "party_size": 2,
          "customer_phone": "numero-invente"})))
    assert captured["args"]["customer_phone"] == "+33612345678"


def test_no_caller_number_leaves_phone_absent(monkeypatch):
    """Numéro masqué/absent : on n'invente rien (le modèle garde son éventuelle valeur)."""
    handler, captured = _handler(monkeypatch, None)
    _run(handler(_FakeParams("create_reservation",
         {"customer_name": "X", "date": "2026-07-21", "time": "20:00", "party_size": 2})))
    assert "customer_phone" not in captured["args"]


def test_not_injected_for_check_availability(monkeypatch):
    handler, captured = _handler(monkeypatch, "+33612345678")
    _run(handler(_FakeParams("check_availability",
         {"date": "2026-07-21", "time": "20:00", "party_size": 2})))
    assert "customer_phone" not in captured["args"]
