"""Catalogue de voix et choix de la voix par établissement.

Ces tests protègent contre une panne SILENCIEUSE, la pire de ce projet : moshi-server
ne signale pas une voix inconnue, il rend la phrase avec sa voix de repli. Rien dans
les journaux, rien dans les métriques — seul l'appelant entend que ce n'est pas la
bonne voix. D'où l'insistance sur « hors catalogue -> on retombe sur le défaut ».
"""
import pytest

from app.tenants import Tenant
from app.voice import voices


def _tenant(voice=None):
    return Tenant(
        id=1,
        name="Resto",
        business_type="restaurant",
        phone_number="+33100000000",
        language="fr-FR",
        greeting="Bonjour.",
        knowledge_base="",
        voice=voice,
    )


class TestCatalogue:
    def test_identifiants_uniques_et_non_vides(self):
        ids = [v.id for v in voices.catalogue()]
        assert ids and len(ids) == len(set(ids))
        assert all(v.label.strip() and v.note.strip() for v in voices.catalogue())

    def test_toutes_les_voix_viennent_d_un_dossier_embarque(self):
        """Garde-fou contre l'erreur qu'on ne verrait pas : proposer dans l'admin une
        voix que l'image du serveur ne contient pas. Élargir EMBEDDED_FOLDERS suppose
        d'élargir VOICE_FOLDERS dans deploy/modal_moshi_server.py ET de redéployer."""
        for voice in voices.catalogue():
            assert voice.id.startswith(voices.EMBEDDED_FOLDERS), voice.id

    def test_la_voix_par_defaut_est_au_catalogue(self):
        assert voices.get(voices.DEFAULT_VOICE) is not None


class TestResolve:
    def test_sans_tenant_donne_le_defaut_du_parc(self, monkeypatch):
        monkeypatch.delenv("MOSHI_TTS_VOICE", raising=False)
        assert voices.resolve() == voices.DEFAULT_VOICE
        assert voices.resolve(None) == voices.DEFAULT_VOICE

    def test_utilise_la_voix_choisie_par_l_etablissement(self):
        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        assert voices.resolve(_tenant(autre.id)) == autre.id

    def test_ignore_une_voix_hors_catalogue(self, monkeypatch):
        """Voix retirée du catalogue, base éditée à la main : on sert le défaut plutôt
        que de laisser le serveur choisir sa voix de repli à notre place."""
        monkeypatch.delenv("MOSHI_TTS_VOICE", raising=False)
        assert voices.resolve(_tenant("dossier-bidon/inconnue.wav")) == voices.DEFAULT_VOICE

    def test_env_inconnue_ne_contamine_pas_le_parc(self, monkeypatch):
        """Même garde-fou pour la variable d'environnement : une faute de frappe dans
        MOSHI_TTS_VOICE ferait répondre TOUT le parc avec la voix de repli."""
        monkeypatch.setenv("MOSHI_TTS_VOICE", "unmute-prod-website/faute-de-frappe.wav")
        assert voices.resolve() == voices.DEFAULT_VOICE

    def test_env_connue_devient_le_defaut_du_parc(self, monkeypatch):
        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        monkeypatch.setenv("MOSHI_TTS_VOICE", autre.id)
        assert voices.resolve() == autre.id
        # …mais le choix de l'établissement reste prioritaire.
        assert voices.resolve(_tenant(voices.DEFAULT_VOICE)) == voices.DEFAULT_VOICE


class TestLabel:
    def test_donne_un_nom_lisible_jamais_un_chemin(self):
        libelle = voices.label_for(_tenant())
        assert "/" not in libelle and ".wav" not in libelle
        assert libelle == voices.get(voices.DEFAULT_VOICE).label


class TestPlomberieTTS:
    """La voix doit être décidée au MÊME endroit pour le TTS live et l'accueil
    pré-rendu — sinon un appel commence dans une voix et continue dans une autre."""

    def test_le_service_moshi_utilise_la_voix_fournie(self):
        pytest.importorskip("pipecat")
        from app.voice.moshi_server_tts import MoshiServerTTSService

        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        assert MoshiServerTTSService(voice=autre.id)._voice == autre.id

    def test_build_tts_transmet_la_voix_du_tenant(self, monkeypatch):
        pytest.importorskip("pipecat")
        from app.voice.bot import build_tts

        monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        assert build_tts(_tenant(autre.id))._voice == autre.id

    def test_build_tts_sans_tenant_reste_utilisable(self, monkeypatch):
        pytest.importorskip("pipecat")
        from app.voice.bot import build_tts

        monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
        monkeypatch.delenv("MOSHI_TTS_VOICE", raising=False)
        assert build_tts()._voice == voices.DEFAULT_VOICE

    def test_accueil_et_tts_live_choisissent_la_meme_voix(self, monkeypatch):
        """Le vrai risque de régression : deux chemins de code, une seule voix."""
        pytest.importorskip("pipecat")
        from app.voice import greeting as greeting_mod
        from app.voice.bot import build_tts

        monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        tenant = _tenant(autre.id)
        assert greeting_mod._voice(tenant) == build_tts(tenant)._voice
