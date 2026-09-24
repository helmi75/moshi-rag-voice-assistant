"""Ce que la transcription garde d'un appel (`bot._extract_transcript`).

Jusqu'au 24/09/2026, elle jetait les appels d'outils — et cette fonction n'était testée
nulle part, ce qui explique que personne ne l'ait vu. L'appel 152 en a montré le prix :
le modèle avait pris DEUX messages pour l'équipe au lieu de la réservation, et rien ne le
montrait, ni au restaurateur, ni au diagnostic (SCRUM-89).
"""
import json

from pipecat.processors.aggregators.llm_context import LLMContext

from app.voice import bot


def _contexte(*messages):
    return LLMContext(messages=[{"role": "system", "content": "consignes"}, *messages])


def _outil(nom, arguments, id_="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": id_, "type": "function",
         "function": {"name": nom, "arguments": arguments}}]}


class TestLesOutilsApparaissentDansLaTranscription:
    def test_un_message_pris_pour_l_equipe_est_visible(self):
        """L'appel 152, reconstitué : le restaurateur doit voir qu'un rappel a été promis."""
        transcription = bot._extract_transcript(_contexte(
            {"role": "user", "content": "I want to make a reservation in English."},
            _outil("take_message", json.dumps({
                "subject": "Client souhaite faire une réservation en anglais",
                "details": "je ne peux pas basculer dans cette langue"})),
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
            {"role": "assistant", "content": "Je suis désolée…"},
        ))
        assert [m["role"] for m in transcription] == ["user", "outil", "assistant"]
        assert transcription[1]["content"].startswith(
            "take_message — subject : Client souhaite faire une réservation en anglais")

    def test_le_resultat_brut_de_l_outil_reste_dehors(self):
        """Le résultat est une donnée technique pour le modèle, pas une ligne à lire."""
        transcription = bot._extract_transcript(_contexte(
            {"role": "user", "content": "Une table ?"},
            _outil("check_availability", json.dumps({"date": "2026-09-25"})),
            {"role": "tool", "tool_call_id": "c1", "content": '{"available": true}'},
        ))
        assert all(m["role"] != "tool" for m in transcription)
        assert "available" not in json.dumps(transcription)

    def test_plusieurs_outils_dans_un_meme_tour(self):
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "check_availability", "arguments": "{}"}},
            {"id": "b", "type": "function",
             "function": {"name": "create_reservation", "arguments": '{"party_size": 2}'}},
        ]}
        transcription = bot._extract_transcript(_contexte(message))
        assert [m["content"] for m in transcription] == [
            "check_availability", "create_reservation — party_size : 2"]

    def test_des_arguments_illisibles_ne_font_pas_tomber_l_extraction(self):
        transcription = bot._extract_transcript(_contexte(
            _outil("take_message", "{pas du json")))
        assert transcription == [{"role": "outil", "content": "take_message"}]

    def test_le_prompt_systeme_n_apparait_jamais(self):
        transcription = bot._extract_transcript(_contexte(
            {"role": "user", "content": "Bonjour"}))
        assert transcription == [{"role": "user", "content": "Bonjour"}]


class TestLesConsommateursNeSontPasTrompes:
    """Un nouveau rôle dans la transcription ne doit rien changer à ce qu'on en tire."""

    def test_un_outil_seul_ne_passe_pas_pour_une_reponse(self):
        """Si le modèle agit mais ne DIT rien ensuite, l'appelant n'a rien entendu."""
        from app import supervision

        transcription = json.dumps([
            {"role": "assistant", "content": "Bonjour, un instant."},
            {"role": "user", "content": "Une table ?"},
            {"role": "outil", "content": "check_availability"},
        ])
        assert supervision._a_repondu(transcription) is False

    def test_l_extrait_de_la_liste_reste_la_parole_du_client(self):
        from app.admin import presenters

        assert presenters.first_customer_line([
            {"role": "outil", "content": "take_message"},
            {"role": "user", "content": "Bonjour"},
        ]) == "« Bonjour »"
