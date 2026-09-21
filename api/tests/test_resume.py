"""Résumé d'appel (app/resume.py).

La colonne `calls.summary` existait sans que rien ne l'écrive : 141 appels, 141 NULL.
Ce qui compte ici, c'est qu'un résumé raté ne coûte RIEN — l'appel est déjà terminé et
la liste doit simplement retomber sur l'ancien extrait.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import calls, db, resume, tenants


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "resume.db")):
        db.init_db()
        yield


@pytest.fixture()
def appel(base):
    """Un appel clôturé, avec une transcription — le cas normal."""
    resto = tenants.create_tenant("Chez Résumé", "+33199000333")
    calls.start_call("CA-resume", resto.id)
    return calls.finish_call("CA-resume", "completed", [
        {"role": "assistant", "content": "Bonjour, restaurant Chez Résumé."},
        {"role": "user", "content": "Bonjour, je voudrais une table pour deux demain à 20 h."},
        {"role": "assistant", "content": "C'est enregistré, au nom de Durand."},
    ])


def _client(texte):
    """Un client OpenRouter bouchonné qui rend `texte`."""
    faux = MagicMock()
    faux.chat.completions.create = _async(SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=texte))]))
    return faux


def _async(valeur):
    async def appeler(*args, **kwargs):
        if isinstance(valeur, Exception):
            raise valeur
        return valeur
    return appeler


def _resume(call_id):
    return db.get_conn().execute(
        "SELECT summary FROM calls WHERE id = ?", (call_id,)).fetchone()["summary"]


class TestDialogue:
    def test_la_transcription_devient_un_dialogue_nomme(self):
        dialogue = resume.en_dialogue([
            {"role": "assistant", "content": "Bonjour."},
            {"role": "user", "content": "Une table pour deux."},
        ])
        assert dialogue == "Assistante : Bonjour.\nClient : Une table pour deux."

    def test_un_appel_sans_un_mot_du_client_ne_se_resume_pas(self):
        """L'appelant a raccroché à l'accueil : « l'appelant n'a rien dit » occuperait
        la place dans la liste sans rien apprendre."""
        assert resume.en_dialogue([{"role": "assistant", "content": "Bonjour."}]) == ""
        assert resume.en_dialogue([]) == ""
        assert resume.en_dialogue(None) == ""

    def test_les_tours_vides_et_les_intrus_sont_ignores(self):
        dialogue = resume.en_dialogue([
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "   "},
            "pas un message",
            {"role": "user", "content": "Bonsoir"},
        ])
        assert dialogue == "Client : Bonsoir"


class TestResumer:
    def test_le_resume_est_ecrit_en_base(self, appel):
        with patch.object(resume.llm, "get_client", lambda: _client(
                "Réservation pour deux personnes demain à 20 h au nom de Durand.")):
            texte = asyncio.run(resume.resumer(appel))
        assert texte.startswith("Réservation pour deux")
        assert _resume(appel) == texte

    def test_un_modele_en_panne_laisse_l_appel_intact(self, appel):
        """L'appelant a raccroché : rien de ce qui se passe ici n'a le droit de remonter."""
        faux = MagicMock()
        faux.chat.completions.create = _async(RuntimeError("502 upstream"))
        with patch.object(resume.llm, "get_client", lambda: faux):
            assert asyncio.run(resume.resumer(appel)) is None
        assert _resume(appel) is None

    def test_une_reponse_vide_n_ecrit_rien(self, appel):
        with patch.object(resume.llm, "get_client", lambda: _client("   ")):
            assert asyncio.run(resume.resumer(appel)) is None
        assert _resume(appel) is None

    def test_un_appel_sans_transcription_n_appelle_pas_le_modele(self, base):
        resto = tenants.create_tenant("Chez Coupé", "+33199000334")
        calls.start_call("CA-coupe", resto.id)
        call_id = calls.finish_call("CA-coupe", "failed", None)
        faux = MagicMock()
        with patch.object(resume.llm, "get_client", lambda: faux):
            assert asyncio.run(resume.resumer(call_id)) is None
        faux.chat.completions.create.assert_not_called()


class TestPlanification:
    def test_la_cloture_lance_le_resume_en_tache_de_fond(self, appel, monkeypatch):
        lancees = []
        monkeypatch.setattr(resume.taches, "lancer",
                            lambda coro, nom: (coro.close(), lancees.append(nom)))
        resume.planifier(appel)
        assert lancees and str(appel) in lancees[0]

    def test_sans_appel_rien_n_est_lance(self, monkeypatch):
        lancees = []
        monkeypatch.setattr(resume.taches, "lancer",
                            lambda coro, nom: (coro.close(), lancees.append(nom)))
        resume.planifier(None)
        assert lancees == []

    def test_on_peut_couper_la_fonction_et_sa_depense(self, appel, monkeypatch):
        monkeypatch.setenv("RESUME_APPELS", "0")
        lancees = []
        monkeypatch.setattr(resume.taches, "lancer",
                            lambda coro, nom: (coro.close(), lancees.append(nom)))
        resume.planifier(appel)
        assert lancees == []
        assert resume.actif() is False


class TestAffichage:
    def test_la_liste_montre_le_resume_quand_il_existe(self):
        from app.admin import presenters

        vue = presenters.call_view({
            "summary": "Réservation pour deux demain à 20 h.",
            "transcript": json.dumps([{"role": "user", "content": "Bonjour"}]),
            "status": "completed",
        })
        assert vue["snippet"] == "Réservation pour deux demain à 20 h."

    def test_sans_resume_la_liste_retombe_sur_l_extrait(self):
        from app.admin import presenters

        vue = presenters.call_view({
            "summary": None,
            "transcript": json.dumps([{"role": "user", "content": "Bonjour"}]),
            "status": "completed",
        })
        assert vue["snippet"] == "« Bonjour »"
