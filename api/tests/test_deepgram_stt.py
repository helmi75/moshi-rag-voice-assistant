"""Tests du STT Deepgram tel que bot.build_stt le configure — aucun réseau.

Le contrat qui compte est celui de nova-3 : `keyterm` et non `keywords` (sinon HTTP 400
et tous les appels muets), décroché en `multi` pour comprendre un anglophone dès sa
première phrase, filtre de grossièretés coupé. Kyutai STT, essayé puis abandonné pour le
téléphone, a été retiré le 18/09/2026 (tag archive/moteurs-locaux)."""
import types

import pytest


def _tenant(name="Chez Test"):
    return types.SimpleNamespace(name=name, language="fr-FR")


class TestBuildSTT:
    def test_construit_deepgram(self, monkeypatch):
        pytest.importorskip("deepgram")  # extra présent en CI, pas dans le venv minimal
        from app.voice.bot import build_stt

        stt = build_stt(_tenant(), "fr")
        assert type(stt).__name__ == "DeepgramSTTService"


class TestDeepgramVocabulary:
    """nova-2 pondère via `keywords`, nova-3 via `keyterm`.

    Ce n'est pas un détail de style : envoyer `keywords` à nova-3 fait répondre
    « Keywords are not supported for Nova-3 » (HTTP 400) et le STT ne démarre pas —
    tous les appels deviennent muets. Contrat vérifié contre l'API le 30/07/2026.
    """

    def _options(self, monkeypatch, **env):
        pytest.importorskip("deepgram")
        from app.voice.bot import build_stt

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return build_stt(_tenant(), "fr-FR")._settings

    def _get(self, settings, key):
        return settings[key] if isinstance(settings, dict) else getattr(settings, key, None)

    def test_nova2_uses_weighted_keywords(self, monkeypatch):
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-2")
        keywords = self._get(s, "keywords")
        assert keywords and any(k.startswith("réservation:") for k in keywords)
        assert not self._get(s, "keyterm")

    def test_nova3_uses_bare_keyterms(self, monkeypatch):
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-3")
        keyterm = self._get(s, "keyterm")
        assert keyterm and "réservation" in keyterm
        # Aucune pondération « mot:3 » : nova-3 la refuse.
        assert all(":" not in t for t in keyterm)
        assert not self._get(s, "keywords")

    def test_default_model_is_nova3(self, monkeypatch):
        """nova-3 est le défaut depuis le 30/07/2026 : nettement meilleur sur les noms
        propres au téléphone, validé à l'oreille sur des appels réels."""
        monkeypatch.delenv("DEEPGRAM_MODEL", raising=False)
        s = self._options(monkeypatch)
        assert str(self._get(s, "model")).startswith("nova-3")
        # Et donc keyterm, jamais keywords — sinon l'API renvoie 400 et le STT est muet.
        assert self._get(s, "keyterm") and not self._get(s, "keywords")

    def test_extra_keywords_lose_their_weight_on_nova3(self, monkeypatch):
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-3",
                          DEEPGRAM_KEYWORDS="Fouquet:5,brunch:2")
        assert "Fouquet" in self._get(s, "keyterm")
        assert "brunch" in self._get(s, "keyterm")

    def test_language_can_be_overridden(self, monkeypatch):
        """`multi` n'existe pas dans l'enum Language de Pipecat mais Deepgram
        l'accepte, et c'est la seule variante de nova-3 qui ponctue le français."""
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-3", DEEPGRAM_LANGUAGE="multi")
        assert str(self._get(s, "language")) == "multi"

    def test_decroche_en_bilingue_par_defaut(self, monkeypatch):
        """Un anglophone doit être compris dès sa première phrase (voice/langue.py)."""
        monkeypatch.delenv("DEEPGRAM_LANGUAGE", raising=False)
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-3")
        assert str(self._get(s, "language")) == "multi"

    def test_filtre_de_grossierete_coupe(self, monkeypatch):
        """Pipecat l'active par défaut : des mots retirés de ce que le modèle reçoit."""
        s = self._options(monkeypatch, DEEPGRAM_MODEL="nova-3")
        assert self._get(s, "profanity_filter") is False
