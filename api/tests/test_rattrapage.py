"""Parole entendue sans être transcrite (voice/rattrapage.py) et annonces sans outil.

Les cas viennent des appels de test 183 à 201 (27/09/2026) : 27 « Vous êtes toujours
là ? », dont 17 alors que l'appelant venait de répondre — le VAD l'avait entendu, aucun
mot n'était arrivé —, et 8 après un « Je vérifie tout de suite. » resté sans outil.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.voice import bot, rattrapage as R

MONTEE = FrameDirection.UPSTREAM
DESCENTE = FrameDirection.DOWNSTREAM


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch):
    monkeypatch.setattr(R, "ATTENTE_SECONDES", 0)


class Banc:
    """Le processeur seul, une horloge qu'on avance à la main, et tout ce qu'il fait."""

    def __init__(self, reponse=("20 heures, 20 heures.", 0.97)):
        self.t = 100.0
        self.transcriptions = []
        self.pardons = 0
        self.notes = []
        self.poussees = []

        async def transcrire(pcm, taux, langue):
            self.transcriptions.append((pcm, taux, langue))
            return reponse

        async def pardonner():
            self.pardons += 1

        self.p = R.RattrapageDeParole(
            transcrire=transcrire, langue=lambda: "fr", pardonner=pardonner,
            noter=lambda quoi, **d: self.notes.append((quoi, d)), horloge=lambda: self.t)

        async def pousser(frame, direction=DESCENTE):
            self.poussees.append((frame, direction))

        self.p.push_frame = AsyncMock(side_effect=pousser)

    async def _voir(self, frame, direction):
        await self.p.process_frame(frame, direction)

    def voir(self, *evenements):
        """(frame, sens, secondes à attendre après) — puis laisse finir le rattrapage."""
        async def scenario():
            for frame, sens, apres in evenements:
                await self._voir(frame, sens)
                self.t += apres
            await asyncio.sleep(0.01)
        asyncio.run(scenario())

    def rattrapes(self):
        """Les transcriptions CRÉÉES par le processeur (les scénarios n'en injectent
        aucune de ce type : ce qui passe est forcément rattrapé)."""
        return [f.text for f, sens in self.poussees
                if type(f) is TranscriptionFrame and sens == DESCENTE]


def _audio(secondes=0.02):
    return InputAudioRawFrame(audio=b"\x01\x00" * int(8000 * secondes), sample_rate=8000,
                              num_channels=1)


def _parole(banc, duree, mots=None):
    """Une prise de parole de `duree` secondes, le VAD autour, avec ou sans mots."""
    evenements = [(VADUserStartedSpeakingFrame(start_secs=0.2), MONTEE, 0.0)]
    pas = 0.02
    for _ in range(int(duree / pas)):
        evenements.append((_audio(pas), DESCENTE, pas))
    if mots is not None:
        evenements.append((mots, DESCENTE, 0.0))
    evenements.append((_audio(0.5), DESCENTE, 0.5))
    evenements.append((VADUserStoppedSpeakingFrame(stop_secs=0.5), MONTEE, 0.0))
    return evenements


class TestLaVoixSansMotsEstRattrapee:
    def test_le_segment_est_retranscrit_et_pousse_comme_une_transcription(self):
        """Appel 199 : « 20 heures, 20 heures » entendu par le VAD, jamais par le flux."""
        banc = Banc()
        banc.voir(*_parole(banc, 1.3))
        assert len(banc.transcriptions) == 1
        pcm, taux, langue = banc.transcriptions[0]
        assert taux == 8000 and langue == "fr"
        # L'audio envoyé couvre la voix, pas seulement la fin.
        assert len(pcm) >= int(8000 * 1.3) * 2
        assert "20 heures, 20 heures." in banc.rattrapes()
        assert banc.notes[0][0] == "rattrape"
        assert banc.pardons == 0

    def test_une_transcription_arrivee_entre_temps_annule_tout(self):
        """Le cas normal : le flux a rendu des mots. Le rattrapage ne doit jamais doubler
        ce que le STT a déjà compris."""
        banc = Banc()
        banc.voir(*_parole(banc, 1.3, mots=InterimTranscriptionFrame("20 heu", "", "t")))
        assert banc.transcriptions == []
        assert banc.rattrapes() == []

    def test_un_souffle_n_est_pas_rattrape(self):
        banc = Banc()
        banc.voir(*_parole(banc, 0.1))
        assert banc.transcriptions == [] and banc.pardons == 0

    def test_rien_n_est_injecte_pendant_que_l_assistante_parle(self):
        """Pousser un texte par-dessus l'assistante la couperait en pleine phrase, avec
        une seconde de retard : pire que de redemander."""
        banc = Banc()
        banc.voir((BotStartedSpeakingFrame(), MONTEE, 0.0), *_parole(banc, 1.3))
        assert banc.rattrapes() == [] and banc.pardons == 0


class TestRienNeRevient:
    def test_elle_dit_pardon_au_lieu_de_vous_etes_toujours_la(self):
        banc = Banc(reponse=None)
        banc.voir(*_parole(banc, 1.0))
        assert banc.pardons == 1
        assert banc.notes[0][0] == "pas_entendu"

    def test_une_confiance_trop_basse_vaut_rien(self):
        banc = Banc(reponse=("30 et 9 5", 0.3))
        banc.voir(*_parole(banc, 1.0))
        assert banc.rattrapes() == [] and banc.pardons == 1

    def test_pas_de_pardon_en_boucle_sur_une_ligne_bruyante(self):
        banc = Banc(reponse=None)
        banc.voir(*_parole(banc, 1.0), *_parole(banc, 1.0))
        assert banc.pardons == 1

    def test_pas_de_pardon_pour_un_bout_de_voix(self):
        """Entre 0,3 et 0,6 s : on tente de rattraper, mais on ne fait pas répéter pour
        ce qui peut n'être qu'un « hm »."""
        banc = Banc(reponse=None)
        banc.voir(*_parole(banc, 0.4))
        assert len(banc.transcriptions) == 1 and banc.pardons == 0


class TestVoixDepuisLeBot:
    def test_l_appelant_a_parle_depuis_que_l_assistante_s_est_tue(self):
        banc = Banc()
        banc.voir((BotStoppedSpeakingFrame(), MONTEE, 1.0), *_parole(banc, 1.0))
        assert banc.p.voix_depuis_le_bot() is True

    def test_un_silence_reste_un_silence(self):
        banc = Banc()
        banc.voir(*_parole(banc, 1.0), (BotStoppedSpeakingFrame(), MONTEE, 8.0))
        assert banc.p.voix_depuis_le_bot() is False


def _m(role, contenu=None, **autres):
    return {"role": role, "content": contenu, **autres}


class TestPromesseEnSuspens:
    def test_je_verifie_sans_outil(self):
        """Appel 187 : « Je n'avais pas saisi. Je vérifie tout de suite. » — puis rien."""
        assert bot.promesse_en_suspens([
            _m("system", "…"), _m("user", "Oui oui 20 heures, j'ai dit 20 heures."),
            _m("assistant", "Je n'avais pas saisi. Je vérifie tout de suite.")])

    def test_je_verifie_avec_outil(self):
        assert not bot.promesse_en_suspens([
            _m("user", "20 heures."), _m("assistant", "Je vérifie tout de suite."),
            _m("assistant", None, tool_calls=[{"id": "1"}]), _m("tool", "IN_PROGRESS")])

    def test_une_question_attend_une_reponse(self):
        assert not bot.promesse_en_suspens([
            _m("user", "Mardi."), _m("assistant", "Je vérifie pour mardi à vingt heures ?")])

    def test_l_accueil_n_est_pas_une_promesse(self):
        """« Bonjour, un instant s'il vous plaît » précède toute parole de l'appelant."""
        assert not bot.promesse_en_suspens([
            _m("system", "…"), _m("assistant", "Bonjour hello resto, un instant s'il vous plait")])

    def test_les_autres_annonces_des_appels_du_27(self):
        for annonce in ("D'accord. Je dois retrouver votre réservation.",
                        "D'accord. Pour annuler la réservation, j'ai besoin de la retrouver.",
                        "Parfait. Je vais chercher la réservation à ce numéro.",
                        "D'accord, je vérifie la disponibilité pour deux personnes. Un instant."):
            assert bot.promesse_en_suspens([_m("user", "…"), _m("assistant", annonce)]), annonce

    def test_une_reponse_ordinaire_n_est_pas_une_annonce(self):
        assert not bot.promesse_en_suspens([
            _m("user", "Oui."), _m("assistant", "Votre demande est bien transmise au restaurant.")])


class TestCablage:
    def test_le_pardon_est_une_phrase_du_pipeline(self):
        """La supervision ne doit pas le compter comme une vraie réponse du modèle."""
        assert R.PARDON["fr"] in bot.phrases_du_pipeline()

    def test_le_pardon_suit_la_langue_de_l_appel(self):
        assert R.pardon("en") == R.PARDON["en"]
        assert R.pardon("fr-FR") == R.PARDON["fr"]
        assert R.pardon(None) == R.PARDON["fr"]


class TestPlaceDansLePipeline:
    """Vérifié sur le texte de `run_bot`, comme les autres règles de câblage : un
    pipeline réel exige Twilio, Deepgram et le GPU."""

    def _source(self):
        import pathlib

        return (pathlib.Path(__file__).resolve().parents[1] / "app" / "voice" / "bot.py"
                ).read_text(encoding="utf-8")

    def test_juste_apres_le_stt(self):
        """Inséré après le détecteur de langue, à la même place : il finit ENTRE le STT
        et lui. Le texte rattrapé compte ainsi pour la langue, et les signaux du VAD,
        qui remontent depuis l'agrégateur, le traversent."""
        source = self._source()
        detecteur = source.index("etapes.insert(etapes.index(stt) + 1, detecteur)")
        rattrapage = source.index("etapes.insert(etapes.index(stt) + 1, rattrapage)")
        assert detecteur < rattrapage

    def test_la_relance_ne_demande_plus_si_l_appelant_est_la_quand_il_a_parle(self):
        source = self._source()
        relance = source[source.index("async def _on_user_idle"):source.index("async def _reset_idle")]
        assert relance.index("promesse_en_suspens") < relance.index('_idle["n"] += 1')
        assert relance.index("voix_depuis_le_bot") < relance.index('_idle["n"] += 1')


class TestJournal:
    def test_les_filets_se_comptent(self):
        from app.voice.journal import JournalDeBord

        j = JournalDeBord()
        j.noter_rattrapage("rattrape", texte="20 heures", confiance=0.97, duree_ms=1300)
        j.noter_rattrapage("pas_entendu", duree_ms=900, confiance=None)
        j.noter_rattrapage("promesse_relancee")
        compteurs = j.journal()["compteurs"]
        assert (compteurs["paroles_rattrapees"], compteurs["paroles_perdues"],
                compteurs["annonces_relancees"]) == (1, 1, 1)
        assert [e["quoi"] for e in j.evenements] == ["rattrape", "pas_entendu", "promesse_relancee"]


class TestLangueDuRattrapage:
    """Appel 204 (28/09/2026) : rattrapée en bilingue, la première phrase d'un appel est
    revenue en anglais inventé, et l'assistante a changé de langue pour rien."""

    def _detecteur(self, stt, langue):
        from types import SimpleNamespace

        return SimpleNamespace(langue_stt=stt, langue=langue)

    def test_jamais_le_bilingue(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_LANGUAGE", raising=False)
        assert bot.langue_du_rattrapage(self._detecteur("multi", None), "fr") == "fr"
        assert bot.langue_du_rattrapage(None, "fr") == "fr"

    def test_la_langue_fixee_du_stt_prime(self):
        assert bot.langue_du_rattrapage(self._detecteur("fr", "fr"), "fr") == "fr"

    def test_un_appel_anglais_se_rattrape_en_anglais(self):
        assert bot.langue_du_rattrapage(self._detecteur("multi", "en"), "fr") == "en"

    def test_une_langue_imposee_est_respectee(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_LANGUAGE", "en")
        assert bot.langue_du_rattrapage(None, "fr") == "en"
