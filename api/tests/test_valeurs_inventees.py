"""Une réservation ne s'enregistre pas avec des valeurs que personne n'a dites (ASSISTANTE-91).

Appel 163, le 25/09/2026 à 08:53. L'appelant ne donne que son nom (« It's gonna be
Raymond ») ; le modèle appelle aussitôt `create_reservation` pour 1 personne à 08:50,
l'heure qu'il était. Rien ne s'est écrit, mais seulement parce que 08:50 était déjà passé :
avec une heure inventée dans l'avenir, la table existait.

Le serveur vérifie donc que le nombre de personnes et l'heure ont été prononcés dans la
conversation, par le client ou par l'assistante (« j'ai vingt heures, ça vous va ? » —
« oui » suffit). Sans la conversation (SMS, tests anciens), rien ne change.
"""
import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import db, llm, tenants
from app.voice import bot

APPELANT = "+33612345678"


@pytest.fixture()
def resto(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "inventees.db")), \
            patch.object(llm.notifications, "planifier"):
        db.init_db()
        yield tenants.create_tenant("Chez Raymond", "+33199000555")


def _demain() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def _creer(tenant, conversation, heure="20:00", couverts=4) -> dict:
    return json.loads(asyncio.run(llm.run_tool(
        tenant, "create_reservation",
        {"customer_name": "Raymond", "date": _demain(), "time": heure, "party_size": couverts},
        APPELANT, conversation=conversation)))


class TestLAppel163:
    def test_rien_n_est_cree_quand_seul_le_nom_a_ete_dit(self, resto):
        r = _creer(resto, "Bonjour, Chez Raymond, je vous écoute. It's gonna be Raymond.",
                   heure="21:50", couverts=1)
        assert "reservation_id" not in r
        assert "personnes" in json.dumps(r, ensure_ascii=False)

    def test_le_refus_nomme_ce_qui_manque_et_seulement_ca(self, resto):
        r = _creer(resto, "Une table pour quatre personnes s'il vous plaît.", heure="21:50")
        texte = json.dumps(r, ensure_ascii=False)
        assert "reservation_id" not in r
        assert "heure" in texte and "personnes" not in texte


class TestCeQuiEstDitPasse:
    @pytest.mark.parametrize("conversation", [
        "Une table pour quatre demain à vingt heures.",
        "On sera 4, vers 20h.",
        "Nous serons quatre. — J'ai 20:00 de libre, ça vous va ? — Oui parfait.",
        "A table for four tomorrow at 8 pm please.",
        "Pour quatre personnes à huit heures du soir.",
    ])
    def test_valeurs_prononcees(self, resto, conversation):
        assert _creer(resto, conversation)["status"] == "confirmed"

    def test_une_personne_dite_en_clair(self, resto):
        assert _creer(resto, "Je serai seul, à vingt heures.", couverts=1)["status"] == "confirmed"

    def test_un_ne_vaut_pas_une_personne(self, resto):
        # « un instant », « une table » : le mot est partout, il ne dit pas le nombre.
        r = _creer(resto, "Un instant. Une table à vingt heures.", couverts=1)
        assert "reservation_id" not in r

    def test_midi(self, resto):
        assert _creer(resto, "Pour deux, à midi.", heure="12:00", couverts=2)["status"] == "confirmed"

    def test_sans_conversation_rien_ne_change(self, resto):
        r = json.loads(asyncio.run(llm.run_tool(
            resto, "create_reservation",
            {"customer_name": "Raymond", "date": _demain(), "time": "20:00", "party_size": 1},
            APPELANT)))
        assert r["status"] == "confirmed"


class TestLeTexteDeLaConversation:
    def test_le_prompt_systeme_et_les_outils_ne_comptent_pas(self):
        # Le prompt cite les horaires (« 19h-23h ») : il rendrait toute heure « dite ».
        messages = [
            {"role": "system", "content": "Horaires : 19h-23h, 8 couverts max."},
            {"role": "user", "content": "It's gonna be Raymond"},
            {"role": "tool", "content": '{"autres_horaires": ["20:00"]}'},
            {"role": "assistant", "content": [{"type": "text", "text": "Bien noté."}]},
        ]
        texte = bot.texte_de_la_conversation(SimpleNamespace(get_messages=lambda: messages))
        assert "Raymond" in texte and "Bien noté" in texte
        assert "19h" not in texte and "20:00" not in texte

    def test_un_contexte_illisible_rend_none(self):
        assert bot.texte_de_la_conversation(object()) is None
