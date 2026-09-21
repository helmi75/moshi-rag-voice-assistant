"""Langue de l'appel (voice/langue.py) : décroché bilingue, puis fixée.

Les phrases ci-dessous ne sont pas inventées : Deepgram les a produites en `multi` le
10/09/2026, en rejouant les pistes des sept appels réels 100 à 106. Les fausses alertes
(« Yes, sir. » pour « C'est ça ») sont celles qui ont fixé les seuils.
"""
import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transcriptions.language import Language

from app.voice import langue as L


def _finale(texte, langues):
    """Une transcription finale telle que Deepgram `multi` la renvoie : une langue par mot."""
    mots = texte.split()
    if isinstance(langues, str):
        langues = [langues] * len(mots)
    brut = {"channel": {"alternatives": [{
        "transcript": texte,
        "words": [{"word": m, "language": l} for m, l in zip(mots, langues)],
    }]}}
    return TranscriptionFrame(texte, "u", "2026-09-10T12:00:00Z", result=brut)


def _detecteur():
    """Un détecteur dont on capture ce qu'il pousse, sans pipeline."""
    notees = []
    d = L.DetecteurDeLangue("fr", reglage=lambda langue: ("reglage", langue),
                            noter=notees.append)
    poussees = []

    async def pousser(frame, direction=FrameDirection.DOWNSTREAM):
        poussees.append((frame, direction))

    d.push_frame = AsyncMock(side_effect=pousser)
    return d, poussees, notees


def _entendre(d, *frames):
    async def scenario():
        for f in frames:
            await d.process_frame(f, FrameDirection.DOWNSTREAM)
    asyncio.run(scenario())


def _reglages(poussees):
    return [f for f, sens in poussees if sens == FrameDirection.UPSTREAM]


class TestTrancher:
    def test_une_phrase_francaise_longue_tranche(self):
        # Appel 100 : « Oui oui, c'est pour la réservation s'il vous plaît. »
        assert L.trancher(Counter(fr=9)) == "fr"

    def test_les_fausses_alertes_mesurees_ne_tranchent_pas(self):
        """Toutes font trois mots au plus : c'est ce qui fixe le seuil à quatre."""
        for texte in ("Yes, sir.", "Oh, yes, yes.", "l m I.", "What?", "Hello.", "That's all."):
            assert L.trancher(Counter(en=len(texte.split()))) is None, texte

    def test_l_anglais_de_l_appel_101_tranche(self):
        assert L.trancher(Counter(en=4)) == "en"  # « Do you speak English? »
        assert L.trancher(Counter(en=12)) == "en"

    def test_une_phrase_trop_melangee_ne_tranche_pas(self):
        assert L.trancher(Counter(fr=3, en=2)) is None


class TestMotsParLangue:
    def test_compte_les_etiquettes_de_chaque_mot(self):
        f = _finale("Bonjour do you speak English", ["fr", "en", "en", "en", "en"])
        assert L.mots_par_langue(f) == Counter(fr=1, en=4)

    def test_lit_aussi_les_objets_du_sdk(self):
        mot = SimpleNamespace(word="Hello", language="en-US")
        brut = SimpleNamespace(channel=SimpleNamespace(
            alternatives=[SimpleNamespace(words=[mot, mot])]))
        f = TranscriptionFrame("Hello Hello", "u", "t", result=brut)
        assert L.mots_par_langue(f) == Counter(en=2)

    def test_rien_d_etiquete_ne_compte_rien(self):
        """Deepgram déjà fixé en `fr` n'étiquette plus rien : pas de décision fantôme."""
        f = TranscriptionFrame("Oui c'est ça", "u", "t", result={"channel": {
            "alternatives": [{"words": [{"word": "Oui"}]}]}})
        assert L.mots_par_langue(f) == Counter()

    def test_repli_sur_la_langue_de_la_phrase(self):
        f = TranscriptionFrame("I want to book", "u", "t", Language.EN)
        assert L.mots_par_langue(f) == Counter(en=4)


class TestDetecteur:
    def test_un_appel_francais_fixe_le_francais_des_la_premiere_phrase(self):
        """Appel 104 en `multi` : « Oui bonjour, je vous fais » ouvre l'appel. Décider
        ici, c'est être en `fr` avant « Yes, sir. » (pour « C'est ça »), arrivé ensuite."""
        d, poussees, notees = _detecteur()
        phrase = _finale("Oui bonjour, je vous fais", "fr")
        _entendre(d, phrase)
        assert d.langue == "fr"
        assert _reglages(poussees) == [("reglage", "fr")]
        assert notees == ["fr"]
        # La transcription passe D'ABORD : la détection n'ajoute rien au blanc.
        assert poussees[0] == (phrase, FrameDirection.DOWNSTREAM)

    def test_un_anglophone_reste_en_bilingue(self):
        d, poussees, notees = _detecteur()
        _entendre(d, _finale("Yes. Hello. My name is Helmi. I want to make a reservation", "en"))
        assert d.langue == "en"
        assert _reglages(poussees) == []
        assert notees == ["en"]

    def test_des_bribes_ne_tranchent_pas(self):
        d, poussees, _ = _detecteur()
        _entendre(d, _finale("Aló.", "es"), _finale("Yes, sir.", "en"))
        assert d.langue is None
        assert _reglages(poussees) == []

    def test_le_cumul_finit_par_trancher(self):
        """Un appelant qui ne répond que par bribes finit quand même en `fr`."""
        d, poussees, _ = _detecteur()
        bribes = [_finale(t, "fr") for t in ("Oui.", "Paul.", "vingt heures", "Oui c'est ça")]
        _entendre(d, *bribes)
        assert d.langue is None  # sept mots : pas encore
        _entendre(d, _finale("D'accord merci.", "fr"))
        assert d.langue == "fr"
        assert _reglages(poussees) == [("reglage", "fr")]

    def test_la_langue_se_redecide_quand_l_appelant_change(self):
        """Le défaut du 20/09/2026 : la première décision valait pour tout l'appel, et
        un appelant qui passait à l'anglais n'était plus transcrit du tout."""
        d, poussees, notees = _detecteur()
        _entendre(d, _finale("Je voudrais faire une réservation", "fr"),
                  _finale("Do you speak English please", "en"))
        assert d.langue == "en"
        assert _reglages(poussees) == [("reglage", "fr"), ("reglage", "multi")]
        assert notees == ["fr", "en"]

    def test_la_meme_langue_ne_repousse_pas_de_reglage(self):
        """Reconnecter le STT à chaque phrase coûterait un blanc à chaque phrase."""
        d, poussees, notees = _detecteur()
        _entendre(d, _finale("Je voudrais faire une réservation", "fr"),
                  _finale("C'est pour demain soir vers vingt heures", "fr"))
        assert _reglages(poussees) == [("reglage", "fr")]
        assert notees == ["fr"]

    def test_les_transcriptions_intermediaires_sont_ignorees(self):
        d, poussees, _ = _detecteur()
        brut = {"channel": {"alternatives": [{"words": [{"word": "w", "language": "en"}] * 6}]}}
        _entendre(d, InterimTranscriptionFrame("w w w w w w", "u", "t", result=brut))
        assert d.langue is None

    def test_une_detection_qui_leve_ne_retient_pas_la_frame(self, monkeypatch):
        def explose(frame):
            raise RuntimeError("forme inattendue")

        monkeypatch.setattr(L, "mots_par_langue", explose)
        d, poussees, _ = _detecteur()
        phrase = _finale("Je voudrais faire une réservation", "fr")
        _entendre(d, phrase)
        assert poussees == [(phrase, FrameDirection.DOWNSTREAM)]
        assert d.langue is None


class _Horloge:
    """Le temps, contrôlé : c'est la DURÉE d'un tour de parole qui décide s'il compte."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avancer(self, secondes):
        self.t += secondes


def _ouvrir_la_bouche(d):
    """Le VAD signale un début de parole, rien de plus : c'est à cet instant que le
    tour PRÉCÉDENT est jugé. L'isoler ainsi est ce qui distingue la surveillance de la
    surdité de la re-décision ordinaire — sans quoi le test passerait même sans elle."""
    asyncio.run(d.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))


def _parler(d, horloge, duree=2.0, texte=None, langues="fr"):
    """Un tour de parole vu par le pipeline : le VAD l'ouvre, la transcription arrive
    s'il y en a une, le VAD le referme."""
    async def scenario():
        await d.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        horloge.avancer(duree)
        if texte is not None:
            await d.process_frame(_finale(texte, langues), FrameDirection.DOWNSTREAM)
        await d.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    asyncio.run(scenario())


class TestOnNeResteJamaisSourd:
    """Une fois le STT fixé sur le français, l'anglais ne produit PLUS RIEN : ni texte,
    ni étiquette de langue. Le seul symptôme qui reste, c'est le silence — le VAD entend
    parler, le STT ne rend rien. C'est lui qu'on surveille (appels 138 à 141 du
    20/09/2026 : l'assistante relançait « Vous êtes toujours là ? » dans le vide)."""

    def _verrouille(self, monkeypatch):
        horloge = _Horloge()
        monkeypatch.setattr(L.time, "monotonic", horloge)
        d, poussees, notees = _detecteur()
        _parler(d, horloge, texte="Je voudrais faire une réservation s'il vous plaît")
        assert _reglages(poussees) == [("reglage", "fr")]
        return d, poussees, notees, horloge

    def test_deux_tours_sans_transcription_rendent_le_bilingue(self, monkeypatch):
        d, poussees, _, horloge = self._verrouille(monkeypatch)
        _parler(d, horloge)  # « Hello, do you speak English? » — rien ne sort
        _parler(d, horloge)  # « Hello? Can you hear me? » — rien non plus
        # Il reprend la parole une troisième fois : le deuxième tour muet est acquis.
        # AUCUNE transcription n'est fournie ici, exprès : le retour au bilingue ne
        # peut venir que de la surveillance de la surdité.
        _ouvrir_la_bouche(d)
        assert _reglages(poussees) == [("reglage", "fr"), ("reglage", "multi")]

    def test_un_seul_tour_muet_ne_suffit_pas(self, monkeypatch):
        d, poussees, _, horloge = self._verrouille(monkeypatch)
        _parler(d, horloge)
        _parler(d, horloge, texte="Oui pardon je disais demain soir")
        assert _reglages(poussees) == [("reglage", "fr")]

    def test_une_toux_ne_compte_pas_pour_un_tour(self, monkeypatch):
        """Un bruit court pris pour de la parole ne doit pas coûter la précision du
        français pour tout le reste de l'appel."""
        d, poussees, _, horloge = self._verrouille(monkeypatch)
        for _ in range(4):
            _parler(d, horloge, duree=0.3)
        assert _reglages(poussees) == [("reglage", "fr")]

    def test_une_transcription_remet_le_compteur_a_zero(self, monkeypatch):
        d, poussees, _, horloge = self._verrouille(monkeypatch)
        _parler(d, horloge)
        _parler(d, horloge, texte="Oui c'est bien ça merci beaucoup")
        _parler(d, horloge)
        _parler(d, horloge, texte="Très bien alors à demain")
        assert _reglages(poussees) == [("reglage", "fr")]

    def test_en_bilingue_le_silence_ne_prouve_rien(self, monkeypatch):
        """Tant que le STT est bilingue, un tour sans transcription est du bruit : il
        n'y a pas de verrou à lever."""
        horloge = _Horloge()
        monkeypatch.setattr(L.time, "monotonic", horloge)
        d, poussees, _ = _detecteur()
        for _ in range(4):
            _parler(d, horloge)
        assert _reglages(poussees) == []

    def test_apres_une_surdite_on_ne_reverrouille_plus(self, monkeypatch):
        """L'appelant a prouvé qu'il change de langue : un deuxième verrou le rendrait
        sourd une deuxième fois. On paie un peu de précision, pas un appel."""
        d, poussees, _, horloge = self._verrouille(monkeypatch)
        _parler(d, horloge)
        _parler(d, horloge)
        _ouvrir_la_bouche(d)
        assert _reglages(poussees)[-1] == ("reglage", "multi")
        _parler(d, horloge, texte="Bon d'accord je reprends en français alors")
        assert d.langue == "fr"
        assert _reglages(poussees)[-1] == ("reglage", "multi")
