"""Catalogue de voix et choix de la voix par établissement.

Depuis le 28/09/2026, toutes les voix viennent de Mistral (Voxtral) : le catalogue est
celui de l'API, relu toutes les heures, avec une copie dans le dépôt. Les voix Moshi ont
été retirées (ASSISTANTE-103) : une ancienne voix enregistrée passe sur la voix par défaut.

La règle de fond ne change pas : jamais un identifiant hors catalogue. Une voix inconnue
serait refusée par Mistral (phrase muette).
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

class TestResolve:
    def test_sans_choix_la_voix_par_defaut_du_parc(self):
        assert voices.resolve() == voices.resolve(None) == voices.VOIX_PAR_DEFAUT

    def test_utilise_la_voix_choisie_par_l_etablissement(self):
        assert voices.resolve(_tenant(_autre().id)) == _autre().id

    def test_ignore_une_voix_hors_catalogue(self):
        assert voices.resolve(_tenant("voxtral/inventee")) == voices.VOIX_PAR_DEFAUT

    def test_une_ancienne_voix_moshi_passe_sur_mistral(self):
        assert voices.resolve(_tenant("unmute-prod-website/developpeuse-3.wav")) == voices.VOIX_PAR_DEFAUT

    def test_les_identifiants_du_matin_restent_valides(self):
        assert voices.resolve(_tenant("voxtral/marie-enthousiaste")) == "voxtral/fr_marie_excited"

    def test_le_defaut_du_parc_se_regle(self, monkeypatch):
        monkeypatch.setenv("VOIX_PAR_DEFAUT", "voxtral/fr_marie_happy")
        assert voices.resolve() == "voxtral/fr_marie_happy"
        monkeypatch.setenv("VOIX_PAR_DEFAUT", "voxtral/faute-de-frappe")
        assert voices.resolve() == voices.VOIX_PAR_DEFAUT


class TestSansCle:
    """Clé Mistral absente : la voix résolue reste celle du catalogue (la supervision
    signale la panne, la vigie rend la ligne) — plus de voix de secours."""

    def test_plus_de_secours_moshi(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY")
        assert voices.resolve(_tenant(_autre().id)) == _autre().id
        assert voices.label_for(_tenant()) == "Marie · enthousiaste"
        assert voices.voxtral_disponible() is False


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

    def test_les_voix_de_voxtral_3_sont_ecartees(self, monkeypatch):
        # Relevé le 15/11/2026 : depuis Voxtral 3 (20/10), Mistral liste 36 voix « pvc »
        # (voice_3141…) que notre modèle refuse (HTTP 400 « voice is not supported for
        # speech v1 », code 3051). Le client de la recette prenait Camille, et l'admin les
        # proposait : un établissement qui en choisissait une n'avait plus de voix.
        ancienne = {"id": "1" * 36, "slug": "fr_marie_neutral", "name": "Marie - Neutral",
                    "languages": ["fr_fr"], "gender": "female", "kind": "ivc"}
        nouvelle = {"id": "2" * 36, "slug": "voice_3141", "name": "Camille",
                    "languages": ["fr"], "gender": "female", "kind": "pvc"}
        inconnue = nouvelle | {"slug": "voice_9999", "kind": "PVC"}
        self._mistral(monkeypatch, lambda r: httpx.Response(
            200, json={"items": [ancienne, nouvelle, inconnue], "total_pages": 1}))
        assert asyncio.run(voices.rafraichir_catalogue()) is True
        assert [v.id for v in voices.catalogue()] == ["voxtral/fr_marie_neutral"]
        assert voices.get("voxtral/voice_3141") is None

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

    def test_sans_cle_build_tts_refuse_au_lieu_de_chercher_un_gpu(self, monkeypatch):
        pytest.importorskip("pipecat")
        from app.voice.bot import build_tts

        monkeypatch.delenv("MISTRAL_API_KEY")
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
            build_tts(_tenant(_autre().id))

    def test_accueil_et_voix_en_appel_choisissent_la_meme_voix(self):
        pytest.importorskip("pipecat")
        from app.voice import greeting as greeting_mod
        from app.voice.bot import build_tts

        tenant = _tenant(_autre().id)
        assert voices.get(greeting_mod._voice(tenant)).voxtral_id == build_tts(tenant)._voix_id
