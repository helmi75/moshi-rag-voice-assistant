"""Catalogue de voix et choix de la voix par établissement.

Depuis le 28/09/2026, toutes les voix viennent de Mistral (Voxtral) : le catalogue est
celui de l'API, relu toutes les heures, avec une copie dans le dépôt. Les voix Moshi ne
se choisissent plus ; elles restent le secours si la clé Mistral manque.

La règle de fond ne change pas : jamais un identifiant hors catalogue. Une voix inconnue
serait refusée par Mistral (phrase muette) ou, côté Moshi, remplacée EN SILENCE par la
voix de repli du serveur.
"""
import asyncio
import functools

import httpx
import pytest

from app.tenants import Tenant
from app.voice import voices


def _tenant(voice=None):
    return Tenant(id=1, name="Resto", business_type="restaurant", phone_number="+33100000000",
                  language="fr-FR", greeting="Bonjour.", knowledge_base="", voice=voice)


@pytest.fixture(autouse=True)
def _cle(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    monkeypatch.delenv("VOIX_PAR_DEFAUT", raising=False)
    monkeypatch.delenv("MOSHI_TTS_VOICE", raising=False)
    avant = voices._catalogue_mistral
    yield
    voices._catalogue_mistral = avant


def _autre():
    return next(v for v in voices.catalogue() if v.id != voices.VOIX_PAR_DEFAUT)


class TestCatalogue:
    def test_les_trente_voix_de_mistral_le_francais_d_abord(self):
        catalogue = voices.catalogue()
        assert len(catalogue) == 30
        assert all(v.fournisseur == voices.VOXTRAL for v in catalogue)
        assert [v.locuteur for v in catalogue[:6]] == ["Marie"] * 6
        assert len({v.id for v in catalogue}) == 30

    def test_les_tons_sont_en_francais_et_accordes(self):
        labels = {v.label for v in voices.catalogue()}
        assert {"Marie · joyeuse", "Paul · joyeux", "Jane · curieuse", "Oliver · curieux",
                "Marie · neutre"} <= labels
        assert all("/" not in v.label for v in voices.catalogue())

    def test_la_voix_par_defaut_est_au_catalogue(self):
        assert voices.get(voices.VOIX_PAR_DEFAUT).label == "Marie · enthousiaste"

    def test_les_voix_moshi_de_secours_viennent_d_un_dossier_embarque(self):
        """Élargir EMBEDDED_FOLDERS suppose d'élargir VOICE_FOLDERS dans
        deploy/modal_moshi_server.py ET de redéployer."""
        for voice in voices.voix_moshi():
            assert voice.id.startswith(voices.EMBEDDED_FOLDERS), voice.id


class TestResolve:
    def test_sans_choix_la_voix_par_defaut_du_parc(self):
        assert voices.resolve() == voices.resolve(None) == voices.VOIX_PAR_DEFAUT

    def test_utilise_la_voix_choisie_par_l_etablissement(self):
        assert voices.resolve(_tenant(_autre().id)) == _autre().id

    def test_ignore_une_voix_hors_catalogue(self):
        assert voices.resolve(_tenant("voxtral/inventee")) == voices.VOIX_PAR_DEFAUT

    def test_une_ancienne_voix_moshi_passe_sur_mistral(self):
        assert voices.resolve(_tenant(voices.DEFAULT_VOICE)) == voices.VOIX_PAR_DEFAUT

    def test_les_identifiants_du_matin_restent_valides(self):
        assert voices.resolve(_tenant("voxtral/marie-enthousiaste")) == "voxtral/fr_marie_excited"

    def test_le_defaut_du_parc_se_regle(self, monkeypatch):
        monkeypatch.setenv("VOIX_PAR_DEFAUT", "voxtral/fr_marie_happy")
        assert voices.resolve() == "voxtral/fr_marie_happy"
        monkeypatch.setenv("VOIX_PAR_DEFAUT", "voxtral/faute-de-frappe")
        assert voices.resolve() == voices.VOIX_PAR_DEFAUT


class TestSansCle:
    """Clé Mistral absente : une voix connue plutôt que le silence."""

    def test_le_secours_moshi(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        assert voices.resolve(_tenant(_autre().id)) == voices.DEFAULT_VOICE
        assert "secours Moshi" in voices.label_for(_tenant())

    def test_une_faute_de_frappe_ne_contamine_pas_le_secours(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        monkeypatch.setenv("MOSHI_TTS_VOICE", "unmute-prod-website/faute-de-frappe.wav")
        assert voices.resolve() == voices.DEFAULT_VOICE


class TestCatalogueVivant:
    def _mistral(self, monkeypatch, repondre):
        origine = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", functools.partial(
            origine, transport=httpx.MockTransport(repondre)))

    def test_une_voix_ajoutee_chez_mistral_apparait(self, monkeypatch):
        voix = {"id": "0" * 36, "slug": "fr_louise_neutral", "name": "Louise - Neutral",
                "languages": ["fr_fr"], "gender": "female", "type": "custom"}
        self._mistral(monkeypatch, lambda r: httpx.Response(
            200, json={"items": [voix, {"cassee": True}], "total_pages": 1}))
        assert asyncio.run(voices.rafraichir_catalogue()) is True
        assert voices.get("voxtral/fr_louise_neutral").label == "Louise · neutre"
        assert len(voices.catalogue()) == 1  # l'entrée illisible est écartée

    def test_mistral_injoignable_garde_l_ancien_catalogue(self, monkeypatch):
        avant = voices.catalogue()
        self._mistral(monkeypatch, lambda r: httpx.Response(503))
        assert asyncio.run(voices.rafraichir_catalogue()) is False
        assert voices.catalogue() == avant


class TestPlomberieTTS:
    """La voix est décidée au MÊME endroit pour la voix en appel et l'accueil pré-rendu —
    sinon un appel commence dans une voix et continue dans une autre."""

    def test_build_tts_prend_la_voix_mistral_du_tenant(self):
        pytest.importorskip("pipecat")
        from app.voice.bot import build_tts
        from app.voice.voxtral_tts import VoxtralTTSService

        tts = build_tts(_tenant(_autre().id))
        assert isinstance(tts, VoxtralTTSService) and tts._voix_id == _autre().voxtral_id

    def test_sans_cle_build_tts_prend_le_secours_moshi(self, monkeypatch):
        pytest.importorskip("pipecat")
        from app.voice.bot import build_tts

        monkeypatch.delenv("MISTRAL_API_KEY")
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        assert build_tts(_tenant(_autre().id))._voice == voices.DEFAULT_VOICE

    def test_accueil_et_voix_en_appel_choisissent_la_meme_voix(self):
        pytest.importorskip("pipecat")
        from app.voice import greeting as greeting_mod
        from app.voice.bot import build_tts

        tenant = _tenant(_autre().id)
        assert voices.get(greeting_mod._voice(tenant)).voxtral_id == build_tts(tenant)._voix_id
