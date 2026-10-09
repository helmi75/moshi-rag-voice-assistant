"""Une réservation annoncée et rien d'enregistré (app/annonce.py, ASSISTANTE-137).

Le 08/10/2026, appel 265 : « Très bien, j'enregistre ça. C'est réservé. » — et aucune
table en base. Ce qu'on tient ici : on reconnaît l'annonce sans crier au loup, le résumé
ne répète pas le mensonge, et l'e-mail au restaurant ne dit que ce qu'on sait. Le
branchement dans l'appel lui-même est dans `test_live.py`.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import annonce, calls, db, notifications, resume, tenants
from app.admin import presenters

APPEL_265 = [
    {"role": "assistant", "content": "Je suis l'assistante vocale, cet appel est enregistré."},
    {"role": "user", "content": "Je veux réserver une table"},
    {"role": "assistant", "content": "Une table pour une personne le jeudi quinze octobre à quatorze "
                                     "heures, au nom de Bi. C'est bien ça ?"},
    {"role": "user", "content": "Oui"},
    {"role": "assistant", "content": "Très bien, j'enregistre ça. C’est réservé. On vous attend "
                                     "pour une personne. Autre chose ?"},
]
VERIFICATION = {"nom": "check_availability", "arguments": {}, "resultat": '{"available": true}'}


class TestReconnaitreUneAnnonce:
    @pytest.mark.parametrize("phrase", [
        "C'est réservé.", "C’est enregistré au nom de L, B.", "Parfait, c'est bien confirmé !",
        "Votre réservation est bien enregistrée.", "La table a bien été réservée pour ce soir.",
        "J'ai enregistré votre demande."])
    def test_ce_qui_annonce_un_enregistrement_comme_fait(self, phrase):
        assert annonce.phrase_d_annonce(phrase)

    @pytest.mark.parametrize("phrase", [
        "Je suis l'assistante vocale, cet appel est enregistré.",
        "Très bien, j'enregistre ça.", "Je vérifie tout de suite.",
        "La réservation n'a pas pu être enregistrée.", "Rien n'est réservé pour l'instant.",
        "C'est bien enregistré à ce nom ?", "C'est noté, merci.", "", None])
    def test_ce_qui_n_en_est_pas_une(self, phrase):
        assert annonce.phrase_d_annonce(phrase) is None

    def test_seule_la_phrase_fautive_est_rendue(self):
        assert annonce.phrase_d_annonce(APPEL_265[-1]["content"]) == "C'est réservé."


class TestAnnonceSansTrace:
    def test_l_appel_265_est_reconnu(self):
        assert annonce.sans_trace(APPEL_265, [VERIFICATION, VERIFICATION]) == "C'est réservé."

    @pytest.mark.parametrize("outil,resultat", [
        ("create_reservation", '{"status": "confirmed", "reservation_id": 91}'),
        ("create_reservation", '{"status": "pending_restaurant_approval", "reservation_id": "r1"}'),
        ("modify_reservation", '{"status": "modified", "reservation_id": 91}'),
        ("take_message", '{"status": "recorded", "message_id": 4}'),
        ("find_reservation", '{"reservations": [{"id": 91}]}')])
    def test_un_outil_qui_a_abouti_explique_l_annonce(self, outil, resultat):
        assert annonce.sans_trace(APPEL_265, [{"nom": outil, "resultat": resultat}]) is None

    @pytest.mark.parametrize("resultat", [
        '{"error": "Ce créneau est fermé."}', "Erreur outil create_reservation: boom", None])
    def test_un_outil_refuse_ou_en_panne_n_explique_rien(self, resultat):
        outils = [{"nom": "create_reservation", "resultat": resultat}]
        assert annonce.sans_trace(APPEL_265, outils) == "C'est réservé."

    def test_ce_que_dit_le_client_ne_compte_pas(self):
        assert annonce.sans_trace([{"role": "user", "content": "C'est réservé ? Super."}], []) is None

    def test_un_appel_sans_annonce_n_est_pas_signale(self):
        assert annonce.sans_trace(APPEL_265[:4], []) is None


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "annonce.db")):
        db.init_db()
        yield


def _appel_clos(journal):
    resto = tenants.create_tenant("Chez Annonce", "+33199000444")
    calls.start_call("CA-annonce", resto.id)
    return calls.finish_call("CA-annonce", "completed", APPEL_265, journal=journal)


class TestLeResumeNeRepetePasLAnnonce:
    def _resumer(self, identifiant):
        vus = []

        async def creer(**kwargs):
            vus.append(kwargs["messages"][1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Résumé."))])

        faux = MagicMock()
        faux.chat.completions.create = creer
        with patch.object(resume.llm, "get_client", lambda: faux):
            asyncio.run(resume.resumer(identifiant))
        return vus[0]

    def test_le_constat_du_journal_est_donne_comme_un_fait(self, base):
        identifiant = _appel_clos({"a_verifier": {"motif": annonce.MOTIF, "phrase": "C'est réservé."}})
        demande = self._resumer(identifiant)
        assert "RIEN n'a été enregistré" in demande and "N'écris pas que l'appelant a réservé" in demande

    def test_un_appel_ordinaire_est_resume_comme_avant(self, base):
        assert "Fait établi" not in self._resumer(_appel_clos({"compteurs": {}}))


class TestDansLAdmin:
    def test_l_appel_est_a_verifier_et_la_phrase_est_montree(self):
        journal = json.dumps({"a_verifier": {"motif": annonce.MOTIF, "phrase": "C'est réservé."}})
        vue = presenters.call_view({"status": "completed", "ended_at": "2026-10-08T06:33:00Z",
                                    "started_at": "2026-10-08T06:31:00Z", "journal": journal})
        assert vue["outcome"] == "a_verifier" and vue["outcome_label"] == "À vérifier"
        assert vue["annonce_sans_trace"] == "C'est réservé."

    @pytest.mark.parametrize("journal", [None, "", "{illisible", '{"compteurs": {}}', '["a_verifier"]'])
    def test_un_journal_sans_constat_ou_illisible_ne_change_rien(self, journal):
        vue = presenters.call_view({"status": "completed", "ended_at": "2026-10-08T06:33:00Z",
                                    "started_at": "2026-10-08T06:31:00Z", "journal": journal})
        assert vue["outcome"] == "info" and vue["annonce_sans_trace"] is None

    def test_une_reservation_reellement_creee_l_emporte(self):
        journal = json.dumps({"a_verifier": {"phrase": "C'est réservé."}})
        assert presenters.outcome_key({"status": "completed", "ended_at": "x", "reservation_id": 3,
                                       "journal": journal}) == "reservation"


class TestLEMailAuRestaurant:
    def test_il_dit_ce_qu_elle_a_dit_et_qui_rappeler(self):
        resto = SimpleNamespace(name="Le Bouchon Doré")
        sujet, corps = notifications.sujet_et_corps("annonce_sans_trace", resto, {
            "phrase": "C'est réservé.", "caller_number": "+33612345678", "appel_id": 265})
        assert sujet.startswith("À vérifier")
        assert "« C'est réservé. »" in corps and "Rappeler le : +33612345678" in corps
        assert "rien n'a été enregistré" in corps

    def test_un_appel_masque_le_dit(self):
        _, corps = notifications.sujet_et_corps("annonce_sans_trace", SimpleNamespace(name="R"),
                                                {"phrase": "C'est réservé.", "caller_number": None})
        assert "numéro masqué" in corps
