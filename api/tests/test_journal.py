"""Journal de bord : le blanc doit devenir DIAGNOSTICABLE (#88).

Le capteur existant rend une liste d'entiers : les quatre symptômes constatés au
téléphone y laissent la même trace. Ces tests vérifient que le nouveau journal les
sépare — et, surtout, qu'il **se recoupe** avec le capteur historique sur le même flux :
deux instruments indépendants qui mesurent le même blanc et tombent d'accord valent mieux
qu'un seul qu'on croit sur parole.
"""
import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData, TurnMetricsData
from pipecat.observers.base_observer import FramePushed

from app.voice.journal import JournalDeBord
from app.voice.latency import TurnLatencyObserver

MS = 1_000_000  # nanosecondes par milliseconde


def _pousser(observateurs, frame, t_ms):
    if not isinstance(observateurs, (list, tuple)):
        observateurs = [observateurs]
    for obs in observateurs:
        asyncio.run(obs.on_push_frame(FramePushed(
            source=None, destination=None, frame=frame,
            direction=None, timestamp=t_ms * MS)))


def _dit(texte):
    """`TTSTextFrame` exige `aggregated_by` : le service TTS le renseigne, pas nous."""
    return TTSTextFrame(text=texte, aggregated_by="test")


def _transcription(texte, confiance=None):
    frame = TranscriptionFrame(text=texte, user_id="u", timestamp="2026-09-01T12:00:00Z")
    if confiance is not None:
        frame.result = {"channel": {"alternatives": [{"confidence": confiance}]}}
    return frame


class TestUnTourComplet:
    def _tour_type(self, journal):
        _pousser(journal, StartFrame(), 0)
        _pousser(journal, UserStartedSpeakingFrame(), 1000)
        _pousser(journal, UserStoppedSpeakingFrame(), 3000)
        _pousser(journal, _transcription("bonjour je voudrais une table", 0.94), 3100)
        _pousser(journal, MetricsFrame(data=[
            TTFBMetricsData(processor="DeepgramSTTService", value=0.2),
            TTFBMetricsData(processor="OpenAILLMService", value=0.5),
            TTFBMetricsData(processor="MoshiServerTTSService", value=1.1),
        ]), 3200)
        _pousser(journal, _dit("Bien sûr,"), 4000)
        _pousser(journal, _dit("pour combien de personnes ?"), 4100)
        _pousser(journal, BotStartedSpeakingFrame(), 5000)
        _pousser(journal, BotStoppedSpeakingFrame(), 7000)

    def test_le_blanc_est_decompose_par_etage(self):
        """LE point du chantier : savoir QUEL maillon a pris le temps."""
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["blanc_ms"] == 2000
        assert (tour["stt_ms"], tour["llm_ms"], tour["tts_ms"]) == (200, 500, 1100)

    def test_l_ecart_non_attribue_est_nomme(self):
        """2000 ms de blanc, 1800 attribuées : les 200 restantes ne disparaissent pas
        en silence. Un trou sans nom se lirait comme une mesure fausse."""
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["non_attribue_ms"] == 200
        etages = tour["stt_ms"] + tour["llm_ms"] + tour["tts_ms"] + tour["outil_ms"]
        assert etages + tour["non_attribue_ms"] == tour["blanc_ms"]

    def test_ce_qui_a_ete_entendu_et_ce_qui_a_ete_dit(self):
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["entendu"] == "bonjour je voudrais une table"
        assert tour["dit"] == "Bien sûr, pour combien de personnes ?"
        assert tour["parole_client_ms"] == 2000
        assert tour["parole_bot_ms"] == 2000

    def test_la_confiance_de_transcription_est_gardee(self):
        """C'est ce qui sépare « elle a mal ENTENDU » de « elle a mal COMPRIS » —
        deux symptômes qui appellent des corrections opposées."""
        j = JournalDeBord()
        self._tour_type(j)
        assert j.journal()["tours"][0]["confiance"] == 0.94


class TestRecoupementAvecLeCapteurHistorique:
    """Deux instruments indépendants sur le même intervalle. S'ils divergeaient, l'un
    des deux mentirait — et on ne saurait pas lequel."""

    def test_le_blanc_est_le_meme_pour_les_deux(self):
        journal, latence = JournalDeBord(), TurnLatencyObserver()
        deux = [journal, latence]
        _pousser(deux, StartFrame(), 0)
        for depart, arrivee in ((1000, 2500), (6000, 6800), (9000, 12000)):
            _pousser(deux, UserStoppedSpeakingFrame(), depart)
            _pousser(deux, BotStartedSpeakingFrame(), arrivee)
            _pousser(deux, BotStoppedSpeakingFrame(), arrivee + 500)
        blancs_journal = [t["blanc_ms"] for t in journal.journal()["tours"]]
        assert blancs_journal == latence.samples == [1500, 800, 3000]

    def test_les_deux_ignorent_une_reprise_sans_parole_prealable(self):
        """L'accueil et les relances partent sans que le client ait parlé."""
        journal, latence = JournalDeBord(), TurnLatencyObserver()
        _pousser([journal, latence], BotStartedSpeakingFrame(), 5000)
        assert journal.journal()["tours"] == [] and latence.samples == []

    def test_les_deux_ecartent_un_blanc_aberrant(self):
        journal, latence = JournalDeBord(), TurnLatencyObserver()
        _pousser([journal, latence], UserStoppedSpeakingFrame(), 0)
        _pousser([journal, latence], BotStartedSpeakingFrame(), 60_000)
        assert journal.journal()["tours"] == [] and latence.samples == []


class TestCoupures:
    def test_le_sens_de_l_interruption_est_enregistre(self):
        """Le client qui coupe l'assistante dit qu'elle est trop longue ; l'inverse est
        un défaut de réglage. Confondre les deux mènerait au mauvais correctif."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        _pousser(j, InterruptionFrame(), 3000)
        journal = j.journal()
        assert journal["tours"][0]["interruption"] == "client_coupe_bot"
        assert journal["compteurs"]["interruptions"] == 1

    def test_la_decision_de_fin_de_tour_est_tracee(self):
        """Smart-turn décide quand le client a fini de parler. Sa probabilité est la
        seule façon de savoir s'il a hésité — ou tranché à tort."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, MetricsFrame(data=[TurnMetricsData(
            processor="SmartTurn", is_complete=True, probability=0.62,
            e2e_processing_time_ms=40)]), 1100)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        assert j.journal()["tours"][0]["smart_turn"] == {
            "complet": True, "proba": 0.62, "ms": 40}

    def test_l_ancien_nom_de_metrique_reste_compris(self):
        """`SmartTurnMetricsData` est déprécié mais peut encore être émis. Ne pas
        l'accepter ferait disparaître la mesure des coupures au prochain changement de
        version, sans aucun signal."""
        from pipecat.metrics.metrics import SmartTurnMetricsData

        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, MetricsFrame(data=[SmartTurnMetricsData(
            processor="SmartTurn", is_complete=False, probability=0.31,
            inference_time_ms=34, e2e_processing_time_ms=40)]), 1100)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        assert j.journal()["tours"][0]["smart_turn"] == {
            "complet": False, "proba": 0.31, "ms": 34}


class TestComprehension:
    def test_les_revisions_de_transcription_sont_comptees(self):
        """Beaucoup de révisions interim→final signalent un audio difficile, pas un
        modèle qui se trompe."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        for _ in range(3):
            _pousser(j, InterimTranscriptionFrame(
                text="je vou", user_id="u", timestamp="t"), 1100)
        _pousser(j, _transcription("je voudrais"), 1200)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        assert j.journal()["tours"][0]["revisions_stt"] == 3

    def test_une_finale_vide_est_comptee(self):
        """Le STT a rendu sa décision sans un mot : l'assistante n'avait rien à quoi
        répondre. C'est un symptôme, pas un non-évènement."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, _transcription("   "), 1100)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        journal = j.journal()
        assert journal["compteurs"]["finales_vides"] == 1
        assert journal["tours"][0]["entendu"] is None


class TestOutils:
    def test_le_temps_d_outil_est_isole(self):
        """Un blanc dû à `check_availability` n'appelle pas le même correctif qu'un
        blanc dû au TTS."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, FunctionCallInProgressFrame(
            function_name="check_availability", tool_call_id="1",
            arguments={}, cancel_on_interruption=False), 1200)
        _pousser(j, FunctionCallResultFrame(
            function_name="check_availability", tool_call_id="1", arguments={},
            result="{}", run_llm=True), 1900)
        _pousser(j, BotStartedSpeakingFrame(), 3000)
        assert j.journal()["tours"][0]["outil_ms"] == 700


class TestRobustesse:
    def test_un_capteur_ne_fait_jamais_tomber_ce_qu_il_mesure(self):
        """Une frame inattendue, un champ manquant : on perd une ligne de journal,
        jamais un appel."""
        class FrameBizarre:
            pass

        j = JournalDeBord()
        _pousser(j, FrameBizarre(), 1000)
        _pousser(j, MetricsFrame(data=None), 1100)
        _pousser(j, UserStoppedSpeakingFrame(), 2000)
        _pousser(j, BotStartedSpeakingFrame(), 3000)
        assert j.journal()["tours"][0]["blanc_ms"] == 1000

    def test_une_confiance_illisible_vaut_none_pas_une_exception(self):
        """La forme du résultat appartient au fournisseur et peut changer sans prévenir."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        frame = _transcription("bonjour")
        frame.result = {"forme": "inattendue"}
        _pousser(j, frame, 1100)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        assert j.journal()["tours"][0]["confiance"] is None

    def test_le_nombre_de_tours_est_borne(self, monkeypatch):
        monkeypatch.setenv("JOURNAL_MAX_TOURS", "3")
        j = JournalDeBord()
        for i in range(10):
            _pousser(j, UserStoppedSpeakingFrame(), 1000 * (2 * i + 1))
            _pousser(j, BotStartedSpeakingFrame(), 1000 * (2 * i + 2))
        journal = j.journal()
        assert len(journal["tours"]) == 3
        assert journal["tronque"] is True

    def test_le_nombre_d_evenements_est_borne(self, monkeypatch):
        monkeypatch.setenv("JOURNAL_MAX_EVENEMENTS", "5")
        j = JournalDeBord()
        for i in range(50):
            _pousser(j, UserStartedSpeakingFrame(), i * 10)
        journal = j.journal()
        assert len(journal["evenements"]) == 5
        assert journal["tronque"] is True

    def test_l_etat_de_l_enregistrement_est_porte(self):
        """« Pas d'enregistrement » sans motif ne se diagnostique pas."""
        j = JournalDeBord()
        etat = {"actif": False, "octets": 0, "chunks_perdus": 0, "raison": "disque"}
        assert j.journal(etat)["enregistrement"]["raison"] == "disque"
