"""Le numéro de l'appelant (Twilio `From`) est la source de vérité — aucun réseau, tout mocké.

Ce numéro sert à DEUX choses, et la seconde est arrivée avec #33 :

1. rattacher la réservation à un contact fiable, quelle que soit la qualité de
   transcription du nom sur du 8 kHz ;
2. **autoriser** la modification et l'annulation ultérieures. C'est ce qui en fait un
   sujet de sécurité et non de confort.

Ces tests visaient à l'origine le handler Pipecat, seul endroit où le numéro était
injecté. Il l'est désormais dans `llm.run_tool` : le mode `gather` n'emprunte pas le
handler et laissait donc passer la valeur proposée par le modèle. Les tests visent le
CONTRAT — « quel que soit le chemin, le numéro vient du réseau » — plutôt que l'endroit.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from app import db, llm, reservations, tenants
from app.voice import bot


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "caller.db")):
        db.init_db()
        yield


@pytest.fixture()
def resto(base):
    return tenants.create_tenant("Chez Numéro", "+33199000222")


def _creer(tenant, args, numero):
    return asyncio.run(llm.run_tool(tenant, "create_reservation", args, numero))


ARGS = {"customer_name": "Dupont", "date": "2026-12-24", "time": "20:00", "party_size": 2}


def test_le_numero_du_reseau_est_rattache(resto):
    _creer(resto, dict(ARGS), "+33612345678")
    assert reservations.list_reservations(resto.id)[0]["customer_phone"] == "+33612345678"


def test_il_ecrase_toute_valeur_venue_du_modele(resto):
    """Le modèle ne devrait plus pouvoir en proposer (le champ a quitté le schéma), mais
    s'il en glissait une malgré tout, elle ne doit pas passer."""
    _creer(resto, {**ARGS, "customer_phone": "numero-invente"}, "+33612345678")
    assert reservations.list_reservations(resto.id)[0]["customer_phone"] == "+33612345678"


def test_un_appel_masque_n_invente_rien(resto):
    _creer(resto, dict(ARGS), None)
    assert reservations.list_reservations(resto.id)[0]["customer_phone"] is None


def test_les_outils_de_consultation_ne_sont_pas_affectes(resto):
    """`check_availability` ne crée rien : le numéro ne doit pas s'y glisser."""
    resultat = json.loads(asyncio.run(llm.run_tool(
        resto, "check_availability",
        {"date": "2026-12-24", "time": "20:00", "party_size": 2}, "+33612345678")))
    assert "available" in resultat
    assert reservations.list_reservations(resto.id) == []


class TestLeHandlerTransmetLeNumero:
    """Le handler Pipecat n'injecte plus le numéro lui-même — il le TRANSMET. S'il
    cessait de le faire, tout ce qui précède resterait vert et la production serait
    cassée : le pipeline vocal est le seul chemin réellement emprunté."""

    class _FakeParams:
        def __init__(self, name, arguments):
            self.function_name, self.arguments, self.result = name, arguments, None

        async def result_callback(self, result):
            self.result = result

    def test_le_numero_parvient_a_run_tool(self, monkeypatch):
        recu = {}

        async def faux_run_tool(tenant, name, args, caller_number=None, call_id=None):
            recu["numero"] = caller_number
            recu["call_id"] = call_id
            return json.dumps({"status": "confirmed", "reservation_id": 7})

        monkeypatch.setattr(bot.llm, "run_tool", faux_run_tool)
        handler = bot.make_tool_handler(object(), [], caller_number="+33612345678",
                                        call_id=42)
        asyncio.new_event_loop().run_until_complete(
            handler(self._FakeParams("create_reservation", dict(ARGS))))
        assert recu["numero"] == "+33612345678"
        # `call_id` suit le même chemin : c'est lui qui rattache un message pris à
        # l'appel qui l'a produit, donc à l'enregistrement qu'on pourra réécouter.
        assert recu["call_id"] == 42
