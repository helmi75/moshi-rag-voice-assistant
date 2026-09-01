"""Journal de bord : le blanc doit devenir DIAGNOSTICABLE (#88).

⚠️ **Ces tests ont été RÉÉCRITS le 01/09/2026 après trois vrais appels.** La première
version décrivait l'ordre des frames tel que je l'imaginais ; le pipeline en émet un
autre. Elle passait au vert pendant que quatre champs sur dix restaient vides en
production. Chaque scénario ci-dessous suit désormais l'ordre RÉELLEMENT observé :
transcription finale AVANT la fin de tour, texte TTS APRÈS le début de parole.

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
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
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
        """L'ordre RÉEL, relevé sur des appels de production.

        Deux inversions par rapport à l'intuition, et ce sont elles qui rendaient le
        journal vide : la transcription finale précède `UserStoppedSpeaking` (c'est elle
        qui déclenche la fin de tour), et le texte TTS suit `BotStartedSpeaking`."""
        _pousser(journal, StartFrame(), 0)
        _pousser(journal, UserStartedSpeakingFrame(), 1000)
        _pousser(journal, VADUserStoppedSpeakingFrame(), 2600)      # il se tait vraiment
        _pousser(journal, _transcription("bonjour je voudrais une table", 0.94), 2800)
        _pousser(journal, UserStoppedSpeakingFrame(), 3000)         # fin de tour décidée
        _pousser(journal, MetricsFrame(data=[
            TTFBMetricsData(processor="OpenAILLMService", value=0.5),
            TTFBMetricsData(processor="MoshiServerTTSService", value=1.1),
        ]), 3200)
        _pousser(journal, BotStartedSpeakingFrame(), 5000)
        _pousser(journal, _dit("Bien sûr,"), 5100)                  # le texte suit
        _pousser(journal, _dit("pour combien de personnes ?"), 5200)
        _pousser(journal, BotStoppedSpeakingFrame(), 7000)

    def test_le_blanc_est_decompose_par_etage(self):
        """LE point du chantier : savoir QUEL maillon a pris le temps.

        Pas de `stt_ms` : Deepgram ne publie aucun TTFB, et le temps de transcription
        n'est de toute façon pas dans le blanc — la transcription le précède."""
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["blanc_ms"] == 2000
        assert (tour["llm_ms"], tour["tts_ms"]) == (500, 1100)

    def test_le_silence_ressenti_inclut_l_attente_de_fin_de_tour(self):
        """L'appelant se tait à 2600 et n'entend une réponse qu'à 5000 : il a vécu
        2,4 s de silence, pas 2,0. Les 400 ms de décision de fin de tour n'étaient
        comptées nulle part — alors qu'elles s'entendent exactement pareil."""
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["attente_tour_ms"] == 400
        assert tour["blanc_ressenti_ms"] == 2400

    def test_l_ecart_non_attribue_est_nomme(self):
        """2000 ms de blanc, 1600 attribuées : les 400 restantes ne disparaissent pas
        en silence. Un trou sans nom se lirait comme une mesure fausse."""
        j = JournalDeBord()
        self._tour_type(j)
        tour = j.journal()["tours"][0]
        assert tour["non_attribue_ms"] == 400
        etages = tour["llm_ms"] + tour["tts_ms"] + tour["outil_ms"]
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
    """⚠️ Réécrits après la production. La première version comptait des
    `InterruptionFrame`, qui sont **diffusées à tous les processeurs dans les deux
    sens** : une seule vraie coupure en produisait des dizaines. Relevé sur de vrais
    appels de deux minutes : 77, 275 et 121 « interruptions ».

    On détecte désormais l'ÉVÉNEMENT : quelqu'un se met à parler pendant que l'autre
    parle. Un seul par coupure, et le sens est celui qu'on croit."""

    def test_le_client_qui_coupe_est_compte_une_fois(self):
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        _pousser(j, UserStartedSpeakingFrame(), 3000)   # il parle par-dessus elle
        journal = j.journal()
        assert journal["compteurs"]["coupures_du_client"] == 1
        assert journal["tours"][0]["interruption"] == "client_coupe_bot"

    def test_l_assistante_qui_coupe_est_comptee_separement(self):
        """Le client qui coupe dit qu'elle est trop longue ; l'inverse est une fin de
        tour décidée trop tôt. Deux symptômes, deux correctifs OPPOSÉS — les confondre
        mènerait à régler le mauvais paramètre."""
        j = JournalDeBord()
        _pousser(j, UserStartedSpeakingFrame(), 1000)    # il parle encore…
        _pousser(j, BotStartedSpeakingFrame(), 1500)     # …et elle démarre
        journal = j.journal()
        assert journal["compteurs"]["coupures_du_bot"] == 1
        assert journal["compteurs"]["coupures_du_client"] == 0

    def test_un_echange_normal_ne_compte_aucune_coupure(self):
        """Le test qui aurait dû exister dès le début : sans lui, 275 échos passaient
        pour 275 coupures."""
        j = JournalDeBord()
        for i in range(5):
            base = i * 10_000
            _pousser(j, UserStartedSpeakingFrame(), base)
            _pousser(j, UserStoppedSpeakingFrame(), base + 2000)
            _pousser(j, BotStartedSpeakingFrame(), base + 3000)
            _pousser(j, BotStoppedSpeakingFrame(), base + 6000)
        c = j.journal()["compteurs"]
        assert c["coupures_du_client"] == 0 and c["coupures_du_bot"] == 0

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
        modèle qui se trompe. Elles précèdent la fin de tour, comme la finale."""
        j = JournalDeBord()
        for _ in range(3):
            _pousser(j, InterimTranscriptionFrame(
                text="je vou", user_id="u", timestamp="t"), 1100)
        _pousser(j, _transcription("je voudrais"), 1200)
        _pousser(j, UserStoppedSpeakingFrame(), 1300)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        tour = j.journal()["tours"][0]
        assert tour["revisions_stt"] == 3
        assert tour["entendu"] == "je voudrais"

    def test_chaque_tour_recupere_SA_transcription(self):
        """Un buffer mal remis à zéro rattacherait le texte du tour précédent au
        suivant : on diagnostiquerait la mauvaise phrase."""
        j = JournalDeBord()
        _pousser(j, _transcription("premier"), 1000)
        _pousser(j, UserStoppedSpeakingFrame(), 1100)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        _pousser(j, BotStoppedSpeakingFrame(), 3000)
        _pousser(j, _transcription("second"), 4000)
        _pousser(j, UserStoppedSpeakingFrame(), 4100)
        _pousser(j, BotStartedSpeakingFrame(), 5000)
        assert [t["entendu"] for t in j.journal()["tours"]] == ["premier", "second"]

    def test_une_finale_vide_est_comptee(self):
        """Le STT a rendu sa décision sans un mot : l'assistante n'avait rien à quoi
        répondre. C'est un symptôme, pas un non-évènement."""
        j = JournalDeBord()
        _pousser(j, _transcription("   "), 1000)
        _pousser(j, UserStoppedSpeakingFrame(), 1100)
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

    def test_le_texte_dit_est_rattache_au_bon_tour(self):
        """Il arrive APRÈS `BotStartedSpeaking` : scoper la collecte à la fenêtre de
        tour le laissait vide en production, sur tous les appels."""
        j = JournalDeBord()
        _pousser(j, UserStoppedSpeakingFrame(), 1000)
        _pousser(j, BotStartedSpeakingFrame(), 2000)
        _pousser(j, _dit("La table"), 2100)
        _pousser(j, _dit("est disponible."), 2200)
        assert j.journal()["tours"][0]["dit"] == "La table est disponible."


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
